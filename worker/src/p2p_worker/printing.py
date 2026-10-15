"""From paint to a printable X2D project, shared by s07, s08 and s10.

The chain is p2p_geometry.production_slice.production_3mf's, split open so the worker can add
the steps it lacks (proof run 1: gray became brown, 1,232 brown speckles, no render sheet):

1. transfer_paint: Meshy's Multi-Color 3MF is fitted to the UNCUT geometry figure
   (fit_meshy_frame, as production_3mf does) and each face of the geometry figure takes the
   color slot of the nearest placed Meshy face (figure_source "geometry": flat products keep
   their plinth cut). Without a GeometryResult, Meshy's own mesh prints (figure_source "meshy").
2. match_colour / map_colours: each source color goes to the nearest loaded filament by
   CIEDE2000 with an achromatic guard: a low-chroma color (gray, black, white) only matches a
   low-chroma filament, so a gray coat never becomes brown. Thresholds are the paint server's
   (p2p_mcp_paint.palette), so s08 and s09 agree. A color with no loaded match within dE00 10
   yields a spool suggestion ("gray PLA"), with the closest catalog spool when one is within
   dE00 10; spool_payloads turns them into contracts SpoolSuggestion messages.
3. despeckle: painted islands narrower than 2 x nozzle merge into the neighbour they share the
   longest boundary with (p2p_sim.simulate.erode, the simulator's rule 1), then any patch still
   smaller than a 2 x nozzle disk joins the label it shares the most edges with. Every 3MF
   write goes through it.
4. write_print + gate: p2p_threemf.write_3mf with the add-on parts from GeometryResult and the
   slicer supports it asks for, then slice_check; floating regions rebuild once with supports.
   Bambu Studio is the ship gate. On a worker without it (a cloud VM) the gate slices with
   p2p_slicer when that can run: the report says slicer "p2p_slicer" and carries NOT_SHIP_GATED,
   and s07/s10 send the job to review ("Bambu gate pending") instead of shipping it.
5. render_outputs: the render sheet (p2p_sim.render_sheet of the file as it prints, filament
   colors, flat products as they sit) and a preview GLB whose COLOR_0 is exact per face.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from p2p_contracts import Filament, FilamentLibrary, PrintSettings, SimParams, SliceReport
from p2p_contracts.geometry import GeometryResult

ACHROMATIC_TARGET_C = 10.0  # a source color this gray (C*ab) only matches a gray filament
ACHROMATIC_FILAMENT_C = 6.0  # a filament this gray counts as gray, black or white
SUGGEST_DE = 10.0  # no loaded filament within this dE00: suggest a spool
MIN_SHARE = 0.005  # source colors under this share of the figure area get no suggestion
FLOATING = "floating regions"
BAMBU = "bambu_studio"
NATIVE = "p2p_slicer"
NOT_SHIP_GATED = ("not ship-gated: sliced with p2p_slicer{why}; re-slice with Bambu Studio on "
                  "the Mac before shipping")
SUPPORTS = {"none": {"enable_support": "0"}, "tree": {"enable_support": "1"},
            "normal": {"enable_support": "1", "support_type": "normal(auto)"}}


# ---------------------------------------------------------------- color

def hex_to_lab(hx: str) -> tuple[float, float, float]:
    from p2p_threemf.colour import hex_to_lab as _h

    return tuple(round(float(x), 3) for x in _h(hx))  # type: ignore[return-value]


def de2000(a, b) -> float:
    from p2p_threemf.colour import ciede2000

    return float(ciede2000(tuple(a), tuple(b)))


def chroma(lab) -> float:
    return float(np.hypot(lab[1], lab[2]))


def color_name(lab) -> str:
    """Plain name for a CIELAB color (gray, pink, ...), for suggestions and region labels."""
    L = float(lab[0])
    if chroma(lab) < ACHROMATIC_TARGET_C:
        if L < 25:
            return "black"
        if L > 88:
            return "white"
        return "light gray" if L > 62 else ("dark gray" if L < 38 else "gray")
    try:
        from p2p_sam3.color import color_name as named

        return str(named(np.asarray(lab, float)))
    except ImportError:
        return "colored"


def candidates(library: FilamentLibrary) -> list[Filament]:
    """Loaded AMS filaments in slot order (project slot N = AMS slot N), else owned ones."""
    loaded = [f for f in library.filaments if f.ams_slot is not None]
    if loaded:
        return sorted(loaded, key=lambda f: f.ams_slot)
    return [f for f in library.filaments if f.owned]


def load_catalog(repo_root: Path) -> list[Filament]:
    """Spools Jackson could buy (owned false): filaments/catalog.json, every single-color Bambu
    filament with an X2D preset (`python -m p2p_filaments catalog`), else the short
    filaments/library.example.json."""
    for name in ("catalog.json", "library.example.json"):
        p = Path(repo_root) / "filaments" / name
        try:
            return list(FilamentLibrary.model_validate_json(p.read_text()).filaments)
        except (OSError, ValueError):
            continue
    return []


# Lines the worker may pick or suggest on its own. Other lines (silk, sparkle, translucent,
# glow, wood, CF, PETG, ABS, TPU, ...) do not print their nominal hex or do not bond with PLA in
# one print, so they are only used when the customer names them in palette_edit slots.
AUTO_PICK_LINES = ("PLA Basic", "PLA Matte")
# Owned spools first: an unowned catalog color is compared at its CIEDE2000 plus this, so it
# only wins when it is more than 3 dE00 closer than the best owned spool (about the step
# between two neighbouring catalog colors; under it the swap is not worth buying a spool).
UNOWNED_PENALTY_DE = 3.0


def auto_pick(catalog: list[Filament], exclude: set[str] | None = None) -> list[Filament]:
    """Catalog colors the worker may add or suggest by itself (AUTO_PICK_LINES)."""
    skip = exclude or set()
    return [f for f in catalog if f.line in AUTO_PICK_LINES and f.id not in skip]


@dataclass
class ColourMatch:
    src_hex: str
    src_lab: tuple[float, float, float]
    filament_id: str
    delta_e: float
    achromatic: bool
    unguarded_id: str  # what plain nearest dE00 would have picked
    unguarded_de: float
    suggestion: str | None = None
    suggest_id: str | None = None  # closest catalog spool, when it is within SUGGEST_DE
    suggest_de: float | None = None
    unowned: bool = False  # the pick is an unowned catalog color (source any)


def _guarded(lab, pool: list[Filament], n_owned: int | None = None) -> tuple[float, int] | None:
    """(cost, index) of the nearest filament under the achromatic guard. Candidates from index
    n_owned on are unowned and cost UNOWNED_PENALTY_DE more."""
    achro = chroma(lab) < ACHROMATIC_TARGET_C
    best = None
    for i, f in enumerate(pool):
        if achro and chroma(f.lab) > ACHROMATIC_FILAMENT_C:
            continue
        d = de2000(lab, f.lab) + (UNOWNED_PENALTY_DE if n_owned is not None and i >= n_owned
                                  else 0.0)
        if best is None or d < best[0]:
            best = (d, i)
    return best


def match_colour(src_hex: str, pool: list[Filament],
                 catalog: list[Filament] | None = None,
                 extra: list[Filament] | None = None) -> ColourMatch:
    """Nearest loaded filament under the achromatic guard; a suggestion past SUGGEST_DE.

    extra: unowned catalog candidates (source any). They compete with the pool at their dE00
    plus UNOWNED_PENALTY_DE, so an owned spool wins a near tie."""
    lab = hex_to_lab(src_hex[:7])
    cands = list(pool) + [x for x in extra or [] if x.id not in {f.id for f in pool}]
    n = len(pool) if len(cands) > len(pool) else None
    pen = [UNOWNED_PENALTY_DE if n is not None and i >= n else 0.0 for i in range(len(cands))]
    plain = min(((de2000(lab, f.lab) + pen[i], i) for i, f in enumerate(cands)))
    pick = _guarded(lab, cands, n) or plain  # no gray filament at all: plain nearest
    f = cands[pick[1]]
    de = pick[0] - pen[pick[1]]
    m = ColourMatch(src_hex=src_hex[:7].upper(), src_lab=lab, filament_id=f.id,
                    delta_e=round(de, 2), achromatic=chroma(lab) < ACHROMATIC_TARGET_C,
                    unguarded_id=cands[plain[1]].id,
                    unguarded_de=round(plain[0] - pen[plain[1]], 2),
                    unowned=n is not None and pick[1] >= n)
    if de > SUGGEST_DE:
        m.suggestion = f"add a {color_name(lab)} PLA"
        have = {x.id for x in cands}
        cat = [x for x in catalog or [] if x.id not in have]
        got = _guarded(lab, cat) if cat else None
        if got is not None and got[0] <= SUGGEST_DE:  # a catalog spool that really matches
            m.suggest_id, m.suggest_de = cat[got[1]].id, round(got[0], 2)
    return m


def map_colours(src_hexes: list[str], pool: list[Filament],
                catalog: list[Filament] | None = None) -> list[ColourMatch]:
    return [match_colour(h, pool, catalog) for h in src_hexes]


def reduce_palette(matches: dict[int, ColourMatch], share: dict[int, float],
                   pool: list[Filament], extra: list[Filament], cap: int,
                   keep: set[str] | None = None) -> dict[int, ColourMatch]:
    """At most `cap` filaments: while there are more, the one covering the least of the model
    (never one in `keep`, e.g. add-on parts) is dropped and its colors re-match among the rest,
    owned first as in match_colour."""
    keep = keep or set()
    out = dict(matches)
    while True:
        area: dict[str, float] = {}
        for s, m in out.items():
            area[m.filament_id] = area.get(m.filament_id, 0.0) + share.get(s, 0.0)
        ids = set(area) | keep
        drop = [i for i in area if i not in keep]
        if len(ids) <= max(cap, 1) or not drop:
            return out
        victim = min(drop, key=lambda i: (area[i], i))
        rest = ids - {victim}
        own = [f for f in pool if f.id in rest]
        ext = [f for f in extra if f.id in rest]
        if not own and not ext:
            return out
        for s, m in list(out.items()):
            if m.filament_id == victim:
                out[s] = match_colour(m.src_hex, own or ext, None, ext if own else None)


def suggestion_text(m: ColourMatch, what: str, catalog: list[Filament] | None = None) -> str:
    """One line for the job's note: 'add a gray PLA (Bambu Lab PLA Basic Gray): #797979 (9% of
    the model), now generic-pla-black, dE00 33.3'."""
    name = ""
    if m.suggest_id:
        f = next((x for x in catalog or [] if x.id == m.suggest_id), None)
        name = f" ({f.brand} {f.line} {f.name})" if f else f" ({m.suggest_id})"
    return (f"{m.suggestion}{name}: {m.src_hex} ({what}), now {m.filament_id}, "
            f"dE00 {m.delta_e:.1f}")


