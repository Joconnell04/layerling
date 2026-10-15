"""PaintSession: the paint server's state for one open job, and every tool as a method.

State lives on disk in jobs/<job_id>/s09_paint/a<N>/ and is rewritten after every mutation:
  face_regions.npy, region_map.json, paint_state.json   the current paint (contracts)
  session.json                                          checkpoints, last outputs
  checkpoints/<cid>/                                    snapshots of the three state files
  renders/                                              every image a tool produced
  labels/                                               SAM 3 labels (cache, reused on resume)
  cache/                                                mesh npz caches

Faces: region ops address the FIGURE only (face ids 0..F-1, the s06 figure in Meshy order).
Renders and simulate_print use the PRODUCT mesh: figure first, then each add-on part in
GeometryResult order, each part in its one filament.
"""

from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from p2p_contracts import SimReport, SliceReport
from p2p_contracts.manifest import FileRef
from p2p_contracts.mcp_tools import (
    BoundaryEditIn,
    CheckpointIn,
    DespeckleIn,
    LabelRegionsIn,
    ListRegionsIn,
    MergeRegionsIn,
    OpenJobIn,
    PaintOpsIn,
    PaintRegionIn,
    PaletteOut,
    PixelOut,
    PreviewPrintIn,
    RegionsOut,
    RegionSummary,
    RenderOut,
    RenderViewsIn,
    SelectByPixelIn,
    SimulatePrintIn,
    SliceCheckIn,
    SnapToCreasesIn,
    SplitRegionIn,
    StateOut,
    SuggestPaletteIn,
    UndoIn,
    Write3mfIn,
    Write3mfOut,
)
from p2p_contracts.regions import MeshRef, PaintState, Region, RegionMap
from p2p_contracts.reports import Checklist, SimParams
from p2p_sim.color import hex_to_rgb
from p2p_sim.mesh import Mesh
from PIL import Image, ImageDraw

from . import labeling, palette, regionops
from .job import JobInputs, PartMesh, jobs_root, load_job, sha256_file

STAGE = "s09_paint"
SUPPORTS = {"none": {"enable_support": "0"}, "tree": {"enable_support": "1"},
            "normal": {"enable_support": "1", "support_type": "normal(auto)"}}
SHEET_CELL = 512
# Region parts and label words a band fix never paints over (they must survive as placed).
PROTECT_PARTS = ("eye", "pupil", "iris", "nose", "mouth", "tongue", "teeth", "eyebrow")


class ToolError(ValueError):
    """A refused edit: the message goes back to the agent."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _png(img: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG", compress_level=4)
    return buf.getvalue()


def _jpeg(img: np.ndarray, quality: int = 88) -> bytes:
    """Model-facing image: a textured 8-view sheet is several MB as PNG, which overflows the
    Agent SDK's message buffer and costs upload time; the PNG stays on disk."""
    buf = io.BytesIO()
    Image.fromarray(img).convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def sheet(cells: list[tuple[str, np.ndarray]], cell: int = SHEET_CELL, cols: int = 3,
          title: str | None = None) -> np.ndarray:
    """Grid of labeled images (each resized to cell x cell)."""
    cols = max(1, min(cols, len(cells)))
    rows = (len(cells) + cols - 1) // cols
    top = 28 if title else 0
    lab = 22
    out = Image.new("RGB", (cols * cell, top + rows * (cell + lab)), (32, 33, 38))
    d = ImageDraw.Draw(out)
    if title:
        d.text((8, 7), title, fill=(235, 235, 235))
    for i, (name, img) in enumerate(cells):
        r, c = divmod(i, cols)
        x, y = c * cell, top + r * (cell + lab)
        im = Image.fromarray(img).resize((cell, cell), Image.LANCZOS)
        out.paste(im, (x, y + lab))
        d.text((x + 6, y + 4), name, fill=(220, 220, 220))
    return np.asarray(out)


@dataclass
class ToolResult:
    """A tool's contract output plus images for the model (paths are job-relative)."""

    out: object
    images: list[tuple[str, bytes]] = field(default_factory=list)  # (label, png bytes)


