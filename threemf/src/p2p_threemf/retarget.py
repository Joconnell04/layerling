"""Retarget a multicolor 3MF (e.g. Meshy Multi-Color) to Jackson's X2D and AMS filaments.

Meshy's Multi-Color 3MF is a Bambu project (written as BambuStudio-02.03.01.00) for an X1 Carbon
with five "Bambu PLA Basic @BBL X1C" slots in Meshy's own colours. We keep its geometry and
per-triangle paint, map each source colour to the nearest loaded filament by CIEDE2000, merge
source slots that land on the same filament, and rewrite the file with our writer as an X2D
0.4 nozzle project with Jackson's presets.

Material preference: the most common material among the candidates (PLA) wins unless another
material's filament is closer by more than `material_margin` Delta E 2000. With Jackson's AMS that
keeps the PETG blue slot for colours that are clearly blue.

Frames (checked on the captured fixture, see docs/research/bambu-3mf.md): Meshy's 3MF mesh is
its glTF mesh turned Y up -> Z up ((x, y, z) -> (x, -z, y)), scaled uniformly (39.495 for a
75 mm tall figure), centred in X and Y and lifted so its base is on z = 0. Face count matches
the glTF but vertex positions differ slightly (median 0.1 mm) and faces are reordered, so paint
cannot be copied to the glTF by face index. GeometryResult.transform maps the glTF frame to the
print frame, so we fit the Meshy 3MF -> glTF step from bounding boxes (the figure PLY taken
back through the inverse transform) and compose.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from p2p_contracts import Filament, FilamentLibrary, PrintSettings
from p2p_contracts.geometry import GeometryResult
from scipy.spatial import cKDTree

from .colour import ciede2000, hex6, hex_to_lab
from .meshfix import clean_mesh
from .ply import read_ply
from .reader import read_3mf_full
from .writer import Part, apply_transform, write_3mf

MATERIAL_MARGIN = 10.0  # Delta E 2000 a non-majority material must win by
# glTF (Y up) -> Meshy 3MF (Z up), column-vector convention.
MESHY_Z_UP = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])
MAX_AXIS_SPREAD = 0.05  # scale disagreement between axes that means the frames do not match
WARN_GAP_MM = 0.5


@dataclass
class SlotMapping:
    src_slot: int
    src_hex: str
    faces: int
    filament_id: str
    target_slot: int
    target_hex: str
    delta_e: float
    runner_up_id: str | None
    runner_up_delta_e: float | None


@dataclass
class FrameFit:
    """Meshy 3MF object frame -> print frame, fitted against the geometry stage's figure."""

    scale: float  # glTF units -> Meshy 3MF mm
    translation: list[float]  # Meshy 3MF offset after rotation and scale
    axis_ratio_spread: float  # 0 = the three axes agree on the scale
    median_gap_mm: float  # placed Meshy vertices to nearest figure vertex, print frame
    p95_gap_mm: float
    matrix: list[list[float]]  # 4x4, Meshy 3MF object frame -> print frame


def load_library(path: str | Path) -> FilamentLibrary:
    return FilamentLibrary.model_validate(json.loads(Path(path).read_text()))


def candidates(library: FilamentLibrary | list[Filament]) -> list[Filament]:
    """Filaments loaded in the AMS (ams_slot set), else owned ones, in AMS slot order."""
    fils = library.filaments if isinstance(library, FilamentLibrary) else list(library)
    loaded = [f for f in fils if f.ams_slot is not None]
    if loaded:
        return sorted(loaded, key=lambda f: f.ams_slot)
    owned = [f for f in fils if f.owned]
    if not owned:
        raise ValueError("no owned filaments in the library")
    return owned