# ---------------------------------------------------------------- paint transfer

@dataclass
class Painted:
    vertices: np.ndarray  # (N, 3) mm, print frame
    faces: np.ndarray  # (M, 3)
    src_slot: np.ndarray  # (M,) 1-based Meshy color slot
    src_colours: list[str]  # Meshy slot hex, index = slot - 1
    figure_source: str  # "geometry" or "meshy"
    fit: dict[str, Any] = field(default_factory=dict)
    transfer: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def transfer_paint(meshy_3mf: Path, glb: Path | None, geometry: GeometryResult | None,
                   job_dir: Path) -> Painted:
    """Meshy's per-face color slots on the mesh that will print."""
    from p2p_threemf.reader import read_3mf_full

    src = read_3mf_full(meshy_3mf, world=False)
    if not src.filament_colours:
        raise ValueError(f"{meshy_3mf.name}: no filament colours in the project")
    colours = [c[:7].upper() for c in src.filament_colours]
    if geometry is None:  # print Meshy's own figure, cleaned as retarget does
        from p2p_threemf.meshfix import clean_mesh

        v, f, s, rep = clean_mesh(src.vertices, src.faces, src.face_slots)
        return Painted(np.asarray(v, float), np.asarray(f, np.int64), np.asarray(s, np.int64),
                       colours, "meshy", transfer={"cleaned": asdict(rep)})
    from p2p_geometry.meshio import apply_transform, load_meshy_glb
    from p2p_geometry.repair import repair_figure
    from p2p_threemf.ply import read_ply
    from p2p_threemf.retarget import MAX_AXIS_SPREAD, WARN_GAP_MM, fit_meshy_frame
    from p2p_threemf.writer import apply_transform as place
    from scipy.spatial import cKDTree

    if glb is None:
        raise ValueError("the geometry figure needs the approved GLB to fit Meshy's frame")
    t = np.asarray(geometry.transform, dtype=np.float64)
    raw = load_meshy_glb(glb)
    uncut = apply_transform(repair_figure(raw.vertices, raw.faces).vertices, t)
    fit = fit_meshy_frame(src.vertices, t, uncut)
    if fit.axis_ratio_spread > MAX_AXIS_SPREAD:
        raise ValueError(f"Meshy 3MF and the geometry figure disagree on scale by axis "
                         f"({fit.axis_ratio_spread:.1%}); frames do not match")
    warnings = list(geometry.warnings)
    if fit.median_gap_mm > WARN_GAP_MM:
        warnings.append(f"placed Meshy mesh sits {fit.median_gap_mm} mm (median) from the "
                        "geometry figure")
    placed = place(src.vertices, fit.matrix)
    fv, ff = read_ply(job_dir / geometry.figure.mesh.path)
    dist, idx = cKDTree(placed[src.faces].mean(axis=1)).query(fv[ff].mean(axis=1))
    return Painted(np.asarray(fv, float), np.asarray(ff, np.int64),
                   np.asarray(src.face_slots, np.int64)[idx], colours, "geometry",
                   fit={"scale": fit.scale, "axis_ratio_spread": fit.axis_ratio_spread,
                        "median_gap_mm": fit.median_gap_mm, "p95_gap_mm": fit.p95_gap_mm},
                   transfer={"median_mm": round(float(np.median(dist)), 4),
                             "p95_mm": round(float(np.percentile(dist, 95)), 4)},
                   warnings=warnings)


