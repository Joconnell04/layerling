"""Paint edits on a finished job: the operations the studio paint editor sends (contracts
payloads.PaintEdit, PaintOp) applied to a PaintSession through its region ops, and the print
preview (simulate_print, the 3MF, a p2p_slicer slice with per-layer toolpaths, the eight proof
views) as contracts payloads.PaintPreview.

Used by the worker's s09 paint_edit attempts (p2p_worker.stages.s09_paint_edit) and by the paint
MCP tools apply_paint_ops and preview_print (open_job with `base`).

Face sets are figure face ids (the region map's mesh order) as ranges, "0-9,15,20-31".
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from p2p_contracts import FilamentLibrary
from p2p_contracts.mcp_tools import PaintRegionIn
from p2p_contracts.payloads import (
    FACE_RANGES_PATTERN,
    PaintOp,
    PaintPreview,
    PrintNumbers,
    ProofImage,
    SimSummary,
)
from p2p_contracts.regions import PaintState, RegionMap

from .job import REPO_ROOT, JobInputs, jobs_root, load_job
from .session import STAGE, PaintSession, ToolError

_RANGES = re.compile(FACE_RANGES_PATTERN)
PROOF_PX = 640


# ---------------------------------------------------------------- face ranges


def encode_faces(ids: Iterable[int] | np.ndarray) -> str:
    """Face ids -> "0-9,15,20-31" (sorted, duplicates dropped)."""
    a = np.unique(np.asarray(list(ids) if not isinstance(ids, np.ndarray) else ids,
                             dtype=np.int64))
    if not len(a):
        return ""
    cut = np.nonzero(np.diff(a) != 1)[0]
    starts = np.r_[a[0], a[cut + 1]]
    ends = np.r_[a[cut], a[-1]]
    return ",".join(str(s) if s == e else f"{s}-{e}" for s, e in zip(starts.tolist(),
                                                                     ends.tolist(), strict=True))


def decode_faces(text: str, n_faces: int) -> np.ndarray:
    """Ranges -> boolean face mask. ToolError on bad text or ids outside the figure."""
    text = (text or "").strip()
    mask = np.zeros(n_faces, bool)
    if not text:
        return mask
    if not _RANGES.fullmatch(text):
        raise ToolError("faces must be ranges like 0-9,15,20-31")
    parts = text.split(",")
    lo = np.empty(len(parts), np.int64)
    hi = np.empty(len(parts), np.int64)
    for i, p in enumerate(parts):
        a, _, b = p.partition("-")
        lo[i] = int(a)
        hi[i] = int(b) if b else lo[i]
    if (hi < lo).any():
        raise ToolError("a face range runs backwards")
    if int(hi.max()) >= n_faces:
        raise ToolError(f"face {int(hi.max())} is outside the figure ({n_faces} faces)")
    edge = np.zeros(n_faces + 1, np.int64)
    np.add.at(edge, lo, 1)
    np.add.at(edge, hi + 1, -1)
    mask[:] = np.cumsum(edge[:-1]) > 0
    return mask


# ---------------------------------------------------------------- the paint behind a result


def _read(p: Path) -> dict:
    return json.loads(p.read_text()) if p.is_file() else {}


def _manifest(job_dir: Path, stage: str, attempt: int | None) -> dict:
    return _read(job_dir / stage / f"a{attempt}" / "manifest.json") if attempt else {}


def current_paint(job_dir: Path) -> str | None:
    """Attempt folder of the paint behind the job's current result, e.g. "s09_paint/a2": the
    paint_state input of s10's accepted attempt, else the accepted s09 or s08 attempt's
    paint_state output. None when the job has no paint map (a thin-slice job before any edit)."""
    attempts = _read(job_dir / "job.json").get("attempts") or {}
    m10 = _manifest(job_dir, "s10_write", attempts.get("s10_write"))
    for i in m10.get("inputs") or []:
        if i.get("role") == "paint_state":
            return i["path"].rsplit("/", 1)[0]
    for stage in ("s09_paint", "s08_palette"):
        m = _manifest(job_dir, stage, attempts.get(stage))
        if m.get("status") != "succeeded":
            continue
        for o in m.get("outputs") or []:
            if o.get("role") == "paint_state":
                return o["path"].rsplit("/", 1)[0]
    return None


# ---------------------------------------------------------------- sessions


def _reuse_cache(stage_dir: Path, adir: Path) -> None:
    """Link an earlier attempt's mesh caches (keyed by sha256) so a 1M-face PLY parses once."""
    cache = adir / "cache"
    for npz in stage_dir.glob("a*/cache/*.npz"):
        if npz.parent == cache or (cache / npz.name).exists():
            continue
        cache.mkdir(parents=True, exist_ok=True)
        try:
            os.link(npz, cache / npz.name)
        except OSError:
            continue