def nearest(src_hex: str, pool: list[Filament], material_margin: float = MATERIAL_MARGIN
            ) -> tuple[Filament, float, Filament | None, float | None]:
    lab = hex_to_lab(src_hex)
    scored = sorted(((ciede2000(lab, f.lab), i, f) for i, f in enumerate(pool)),
                    key=lambda t: (t[0], t[1]))
    majority = Counter(f.material for f in pool).most_common(1)[0][0]
    best_major = next((t for t in scored if t[2].material == majority), None)
    pick = scored[0]
    if (best_major is not None and pick[2].material != majority
            and pick[0] + material_margin > best_major[0]):
        pick = best_major
    others = [t for t in scored if t[2].id != pick[2].id]
    ru = others[0] if others else None
    return pick[2], pick[0], (ru[2] if ru else None), (ru[0] if ru else None)


def _as_matrix(transform) -> np.ndarray:
    m = np.asarray(transform, dtype=np.float64)
    if m.shape == (3, 4):
        m = np.vstack([m, [0, 0, 0, 1]])
    if m.shape != (4, 4):
        raise ValueError("transform must be 4x4")
    return m


def fit_meshy_frame(meshy_vertices: np.ndarray, gltf_to_print, figure_print_vertices: np.ndarray
                    ) -> FrameFit:
    """Compose Meshy 3MF -> glTF (fitted) with glTF -> print (given)."""
    t_gp = _as_matrix(gltf_to_print)
    g = apply_transform(np.asarray(figure_print_vertices, dtype=np.float64), np.linalg.inv(t_gp))
    gr = g @ MESHY_Z_UP.T
    ext_m = np.ptp(meshy_vertices, axis=0)
    ext_g = np.ptp(gr, axis=0)
    ratios = ext_m / np.maximum(ext_g, 1e-12)
    scale = float(np.median(ratios))
    spread = float(ratios.max() / ratios.min() - 1.0)
    trans = meshy_vertices.min(axis=0) - scale * gr.min(axis=0)
    m_to_g = np.eye(4)
    m_to_g[:3, :3] = MESHY_Z_UP.T / scale
    m_to_g[:3, 3] = -MESHY_Z_UP.T @ trans / scale
    total = t_gp @ m_to_g
    placed = apply_transform(meshy_vertices, total)
    gap, _ = cKDTree(np.asarray(figure_print_vertices, dtype=np.float64)).query(placed)
    return FrameFit(scale=scale, translation=[float(x) for x in trans],
                    axis_ratio_spread=round(spread, 6), median_gap_mm=round(float(np.median(gap)), 4),
                    p95_gap_mm=round(float(np.percentile(gap, 95)), 4),
                    matrix=[[float(x) for x in row] for row in total])


def fill_from_neighbours(faces: np.ndarray, slots: np.ndarray, mask: np.ndarray
                         ) -> tuple[np.ndarray, int]:
    """Give every masked face the majority slot of its edge neighbours, flooding inward.

    For faces the geometry stage added (hole fills, the plinth cap; -1 in face_index_map):
    nearest-face transfer gives them the paint of whatever Meshy face is closest, which on a
    plinth cut is the removed plinth, so the cap prints striped. Ties go to the lower slot.
    Returns (slots, faces filled)."""
    f = np.asarray(faces, dtype=np.int64)
    out = np.asarray(slots, dtype=np.int64).copy()
    todo = np.asarray(mask, dtype=bool).copy()
    if not todo.any():
        return out, 0
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    fid = np.tile(np.arange(len(f)), 3)
    key = np.sort(e, axis=1)
    order = np.lexsort((key[:, 1], key[:, 0]))
    key, fid = key[order], fid[order]
    same = np.all(key[1:] == key[:-1], axis=1)
    a, b = fid[:-1][same], fid[1:][same]
    pa, pb = np.concatenate([a, b]), np.concatenate([b, a])  # (face, neighbour), both ways
    n_slots = int(out.max()) + 1
    filled = 0
    while todo.any():
        live = todo[pa] & ~todo[pb]
        if not live.any():
            break
        votes = np.zeros((len(f), n_slots), dtype=np.int64)
        np.add.at(votes, (pa[live], out[pb[live]]), 1)
        got = todo & (votes.sum(axis=1) > 0)
        out[got] = votes[got].argmax(axis=1)
        todo[got] = False
        filled += int(got.sum())
    return out, filled