def area_share(vertices: np.ndarray, faces: np.ndarray, labels: np.ndarray) -> dict[int, float]:
    tri = vertices[faces]
    area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    tot = max(float(area.sum()), 1e-12)
    w = np.bincount(labels, weights=area)
    return {int(i): float(w[i]) / tot for i in np.nonzero(w)[0]}


# ---------------------------------------------------------------- despeckle

def despeckle(vertices: np.ndarray, faces: np.ndarray, face_labels: np.ndarray,
              settings: PrintSettings) -> tuple[np.ndarray, int]:
    """Islands narrower than 2 x nozzle merge into their neighbour. Returns (labels, changed)."""
    from p2p_sim import Mesh
    from p2p_sim.simulate import erode

    params = SimParams(nozzle_mm=settings.nozzle_mm, layer_mm=settings.layer_mm)
    labels = np.asarray(face_labels, dtype=np.int64)
    if len(np.unique(labels)) < 2:
        return labels, 0
    mesh = Mesh(vertices, faces)
    out, _ = erode(mesh, labels.copy(), params)
    out = merge_small(mesh, np.asarray(out, dtype=np.int64), min_island_mm2(settings))
    return out, int((out != labels).sum())


def min_island_mm2(settings: PrintSettings) -> float:
    """Area of a disk 2 x nozzle across: a patch smaller than this cannot print."""
    return float(np.pi * settings.nozzle_mm ** 2)