def seed(s: PaintSession, state_path: Path) -> None:
    """Load the paint at state_path (a paint_state.json and the region map it names) into a
    fresh session, save it in the session's attempt folder and checkpoint it."""
    job_dir = s.job_dir
    ps = PaintState.model_validate_json(state_path.read_text())
    rm = RegionMap.model_validate_json((job_dir / ps.region_map.path).read_text())
    fr = np.load(job_dir / rm.face_regions.path).astype(np.int32)
    fig = s.inp.figure
    if len(fr) != fig.n_faces or (s.inp.figure_sha and rm.mesh.sha256 != s.inp.figure_sha):
        raise ToolError(f"{state_path.name} indexes {rm.mesh.path} ({len(fr)} faces), not the "
                        f"figure the paint server loads ({fig.n_faces} faces)")
    s.fr = fr
    s.regions = {r.id: r for r in rm.regions}
    s.next_id = rm.next_region_id
    s.palette = list(ps.palette)
    s.assignment = {int(k): v for k, v in ps.assignment.items()}
    s.default_filament = ps.default_filament
    s.part_filaments = dict(ps.part_filaments) or s.part_filaments
    s.iteration = ps.iteration
    s.dirty = False
    s.sim_key = None
    s._save()
    s._checkpoint(f"base: {s.rel(state_path.parent)}", bump=False)


def bootstrap(s: PaintSession) -> None:
    """No paint map yet (a thin-slice job): each connected same-filament patch of the s07
    baseline becomes a region, largest first (region 1, s1.body), like s08's first pass and
    without SAM 3, so Fill works on patches rather than whole colors."""
    from p2p_contracts.regions import Region
    from p2p_sim.geodesic import components

    from .labeling import filament_short_names

    fig = s.inp.figure
    fil = np.asarray(s._baseline_faces()).astype(str)
    ids, code = np.unique(fil, return_inverse=True)
    n, comp = components(fig, np.ones(fig.n_faces, bool), code)
    area = np.bincount(comp, weights=fig.face_areas, minlength=n)
    rid = np.empty(n, np.int32)
    rid[np.argsort(-area, kind="stable")] = np.arange(1, n + 1, dtype=np.int32)
    fr = rid[comp]
    first = np.full(n + 1, -1, np.int64)
    first[fr[::-1]] = np.arange(len(fr))[::-1]
    names = filament_short_names(s.inp.all_filaments)
    counts: dict[str, int] = {}
    regions, assignment = [], {}
    for r in range(1, n + 1):
        fid = str(ids[code[first[r]]])
        word = names.get(fid, fid).replace(" ", "_")
        counts[word] = counts.get(word, 0) + 1
        label, part = ("s1.body", "body") if r == 1 else (f"s1.{word}_{counts[word]}", "other")
        regions.append(Region(id=r, label=label, subject_id="s1", part=part,
                              source="meshy_color", n_faces=0, area_mm2=0.0,
                              centroid_mm=(0, 0, 0), bbox_min_mm=(0, 0, 0),
                              bbox_max_mm=(0, 0, 0)))
        assignment[r] = fid
    s._build_from(fr, regions, assignment=assignment)