def retarget(
    src_3mf: str | Path,
    library: FilamentLibrary | list[Filament],
    settings: PrintSettings,
    out_path: str | Path,
    *,
    palette_mode: str = "ams",
    material_margin: float = MATERIAL_MARGIN,
    object_name: str | None = None,
    config_overrides: dict | None = None,
    transform=None,
    parts: list[Part] | None = None,
    figure: tuple[np.ndarray, np.ndarray] | None = None,
    extra_sidecar: dict | None = None,
    clean: bool = True,
    figure_fill: np.ndarray | None = None,
) -> tuple[dict, list[SlotMapping]]:
    """Write the retargeted project. palette_mode "ams" writes every loaded filament in AMS
    order (project slot N = AMS slot N); "used" writes only the filaments the model uses.

    The source mesh is taken in its object coordinates (what read_3mf returns: component
    transforms applied, build placement not). `transform` (4x4) moves it into the product
    frame; `parts` are add-ons already in that frame, each with its own palette slot, e.g.
    Part(loop_v, loop_f, slot=2, name="keyring_loop"). Add-on slots index the output palette
    (AMS order in "ams" mode). `figure` (vertices, faces in the product frame) replaces the
    source mesh; each of its faces takes the paint of the nearest placed source face, except
    faces in `figure_fill` (bool per figure face, e.g. face_index_map < 0), which take their
    neighbours' majority paint (see fill_from_neighbours)."""
    src = read_3mf_full(src_3mf, world=False)
    if not src.filament_colours:
        raise ValueError(f"{src_3mf}: no filament colours in the project")
    pool = candidates(library)
    used = sorted(int(s) for s in np.unique(src.face_slots))
    counts = Counter(src.face_slots.tolist())

    picks: dict[int, tuple[Filament, float, Filament | None, float | None]] = {}
    for s in used:
        if s > len(src.filament_colours):
            raise ValueError(f"face slot {s} has no colour in the source project")
        picks[s] = nearest(src.filament_colours[s - 1], pool, material_margin)

    if palette_mode == "ams":
        palette = pool
    elif palette_mode == "used":
        if parts:
            raise ValueError("add-on parts need palette_mode='ams' so their slots are stable")
        seen: dict[str, Filament] = {}
        for s in used:
            seen.setdefault(picks[s][0].id, picks[s][0])
        palette = sorted(seen.values(), key=lambda f: (f.ams_slot or 99, f.id))
    else:
        raise ValueError("palette_mode must be 'ams' or 'used'")
    slot_of = {f.id: i + 1 for i, f in enumerate(palette)}

    lut = np.zeros(max(used) + 1, dtype=np.int64)
    mappings = []
    for s in used:
        fil, de, ru, ru_de = picks[s]
        lut[s] = slot_of[fil.id]
        mappings.append(SlotMapping(
            src_slot=s, src_hex=hex6(src.filament_colours[s - 1]), faces=counts[s],
            filament_id=fil.id, target_slot=slot_of[fil.id], target_hex=fil.hex.upper(),
            delta_e=round(de, 2), runner_up_id=ru.id if ru else None,
            runner_up_delta_e=round(ru_de, 2) if ru_de is not None else None))
    face_slots = lut[src.face_slots]

    verts, faces, write_transform = src.vertices, src.faces, transform
    transfer = None
    if figure is not None:
        placed = apply_transform(src.vertices, transform) if transform is not None else src.vertices
        fv, ff = np.asarray(figure[0], dtype=np.float64), np.asarray(figure[1], dtype=np.int64)
        dist, idx = cKDTree(placed[src.faces].mean(axis=1)).query(fv[ff].mean(axis=1))
        face_slots = face_slots[idx]
        verts, faces, write_transform = fv, ff, None
        transfer = {"median_mm": round(float(np.median(dist)), 4),
                    "p95_mm": round(float(np.percentile(dist, 95)), 4)}
        if figure_fill is not None:
            fill = np.asarray(figure_fill, dtype=bool)
            if len(fill) != len(ff):
                raise ValueError(f"figure_fill has {len(fill)} entries for {len(ff)} faces")
            face_slots, n_fill = fill_from_neighbours(ff, face_slots, fill)
            transfer["neighbour_filled"] = n_fill

    cleaned = None
    if clean:
        # Meshy meshes carry triangles with a repeated vertex (invalid in 3MF core) and
        # duplicates. Dropping them changes neither the slice nor the paint (measured on the
        # 532k-face M1 file: same grams and minutes, 0.0003 mm2 of area).
        verts, faces, face_slots, crep = clean_mesh(verts, faces, face_slots)
        cleaned = asdict(crep)
    if len(palette) > settings.ams_slots:
        settings = settings.model_copy(update={"ams_slots": min(16, len(palette))})
    name = object_name or Path(src_3mf).stem
    side = {"retarget": {
        "source": Path(src_3mf).name,
        "source_application": src.application,
        "source_printer": src.printer_settings_id,
        "material_margin": material_margin,
        "slots": [asdict(m) for m in mappings],
        "paint_transfer": transfer,
        "cleaned": cleaned,
    }}
    if extra_sidecar:
        side["retarget"].update(extra_sidecar)
    res = write_3mf(verts, faces, face_slots, palette, settings, out_path,
                    object_name=name, config_overrides=config_overrides,
                    transform=write_transform, parts=parts, extra_sidecar=side)
    return dict(res, cleaned=cleaned), mappings