def merge_small(mesh, labels: np.ndarray, min_area: float, rounds: int = 4) -> np.ndarray:
    """Patches under min_area that erode kept (its thin-width test has a tolerance) take the
    label they share the most edges with. A patch with no other label around it stays."""
    from p2p_sim.geodesic import components

    out = labels.copy()
    adj = mesh.face_adjacency
    everything = np.ones(mesh.n_faces, bool)
    for _ in range(rounds):
        n, comp = components(mesh, everything, out)
        area = np.bincount(comp, weights=mesh.face_areas, minlength=n)
        small = area < min_area
        if not small.any():
            break
        a, b = adj[:, 0], adj[:, 1]
        cross = (comp[a] != comp[b])
        pairs = np.concatenate([np.c_[comp[a], out[b]][cross & small[comp[a]]],
                                np.c_[comp[b], out[a]][cross & small[comp[b]]]])
        if not len(pairs):
            break
        key, counts = np.unique(pairs, axis=0, return_counts=True)
        order = np.lexsort((-counts, key[:, 0]))  # per patch, the most shared label first
        key = key[order]
        first = np.r_[True, key[1:, 0] != key[:-1, 0]]
        target = np.full(n, -1, dtype=np.int64)
        target[key[first, 0]] = key[first, 1]
        move = target[comp] >= 0
        out[move] = target[comp][move]
    return out


