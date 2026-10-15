"""Load everything the paint server needs from a job folder.

Sources, newest succeeded attempt of each stage:
- s06_printability: geometry.json (GeometryResult), figure.ply, part PLYs.
- s07_baseline: baseline_x2d.3mf, the X2D project whose figure part carries Meshy's Multi-Color
  paint on the s06 figure (same faces, same order). Its palette is the s07 manifest's
  data.palette (project slot N = palette[N - 1]).
- s05_mesh: the approved GLB, for per-face texture colors (face order = figure order, or through
  GeometryResult.face_index_map when repair changed faces).
- s02 color spec when present, else s01 color_regions.json (what the customer asked for).
- filaments/library.json (loaded spools) and the catalog: every PLA Basic and PLA Matte color in
  filaments/catalog.json (the worker's auto-pick lines; library.example.json when the catalog
  is missing), plus any catalog color an add-on part already uses. The old 16-color example
  library lost the Frenchie's pink ears (dE00 17.6 against 10.8 for Matte Sakura Pink).
- s06 product_resolved.json (palette source: owned spools only, or any catalog color) and
  concept_read.json (the approved concept's vision read).

A job folder without s06 (tests, fixtures) can pass a figure PLY directly: see load_fixture.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from p2p_contracts import FilamentLibrary, PrintSettings
from p2p_contracts.filaments import Filament
from p2p_contracts.geometry import GeometryResult
from p2p_sim.color import srgb_to_lab
from p2p_sim.mesh import Mesh, load_glb, load_ply

REPO_ROOT = Path(__file__).resolve().parents[3]


def jobs_root() -> Path:
    env = os.environ.get("P2P_JOBS_DIR")
    return Path(env) if env else REPO_ROOT / "jobs"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class PartMesh:
    name: str
    role: str
    filament_id: str | None
    mesh: Mesh
    path: str | None = None


@dataclass
class JobInputs:
    job_id: str
    job_dir: Path
    settings: PrintSettings
    product_type: str
    orientation: str
    figure: Mesh
    figure_rel: str | None
    figure_sha: str
    parts: list[PartMesh]
    library: list[Filament]  # owned spools (library.json)
    catalog: list[Filament]  # not owned, for swap suggestions
    baseline_filaments: np.ndarray | None = None  # (F,) object array of filament ids or None
    baseline_rel: str | None = None
    tex_rgb: np.ndarray | None = None  # (F, 3) uint8 per figure face
    color_spec: list[dict] = field(default_factory=list)
    geometry: GeometryResult | None = None
    geometry_rel: str | None = None
    glb_rel: str | None = None
    supports: str = "none"
    notes: list[str] = field(default_factory=list)
    palette_source: str = "owned"  # any: unowned catalog colors may be painted (spools to buy)
    spec: dict = field(default_factory=dict)  # s06 product_spec.json
    concept_read: dict | None = None  # s06 concept_read.json "read"

    @property
    def tex_lab(self) -> np.ndarray | None:
        if self.tex_rgb is None:
            return None
        if "_lab" not in self.__dict__:
            self.__dict__["_lab"] = srgb_to_lab(self.tex_rgb)
        return self.__dict__["_lab"]

    def filament(self, fid: str) -> Filament | None:
        for f in self.library + self.catalog:
            if f.id == fid:
                return f
        return None

    def owned(self, fid: str) -> bool:
        return any(f.id == fid for f in self.library)

    @property
    def allows_unowned(self) -> bool:
        return self.palette_source == "any"

    @property
    def all_filaments(self) -> list[Filament]:
        seen = {f.id for f in self.library}
        return self.library + [f for f in self.catalog if f.id not in seen]


# ---------------------------------------------------------------- helpers


def _read_json(p: Path):
    return json.loads(p.read_text())


def latest_manifest(job_dir: Path, stage: str) -> tuple[int, dict] | None:
    d = job_dir / stage
    if not d.is_dir():
        return None
    best = None
    for a in sorted(d.glob("a*"), key=lambda p: int(p.name[1:]) if p.name[1:].isdigit() else 0):
        mp = a / "manifest.json"
        if not mp.is_file():
            continue
        m = _read_json(mp)
        if m.get("status") == "succeeded":
            best = (int(a.name[1:]), m)
    return best


def output_path(m: dict, role: str) -> str | None:
    for o in m.get("outputs", []):
        if o.get("role") == role:
            return o["path"]
    return None


AUTO_PICK_LINES = ("PLA Basic", "PLA Matte")  # p2p_worker.printing.AUTO_PICK_LINES


def load_library(repo: Path = REPO_ROOT, also: set[str] | None = None
                 ) -> tuple[list[Filament], list[Filament]]:
    """(owned spools, catalog). The catalog is every auto-pick color of filaments/catalog.json
    (plus the ids in `also`, e.g. add-on part filaments), else library.example.json."""
    owned: list[Filament] = []
    lib = repo / "filaments" / "library.json"
    if lib.is_file():
        owned = list(FilamentLibrary.model_validate(_read_json(lib)).filaments)
    catalog: list[Filament] = []
    full = repo / "filaments" / "catalog.json"
    ex = repo / "filaments" / "library.example.json"
    if full.is_file():
        for f in FilamentLibrary.model_validate(_read_json(full)).filaments:
            if f.line in AUTO_PICK_LINES or (also and f.id in also):
                catalog.append(f.model_copy(update={"owned": False, "ams_slot": None}))
    elif ex.is_file():
        for f in FilamentLibrary.model_validate(_read_json(ex)).filaments:
            catalog.append(f.model_copy(update={"owned": False, "ams_slot": None}))
    owned.sort(key=lambda f: (f.ams_slot is None, f.ams_slot or 0, f.id))
    have = {f.id for f in owned}
    seen: set[str] = set()
    out = []
    for f in catalog:
        if f.id not in have and f.id not in seen:
            seen.add(f.id)
            out.append(f)
    return owned, out


def _s06_file(job_dir: Path, name: str) -> dict | None:
    got = latest_manifest(job_dir, "s06_printability")
    if not got:
        return None
    p = job_dir / f"s06_printability/a{got[0]}" / name
    return _read_json(p) if p.is_file() else None


def palette_source_of(job_dir: Path, spec: dict | None) -> str:
    """The resolved s06 product spec, else input/request.json and job.json (owned default)."""
    if spec and spec.get("palette_source") in ("owned", "any"):
        return spec["palette_source"]
    for p in (job_dir / "input" / "request.json", job_dir / "job.json"):
        if p.is_file():
            v = _read_json(p).get("palette_source")
            if v in ("owned", "any"):
                return v
    return "owned"


def cached_ply(path: Path, cache_dir: Path) -> tuple[Mesh, str]:
    """load_ply is slow on 1M-face meshes; keep an npz next to the attempt keyed by sha256."""
    sha = sha256_file(path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    npz = cache_dir / f"{path.stem}_{sha[:16]}.npz"
    if npz.is_file():
        z = np.load(npz)
        return Mesh(vertices=z["v"], faces=z["f"], name=path.stem), sha
    m = load_ply(path)
    np.savez(npz, v=m.vertices, f=m.faces)
    return m, sha


def _color_spec(job_dir: Path) -> list[dict]:
    got = latest_manifest(job_dir, "s02_color_spec")
    if got:
        rel = output_path(got[1], "color_spec")
        if rel and (job_dir / rel).is_file():
            spec = _read_json(job_dir / rel)
            items = []
            for s in spec.get("subjects", []):
                for p in s.get("parts", []):
                    items.append({"subject_id": s.get("subject_id"), "region": p.get("label"),
                                  "part": p.get("part"), "color": p.get("color_name"),
                                  "hex": p.get("hex"), "kind": "part",
                                  "required": p.get("required", True)})
            if items:
                return items
    got = latest_manifest(job_dir, "s01_intake")
    if got:
        p = job_dir / f"s01_intake/a{got[0]}/color_regions.json"
        if p.is_file():
            return list(_read_json(p))
    return []


def _baseline(job_dir: Path, figure: Mesh) -> tuple[np.ndarray | None, str | None, list[str]]:
    """Per figure face filament id from the s07 X2D project (figure part), if it matches."""
    got = latest_manifest(job_dir, "s07_baseline")
    if not got:
        return None, None, ["no s07 baseline"]
    _, m = got
    rel = output_path(m, "baseline_3mf")
    palette = (m.get("data") or {}).get("palette") or []
    if not rel or not palette:
        return None, None, ["s07 has no X2D project"]
    from p2p_threemf import read_3mf_full

    tm = read_3mf_full(job_dir / rel)
    for pid in np.unique(tm.face_part):
        sel = tm.face_part == pid
        if int(sel.sum()) != figure.n_faces:
            continue
        cen = tm.vertices[tm.faces[sel]].mean(axis=1)
        gap = float(np.abs(cen - figure.face_centroids).max())
        if gap > 0.05:
            continue
        slots = tm.face_slots[sel].astype(np.int64)
        ids = np.array([palette[s - 1] if 0 < s <= len(palette) else None for s in range(17)],
                       dtype=object)
        return ids[np.clip(slots, 0, 16)], rel, [f"baseline from {rel} (part {pid}, gap {gap:.2g} mm)"]
    return None, None, [f"{rel}: no part matches the s06 figure face for face"]


def _texture(job_dir: Path, geometry: GeometryResult | None, figure: Mesh):
    got = latest_manifest(job_dir, "s05_mesh")
    if not got:
        return None, None, ["no s05 mesh"]
    rel = output_path(got[1], "approved_mesh")
    if not rel:
        return None, None, ["s05 has no approved mesh"]
    g = load_glb(job_dir / rel)
    rgb = g.face_texture_colors()
    if rgb is None:
        return None, rel, ["approved mesh has no texture"]
    fim = None
    if geometry is not None and geometry.face_index_map is not None:
        fim = np.load(job_dir / geometry.face_index_map.path).astype(np.int64)
    if fim is not None:
        out = np.zeros((len(fim), 3), np.uint8)
        ok = (fim >= 0) & (fim < len(rgb))
        out[ok] = rgb[fim[ok]]
        if (~ok).any():  # added faces (caps): nearest mapped face by centroid
            from scipy.spatial import cKDTree

            c = figure.face_centroids
            _, j = cKDTree(c[ok]).query(c[~ok])
            out[~ok] = out[np.nonzero(ok)[0][j]]
        rgb = out
    if len(rgb) != figure.n_faces:
        return None, rel, [f"texture faces {len(rgb)} != figure faces {figure.n_faces}"]
    return rgb, rel, []


# ---------------------------------------------------------------- entry points


def load_job(job_id: str, cache_dir: Path, root: Path | None = None) -> JobInputs:
    job_dir = (root or jobs_root()) / job_id
    if not job_dir.is_dir():
        raise FileNotFoundError(f"job folder not found: {job_dir}")
    job = _read_json(job_dir / "job.json") if (job_dir / "job.json").is_file() else {}
    settings = PrintSettings.model_validate(job.get("settings") or {})
    got = latest_manifest(job_dir, "s06_printability")
    if not got:
        raise FileNotFoundError("s06_printability has not succeeded; nothing to paint")
    geo_rel = output_path(got[1], "geometry") or f"s06_printability/a{got[0]}/geometry.json"
    geometry = GeometryResult.model_validate(_read_json(job_dir / geo_rel))
    figure, sha = cached_ply(job_dir / geometry.figure.mesh.path, cache_dir)
    parts = []
    for p in geometry.parts:
        if p.role == "cutter":
            continue
        pm, _ = cached_ply(job_dir / p.mesh.path, cache_dir)
        parts.append(PartMesh(p.name, p.role, p.filament_id, pm, p.mesh.path))
    owned, catalog = load_library(also={p.filament_id for p in geometry.parts
                                        if p.filament_id})
    base, base_rel, notes = _baseline(job_dir, figure)
    tex, glb_rel, n2 = _texture(job_dir, geometry, figure)
    spec = (_s06_file(job_dir, "product_resolved.json")
            or _s06_file(job_dir, "product_spec.json") or {})
    read = (_s06_file(job_dir, "concept_read.json") or {}).get("read")
    return JobInputs(
        job_id=job_id, job_dir=job_dir, settings=settings,
        product_type=str(job.get("product_type") or "figurine"),
        orientation=geometry.orientation, figure=figure,
        figure_rel=geometry.figure.mesh.path, figure_sha=sha, parts=parts,
        library=owned, catalog=catalog, baseline_filaments=base, baseline_rel=base_rel,
        tex_rgb=tex, color_spec=_color_spec(job_dir), geometry=geometry, geometry_rel=geo_rel,
        glb_rel=glb_rel, supports=geometry.supports, notes=notes + n2,
        palette_source=palette_source_of(job_dir, spec), spec=spec, concept_read=read,
    )


def load_fixture(job_dir: Path, figure: Mesh, *, tex_rgb=None, parts=(), color_spec=(),
                 baseline_filaments=None, settings: PrintSettings | None = None,
                 orientation: str = "upright", library=None, catalog=None) -> JobInputs:
    """Inputs for a procedural or fixture job with no stage folders (tests, demos)."""
    owned, cat = load_library()
    return JobInputs(
        job_id=job_dir.name, job_dir=job_dir, settings=settings or PrintSettings(),
        product_type="figurine", orientation=orientation, figure=figure, figure_rel=None,
        figure_sha=hashlib.sha256(figure.faces.tobytes() + figure.vertices.tobytes()).hexdigest(),
        parts=list(parts), library=list(library if library is not None else owned),
        catalog=list(catalog if catalog is not None else cat),
        baseline_filaments=baseline_filaments, tex_rgb=tex_rgb, color_spec=list(color_spec),
    )