def _geometry_inputs(src_3mf: Path, geometry: GeometryResult, job_dir: Path,
                     palette_ids: list[str]) -> tuple[FrameFit, list[Part], tuple, list[str]]:
    """Frame fit, add-on parts and the figure mesh from a GeometryResult."""
    warnings: list[str] = []
    fig_v, fig_f = read_ply(job_dir / geometry.figure.mesh.path)
    src = read_3mf_full(src_3mf, world=False)
    fit = fit_meshy_frame(src.vertices, geometry.transform, fig_v)
    if fit.axis_ratio_spread > MAX_AXIS_SPREAD:
        raise ValueError(f"Meshy 3MF and the geometry figure disagree on scale by axis "
                         f"({fit.axis_ratio_spread:.1%}); frames do not match")
    if fit.median_gap_mm > WARN_GAP_MM:
        warnings.append(f"placed Meshy mesh sits {fit.median_gap_mm} mm (median) from the "
                        "geometry figure")
    parts: list[Part] = []
    for pr in geometry.parts:
        pv, pf = read_ply(job_dir / pr.mesh.path)
        if pr.role == "cutter":
            parts.append(Part(pv, pf, name=pr.name, subtype="negative_part"))
            continue
        if pr.filament_id is None:
            raise ValueError(f"add-on part {pr.name} has no filament_id")
        if pr.filament_id not in palette_ids:
            raise ValueError(f"add-on part {pr.name} uses {pr.filament_id}, which is not "
                             f"loaded in the AMS ({palette_ids})")
        parts.append(Part(pv, pf, slot=palette_ids.index(pr.filament_id) + 1, name=pr.name))
        if not pr.watertight:
            warnings.append(f"add-on part {pr.name} is not watertight")
    return fit, parts, (fig_v, fig_f), warnings + list(geometry.warnings)