def islands(vertices: np.ndarray, faces: np.ndarray, labels: np.ndarray,
            settings: PrintSettings) -> int:
    """Patches smaller than a 2 x nozzle disk (what despeckle removes first)."""
    from p2p_sim import Mesh
    from p2p_sim.geodesic import components

    mesh = Mesh(vertices, faces)
    n, comp = components(mesh, np.ones(mesh.n_faces, bool), np.asarray(labels))
    area = np.bincount(comp, weights=mesh.face_areas, minlength=n)
    return int((area < min_island_mm2(settings)).sum())


# ---------------------------------------------------------------- write and gate

def parts_for(geometry: GeometryResult | None, job_dir: Path, palette_ids: list[str],
              part_filaments: dict[str, str] | None = None):
    from p2p_threemf.ply import read_ply
    from p2p_threemf.writer import Part

    parts = []
    for pr in (geometry.parts if geometry else []):
        pv, pf = read_ply(job_dir / pr.mesh.path)
        if pr.role == "cutter":
            parts.append(Part(pv, pf, name=pr.name, subtype="negative_part"))
            continue
        fid = (part_filaments or {}).get(pr.name) or pr.filament_id
        if fid not in palette_ids:
            raise ValueError(f"add-on part {pr.name} uses {fid}, which is not loaded")
        parts.append(Part(pv, pf, slot=palette_ids.index(fid) + 1, name=pr.name))
    return parts


def tower_overrides(vertices: np.ndarray, parts) -> dict[str, str]:
    """Move the prime tower when the centred object covers Bambu's default spot (a 150 mm
    nameplate does): p2p_geometry.production_slice.tower_overrides on the XY footprint."""
    try:
        from p2p_geometry.production_slice import tower_overrides as tower
    except ImportError:
        return {}
    pts = [np.asarray(vertices, float)] + [np.asarray(p.vertices, float) for p in parts
                                           if getattr(p, "subtype", "normal_part")
                                           == "normal_part"]
    allv = np.concatenate(pts)
    size = allv.max(axis=0) - allv.min(axis=0)
    return dict(tower((float(size[0]), float(size[1]))))


def part_filaments_missing(geometry: GeometryResult | None, pool: list[Filament],
                           library: FilamentLibrary | None,
                           catalog: list[Filament]) -> list[Filament]:
    """Add-on part filaments that are not in the pool (an unowned catalog color the job
    allowed, or an owned spool not in the AMS), in part order, for the palette."""
    have = {f.id for f in pool}
    known = {f.id: f for f in catalog}
    known.update({f.id: f for f in (library.filaments if library else [])})
    out: list[Filament] = []
    for p in (geometry.parts if geometry else []):
        fid = p.filament_id
        if p.role == "cutter" or not fid or fid in have or fid not in known:
            continue
        have.add(fid)
        out.append(known[fid])
    return out


def write_print(out: Path, vertices: np.ndarray, faces: np.ndarray, face_slots: np.ndarray,
                palette: list[Filament], geometry: GeometryResult | None,
                settings: PrintSettings, job_dir: Path, *, overrides: dict[str, str] | None = None,
                part_filaments: dict[str, str] | None = None,
                sidecar: dict | None = None) -> dict:
    """The X2D project. face_slots are 1-based indexes into palette (AMS order)."""
    from p2p_threemf.writer import write_3mf

    ids = [f.id for f in palette]
    parts = parts_for(geometry, job_dir, ids, part_filaments)
    cfg = dict(settings.process_overrides)
    if geometry is not None:
        cfg.update(SUPPORTS.get(geometry.supports, {}))
    cfg.update(tower_overrides(vertices, parts))
    cfg.update(overrides or {})
    s = settings.model_copy(update={"ams_slots": max(settings.ams_slots, len(palette))})
    res = dict(write_3mf(vertices, faces, face_slots, palette, s, out, object_name="figure",
                         config_overrides=cfg, extra_sidecar=sidecar or {}, parts=parts))
    res["process_overrides"] = cfg
    res["palette"] = ids
    return res


def failed_report(settings, reason: str) -> SliceReport:
    return SliceReport(ok=False, slicer=settings.slicer, slicer_version="not run",
                       printer_profile="", process_profile="", errors=[reason])