class PaintSession:
    def __init__(self, inp: JobInputs, attempt_dir: Path, attempt: int,
                 sam3_backend: str | None = None):
        self.inp = inp
        self.dir = attempt_dir
        self.attempt = attempt
        self.sam3_backend = sam3_backend
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fr = np.zeros(inp.figure.n_faces, np.int32)
        self.regions: dict[int, Region] = {}
        self.next_id = 1
        self.palette: list[str] = []
        self.assignment: dict[int, str] = {}
        self.default_filament = ""
        self.part_filaments: dict[str, str] = {p.name: p.filament_id for p in inp.parts
                                               if p.filament_id}
        self.iteration = 0
        self.checkpoint_id: str | None = None
        self.checkpoints: list[dict] = []
        self.dirty = False
        self.sim = None  # p2p_sim SimResult on the product mesh
        self.sim_key: str | None = None
        self.sim_report: SimReport | None = None
        self.baseline_sim: dict | None = None
        self.last_3mf: str | None = None
        self.last_3mf_slots: int | None = None
        self.last_slice: dict | None = None
        self.log: list[dict] = []
        self._product: Mesh | None = None
        self._render_n = 0
        self.timings: dict[str, float] = {}
        self.plan: dict | None = None  # palette_plan.json: the cost-aware palette decision
        self._planner = None

    # ================================================================ construction

    @classmethod
    def open(cls, req: OpenJobIn, root: Path | None = None,
             sam3_backend: str | None = None) -> PaintSession:
        root = root or jobs_root()
        stage_dir = root / req.job_id / STAGE
        attempt = req.attempt
        if attempt is None:
            nums = [int(p.name[1:]) for p in stage_dir.glob("a*") if p.name[1:].isdigit()]
            attempt = max(nums, default=0) + 1
        adir = stage_dir / f"a{attempt}"
        inp = load_job(req.job_id, adir / "cache", root)
        s = cls(inp, adir, attempt, sam3_backend)
        if (adir / "paint_state.json").is_file():
            s._load_state()
        else:
            s._initial_state(stage_dir)
        return s

    @classmethod
    def resume(cls, inp: JobInputs, attempt_dir: Path, attempt: int,
               sam3_backend: str | None = None) -> PaintSession:
        """Reload an attempt's state from disk, reusing already loaded inputs."""
        s = cls(inp, attempt_dir, attempt, sam3_backend)
        s._load_state()
        return s

    @classmethod
    def from_inputs(cls, inp: JobInputs, attempt_dir: Path, *, face_regions=None,
                    regions: list[Region] | None = None, assignment: dict[int, str] | None = None,
                    sam3_backend: str | None = "mock") -> PaintSession:
        """A session on a fixture: give regions directly, or let SAM 3 (mock) label."""
        s = cls(inp, attempt_dir, int(attempt_dir.name[1:]) if attempt_dir.name[1:].isdigit()
                else 1, sam3_backend)
        if face_regions is not None:
            s._build_from(np.asarray(face_regions, np.int32), list(regions or []),
                          assignment=assignment)
        else:
            s._initial_state(attempt_dir.parent)
        return s

    def _initial_state(self, stage_dir: Path) -> None:
        inp = self.inp
        t = time.time()
        labels = labeling.load_labels(self.dir / "labels", inp.figure.n_faces)
        if labels is None:  # reuse an earlier attempt's labels for the same figure
            for p in sorted(stage_dir.glob("a*/labels")):
                if p.parent == self.dir:
                    continue
                meta = p / "figure_sha.txt"
                if meta.is_file() and meta.read_text().strip() == inp.figure_sha:
                    labels = labeling.load_labels(p, inp.figure.n_faces)
                    if labels:
                        labeling.save_labels(self.dir / "labels", labels)
                        (self.dir / "labels" / "figure_sha.txt").write_text(inp.figure_sha)
                        break
        if labels is None and inp.tex_rgb is not None:
            labels = labeling.run_sam3(inp.figure, inp.tex_rgb, LabelRegionsIn(), inp.orientation,
                                       backend=self.sam3_backend, tex_lab=inp.tex_lab)
            labeling.save_labels(self.dir / "labels", labels)
            (self.dir / "labels" / "figure_sha.txt").write_text(inp.figure_sha)
        self.timings["labels_s"] = round(time.time() - t, 2)
        if labels is None:
            fr = np.zeros(inp.figure.n_faces, np.int32)
            regions: list[Region] = []
        else:
            fr, regions = labels.face_regions, labels.regions
            self.next_id = labels.next_region_id
        self._build_from(fr, regions)

    def _baseline_faces(self) -> np.ndarray:
        """Per figure face filament id: the s07 baseline, else texture -> nearest loaded."""
        inp = self.inp
        if inp.baseline_filaments is not None:
            return inp.baseline_filaments
        loaded = inp.library
        if inp.tex_lab is None or not loaded:
            return np.array([loaded[0].id if loaded else "none"] * inp.figure.n_faces, object)
        j, _ = palette.nearest(inp.tex_lab, loaded)
        return np.array([loaded[i].id for i in j], dtype=object)

    def _build_from(self, fr: np.ndarray, regions: list[Region],
                    assignment: dict[int, str] | None = None) -> None:
        inp = self.inp
        self.next_id = max([self.next_id] + [r.id + 1 for r in regions])
        if assignment is None:
            face_fil = self._baseline_faces()
            names = labeling.filament_short_names(inp.all_filaments)
            fr, regions, self.next_id, assignment = labeling.split_by_filament(
                inp.figure, fr, regions, face_fil, self.next_id, names)
        self.fr = np.asarray(fr, np.int32)
        self.regions = {r.id: r for r in regions if (self.fr == r.id).any()}
        self.assignment = {int(k): v for k, v in assignment.items() if int(k) in self.regions}
        used = set(self.assignment.values()) | set(self.part_filaments.values())
        order = {f.id: i for i, f in enumerate(inp.all_filaments)}
        self.palette = sorted(used, key=lambda x: order.get(x, 999))
        area = regionops.region_areas(inp.figure, self.fr)
        by_fil: dict[str, float] = {}
        for rid, fid in self.assignment.items():
            by_fil[fid] = by_fil.get(fid, 0.0) + float(area[rid]) if rid < len(area) else 0.0
        self.default_filament = max(by_fil, key=by_fil.get) if by_fil else (
            self.palette[0] if self.palette else "")
        if self.default_filament and self.default_filament not in self.palette:
            self.palette.append(self.default_filament)
        self._refresh_regions(list(self.regions), full=True)
        self._save()
        self._checkpoint("initial: SAM 3 labels split by the baseline paint", bump=False)

    # ================================================================ persistence

    @property
    def job_dir(self) -> Path:
        return self.inp.job_dir

    def rel(self, p: Path) -> str:
        try:
            return p.relative_to(self.job_dir).as_posix()
        except ValueError:
            return p.as_posix()

    def _fileref(self, p: Path, kind: str | None = None, role: str | None = None) -> FileRef:
        return FileRef(path=self.rel(p), sha256=sha256_file(p), bytes=p.stat().st_size,
                       kind=kind or p.suffix.lstrip("."), role=role)

    def region_map(self) -> RegionMap:
        fig = self.inp.figure
        mesh_ref = MeshRef(path=self.inp.figure_rel or "fixture", sha256=self.inp.figure_sha,
                           n_faces=fig.n_faces, n_vertices=fig.n_vertices)
        fpath = self.dir / "face_regions.npy"
        return RegionMap(mesh=mesh_ref, face_regions=self._fileref(fpath, "npy", "face_regions"),
                         regions=[self.regions[k] for k in sorted(self.regions)],
                         next_region_id=self.next_id)

    def paint_state(self) -> PaintState:
        return PaintState(region_map=self._fileref(self.dir / "region_map.json", "json",
                                                   "region_map"),
                          palette=list(self.palette),
                          assignment={str(k): v for k, v in sorted(self.assignment.items())},
                          default_filament=self.default_filament,
                          part_filaments=dict(self.part_filaments), iteration=self.iteration,
                          checkpoint_id=self.checkpoint_id)

    def _save(self) -> None:
        np.save(self.dir / "face_regions.npy", self.fr)
        (self.dir / "region_map.json").write_text(self.region_map().model_dump_json(indent=1))
        (self.dir / "paint_state.json").write_text(self.paint_state().model_dump_json(indent=1))
        (self.dir / "session.json").write_text(json.dumps({
            "job_id": self.inp.job_id, "attempt": self.attempt, "checkpoints": self.checkpoints,
            "last_3mf": self.last_3mf, "last_3mf_slots": self.last_3mf_slots,
            "last_slice": self.last_slice,
            "baseline_sim": self.baseline_sim, "dirty": self.dirty, "log": self.log[-200:],
            "timings": self.timings}, indent=1))

    def _load_state(self) -> None:
        self.fr = np.load(self.dir / "face_regions.npy").astype(np.int32)
        rm = RegionMap.model_validate_json((self.dir / "region_map.json").read_text())
        ps = PaintState.model_validate_json((self.dir / "paint_state.json").read_text())
        self.regions = {r.id: r for r in rm.regions}
        self.next_id = rm.next_region_id
        self.palette = list(ps.palette)
        self.assignment = {int(k): v for k, v in ps.assignment.items()}
        self.default_filament = ps.default_filament
        self.part_filaments = dict(ps.part_filaments) or self.part_filaments
        self.iteration = ps.iteration
        self.checkpoint_id = ps.checkpoint_id
        sj = self.dir / "session.json"
        if sj.is_file():
            j = json.loads(sj.read_text())
            self.checkpoints = j.get("checkpoints", [])
            self.last_3mf = j.get("last_3mf")
            self.last_3mf_slots = j.get("last_3mf_slots")
            self.last_slice = j.get("last_slice")
            self.baseline_sim = j.get("baseline_sim")
            self.dirty = bool(j.get("dirty"))
            self.log = j.get("log", [])
        pp = self.dir / "palette_plan.json"
        if pp.is_file():
            self.plan = json.loads(pp.read_text())

    # ================================================================ derived arrays

    def filament(self, fid: str):
        f = self.inp.filament(fid)
        if f is None:
            raise ToolError(f"unknown filament {fid!r}")
        return f

    def face_filament(self) -> np.ndarray:
        """(F,) filament id per figure face."""
        n = int(self.fr.max()) + 1
        lut = np.array([self.default_filament] * max(n, 1), dtype=object)
        for rid, fid in self.assignment.items():
            if rid < n:
                lut[rid] = fid
        return lut[self.fr]

    def face_slots(self) -> np.ndarray:
        """(F,) 1-based palette slot per figure face."""
        idx = {f: i + 1 for i, f in enumerate(self.palette)}
        n = int(self.fr.max()) + 1
        lut = np.full(max(n, 1), idx.get(self.default_filament, 1), np.int64)
        for rid, fid in self.assignment.items():
            if rid < n:
                lut[rid] = idx[fid]
        return lut[self.fr]

    def palette_labs(self) -> np.ndarray:
        return np.array([self.filament(f).lab for f in self.palette], np.float64).reshape(-1, 3)

    def palette_rgb(self) -> np.ndarray:
        return np.array([hex_to_rgb(self.filament(f).hex) for f in self.palette], np.uint8)

    @property
    def product(self) -> Mesh:
        if self._product is None:
            meshes = [self.inp.figure] + [p.mesh for p in self.inp.parts]
            vs, fs, off = [], [], 0
            for m in meshes:
                vs.append(m.vertices)
                fs.append(m.faces + off)
                off += m.n_vertices
            self._product = Mesh(vertices=np.concatenate(vs), faces=np.concatenate(fs),
                                 name="product")
        return self._product

    def face_parts(self) -> np.ndarray:
        n = [self.inp.figure.n_faces] + [p.mesh.n_faces for p in self.inp.parts]
        return np.repeat(np.arange(len(n)), n)

    def part_slot(self, p: PartMesh) -> int:
        fid = self.part_filaments.get(p.name) or p.filament_id or self.default_filament
        return self.palette.index(fid) + 1 if fid in self.palette else 1

    def product_slots(self, figure_slots: np.ndarray | None = None) -> np.ndarray:
        s = [self.face_slots() if figure_slots is None else figure_slots]
        s += [np.full(p.mesh.n_faces, self.part_slot(p), np.int64) for p in self.inp.parts]
        return np.concatenate(s)

    def product_regions(self) -> np.ndarray:
        extra = sum(p.mesh.n_faces for p in self.inp.parts)
        return np.concatenate([self.fr, np.zeros(extra, np.int32)])

    # ================================================================ region bookkeeping

    def _refresh_regions(self, ids, full: bool = False) -> None:
        """Recompute geometry and color for the given region ids; drop empty ones."""
        from p2p_sim.regions import min_widths

        fig = self.inp.figure
        fr = self.fr
        n = int(fr.max()) + 1
        area = fig.face_areas
        nf = np.bincount(fr, minlength=n)
        ar = np.bincount(fr, weights=area, minlength=n)
        cen = np.stack([np.bincount(fr, weights=area * fig.face_centroids[:, k], minlength=n)
                        for k in range(3)], 1)
        ids = [int(i) for i in ids]
        live = [i for i in ids if i < n and nf[i] > 0]
        for i in ids:
            if i not in live:
                self.regions.pop(i, None)
                self.assignment.pop(i, None)
        if not live:
            return
        sel = np.isin(fr, live)
        # min widths on a submesh: the regions plus a one-face ring around them
        adj = fig.face_adjacency
        ring = sel.copy()
        touch = sel[adj[:, 0]] | sel[adj[:, 1]]
        ring[adj[touch].ravel()] = True
        fidx = np.nonzero(ring)[0]
        if full or len(fidx) == fig.n_faces:
            widths = min_widths(fig, fr)
        else:
            sub_f = fig.faces[fidx]
            uv, inv = np.unique(sub_f.ravel(), return_inverse=True)
            sub = Mesh(vertices=fig.vertices[uv], faces=inv.reshape(-1, 3))
            widths = min_widths(sub, fr[fidx])
        tri = fig.triangles
        lab = self.inp.tex_lab
        for i in live:
            m = fr == i
            c = cen[i] / max(ar[i], 1e-12)
            lo = tri[m].reshape(-1, 3).min(0)
            hi = tri[m].reshape(-1, 3).max(0)
            color = None
            if lab is not None:
                w = area[m]
                color = tuple(round(float(x), 2) for x in (lab[m] * w[:, None]).sum(0) / w.sum())
            r = self.regions.get(i)
            upd = {"n_faces": int(nf[i]), "area_mm2": round(float(ar[i]), 4),
                       "centroid_mm": tuple(round(float(x), 3) for x in c),
                       "bbox_min_mm": tuple(round(float(x), 3) for x in lo),
                       "bbox_max_mm": tuple(round(float(x), 3) for x in hi),
                       "min_width_mm": (round(float(widths[i]), 3) if widths.get(i) is not None
                                     else None),
                       "color_lab": color}
            if r is None:
                r = Region(id=i, label=f"s1.region_{i}", subject_id="s1", part="other",
                           source="manual", **upd)
            else:
                r = r.model_copy(update=upd)
            self.regions[i] = r

    def _apply(self, new_fr: np.ndarray, message: str, extra_ids=()) -> StateOut:
        """Commit a new label array: refresh touched regions, save, report."""
        new_fr = np.asarray(new_fr, np.int32)
        changed = new_fr != self.fr
        touched = set(np.unique(self.fr[changed]).tolist()) | set(
            np.unique(new_fr[changed]).tolist()) | {int(x) for x in extra_ids}
        touched.discard(0)
        self.fr = new_fr
        self._refresh_regions(sorted(touched))
        self.dirty = True
        self.sim_key = None
        self._save()
        return self.state(sorted(t for t in touched), message)

    def _new_region(self, mask: np.ndarray, label: str, part: str, source: str,
                    filament_id: str, subject_id: str | None = "s1",
                    parent_ids=()) -> int:
        rid = self.next_id
        self.next_id += 1
        self.regions[rid] = Region(id=rid, label=label, subject_id=subject_id, part=part,
                                   source=source, n_faces=int(mask.sum()), area_mm2=0.0,
                                   centroid_mm=(0, 0, 0), bbox_min_mm=(0, 0, 0),
                                   bbox_max_mm=(0, 0, 0), parent_ids=list(parent_ids))
        self.assignment[rid] = filament_id
        return rid

    def _need(self, rid: int) -> Region:
        if rid not in self.regions:
            raise ToolError(f"no region {rid}; list_regions shows the current ids")
        return self.regions[rid]

    def _note(self, tool: str, args: dict, msg: str) -> None:
        self.log.append({"at": _now(), "iteration": self.iteration, "tool": tool,
                         "args": args, "result": msg})

    # ================================================================ tools: state

    def state(self, changed=(), message: str | None = None) -> StateOut:
        fig = self.inp.figure
        un = float(fig.face_areas[self.fr == 0].sum() / max(fig.face_areas.sum(), 1e-12))
        return StateOut(job_id=self.inp.job_id, attempt=self.attempt, iteration=self.iteration,
                        checkpoint_id=self.checkpoint_id, palette=list(self.palette),
                        n_regions=len(self.regions), unlabeled_area_pct=round(100 * un, 3),
                        changed_region_ids=[int(x) for x in changed], message=message)

    def get_state(self) -> ToolResult:
        return ToolResult(self.state(message=self._state_note()))

    def _state_note(self) -> str:
        fil = self.face_filament()
        area = self.inp.figure.face_areas
        parts = []
        for f in self.palette:
            a = float(area[fil == f].sum())
            parts.append(f"{f}={a:.0f}mm2")
        parts += [f"{k}(part)={v}" for k, v in self.part_filaments.items()]
        dirty = "uncheckpointed edits" if self.dirty else "clean"
        return f"figure paint: {', '.join(parts)}; default {self.default_filament}; {dirty}"

    def list_regions(self, req: ListRegionsIn) -> ToolResult:
        out = []
        for rid in sorted(self.regions):
            r = self.regions[rid]
            if req.subject_id and r.subject_id != req.subject_id:
                continue
            if req.part and r.part != req.part:
                continue
            fid = self.assignment.get(rid)
            if req.unassigned_only and fid is not None:
                continue
            out.append(RegionSummary(id=rid, label=r.label, part=r.part, subject_id=r.subject_id,
                                     filament_id=fid, area_mm2=r.area_mm2,
                                     min_width_mm=r.min_width_mm, confidence=r.confidence))
        return ToolResult(RegionsOut(regions=out))

    def region_table(self) -> str:
        """Compact text table for prompts: id label filament area width pieces color match.
        "pieces" is how many separate patches the region prints as, with the largest one's
        share: a band in 15 pieces reads as spots (live2 sombrero)."""
        rows = [("id | label | filament | area_mm2 | min_width_mm | pieces (largest %) | "
                 "texture Lab | dE00 to filament")]
        count, big = self._piece_counts()
        for rid in sorted(self.regions):
            r = self.regions[rid]
            fid = self.assignment.get(rid, self.default_filament)
            de = ""
            if r.color_lab is not None:
                _, d = palette.guarded_de([r.color_lab], [self.filament(fid).lab])
                de = f"{float(d[0, 0]):.1f}"
            lab = "" if r.color_lab is None else "({:.0f},{:.0f},{:.0f})".format(*tuple(r.color_lab))
            w = "" if r.min_width_mm is None else f"{r.min_width_mm:.2f}"
            pcs = ""
            if rid < len(count) and count[rid]:
                pcs = str(int(count[rid]))
                if count[rid] > 1:
                    pcs += f" ({100 * big[rid] / max(r.area_mm2, 1e-9):.0f}%)"
            rows.append(f"{rid} | {r.label} | {fid} | {r.area_mm2:.1f} | {w} | {pcs} | {lab} "
                        f"| {de}")
        return "\n".join(rows)

    def _piece_counts(self) -> tuple[np.ndarray, np.ndarray]:
        """Per region id: separate pieces on the figure, and the largest piece's area."""
        from p2p_sim.geodesic import components

        fig = self.inp.figure
        n = int(self.fr.max()) + 1
        count, big = np.zeros(n, np.int64), np.zeros(n)
        lab = self.fr > 0
        if not lab.any():
            return count, big
        k, comp = components(fig, lab, self.fr)
        ca = np.bincount(comp[lab], weights=fig.face_areas[lab], minlength=k)
        owner = np.zeros(k, np.int64)
        owner[comp[lab]] = self.fr[lab]
        np.add.at(count, owner, 1)
        np.maximum.at(big, owner, ca)
        return count, big

    # ================================================================ tools: labels

    def label_regions(self, req: LabelRegionsIn) -> ToolResult:
        inp = self.inp
        if inp.tex_rgb is None:
            raise ToolError("this job has no texture colors to label")
        res = labeling.run_sam3(inp.figure, inp.tex_rgb, req, inp.orientation,
                                backend=self.sam3_backend, tex_lab=inp.tex_lab,
                                first_region_id=self.next_id)
        if req.replace:
            face_fil = self.face_filament()
            names = labeling.filament_short_names(inp.all_filaments)
            self.next_id = res.next_region_id
            fr, regions, self.next_id, assign = labeling.split_by_filament(
                inp.figure, res.face_regions, res.regions, face_fil, self.next_id, names)
            self.regions = {r.id: r for r in regions}
            self.assignment = assign
            self._apply(fr, "labels replaced", extra_ids=[r.id for r in regions])
        else:
            specific = {"eye", "pupil", "iris", "eye_highlight", "nose", "mouth", "tongue",
                        "teeth", "inner_ear", "paw_pad", "collar", "tag", "ear", "eyebrow"}
            new_fr = self.fr.copy()
            face_fil = self.face_filament()
            made = []
            present = {(r.label) for r in self.regions.values()}
            for r in res.regions:
                if r.part not in specific or r.label in present:
                    continue
                m = res.face_regions == r.id
                if not m.any():
                    continue
                vals, cnt = np.unique(face_fil[m], return_counts=True)
                rid = self._new_region(m, r.label, r.part, "sam3", str(vals[np.argmax(cnt)]),
                                       r.subject_id, parent_ids=np.unique(self.fr[m]).tolist())
                self.regions[rid] = self.regions[rid].model_copy(
                    update={"confidence": r.confidence, "views_seen": r.views_seen})
                new_fr[m] = rid
                made.append(rid)
            self.next_id = max(self.next_id, res.next_region_id)
            self._apply(new_fr, f"refined: {len(made)} new part regions", extra_ids=made)
        self._note("label_regions", req.model_dump(), "ok")
        return self.list_regions(ListRegionsIn())

    # ================================================================ tools: paint

    # ================================================================ palette plan

    def planner(self):
        """The cost-aware palette planner on this session (palette_plan.Planner)."""
        if self._planner is None:
            from .palette_plan import Planner

            self._planner = Planner(self)
        return self._planner

    def _save_plan(self) -> None:
        if self.plan is not None:
            (self.dir / "palette_plan.json").write_text(
                json.dumps(self.plan, indent=1, default=str))

    def apply_palette_plan(self) -> dict:
        """Decide the palette in code (palette_plan: every filament earns its place against
        purge and print time) and paint every region with its filament: the concept color
        area its texture is closest to, else its texture's nearest. Checkpointed."""
        from .palette_plan import summary_line

        pl = self.planner()
        d = pl.plan()
        asg = pl.assignment_for(d["palette"])
        self.palette = list(d["palette"])
        self.assignment = {rid: fid for rid, fid in asg.items() if rid in self.regions}
        area = regionops.region_areas(self.inp.figure, self.fr)
        by: dict[str, float] = {}
        for rid, fid in self.assignment.items():
            by[fid] = by.get(fid, 0.0) + float(area[rid]) if rid < len(area) else 0.0
        if by:
            self.default_filament = max(by, key=by.get)
        self._prune_palette()
        d["palette"] = list(self.palette)
        d["added"] = []
        d["refused"] = []
        self.plan = d
        self._save_plan()
        self.dirty, self.sim_key = True, None
        self._save()
        line = summary_line(d)
        self._note("palette_plan", {}, line)
        self._checkpoint("palette plan (code): " + line[:240], bump=False)
        return d

    def _gate_new_filament(self, fid: str, region_ids: list[int]) -> None:
        """A filament outside the palette must earn its place (palette_plan.earns_place);
        refused with the numbers when it does not."""
        if self.plan is None:
            return
        ok, info = self.planner().earns_place(fid, list(region_ids))
        self.plan.setdefault("added" if ok else "refused", []).append(info)
        self._save_plan()
        if not ok:
            raise ToolError(
                f"{fid} does not earn its place in the palette: {info['why']}. Paint these "
                f"regions with a palette filament ({', '.join(self.palette)}), or report the "
                "color in issues if the print clearly misses it.")

    def paint_region(self, req: PaintRegionIn) -> ToolResult:
        fid = req.filament_id
        self.filament(fid)
        if fid not in self.palette and not self.inp.owned(fid) and not self.inp.allows_unowned:
            raise ToolError(f"{fid} is not an owned spool and this job uses owned spools only; "
                            "paint the best owned filament and report the swap")
        if fid not in self.palette:
            if not req.add_to_palette:
                raise ToolError(f"{fid} is not in the palette {self.palette}; pass "
                                "add_to_palette=true (needs a free AMS slot) or run "
                                "suggest_palette")
            if len(self.palette) >= self.inp.settings.ams_slots:
                raise ToolError(f"no free AMS slot ({len(self.palette)} of "
                                f"{self.inp.settings.ams_slots} used); free one first")
            self._gate_new_filament(fid, req.region_ids)
            self.palette.append(fid)
        for rid in req.region_ids:
            self._need(rid)
        for rid in req.region_ids:
            self.assignment[rid] = fid
        self.dirty = True
        self.sim_key = None
        self._save()
        msg = f"painted {len(req.region_ids)} region(s) {fid}"
        self._note("paint_region", req.model_dump(), msg)
        return ToolResult(self.state(req.region_ids, msg))

    def _prune_palette(self) -> None:
        used = set(self.assignment.values()) | set(self.part_filaments.values()) | {
            self.default_filament}
        self.palette = [f for f in self.palette if f in used]

    def merge_regions(self, req: MergeRegionsIn) -> ToolResult:
        keep = req.region_ids[0]
        base = self._need(keep)
        for r in req.region_ids[1:]:
            self._need(r)
        new_fr = self.fr.copy()
        new_fr[np.isin(self.fr, req.region_ids)] = keep
        upd = {"source": "merge", "parent_ids": sorted(set(base.parent_ids) | set(
            req.region_ids[1:]))}
        if req.label:
            upd["label"] = req.label
        self.regions[keep] = base.model_copy(update=upd)
        for r in req.region_ids[1:]:
            self.assignment.pop(r, None)
        out = self._apply(new_fr, f"merged {req.region_ids[1:]} into {keep}", extra_ids=[keep])
        # a band-like part merged from scattered pieces (live2 sombrero: s1.hat_trim from 19
        # dashes) is bridged into one strip here; merging alone only renamed the pieces
        done = self.consolidate_bands([keep], merge=False, why="merge_regions")
        if done:
            out = self.state([keep], f"{out.message}; {self._band_text(done)}")
        else:
            info = regionops.pieces(self.inp.figure, self.fr, keep)
            if regionops.fragmented(info):
                out.message = (f"{out.message}; still {info['pieces']} separate pieces "
                               f"(largest {100 * info['largest_share']:.0f}%): it prints as "
                               "spots")
        self._note("merge_regions", req.model_dump(), out.message or "")
        return ToolResult(out)

    def split_region(self, req: SplitRegionIn) -> ToolResult:
        r = self._need(req.region_id)
        fig = self.inp.figure
        if req.method == "connected_components":
            masks = regionops.split_components(fig, self.fr, r.id)
        elif req.method == "plane":
            if req.plane_point_mm is None or req.plane_normal is None:
                raise ToolError("plane split needs plane_point_mm and plane_normal")
            masks = regionops.split_plane(fig, self.fr, r.id, req.plane_point_mm,
                                          req.plane_normal)
        elif req.method == "geodesic_seed":
            try:
                masks = regionops.split_seeds(fig, self.fr, r.id, req.seed_face_ids or [])
            except ValueError as e:
                raise ToolError(str(e)) from e
        else:  # prompt
            if not req.prompt:
                raise ToolError("prompt split needs prompt")
            res = labeling.run_sam3(fig, self.inp.tex_rgb, LabelRegionsIn(
                prompts=[req.prompt], markings=False), self.inp.orientation,
                backend=self.sam3_backend)
            hit = (res.face_regions > 0) & (self.fr == r.id)
            masks = [hit, (self.fr == r.id) & ~hit]
        masks = [m for m in masks if m.any()]
        if len(masks) < 2:
            raise ToolError("the split leaves one piece; nothing changed")
        new_fr = self.fr.copy()
        made = []
        fid = self.assignment.get(r.id, self.default_filament)
        for i, m in enumerate(masks[1:], start=1):
            name = (req.labels[i] if req.labels and i < len(req.labels)
                    else f"{r.label}_part{i + 1}")
            rid = self._new_region(m, name, r.part, "split", fid, r.subject_id, [r.id])
            new_fr[m] = rid
            made.append(rid)
        if req.labels:
            self.regions[r.id] = r.model_copy(update={"label": req.labels[0]})
        out = self._apply(new_fr, f"split {r.id} into {[r.id] + made}", extra_ids=[r.id] + made)
        self._note("split_region", req.model_dump(), out.message or "")
        return ToolResult(out)

    def _boundary(self, tool: str, req: BoundaryEditIn, fn) -> ToolResult:
        self._need(req.region_id)
        try:
            new_fr = fn(self.inp.figure, self.fr, req.region_id, req.mm)
        except ValueError as e:
            raise ToolError(str(e)) from e
        moved = float(self.inp.figure.face_areas[new_fr != self.fr].sum())
        out = self._apply(new_fr, f"{tool} {req.region_id} by {req.mm} mm: {moved:.2f} mm2 "
                                  "changed hands", extra_ids=[req.region_id])
        self._note(tool, req.model_dump(), out.message or "")
        return ToolResult(out)

    def grow(self, req: BoundaryEditIn) -> ToolResult:
        return self._boundary("grow", req, regionops.grow)

    def shrink(self, req: BoundaryEditIn) -> ToolResult:
        return self._boundary("shrink", req, regionops.shrink)

    def smooth_boundary(self, req: BoundaryEditIn) -> ToolResult:
        return self._boundary("smooth_boundary", req, regionops.smooth_boundary)

    def snap_to_creases(self, req: SnapToCreasesIn) -> ToolResult:
        for r in req.region_ids or []:
            self._need(r)
        new_fr = regionops.snap_to_creases(self.inp.figure, self.fr, req.region_ids,
                                           req.max_shift_mm, req.crease_angle_deg)
        moved = float(self.inp.figure.face_areas[new_fr != self.fr].sum())
        share = regionops.boundary_on_crease_share(self.inp.figure, new_fr,
                                                   req.crease_angle_deg)
        out = self._apply(new_fr, f"snapped: {moved:.2f} mm2 changed hands; "
                                  f"{100 * share:.1f}% of boundary length now on creases")
        self._note("snap_to_creases", req.model_dump(), out.message or "")
        return ToolResult(out)

    def despeckle(self, req: DespeckleIn) -> ToolResult:
        for r in req.region_ids or []:
            self._need(r)
        fig = self.inp.figure
        before = regionops.islands(fig, self.face_filament(), req.min_area_mm2)
        new_fr = regionops.despeckle(fig, self.fr, req.min_area_mm2, req.region_ids,
                                     face_filament=self._fil_of(self.fr))
        self_fr_old = self.fr
        out = self._apply(new_fr, "")
        after = regionops.islands(fig, self.face_filament(), req.min_area_mm2)
        gone = sorted(set(np.unique(self_fr_old).tolist()) - set(np.unique(self.fr).tolist()))
        out.message = (f"islands under {req.min_area_mm2} mm2: {before[0]} ({before[1]:.2f} mm2)"
                       f" -> {after[0]} ({after[1]:.2f} mm2); regions removed: {gone[:30]}")
        self._save()
        self._note("despeckle", req.model_dump(), out.message)
        return ToolResult(out)

    # ================================================================ bands

    def _protected(self) -> list[int]:
        """Face-part regions (eyes, nose, mouth) that a band fix never paints over."""
        words = PROTECT_PARTS
        return sorted(rid for rid, r in self.regions.items()
                      if r.part in words or any(w in r.label.lower() for w in words))

    def consolidate_bands(self, region_ids=None, notes: dict | None = None, *,
                          merge: bool = True, why: str = "") -> list[dict]:
        """Band-like regions that print as scattered pieces become one strip, or (merge=True,
        when bridging would not make one strip) take the color of what surrounds each piece:
        regionops.consolidate. A region is band-like when its label, or a note about it (the
        final check's words), names a band, trim, stripe, collar and the like; spots and
        patches are markings and never change here. Largest first, so a bridged band swallows
        the other stripe color's pieces between its dashes. Returns what changed."""
        notes = {int(k): v for k, v in (notes or {}).items()}
        ids = sorted(self.regions) if region_ids is None else [
            int(r) for r in region_ids if int(r) in self.regions]
        protect = self._protected()
        fig = self.inp.figure
        fr = self.fr
        done: list[dict] = []
        for rid in sorted(ids, key=lambda r: -self.regions[r].area_mm2):
            r = self.regions.get(rid)
            if r is None or rid in protect or not (fr == rid).any():
                continue
            if not regionops.band_like(r.label, notes.get(rid)):
                continue
            new, info = regionops.consolidate(fig, fr, rid, protect=protect, merge=merge)
            if info["action"] in ("bridged", "merged"):
                fr = new
                done.append({**info, "label": r.label})
        if done:
            msg = self._band_text(done)
            self._apply(fr, msg)
            self._note("consolidate_bands", {"region_ids": [d["region"] for d in done],
                                             "why": why}, msg)
        return done

    @staticmethod
    def _band_text(done: list[dict]) -> str:
        parts = []
        for d in done:
            b = d["before"]
            head = (f"band {d['region']} {d.get('label', '')}: {b['pieces']} pieces, largest "
                    f"{100 * b['largest_share']:.0f}%")
            if d["action"] == "bridged":
                parts.append(f"{head} -> bridged into {d['bridged']['pieces']} strip(s)")
            else:
                parts.append(f"{head} -> could not be one strip; merged into {d.get('into')}")
        return "; ".join(parts)

    def _fil_of(self, fr: np.ndarray) -> np.ndarray:
        """Filament code per face for a label array (for filament-level despeckle)."""
        idx = {f: i + 1 for i, f in enumerate(sorted(set(self.assignment.values()) | {
            self.default_filament}))}
        n = int(fr.max()) + 1
        lut = np.full(n, idx[self.default_filament], np.int64)
        for rid, fid in self.assignment.items():
            if rid < n:
                lut[rid] = idx[fid]
        return lut[fr]

    # ================================================================ tools: pixels

    def _camera(self, view: str, size: int):
        from p2p_sim.camera import camera_matrices

        return camera_matrices(view, self.product, size)

    def select_by_pixel(self, req: SelectByPixelIn) -> ToolResult:
        from p2p_contracts.regions import RENDER_SIZE_PX
        from p2p_sim.raster import rasterize

        cam = self._camera(req.view, RENDER_SIZE_PX)
        r = rasterize(self.product, cam)
        h, w = r.face_id.shape
        if not (0 <= req.x < w and 0 <= req.y < h):
            raise ToolError(f"pixel ({req.x}, {req.y}) is outside the {w}x{h} view")
        yy, xx = np.mgrid[0:h, 0:w]
        disk = (xx - req.x) ** 2 + (yy - req.y) ** 2 <= max(req.radius_px, 0) ** 2
        fids = np.unique(r.face_id[disk])
        fids = fids[(fids >= 0) & (fids < self.inp.figure.n_faces)]
        center = int(r.face_id[req.y, req.x])
        on_fig = 0 <= center < self.inp.figure.n_faces
        pt = None
        if center >= 0:
            pt = tuple(round(float(x), 3) for x in self.product.face_centroids[center])
        out = PixelOut(face_id=center if center >= 0 else None,
                       region_id=int(self.fr[center]) if on_fig else None, point_mm=pt)
        if req.action == "info":
            return ToolResult(out)
        if fids.size == 0:
            raise ToolError("no figure faces under that pixel")
        mask = np.zeros(self.inp.figure.n_faces, bool)
        mask[fids] = True
        new_fr = self.fr.copy()
        if req.action == "new_region":
            fil = self.face_filament()[fids]
            vals, cnt = np.unique(fil, return_counts=True)
            rid = self._new_region(mask, req.label or f"s1.pixel_{self.next_id}", "other",
                                   "pixel", str(vals[np.argmax(cnt)]),
                                   parent_ids=np.unique(self.fr[fids]).tolist())
            new_fr[mask] = rid
            out.new_region_id = rid
        else:
            if req.region_id is None:
                raise ToolError("add_to_region needs region_id")
            self._need(req.region_id)
            new_fr[mask] = req.region_id
        st = self._apply(new_fr, f"{req.action}: {len(fids)} faces",
                         extra_ids=[out.new_region_id or req.region_id])
        self._note("select_by_pixel", req.model_dump(), st.message or "")
        return ToolResult(out)

    # ================================================================ tools: palette

    def _targets(self) -> list[palette.Target]:
        fig_area = float(self.inp.figure.face_areas.sum())
        face_parts = {"eye", "pupil", "iris", "nose", "mouth", "tongue", "inner_ear",
                      "eye_highlight"}
        out = []
        for rid, r in self.regions.items():
            if r.color_lab is None:
                continue
            important = r.part in face_parts
            w = r.area_mm2 + (0.03 * fig_area if important else 0.0)
            req = important or r.area_mm2 >= 0.01 * fig_area
            out.append(palette.Target(f"r{rid}", np.array(r.color_lab), w, req, (rid,), r.label))
        for i, s in enumerate(self.inp.color_spec):
            if not s.get("hex"):
                continue
            out.append(palette.Target(f"spec:{i}", palette.hex_lab(s["hex"]), 0.02 * fig_area,
                                      bool(s.get("required", True)), (),
                                      f"spec {s.get('region')} ({s.get('color')})"))
        return out

    def suggest_palette(self, req: SuggestPaletteIn) -> ToolResult:
        inp = self.inp
        if self.plan is not None and not req.owned_only:
            # the cost-aware decision owns the palette: re-plan (and apply) instead of the
            # closest-color search, so extra shades never come back through this tool
            if req.apply:
                d = self.apply_palette_plan()
            else:
                d = self.planner().plan()
            de: dict[str, float] = {}
            out = PaletteOut(palette=list(d["palette"]), delta_e=de, swap_suggestions=[])
            self._note("suggest_palette", req.model_dump(), f"plan {out.palette}")
            return ToolResult(out)
        # an owned-only job never applies an unowned color (it only suggests the swap)
        owned_only = req.owned_only or (req.apply and not inp.allows_unowned)
        cands = inp.library if owned_only else inp.all_filaments
        fixed = list(dict.fromkeys(list(req.fixed) + list(self.part_filaments.values())))
        for f in fixed:
            self.filament(f)
        cands = cands + [self.filament(f) for f in fixed if f not in {c.id for c in cands}]
        targets = self._targets()
        k = min(req.k, inp.settings.ams_slots)
        chosen = palette.choose_palette(targets, cands, k, fixed,
                                        owned={f.id for f in inp.library})
        region_targets = [t for t in targets if t.key.startswith("r")]
        de: dict[str, float] = {}
        if region_targets and chosen:
            j, d = palette.nearest(np.array([t.lab for t in region_targets]), chosen)
            for t, jj, dd in zip(region_targets, j, d):
                de[str(t.region_ids[0])] = round(float(dd), 2)
        area = regionops.region_areas(inp.figure, self.fr)
        usage: dict[str, float] = {}
        for rid, fid in self.assignment.items():
            usage[fid] = usage.get(fid, 0.0) + float(area[rid])
        swaps = palette.swap_suggestions(targets, chosen, inp.library, inp.catalog,
                                         inp.settings.ams_slots, usage)
        if req.apply:
            if not chosen:
                raise ToolError("no palette to apply")
            j, _ = palette.nearest(np.array([t.lab for t in region_targets]), chosen)
            self.palette = [f.id for f in chosen]
            for t, jj in zip(region_targets, j):
                self.assignment[t.region_ids[0]] = chosen[int(jj)].id
            for rid in self.regions:
                if self.assignment.get(rid) not in self.palette:
                    self.assignment[rid] = self.palette[0]
            by = {}
            for rid, fid in self.assignment.items():
                by[fid] = by.get(fid, 0.0) + float(area[rid])
            self.default_filament = max(by, key=by.get)
            self._prune_palette()
            self.dirty = True
            self.sim_key = None
            self._save()
        out = PaletteOut(palette=[f.id for f in chosen], delta_e=de, swap_suggestions=swaps)
        self._note("suggest_palette", req.model_dump(), f"{out.palette}; swaps {len(swaps)}")
        return ToolResult(out)

    # ================================================================ tools: simulate

    def _sim_key(self, params: SimParams) -> str:
        import hashlib

        h = hashlib.sha256(self.product_slots().tobytes())
        h.update(params.model_dump_json().encode())
        h.update(",".join(self.palette).encode())
        return h.hexdigest()

    def run_sim(self, params: SimParams | None = None, out_dir: Path | None = None):
        from p2p_sim.simulate import simulate_print_detailed

        params = params or SimParams(nozzle_mm=self.inp.settings.nozzle_mm,
                                     layer_mm=self.inp.settings.layer_mm)
        key = self._sim_key(params)
        if self.sim is not None and self.sim_key == key:
            return self.sim
        parts = self.face_parts()
        part_slots = [self.palette.index(self.default_filament) + 1] + [
            self.part_slot(p) for p in self.inp.parts]
        t = time.time()
        res = simulate_print_detailed(
            self.product, self.product_slots(), self.palette_labs(), params,
            face_regions=self.product_regions(), out_dir=out_dir or self.dir,
            job_dir=self.job_dir, face_parts=parts if self.inp.parts else None,
            part_slots=part_slots if self.inp.parts else None)
        self.timings["last_sim_s"] = round(time.time() - t, 2)
        self.sim, self.sim_key, self.sim_report = res, key, res.report
        return res

    def simulate_print(self, req: SimulatePrintIn) -> ToolResult:
        res = self.run_sim(req.params)
        rep = res.report
        images = []
        if req.render:
            views = self._views()
            imgs = self._render("sim", views, sim=res)
            ch = self._changed_images(res, views)
            images.append(self._sheet_png("sim", imgs, "simulated print (filament colors)"))
            images.append(self._sheet_png("sim_changed", ch,
                                          "magenta = the slicer changes the paint here"))
            rep = rep.model_copy(update={"images": [self._fileref(p, "png", "sim_render")
                                                    for p in self._last_paths]})
        self.sim_report = rep
        self._note("simulate_print", req.model_dump(), f"purge {rep.purge_pct:.1f}%")
        return ToolResult(rep, images)

    def sim_summary(self) -> dict:
        res = self.run_sim()
        n = self.inp.figure.n_faces
        area = self.inp.figure.face_areas
        changed = res.face_slots[:n] != res.face_slots_in[:n]
        m = int(self.fr.max()) + 1
        tot = np.bincount(self.fr, weights=area, minlength=m)
        kept = np.bincount(self.fr[~changed], weights=area[~changed], minlength=m)
        lost = [int(r) for r in self.regions if tot[r] > 0 and kept[r] < 0.05 * tot[r]]
        return {"changed_area_mm2": round(float(area[changed].sum()), 2),
                "changed_pct": round(100 * float(area[changed].sum() / area.sum()), 2),
                "regions_lost": lost,
                "bleed_flags": len(res.report.bleed_flags),
                "purge_pct": round(res.report.purge_pct, 1),
                "purge_g": round(res.report.purge_g_est, 2),
                "model_g": round(res.report.model_g_est, 2),
                "swaps_total": res.report.swaps_total}

    # ================================================================ tools: renders

    def _views(self) -> list[str]:
        """Every side (contracts PROOF_VIEWS): a 3D figure is judged from all eight."""
        if self.inp.orientation == "flat_face_up":
            return ["top", "front_3q", "front", "back", "left", "right"]
        return ["front", "front_3q", "left_3q", "left", "right", "back", "back_3q", "top"]

    def figure_rgb(self, figure_slots: np.ndarray | None = None) -> np.ndarray:
        rgb = self.palette_rgb()
        return rgb[self.product_slots(figure_slots) - 1]

    def _render(self, kind: str, views, size: int = 1024, sim=None,
                region_ids=None) -> list[tuple[str, np.ndarray]]:
        from p2p_sim.render import render_crops, render_views

        self._render_n += 1
        tag = f"i{self.iteration:02d}_{self._render_n:03d}"
        rdir = self.dir / "renders"
        rdir.mkdir(exist_ok=True)
        self._last_paths: list[Path] = []
        out = []
        prod = self.product
        if kind == "crops":
            ids = [int(r) for r in (region_ids or [])][:9]
            if not ids:
                raise ToolError("crops need region_ids")
            rr = render_crops(prod, self.product_regions(), ids, self.figure_rgb(), size_px=512)
            for rid, r in zip(ids, rr):
                p = rdir / f"{tag}_crop_r{rid}.png"
                r.save(p)
                self._last_paths.append(p)
                out.append((f"region {rid} {self.regions[rid].label}", r.image))
            return out
        if kind == "texture":
            if self.inp.tex_rgb is None:
                raise ToolError("no texture colors for this job")
            extra = self.product_slots()[self.inp.figure.n_faces:]
            prgb = self.palette_rgb()
            fc = np.concatenate([self.inp.tex_rgb, prgb[extra - 1]]) if len(extra) else \
                self.inp.tex_rgb
            rr = render_views(prod, ("shaded",), views, fc, size_px=size)
        elif kind == "sim":
            rr = render_views(prod, ("sim",), views, None, size_px=size, sim=sim)
        else:
            rr = render_views(prod, (kind,), views, self.figure_rgb(),
                              self.product_regions(), size_px=size)
        for r in rr:
            p = rdir / f"{tag}_{kind}_{r.view}.png"
            r.save(p)
            self._last_paths.append(p)
            out.append((r.view, r.image))
        return out

    def _changed_images(self, res, views) -> list[tuple[str, np.ndarray]]:
        from p2p_sim.render import render_views

        n = self.inp.figure.n_faces
        rgb = res.face_rgb.copy()
        ch = res.face_slots != res.face_slots_in
        ch[n:] = False
        rgb = (0.35 * rgb + 0.65 * 200).astype(np.uint8)
        rgb[ch] = (255, 0, 200)
        rr = render_views(self.product, ("flat",), views, rgb, size_px=768)
        return [(r.view, r.image) for r in rr]

    def _sheet_png(self, name: str, cells, title: str) -> tuple[str, bytes]:
        img = sheet(cells, title=title)
        p = self.dir / "renders" / f"i{self.iteration:02d}_{self._render_n:03d}_{name}_sheet.png"
        p.parent.mkdir(exist_ok=True)
        Image.fromarray(img).save(p)
        return (self.rel(p), _jpeg(img))

    def render_views(self, req: RenderViewsIn) -> ToolResult:
        views = list(req.views or self._views())
        paths, images = [], []
        for kind in req.kinds:
            sim = self.run_sim() if kind == "sim" else None
            cells = self._render(kind, views, req.size_px, sim=sim, region_ids=req.region_ids)
            for p, (label, img) in zip(self._last_paths, cells):
                meta = {"kind": kind, "view": label if kind != "crops" else "crop",
                        "path": self.rel(p), "width": int(img.shape[1]),
                        "height": int(img.shape[0])}
                if kind == "crops":
                    meta["region_id"] = int(p.stem.rsplit("_r", 1)[-1])
                paths.append(meta)
            if len(cells) == 1:
                images.append((paths[-1]["path"], _png(cells[0][1])))
            else:
                title = {"flat": "ideal paint, filament colors", "overlay": "region ids",
                         "shaded": "shaded", "texture": "Meshy texture (reference)",
                         "sim": "simulated print", "crops": "close-ups"}[kind]
                images.append(self._sheet_png(kind, cells, title + " (cells 512 px; view "
                                                           "pixels are 2x cell pixels)"))
        self._note("render_views", req.model_dump(), f"{len(paths)} images")
        return ToolResult(RenderOut(images=paths), images)

    # ================================================================ tools: safety

    def _checkpoint(self, note: str, bump: bool = True) -> str:
        if bump:
            self.iteration += 1
        cid = f"c{len(self.checkpoints):03d}"
        d = self.dir / "checkpoints" / cid
        d.mkdir(parents=True, exist_ok=True)
        self.checkpoint_id = cid
        self.dirty = False
        self._save()
        for name in ("face_regions.npy", "region_map.json", "paint_state.json"):
            (d / name).write_bytes((self.dir / name).read_bytes())
        self.checkpoints.append({"id": cid, "note": note, "iteration": self.iteration,
                                 "at": _now()})
        self._save()
        return cid

    def checkpoint(self, req: CheckpointIn) -> ToolResult:
        cid = self._checkpoint(req.note)
        self._note("checkpoint", req.model_dump(), cid)
        return ToolResult(self.state(message=f"checkpoint {cid}: {req.note}"))

    def undo(self, req: UndoIn) -> ToolResult:
        if not self.checkpoints:
            raise ToolError("no checkpoints")
        ids = [c["id"] for c in self.checkpoints]
        if req.checkpoint_id:
            if req.checkpoint_id not in ids:
                raise ToolError(f"unknown checkpoint {req.checkpoint_id}; have {ids}")
            target = req.checkpoint_id
        elif self.dirty:
            target = self.checkpoint_id or ids[-1]
        else:
            i = ids.index(self.checkpoint_id) if self.checkpoint_id in ids else len(ids) - 1
            target = ids[max(0, i - 1)]
        d = self.dir / "checkpoints" / target
        fr = np.load(d / "face_regions.npy").astype(np.int32)
        rm = RegionMap.model_validate_json((d / "region_map.json").read_text())
        ps = PaintState.model_validate_json((d / "paint_state.json").read_text())
        self.fr = fr
        self.regions = {r.id: r for r in rm.regions}
        self.next_id = max(self.next_id, rm.next_region_id)  # ids are never reused
        self.palette = list(ps.palette)
        self.assignment = {int(k): v for k, v in ps.assignment.items()}
        self.default_filament = ps.default_filament
        self.part_filaments = dict(ps.part_filaments)
        self.checkpoint_id = target
        self.dirty = False
        self.sim_key = None
        self._save()
        msg = f"restored {target}"
        self._note("undo", req.model_dump(), msg)
        return ToolResult(self.state(message=msg))

    # ================================================================ tools: output

    def write_3mf(self, req: Write3mfIn) -> ToolResult:
        from p2p_threemf import write_3mf
        from p2p_threemf.writer import Part

        inp = self.inp
        slots = self.face_slots()
        if req.use_sim:
            slots = np.asarray(self.run_sim().face_slots[: inp.figure.n_faces], np.int64)
        pal = [self.filament(f) for f in self.palette]
        overrides = dict(inp.settings.process_overrides)
        overrides.update(SUPPORTS.get(inp.supports, {}))
        settings = inp.settings.model_copy(update={"process_overrides": overrides})
        parts = [Part(p.mesh.vertices, p.mesh.faces, slot=self.part_slot(p), name=p.name)
                 for p in inp.parts]
        name = "".join(c for c in req.name if c.isalnum() or c in "-_") or "final"
        out = self.dir / f"{name}.3mf"
        res = write_3mf(inp.figure.vertices, inp.figure.faces, slots, pal, settings, out,
                        object_name=inp.job_id[:8], parts=parts or None,
                        extra_sidecar={"paint_attempt": self.attempt,
                                       "checkpoint": self.checkpoint_id, "use_sim": req.use_sim})
        self.last_3mf = self.rel(out)
        self._save()
        used = sorted(set(np.unique(slots).tolist()) | {self.part_slot(p) for p in inp.parts})
        self.last_3mf_slots = len(used)
        o = Write3mfOut(path=self.last_3mf, sha256=res["sha256"], n_faces=int(res["n_faces"]),
                        palette=list(self.palette), slots_used=len(used))
        self._note("write_3mf", req.model_dump(), o.path)
        return ToolResult(o)

    def expected_slots(self, use_sim: bool = True) -> int:
        if use_sim:
            s = self.run_sim().face_slots
        else:
            s = self.product_slots()
        return len(np.unique(s))

    def slice_check(self, req: SliceCheckIn) -> ToolResult:
        from p2p_threemf import slice_check

        rel = req.path or self.last_3mf
        if not rel:
            raise ToolError("no 3MF yet; call write_3mf first")
        path = self.job_dir / rel
        if not path.is_file():
            raise ToolError(f"{rel} does not exist")
        # every filament the paint intends must print: a slot the slicer drops fails the gate
        exp = (self.last_3mf_slots if rel == self.last_3mf and self.last_3mf_slots
               else self.expected_slots(use_sim=False))
        t = time.time()
        rep: SliceReport = slice_check(path, self.inp.settings, exp, filament_ids=self.palette,
                                       keep_gcode=req.keep_gcode, out_dir=self.dir,
                                       job_root=self.job_dir)
        self.timings["last_slice_s"] = round(time.time() - t, 1)
        self.last_slice = rep.model_dump(mode="json")
        (self.dir / "slice_report.json").write_text(rep.model_dump_json(indent=1))
        self._save()
        self._note("slice_check", req.model_dump(), f"ok={rep.ok}")
        return ToolResult(rep)

    # ================================================================ checklist

    def run_checklist(self) -> ToolResult:
        from .checklist import run_checklist

        cl: Checklist = run_checklist(self)
        (self.dir / "checklist_code.json").write_text(cl.model_dump_json(indent=1))
        return ToolResult(cl)

    # ================================================================ studio paint edits

    def apply_paint_ops(self, req: PaintOpsIn) -> ToolResult:
        """The studio editor's ops (fill_region, brush_faces, replace_slot); edits.apply_ops."""
        from .edits import EditResult, apply_ops

        res = apply_ops(self, list(req.ops), source=req.source)
        acc = getattr(self, "_edits", None) or EditResult(palette_before=res.palette_before)
        acc.ops += res.ops
        acc.changed_faces += res.changed_faces
        acc.changed_regions = sorted(set(acc.changed_regions) | set(res.changed_regions))
        acc.palette = res.palette
        self._edits = acc
        msg = "; ".join(res.messages) + f"; {res.changed_faces} faces changed"
        return ToolResult(self.state(res.changed_regions, msg))

    def preview_print(self, req: PreviewPrintIn) -> ToolResult:
        """simulate_print, final.3mf, p2p_slicer numbers and toolpaths, 8 proof views. `before`
        is the previous preview's numbers (preview once before editing to get them)."""
        from .edits import preview

        last = getattr(self, "_preview", None)
        out = preview(self, self.dir / "preview", edit=getattr(self, "_edits", None),
                      before=last.after if last else None, proof=req.proof,
                      toolpaths=req.toolpaths)
        self._preview, self._edits = out, None
        (self.dir / "preview" / "paint_preview.json").write_text(out.model_dump_json(indent=1))
        images = []
        sheet = self.dir / "preview" / "proof" / "print_sheet.png"
        if sheet.is_file():
            images.append((self.rel(sheet), _jpeg(np.asarray(Image.open(sheet)))))
        self._note("preview_print", req.model_dump(),
                   f"time {out.after.print_time_s if out.after else None} s")
        return ToolResult(out, images)