def retarget_3mf(
    meshy_3mf: str | Path,
    geometry: GeometryResult | None,
    job_dir: str | Path,
    library: FilamentLibrary | list[Filament] | str | Path,
    settings: PrintSettings,
    out_path: str | Path,
    *,
    palette_mode: str = "ams",
    material_margin: float = MATERIAL_MARGIN,
    config_overrides: dict | None = None,
    figure_source: str = "meshy",
    clean: bool = True,
) -> dict:
    """Pinned s07 entry point (contracts/geometry.py). Returns dict(path, sha256, n_faces,
    slots_used, palette) plus mapping, frame_fit and warnings.

    With geometry: Meshy's painted mesh goes into the print frame through geometry.transform
    (glTF -> print) composed with the fitted Meshy 3MF -> glTF step, keeps its paint, and every
    geometry.parts PLY (job-relative) becomes a part of the same object with its filament as
    the part extruder ("cutter" parts become negative volumes). figure_source="geometry" uses
    the geometry stage's repaired figure mesh instead and copies paint face by face from the
    nearest placed Meshy face. Run slice_check (or retarget_and_check) before shipping."""
    if isinstance(library, (str, Path)):
        library = load_library(library)
    src = Path(meshy_3mf)
    job_dir = Path(job_dir)
    pool = candidates(library)
    palette_ids = [f.id for f in pool]
    fit = None
    warnings: list[str] = []
    kwargs: dict = {}
    if geometry is not None:
        if palette_mode != "ams":
            raise ValueError("geometry add-ons need palette_mode='ams'")
        fit, parts, figure, warnings = _geometry_inputs(src, geometry, job_dir, palette_ids)
        kwargs = {"transform": fit.matrix, "parts": parts,
                  "extra_sidecar": {"frame_fit": asdict(fit)}}
        if figure_source == "geometry":
            kwargs["figure"] = figure
            if geometry.face_index_map is not None:
                fim = np.load(job_dir / geometry.face_index_map.path)
                if len(fim) == len(figure[1]):
                    kwargs["figure_fill"] = fim < 0
        elif figure_source != "meshy":
            raise ValueError("figure_source must be 'meshy' or 'geometry'")
    res, mappings = retarget(src, library, settings, out_path, palette_mode=palette_mode,
                             material_margin=material_margin, config_overrides=config_overrides,
                             clean=clean, **kwargs)
    res = dict(res)
    res["mapping"] = [asdict(m) for m in mappings]
    res["frame_fit"] = asdict(fit) if fit else None
    res["warnings"] = warnings
    return res


FLOATING_RE = "floating regions"


def retarget_and_check(
    src_3mf: str | Path,
    library: FilamentLibrary | list[Filament],
    settings: PrintSettings,
    out_path: str | Path,
    *,
    geometry: GeometryResult | None = None,
    job_dir: str | Path | None = None,
    palette_mode: str = "ams",
    material_margin: float = MATERIAL_MARGIN,
    supports: str = "auto",
    keep_gcode: bool = False,
    figure_source: str = "meshy",
):
    """retarget_3mf, then the slicer gate. supports="auto" turns on the process's supports
    (tree, Bambu's default type) and re-slices when the slicer reports floating regions, the
    same edit Jackson made by hand on his Meshy imports. Returns (retarget result, SliceReport,
    supports_enabled)."""
    from .slicer import slice_check

    if supports not in ("auto", "on", "off"):
        raise ValueError("supports must be auto, on or off")
    job = Path(job_dir) if job_dir else Path(src_3mf).parent

    def run(overrides):
        res = retarget_3mf(src_3mf, geometry, job, library, settings, out_path,
                           palette_mode=palette_mode, material_margin=material_margin,
                           config_overrides=overrides, figure_source=figure_source)
        s = settings.model_copy(update={"ams_slots": max(settings.ams_slots, len(res["palette"]))})
        rep = slice_check(out_path, s, len(res["slots_used"]), filament_ids=res["palette"],
                          keep_gcode=keep_gcode)
        rep.warnings.extend(w for w in res["warnings"] if w not in rep.warnings)
        return res, rep

    res, report = run({"enable_support": "1"} if supports == "on" else None)
    enabled = supports == "on"
    if supports == "auto" and report.ok and any(FLOATING_RE in w for w in report.warnings):
        res, report = run({"enable_support": "1"})
        enabled = True
    return res, report, enabled