# ---------------------------------------------------------------- gate backend

def gate_settings(settings: PrintSettings) -> PrintSettings:
    """The settings slice_check runs with here. Bambu Studio is the ship gate; where its binary
    is missing (SLICER_BIN or the Mac app) and p2p_slicer can slice, use p2p_slicer so the job
    reaches review with time and grams instead of failing on "not installed"."""
    from p2p_threemf import slicer as sl

    if settings.slicer != BAMBU or sl.backend_available(BAMBU):
        return settings
    if not sl.backend_available(NATIVE):
        return settings  # the gate reports "bambu_studio is not installed", as before
    return settings.model_copy(update={"slicer": NATIVE})


def ship_gated(report: SliceReport | None) -> bool:
    """False for a p2p_slicer report: an estimate that must never ship on its own."""
    return report is not None and report.slicer != NATIVE


def mark_estimate(report: SliceReport, fallback: bool) -> SliceReport:
    """Put NOT_SHIP_GATED first in a p2p_slicer report's warnings (once)."""
    if ship_gated(report):
        return report
    note = NOT_SHIP_GATED.format(why=" (Bambu Studio is not installed here)" if fallback else "")
    if not any(w.startswith("not ship-gated") for w in report.warnings):
        report.warnings.insert(0, note)
    return report


def paint_slice_fn(settings: PrintSettings) -> Callable | None:
    """s09's final gate on a worker without Bambu Studio: the paint session's own slice_check,
    run with p2p_slicer. None where Bambu Studio is installed (the session gates as before)."""
    gs = gate_settings(settings)
    if gs.slicer == settings.slicer:
        return None

    def fn(session) -> SliceReport:
        import dataclasses

        from p2p_contracts.mcp_tools import SliceCheckIn

        orig = session.inp
        session.inp = dataclasses.replace(orig, settings=orig.settings.model_copy(
            update={"slicer": gs.slicer}))
        try:
            rep = session.slice_check(SliceCheckIn()).out
        finally:
            session.inp = orig
        mark_estimate(rep, fallback=True)
        (session.dir / "slice_report.json").write_text(rep.model_dump_json(indent=1))
        return rep

    return fn


def build_and_gate(out: Path, write: Callable[[dict[str, str]], dict], settings: PrintSettings,
                   out_dir: Path, job_root: Path, *, gate: bool = True
                   ) -> tuple[dict, SliceReport | None]:
    """write(overrides) builds the project; slice_check gates it. Floating regions rebuild
    once with supports on. gate=False writes only (report None)."""
    from p2p_threemf import slice_check

    def one(overrides: dict[str, str]) -> tuple[dict, SliceReport | None]:
        try:
            res = write(overrides)
        except Exception as e:  # noqa: BLE001 a failed write is a failed gate
            return {}, failed_report(settings, f"3MF write failed: {type(e).__name__}: {e}")
        if not gate:
            return res, None
        palette = list(res.get("palette") or [])
        used = res.get("slots_used") or []
        s = gate_settings(settings.model_copy(update={
            "ams_slots": max(settings.ams_slots, len(palette) or 1),
            "process_overrides": res.get("process_overrides") or settings.process_overrides}))
        rep = slice_check(out, s, len(used) if used else None, filament_ids=palette,
                          out_dir=out_dir, job_root=job_root)
        mark_estimate(rep, fallback=s.slicer != settings.slicer)
        rep.warnings.extend(w for w in res.get("warnings") or [] if w not in rep.warnings)
        return res, rep

    res, rep = one({})
    supports = (res.get("process_overrides") or {}).get("enable_support") == "1"
    if rep is not None and rep.ok and not supports and any(FLOATING in w for w in rep.warnings):
        res, rep = one({"enable_support": "1"})
        supports = True
    res["supports"] = supports
    return res, rep


# ---------------------------------------------------------------- previews