def open_edit_session(job_id: str, *, base: str | None = "current", root: Path | None = None,
                      attempt: int | None = None, attempt_dir: Path | None = None,
                      inputs: JobInputs | None = None,
                      sam3_backend: str | None = None) -> PaintSession:
    """A paint session on a new s09 attempt (or `attempt` / `attempt_dir`), seeded from the
    paint in `base` ("current", an attempt folder such as s09_paint/a2, or None for the
    baseline split) instead of fresh SAM 3 labels."""
    root = root or jobs_root()
    job_dir = root / job_id
    stage_dir = job_dir / STAGE
    if attempt_dir is None:
        if attempt is None:
            nums = [int(p.name[1:]) for p in stage_dir.glob("a*") if p.name[1:].isdigit()]
            attempt = max(nums, default=0) + 1
        attempt_dir = stage_dir / f"a{attempt}"
    attempt = attempt or int(attempt_dir.name[1:])
    attempt_dir.mkdir(parents=True, exist_ok=True)
    _reuse_cache(stage_dir, attempt_dir)
    inp = inputs or load_job(job_id, attempt_dir / "cache", root)
    s = PaintSession(inp, attempt_dir, attempt, sam3_backend)
    folder = current_paint(job_dir) if base == "current" else base
    if folder:
        state = job_dir / folder / "paint_state.json"
        if not state.is_file():
            raise ToolError(f"no paint_state.json in {folder}")
        seed(s, state)
    else:
        bootstrap(s)
    return s


# ---------------------------------------------------------------- ops


@dataclass
class EditResult:
    ops: int = 0
    changed_faces: int = 0
    changed_regions: list[int] = field(default_factory=list)
    new_regions: list[int] = field(default_factory=list)
    palette_before: list[str] = field(default_factory=list)
    palette: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def _full_catalog(repo: Path = REPO_ROOT) -> dict[str, Any]:
    """Every catalog color by id (the session's catalog holds the auto-pick lines only; a person
    in the editor may pick any line, as palette_edit allows)."""
    p = repo / "filaments" / "catalog.json"
    if not p.is_file():
        return {}
    return {f.id: f for f in FilamentLibrary.model_validate_json(p.read_text()).filaments}


def _known(s: PaintSession, fid: str) -> None:
    if s.inp.filament(fid) is not None:
        return
    f = _full_catalog().get(fid)
    if f is None:
        raise ToolError(f"unknown filament {fid!r}")
    s.inp.catalog.append(f.model_copy(update={"owned": False, "ams_slot": None}))


def _allowed(s: PaintSession, fid: str) -> None:
    if fid not in s.palette and not s.inp.owned(fid) and not s.inp.allows_unowned:
        raise ToolError(f"{fid} is not an owned spool; send source any to use catalog colors")


def _ensure(s: PaintSession, fid: str) -> None:
    """fid in the palette, taking a free AMS slot when it is new."""
    if fid in s.palette:
        return
    _allowed(s, fid)
    if len(s.palette) >= s.inp.settings.ams_slots:
        raise ToolError(f"no free AMS slot for {fid} ({len(s.palette)} of "
                        f"{s.inp.settings.ams_slots} used)")
    s.palette.append(fid)


def _target(s: PaintSession, op: PaintOp) -> str:
    if op.slot is not None:
        if op.slot > len(s.palette):
            raise ToolError(f"slot {op.slot}: the palette has {len(s.palette)} slots")
        return s.palette[op.slot - 1]
    _known(s, op.filament_id)
    return op.filament_id


def _used(s: PaintSession) -> set[str]:
    return set(s.assignment.values()) | set(s.part_filaments.values()) | {s.default_filament}


def _fill(s: PaintSession, op: PaintOp, fid: str) -> str:
    _allowed(s, fid)
    for rid in op.region_ids:
        s._need(rid)
    s.paint_region(PaintRegionIn(region_ids=op.region_ids, filament_id=fid,
                                 add_to_palette=True))
    return f"filled {len(op.region_ids)} region(s) {fid}"