def _linear_u8(rgb: np.ndarray) -> np.ndarray:
    c = rgb.astype(np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return np.round(lin * 255).astype(np.uint8)


def per_face_color_glb(vertices_mm: np.ndarray, faces: np.ndarray, face_rgb: np.ndarray,
                       out: Path) -> int:
    """GLB (glTF frame, metres) with COLOR_0 exact per face: each color keeps its own copy of
    the vertices it touches, so boundaries stay sharp and only boundary vertices double.
    COLOR_0 is linear RGB (glTF spec). Returns the vertex count."""
    import trimesh
    from p2p_sim.mesh import GLTF_TO_CONTRACT

    v = np.asarray(vertices_mm, float)
    lo, hi = v.min(axis=0), v.max(axis=0)
    v = v - [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]]  # off the plate: centred, base z 0
    v = (v / 1000.0) @ GLTF_TO_CONTRACT  # contract -> glTF
    f = np.asarray(faces, np.int64)
    rgb = np.asarray(face_rgb, np.uint8)
    key = (rgb[:, 0].astype(np.int64) << 16) | (rgb[:, 1].astype(np.int64) << 8) | rgb[:, 2]
    vs, fs, cs, off = [], [], [], 0
    for k in np.unique(key):
        sel = key == k
        used, inv = np.unique(f[sel], return_inverse=True)
        vs.append(v[used])
        fs.append(inv.reshape(-1, 3) + off)
        col = _linear_u8(rgb[sel][:1])[0]
        cs.append(np.tile(np.append(col, 255).astype(np.uint8), (len(used), 1)))
        off += len(used)
    mesh = trimesh.Trimesh(np.concatenate(vs), np.concatenate(fs),
                           vertex_colors=np.concatenate(cs), process=False, validate=False)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(trimesh.exchange.gltf.export_glb(trimesh.Scene(mesh)))
    return int(off)


def face_colors_of(threemf: Path):
    """(ThreeMF as placed on the plate, per-face sRGB uint8) for a written project."""
    from p2p_threemf.reader import read_3mf_full

    tm = read_3mf_full(threemf, world=True)
    colours = [c.lstrip("#")[:6] for c in tm.filament_colours]
    rgb = np.array([[int(c[i:i + 2], 16) for i in (0, 2, 4)] for c in colours], dtype=np.uint8)
    return tm, rgb[np.clip(np.asarray(tm.face_slots) - 1, 0, len(rgb) - 1)]


def negative_parts(threemf: Path) -> set[int]:
    """Part ids written as negative volumes (magnet pockets): not drawn in previews."""
    import zipfile

    from p2p_threemf.reader import _model_settings

    with zipfile.ZipFile(threemf) as z:
        _, _, sub = _model_settings(z)
    return {pid for (_, pid), s in sub.items() if s == "negative_part"}


def render_outputs(threemf: Path, sheet_png: Path | None, preview_glb: Path | None,
                   title: str | None, cell_px: int = 384) -> dict:
    """Render sheet and preview GLB of the file exactly as it prints (negative parts left out;
    the slicer subtracts them)."""
    from p2p_sim import Mesh, render_sheet

    tm, face_rgb = face_colors_of(threemf)
    keep = np.ones(len(tm.faces), bool)
    neg = negative_parts(threemf)
    if neg:
        keep &= ~np.isin(np.asarray(tm.face_part), sorted(neg))
    faces, rgb = np.asarray(tm.faces)[keep], face_rgb[keep]
    out: dict[str, Any] = {"faces": int(keep.sum()),
                           "slots": sorted({int(s) for s in np.asarray(tm.face_slots)[keep]})}
    if sheet_png is not None:
        render_sheet(Mesh(tm.vertices, faces), rgb, sheet_png, kind="shaded", cell_px=cell_px,
                     title=title)
    if preview_glb is not None:
        out["glb_vertices"] = per_face_color_glb(tm.vertices, faces, rgb, preview_glb)
    return out


def proof_renders(out_dir: Path, vertices: np.ndarray, faces: np.ndarray,
                  face_slots: np.ndarray, palette: list[Filament],
                  geometry: GeometryResult | None, settings: PrintSettings, job_dir: Path,
                  part_filaments: dict[str, str] | None = None, title: str = "",
                  hang: dict | None = None) -> dict:
    """The shipped product from the eight proof views (contracts PROOF_VIEWS): model (the 3D
    model's texture on the figure), paint (filament colors) and print (simulate_print), as it
    stands on a shelf; with a loop (hang: s06 metrics.hang) also paint and print as it hangs
    (view "hanging", p2p_sim.proof.hanging_view). Returns {"images": [{kind, view, path}],
    "sim": {...}}."""
    from p2p_sim import Mesh
    from p2p_sim.proof import proof_set
    from p2p_sim.simulate import simulate_print_detailed
    from p2p_threemf.ply import read_ply

    ids = [f.id for f in palette]
    meshes = [Mesh(np.asarray(vertices, float), np.asarray(faces, np.int64))]
    part_slot = []
    for pr_ in (geometry.parts if geometry else []):
        if pr_.role == "cutter":
            continue
        pv, pf = read_ply(job_dir / pr_.mesh.path)
        fid = (part_filaments or {}).get(pr_.name) or pr_.filament_id
        meshes.append(Mesh(np.asarray(pv, float), np.asarray(pf, np.int64)))
        part_slot.append(ids.index(fid) + 1 if fid in ids else 1)
    vs, fs, fp, off = [], [], [], 0
    for i, m in enumerate(meshes):
        vs.append(m.vertices)
        fs.append(m.faces + off)
        fp.append(np.full(m.n_faces, i, np.int64))
        off += m.n_vertices
    product = Mesh(np.concatenate(vs), np.concatenate(fs))
    parts = np.concatenate(fp)
    slots = np.concatenate([np.asarray(face_slots, np.int64)] + [
        np.full(m.n_faces, sl, np.int64) for m, sl in zip(meshes[1:], part_slot, strict=True)])
    rgb = np.array([[int(f.hex.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)] for f in palette],
                   np.uint8)
    paint_rgb = rgb[slots - 1]
    body = int(np.bincount(np.asarray(face_slots, np.int64)).argmax())
    sim = simulate_print_detailed(
        product, slots, np.array([f.lab for f in palette], float),
        SimParams(nozzle_mm=settings.nozzle_mm, layer_mm=settings.layer_mm),
        face_parts=parts if len(meshes) > 1 else None,
        part_slots=[body] + part_slot if len(meshes) > 1 else None)
    model_rgb = None
    try:
        from p2p_mcp_paint.job import _texture

        tex, _, _ = _texture(job_dir, geometry, meshes[0])
        if tex is not None:
            model_rgb = paint_rgb.copy()
            model_rgb[: meshes[0].n_faces] = tex
    except Exception:  # noqa: BLE001 no texture: the model kind is skipped
        model_rgb = None
    orientation = geometry.orientation if geometry else "upright"
    rows = proof_set(product, orientation, out_dir, model_rgb=model_rgb, paint_rgb=paint_rgb,
                     sim=sim, title=title)
    if hang and hang.get("pivot_mm") and hang.get("com_mm"):
        from p2p_sim.proof import hanging_view

        rows += [{k: v for k, v in r.items() if k != "tilt_deg"} for r in hanging_view(
            product, orientation, hang, out_dir, paint_rgb=paint_rgb, sim=sim)]
    rep_ = sim.report
    return {"images": rows, "sim": {"purge_g_est": rep_.purge_g_est, "model_g_est":
                                    rep_.model_g_est, "swaps_total": rep_.swaps_total,
                                    "tower_g": sim.tower_g, "flush_g": sim.flush_g}}


def asdict_matches(ms: list[ColourMatch]) -> list[dict]:
    return [asdict(m) for m in ms]


def spool_payloads(items: list[dict]) -> list[dict]:
    """contracts SpoolSuggestion payloads, one per suggested spool (first mention wins, callers
    order by importance): "gray PLA", target color, dE00 now and with the catalog spool, the
    regions it affects and the share of the model."""
    out: dict[str, dict] = {}
    for it in items:
        if not it.get("suggestion"):
            continue
        name = it["suggestion"].removeprefix("add a ").removeprefix("add an ")
        p = out.setdefault(name, {
            "suggestion": name, "target_hex": it["src_hex"],
            "target_lab": [round(float(x), 2) for x in it["src_lab"]],
            "delta_e_now": round(float(it["delta_e"]), 1),
            "delta_e_with": (round(float(it["suggest_de"]), 1)
                             if it.get("suggest_de") is not None else None),
            "regions": [], "area_pct": None, "filament_id": it.get("suggest_id")})
        p["regions"] = sorted(set(p["regions"]) | set(it.get("regions") or []))[:50]
        if it.get("share") is not None:
            p["area_pct"] = round((p["area_pct"] or 0) + 100 * float(it["share"]), 1)
    return list(out.values())