def _brush(s: PaintSession, op: PaintOp, fid: str, read_faces: Callable[[str], str]) -> str:
    text = op.faces if op.faces is not None else read_faces(op.faces_path or "")
    mask = decode_faces(text, s.inp.figure.n_faces)
    if not mask.any():
        return "empty stroke"
    _ensure(s, fid)
    parents, counts = np.unique(s.fr[mask], return_counts=True)
    subjects: dict[str, int] = {}
    for p, c in zip(parents.tolist(), counts.tolist(), strict=True):
        sid = s.regions[p].subject_id if p in s.regions else None
        if sid:
            subjects[sid] = subjects.get(sid, 0) + c
    subject = max(subjects, key=subjects.get) if subjects else "s1"
    rid = s.next_id
    label = op.label or f"{subject}.paint_{rid}"
    s._new_region(mask, label, "other", "manual", fid, subject,
                  parent_ids=[p for p in parents.tolist() if p > 0])
    new_fr = s.fr.copy()
    new_fr[mask] = rid
    s._apply(new_fr, f"brushed {int(mask.sum())} faces {fid}", extra_ids=[rid])
    return f"brushed {int(mask.sum())} faces into region {rid} {fid}"


def _replace(s: PaintSession, op: PaintOp, fid: str) -> list[int]:
    k = op.from_slot or 0
    if k > len(s.palette):
        raise ToolError(f"from_slot {k}: the palette has {len(s.palette)} slots")
    old = s.palette[k - 1]
    if fid == old:
        return []
    _allowed(s, fid)
    moved = [rid for rid, f in s.assignment.items() if f == old]
    for rid in moved:
        s.assignment[rid] = fid
    if s.default_filament == old:
        s.default_filament = fid
    s.part_filaments = {p: (fid if f == old else f) for p, f in s.part_filaments.items()}
    if fid in s.palette:
        s.palette.remove(old)
    else:
        s.palette[k - 1] = fid
    s.dirty = True
    s.sim_key = None
    s._save()
    return moved


def apply_ops(s: PaintSession, ops: list[PaintOp], *, source: str = "owned",
              read_faces: Callable[[str], str] | None = None) -> EditResult:
    """Apply ops in order. A refused op raises ToolError naming it; the ops before it stay
    applied in the session (the worker throws the attempt away). Filaments the ops leave unused
    drop out of the palette at the end; unused slots the base already had stay."""
    if source == "any":
        s.inp.palette_source = "any"
    read_faces = read_faces or (lambda rel: (s.job_dir / rel).read_text())
    out = EditResult(ops=len(ops), palette_before=list(s.palette))
    before = s.face_filament().copy()
    base_unused = [f for f in s.palette if f not in _used(s)]
    touched: set[int] = set()
    for i, op in enumerate(ops, start=1):
        try:
            fid = _target(s, op)
            if op.op == "fill_region":
                msg = _fill(s, op, fid)
                touched |= set(op.region_ids)
            elif op.op == "brush_faces":
                nxt = s.next_id
                msg = _brush(s, op, fid, read_faces)
                if s.next_id > nxt:
                    out.new_regions.append(nxt)
                    touched.add(nxt)
            else:
                moved = _replace(s, op, fid)
                touched |= set(moved)
                msg = f"slot {op.from_slot} -> {fid}: {len(moved)} region(s)"
        except ToolError as e:
            raise ToolError(f"op {i} ({op.op}): {e}") from e
        out.messages.append(msg)
    used = _used(s)
    s.palette = [f for f in s.palette if f in used or f in base_unused]
    s._save()
    after = s.face_filament()
    changed = before != after
    out.changed_faces = int(changed.sum())
    out.changed_regions = sorted(r for r in touched if r in s.regions)
    out.palette = list(s.palette)
    s._note("apply_paint_ops", {"ops": len(ops), "source": source},
            f"{out.changed_faces} faces changed; palette {out.palette}")
    return out


# ---------------------------------------------------------------- preview


WriteFn = Callable[[PaintSession, Path], dict]


def write_session_3mf(s: PaintSession, out_dir: Path) -> dict:
    """The session's own write_3mf (the paint server's rule: the ideal paint, no despeckle)."""
    from p2p_contracts.mcp_tools import Write3mfIn

    res = s.write_3mf(Write3mfIn(name="final")).out
    return {"path": s.job_dir / res.path, "slots_used": res.slots_used}


def slice_numbers(threemf: Path, palette: list[str], out_dir: Path | None = None,
                  *, expected_slots: int | None = None, slots: list[dict] | None = None):
    """p2p_slicer on a project 3MF: (PrintNumbers, toolpath index or None, warnings). With
    out_dir the slice report, result.json and the toolpaths are written there."""
    from p2p_slicer.api import slice_project
    from p2p_slicer.preview import print_numbers, write_toolpaths

    res = slice_project(threemf, out_dir=out_dir, expected_slots=expected_slots)
    nums = print_numbers(res.report, palette, layers=res.analysis.layer_count)
    index = None
    if out_dir is not None:
        index = write_toolpaths(res.moves, out_dir, slots=slots,
                                per_layer_s=res.analysis.time.per_layer_s)
    return nums, index, list(res.report.errors) + list(res.report.warnings)


def preview(s: PaintSession, out_dir: Path, *, edit: EditResult | None = None,
            before: PrintNumbers | None = None, base_attempt: str | None = None,
            write: WriteFn | None = None, proof: bool = True, toolpaths: bool = True,
            log: Callable[[str], None] | None = None) -> PaintPreview:
    """simulate_print, the 3MF, p2p_slicer numbers and toolpaths, the proof views."""
    from p2p_sim.proof import proof_set

    log = log or (lambda _m: None)
    out_dir.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    warnings: list[str] = []
    t = time.time()
    sim = s.run_sim()
    summary = s.sim_summary()
    timings["sim"] = round(time.time() - t, 2)
    log(f"sim {timings['sim']} s")
    t = time.time()
    written = (write or write_session_3mf)(s, out_dir)
    threemf = Path(written["path"])
    timings["write"] = round(time.time() - t, 2)
    after = None
    index = None
    t = time.time()
    try:
        slots = [{"slot": i + 1, "filament_id": f, "hex": s.filament(f).hex}
                 for i, f in enumerate(s.palette)]
        after, index, notes = slice_numbers(
            threemf, s.palette, out_dir if toolpaths else None,
            expected_slots=written.get("slots_used"), slots=slots)
        warnings += notes[:8]
    except Exception as e:  # noqa: BLE001 the preview never blocks the gate that follows
        warnings.append(f"p2p_slicer failed: {type(e).__name__}: {e}"[:300])
    timings["slice"] = round(time.time() - t, 2)
    log(f"slice {timings['slice']} s")
    images: list[ProofImage] = []
    if proof:
        t = time.time()
        try:
            rows = proof_set(s.product, s.inp.orientation, out_dir / "proof",
                             paint_rgb=s.figure_rgb(), sim=sim, size_px=PROOF_PX, fmt="jpg",
                             title=str(s.inp.product_type).capitalize())
            images = [ProofImage(kind=r["kind"], view=r["view"], path=s.rel(Path(r["path"])))
                      for r in rows if r["view"] != "sheet"]
        except Exception as e:  # noqa: BLE001
            warnings.append(f"proof renders failed: {type(e).__name__}: {e}"[:300])
        timings["proof"] = round(time.time() - t, 2)
        log(f"proof {timings['proof']} s")
    rep = sim.report
    return PaintPreview(
        base_attempt=base_attempt, ops=edit.ops if edit else 0,
        changed_faces=edit.changed_faces if edit else 0,
        changed_regions=edit.changed_regions if edit else [], palette=list(s.palette),
        threemf=s.rel(threemf), before=before, after=after,
        sim=SimSummary(changed_pct=summary["changed_pct"], regions_lost=summary["regions_lost"],
                       bleed_flags=summary["bleed_flags"], purge_g_est=round(rep.purge_g_est, 3),
                       model_g_est=round(rep.model_g_est, 3), swaps_total=rep.swaps_total),
        proof_images=images,
        toolpaths=s.rel(out_dir / "toolpaths.json") if index is not None else None,
        warnings=warnings, timings_s=timings)
