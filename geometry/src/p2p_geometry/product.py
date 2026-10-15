"""build_product_geometry: scale, orient, repair and add exact-geometry parts per product.

The product's FORM is the customer's choice, made at intake and shown in the approved concept
(print frame: mm, Z up, figure front -Y, bottom on z = 0):
- figure (the default for every product but the magnet): upright, full depth, as Meshy made
  it. Supports only under the belly and chin; the face points -Y and is never a support
  surface. A 3D keychain gets its loop on top, placed so it hangs straight (the center of mass
  under the top of the hole), and a heavier ring, because an upright ring is loaded across its
  layers.
- flat: a true relief of the front view. The figure lies face up (+Z), head toward +Y; a cut
  parallel to the bed keeps only its front (the part the front view shows), the depth of that
  front is squeezed only as far as the charm's thickness aim needs, and a flat back fills every
  gap under it, so it prints with no supports at all. A keyring printed flat is loaded along its
  layers, the strong direction.
A 3D figure is never flattened unless the job chose flat. The magnet defaults to flat because
its concept prompt draws a shallow relief with a flat back; a magnet with form figure stands
upright on a round base that holds the magnet pocket.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from manifold3d import Manifold
from p2p_contracts.filaments import Filament, FilamentLibrary
from p2p_contracts.job import (
    DEFAULT_SIZE_TIER,
    FORM_DEFAULTS,
    PRODUCT_DEFAULTS,
    SIZE_TIERS_MM,
    AddOn,
    AddOnKind,
    PrintSettings,
    ProductType,
    SizeTier,
)

from . import addons as ad
from .color import default_structure_filament, find
from .meshio import (
    GLTF_TO_PRINT,
    apply_transform,
    cross_section_to_shapely,
    from_manifold,
    projected_silhouette,
    try_manifold,
)
from .nameplate import nameplate
from .repair import repair_figure
from .solid import Solid, voxelize
from .types import GeometrySettings, Part, ProductGeometry

FLAT_PRODUCTS = {ProductType.KEYCHAIN, ProductType.MAGNET}  # thickness aims for flat charms
DEFAULT_RELIEF = {ProductType.KEYCHAIN: 0.45, ProductType.MAGNET: 0.5}  # old whole-figure squash
FORMS = ("figure", "flat")
FORM_ALIASES = {
    "flat": "flat", "flat_charm": "flat", "charm": "flat", "relief": "flat", "2d": "flat",
    "flat_face_up": "flat",
    "figure": "figure", "3d": "figure", "3d_figure": "figure", "full_3d": "figure",
    "full": "figure", "upright": "figure", "standing": "figure",
}
# When the job says nothing: the contract's FORM_DEFAULTS (keychain figure, magnet flat; the
# form is the customer's pick at intake) and the full 3D figure for every other product.
DEFAULT_FORM = {pt: f.value for pt, f in FORM_DEFAULTS.items()}
# True relief: the back cut keeps the front part of the figure whose front silhouette covers
# this share of the whole figure's (ASSUMED), and never less than MIN_RELIEF_KEEP of its depth.
RELIEF_COVER = 0.97
MIN_RELIEF_KEEP = 0.35
# ASSUMED heavier ring for an upright 3D keychain (loaded across its layers); needs a test print.
UPRIGHT_KEYRING = {"section_mm": 3.0, "thickness_mm": 5.0}
# ASSUMED: thickest a flat charm should get before the depth is squeezed further, as a share of
# its size (10 mm on a 45 mm keychain, 15 mm on a 60 mm magnet), never under 6 mm.
MAX_FLAT_THICKNESS_RATIO = {ProductType.KEYCHAIN: 10.0 / 45.0, ProductType.MAGNET: 15.0 / 60.0}
MIN_FLAT_THICKNESS_MM = 6.0
MIN_RELIEF = 0.2

# Rotations after the glTF -> print frame change.
_R_FLAT = {
    # face up: figure front (-Y) -> +Z, figure up (+Z) -> +Y
    "front": np.array([[1.0, 0, 0], [0, 0, 1.0], [0, -1.0, 0]]),
    # viewer-left flank up, nose -> +X, figure up -> +Y
    "left": np.array([[0, -1.0, 0], [0, 0, 1.0], [-1.0, 0, 0]]),
    # viewer-right flank up, nose -> -X, figure up -> +Y
    "right": np.array([[0, 1.0, 0], [0, 0, 1.0], [1.0, 0, 0]]),
}


@dataclass
class _Layout:
    fig_v: np.ndarray  # placed figure vertices
    parts: list[Part]
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    translate: np.ndarray = field(default_factory=lambda: np.zeros(3))
    relief: float = 1.0

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        lo = self.fig_v.min(axis=0)
        hi = self.fig_v.max(axis=0)
        for p in self.parts:
            if p.role == "cutter":
                continue
            lo = np.minimum(lo, p.vertices.min(axis=0))
            hi = np.maximum(hi, p.vertices.max(axis=0))
        return lo, hi

    def longest(self) -> float:
        lo, hi = self.bounds()
        return float((hi - lo).max())

    def measure(self, what: str) -> float:
        lo, hi = self.bounds()
        return float(hi[2] - lo[2]) if what == "height" else float((hi - lo).max())


# What a size tier measures (contracts/job.py SIZE_TIERS_MM): figurine height, nameplate plate
# width, everything else the longest side.
SIZE_MEASURE = {ProductType.FIGURINE: "height", ProductType.NAMEPLATE: "plate width"}


def resolve_size(product: ProductType, target_size_mm: float | None,
                 size_tier: SizeTier | str | None) -> tuple[float, SizeTier | None, str]:
    """(target mm, tier or None for a custom size, note). A tier wins over target_size_mm."""
    tiers = SIZE_TIERS_MM.get(product, {})
    what = SIZE_MEASURE.get(product, "longest side")
    if size_tier is not None and tiers:
        tier = SizeTier(size_tier)
        mm = float(tiers[tier])
        note = f"Size tier {tier.value.upper()}: {mm:g} mm {what}"
        if target_size_mm and abs(float(target_size_mm) - mm) > 0.5:
            note += f" (target_size_mm {float(target_size_mm):g} ignored)"
        return mm, tier, note
    if target_size_mm:
        mm = float(target_size_mm)
        tier = next((t for t, v in tiers.items() if abs(v - mm) <= 0.5), None)
        if tier is not None:
            return mm, tier, f"Size tier {tier.value.upper()}: {mm:g} mm {what}"
        return mm, None, f"Custom size: {mm:g} mm {what}"
    if tiers:
        mm = float(tiers[DEFAULT_SIZE_TIER])
        return mm, DEFAULT_SIZE_TIER, (f"Size tier {DEFAULT_SIZE_TIER.value.upper()} (default): "
                                      f"{mm:g} mm {what}")
    mm = float(PRODUCT_DEFAULTS[product]["target_size_mm"])
    return mm, None, f"Default size: {mm:g} mm {what}"


def _merge_addons(product: ProductType, addons: list[AddOn]) -> dict[AddOnKind, AddOn]:
    out: dict[AddOnKind, AddOn] = {}
    for kind in PRODUCT_DEFAULTS.get(product, {}).get("addons", []):
        out[kind] = AddOn(kind=kind)
    for a in addons or []:
        out[a.kind] = a
    return out


def _param(addons: dict[AddOnKind, AddOn], key: str, default=None):
    for a in addons.values():
        if key in a.params:
            return a.params[key]
    return default


def _num(addon: AddOn | None, key: str, default: float) -> float:
    if addon is None or key not in addon.params:
        return default
    try:
        return float(addon.params[key])
    except (TypeError, ValueError):
        return default


def normalize_form(value) -> str | None:
    """'figure', 'flat' or None for anything else (aliases in FORM_ALIASES)."""
    if value is None:
        return None
    return FORM_ALIASES.get(str(value).strip().lower().replace("-", "_").replace(" ", "_"))


def resolve_form(product: ProductType, form: str | None,
                 addons: dict[AddOnKind, AddOn]) -> tuple[str, str]:
    """(form, where it came from). The job's form wins; then an add-on's `form` param; then the
    legacy `orientation` param (flat or upright); then DEFAULT_FORM (figure but for magnets)."""
    f = normalize_form(form)
    if f:
        return f, "job"
    f = normalize_form(_param(addons, "form", None))
    if f:
        return f, "add-on form"
    f = normalize_form(_param(addons, "orientation", None))
    if f:
        return f, "add-on orientation"
    return DEFAULT_FORM.get(product, "figure"), "product default"


def _coerce_library(library) -> tuple[FilamentLibrary, list[str]]:
    if isinstance(library, FilamentLibrary):
        return library, []
    if isinstance(library, (str, Path)):
        return FilamentLibrary.model_validate_json(Path(library).read_text()), []
    if isinstance(library, dict):
        return FilamentLibrary.model_validate(library), []
    from datetime import datetime

    fb = FilamentLibrary(
        updated_at=datetime.now(UTC),
        filaments=[
            Filament(id="pla-black", brand="generic", line="PLA", name="Black", hex="#161616",
                     lab=(7.25, 0.0, 0.0), ams_slot=1),
            Filament(id="pla-white", brand="generic", line="PLA", name="White", hex="#FFFFFF",
                     lab=(100.0, 0.0, 0.0), ams_slot=2),
        ],
    )
    return fb, ["No filament library given; used a black and white placeholder"]


def _mesh_part(name, role, m: Manifold, filament_id, note=None) -> Part:
    v, f = from_manifold(m)
    return Part(name=name, role=role, vertices=v, faces=f, filament_id=filament_id, note=note)


def _feet_sink(solid: Solid, overlap: float, max_extra: float = 3.0) -> tuple[float, dict]:
    """How far to sink the figure so every foot overlaps the plate by at least `overlap`."""
    from scipy import ndimage

    zb = solid.z_bottom
    zmin = float(np.nanmin(zb))
    band = zmin + max(3.0, 4 * solid.pitch)
    mask = np.isfinite(zb) & (zb <= band)
    lab, n = ndimage.label(mask)
    feet = []
    for k in range(1, n + 1):
        cells = lab == k
        area = cells.sum() * solid.pitch**2
        if area < 2.0:
            continue
        feet.append((float(np.nanmin(zb[cells])), area))
    if not feet:
        return overlap, {"feet": 0}
    highest_foot = max(f[0] for f in feet) - zmin
    extra = min(highest_foot, max_extra)
    return overlap + extra, {
        "feet": len(feet),
        "foot_height_spread_mm": highest_foot,
        "contact_area_mm2": float(sum(f[1] for f in feet)),
        "extra_sink_mm": extra,
    }


def _remove_plinth(rep_fig, notes: list[str], warnings: list[str]) -> None:
    """Cut Meshy's plinth off the repaired figure in place (flat products only)."""
    from .plinth import cut_below, detect_plinth
    from .repair import edge_stats

    up = GLTF_TO_PRINT[:3, :3]
    v_up = rep_fig.vertices @ up.T
    pl = detect_plinth(v_up, rep_fig.faces)
    if not pl.found:
        notes.append(f"No Meshy plinth found ({pl.reason})")
        return
    height = float(np.ptp(v_up[:, 2]))
    z_cut = pl.z_top + 0.004 * height
    v2, f2, fmap, vmap, n_cap = cut_below(v_up, rep_fig.faces, z_cut)
    before = rep_fig.face_source
    kept_src = set(before[fmap[fmap >= 0]].tolist())
    dropped = sorted(int(x) for x in set(before[before >= 0].tolist()) - kept_src)
    new_src = np.where(fmap >= 0, before[np.clip(fmap, 0, None)], -1)
    vs = rep_fig.vertex_source
    rep_fig.vertex_source = np.where(vs >= 0, vmap[np.clip(vs, 0, None)], -1)
    rep_fig.vertices = v2 @ up  # back to the glTF frame (up is orthonormal)
    rep_fig.faces = f2
    rep_fig.face_source = new_src
    r = rep_fig.report
    r.removed_faces["plinth"] = dropped
    r.plinth_cap_faces = n_cap
    r.plinth_height_frac = pl.height_frac
    nm, bd = edge_stats(f2)
    r.nonmanifold_edges_after, r.boundary_edges_after = nm, bd
    r.watertight = nm == 0 and bd == 0
    notes.append(
        f"Removed Meshy's plinth ({pl.height_frac:.0%} of the height): {len(dropped)} Meshy faces "
        f"dropped, faces crossing the cut clipped, opening capped with {n_cap} faces "
        "(face_index_map)"
    )
    if not r.watertight:
        warnings.append("Figure not watertight after the plinth cut")


def union_center_of_mass(meshes: list[tuple[np.ndarray, np.ndarray]],
                         cutters: list[tuple[np.ndarray, np.ndarray]] | None = None) -> np.ndarray:
    """Center of mass of the printed solid: the UNION of the parts (overlaps count once) minus
    any cutters. Exact with manifold3d; falls back to a shared voxel grid for a figure that is
    not a closed manifold."""
    import trimesh
    from manifold3d import OpType

    ms = [try_manifold(v, f) for v, f in meshes]
    cs = [try_manifold(v, f) for v, f in (cutters or [])]
    if all(m is not None for m in ms + cs):
        u = Manifold.batch_boolean(ms, OpType.Add)
        if cs:
            u = u - Manifold.batch_boolean(cs, OpType.Add)
        v, f = from_manifold(u)
        return np.asarray(trimesh.Trimesh(v, f, process=False).center_mass)
    allv = np.concatenate([v for v, _ in meshes])
    lo, hi = allv.min(axis=0), allv.max(axis=0)
    pitch = float(np.clip((hi - lo).max() / 200, 0.2, 1.0))
    occ = None
    for v, f in meshes:
        sol = voxelize(v, f, pitch, bounds=(lo, hi))
        occ = sol.occ if occ is None else occ | sol.occ
    for v, f in cutters or []:
        occ &= ~voxelize(v, f, pitch, bounds=(lo, hi)).occ
    ijk = np.argwhere(occ)
    return sol.origin + ijk.mean(axis=0) * pitch


def _com(parts_meshes: list[tuple[np.ndarray, np.ndarray]]) -> np.ndarray:
    return union_center_of_mass(parts_meshes)


def _lay_com(ctx, lay) -> np.ndarray:
    solids = [(lay.fig_v, ctx.F)] + [(p.vertices, p.faces) for p in lay.parts if p.role != "cutter"]
    cutters = [(p.vertices, p.faces) for p in lay.parts if p.role == "cutter"]
    return union_center_of_mass(solids, cutters)


def _loop_spec(addon: AddOn, kind: AddOnKind, gs: GeometrySettings, warnings: list[str],
               upright: bool = False):
    keyring = kind == AddOnKind.KEYRING_LOOP
    heavy = keyring and upright
    spec = ad.LoopSpec(
        inner_d_mm=_num(addon, "loop_inner_d_mm", 5.0),
        section_mm=_num(addon, "loop_section_mm",
                        UPRIGHT_KEYRING["section_mm"] if heavy else 3.0 if keyring else 2.5),
        thickness_mm=_num(addon, "loop_thickness_mm",
                          UPRIGHT_KEYRING["thickness_mm"] if heavy else 4.0 if keyring else 3.0),
    )
    if spec.inner_d_mm < gs.loop_min_hole_mm:
        warnings.append(f"Loop hole {spec.inner_d_mm:g} mm raised to {gs.loop_min_hole_mm:g} mm")
        spec.inner_d_mm = gs.loop_min_hole_mm
    for attr in ("section_mm", "thickness_mm"):
        if getattr(spec, attr) < gs.loop_min_section_mm:
            warnings.append(
                f"Loop {attr[:-3]} {getattr(spec, attr):g} mm raised to {gs.loop_min_section_mm:g} mm"
            )
            setattr(spec, attr, gs.loop_min_section_mm)
    if keyring and spec.thickness_mm < gs.keychain_min_thickness_mm:
        warnings.append(
            f"Keyring thickness {spec.thickness_mm:g} mm raised to "
            f"{gs.keychain_min_thickness_mm:g} mm (keychain minimum)"
        )
        spec.thickness_mm = gs.keychain_min_thickness_mm
    spec.embed_mm = max(spec.embed_mm, gs.loop_min_overlap_mm + 0.5)
    return spec


# ---------------------------------------------------------------- main


class _Ctx:
    def __init__(self, V, F, product, addons, target, library, gs, orientation, display, relief):
        self.V, self.F = V, F
        self.product, self.addons, self.target = product, addons, target
        self.library, self.gs = library, gs
        self.orientation, self.display, self.relief = orientation, display, relief
        self.structure = find(library, gs.structure_filament_id) or default_structure_filament(library)
        self.loop_pos: str | None = None
        self.loop_scores: dict | None = None
        self.relief_fixed = False
        self.relief_cut = False
        self.loop_light = False
        self.scale = 1.0  # mm per unit of the current layout pass
        self.loop_seat_unit: tuple[float, float] | None = None  # straight-hang seat, unit frame
        self.loop_balance: dict | None = None
        self.loop_area = "any"  # head, back, side or any (loop_area: the customer's answer)
        self.head_top_unit: float | None = None  # top of the head in the unit frame (z)
        self.loop_seat_prev: tuple | None = None  # (seat unit xy, balance) before the refit
        self.max_thickness = 0.0

    def filament_for(self, addon: AddOn | None) -> str:
        if addon is not None and addon.filament_id:
            if find(self.library, addon.filament_id) is None:
                return self.structure.id
            return addon.filament_id
        return self.structure.id


def build_product_geometry(
    figure_vertices: np.ndarray,
    figure_faces: np.ndarray,
    product: ProductType | str,
    addons: list[AddOn] | None = None,
    target_size_mm: float | None = None,
    library: FilamentLibrary | str | Path | None = None,
    settings: GeometrySettings | PrintSettings | None = None,
    size_tier: SizeTier | str | None = None,
    form: str | None = None,
    colors=None,
) -> ProductGeometry:
    """form: 'figure' or 'flat' (the job's choice; None: an add-on's form or orientation
    param, else DEFAULT_FORM). colors: addon_color.AddonColorInputs; when given, every add-on
    part without an explicit filament takes a color from the job, the approved concept or the
    figure it touches (addon_color.assign_addon_colors), never the darkest loaded spool."""
    product = ProductType(product)
    gs = GeometrySettings.coerce(settings)
    library, lib_warn = _coerce_library(library)
    merged = _merge_addons(product, addons or [])
    form, form_source = resolve_form(product, form, merged)
    base_added = False
    if (product == ProductType.MAGNET and form == "figure"
            and not ({AddOnKind.ROUND_BASE, AddOnKind.OVAL_BASE} & set(merged))):
        merged[AddOnKind.ROUND_BASE] = AddOn(kind=AddOnKind.ROUND_BASE)  # holds the pocket
        base_added = True
    target, tier, size_note = resolve_size(product, target_size_mm, size_tier)
    measure = SIZE_MEASURE.get(product, "longest")

    repaired = repair_figure(figure_vertices, figure_faces)
    rep = repaired.report
    warnings = list(lib_warn)
    notes: list[str] = []
    if rep.removed_faces:
        n = sum(len(v) for v in rep.removed_faces.values())
        notes.append(
            f"Repair removed {n} faces ({', '.join(f'{k} {len(v)}' for k, v in rep.removed_faces.items())}); "
            "face_index_map carries labels over"
        )
    if rep.holes_filled:
        notes.append(f"Repair filled {rep.holes_filled} holes with {rep.hole_faces_added} faces")
    if not rep.watertight:
        warnings.append(
            f"Figure not watertight after repair: {rep.nonmanifold_edges_after} non-manifold and "
            f"{rep.boundary_edges_after} open edges; holes left open: {rep.holes_skipped}"
        )
    if rep.small_components:
        warnings.append(f"{len(rep.small_components)} loose fragments in the figure mesh")
    big = rep.components - len(rep.small_components)
    if big > 1:
        warnings.append(f"Figure is {big} separate pieces; they print loose unless they touch")

    orientation = "flat" if form == "flat" else "upright"
    display = str(_param(merged, "display", "front"))
    if display not in _R_FLAT:
        warnings.append(f"Display {display!r} unknown; used front")
        display = "front"
    relief_param = _param(merged, "relief_scale", None)
    relief = float(relief_param) if relief_param is not None else DEFAULT_RELIEF.get(product, 0.5)
    if orientation == "upright":
        relief = 1.0

    # Upright height of the whole Meshy figure (plinth included), for the paint detail rule.
    figure_unit_height = float(np.ptp(apply_transform(repaired.vertices, GLTF_TO_PRINT)[:, 2]))
    remove = str(_param(merged, "remove_plinth", "auto")).lower()
    if orientation == "flat" and remove not in ("0", "false", "no", "keep"):
        _remove_plinth(repaired, notes, warnings)

    # source_pose: how the model arrives. upright (a Meshy 3D figure: up +Z, front -Y in the
    # print frame) or face_up (a Creative Lab keychain or magnet build: already a relief lying
    # face up, head toward +Y). A face_up relief is only turned into the display, never stood on
    # its edge (live2 run: the cat charm was turned as if upright, then squeezed to a sliver).
    source_pose = str(_param(merged, "source_pose", "upright")).lower()
    if source_pose not in ("upright", "face_up"):
        warnings.append(f"Source pose {source_pose!r} unknown; used upright")
        source_pose = "upright"
    R = np.eye(4)
    if orientation == "flat":
        R[:3, :3] = (_R_FLAT[display] if source_pose == "upright"
                     else _R_FLAT[display] @ _R_FLAT["front"].T)
        if source_pose == "face_up":
            notes.append("Source is a relief lying face up (Creative Lab build): laid as built, "
                         "not turned from a standing figure")
    M_or = R @ GLTF_TO_PRINT
    relief_cut = None
    if orientation == "flat" and str(_param(merged, "relief_cut", "auto")).lower() not in (
            "0", "false", "no", "off"):
        relief_cut = _relief_cut(repaired, M_or[:3, :3], notes, warnings)
    if relief_cut is not None and relief_param is None:
        relief = 1.0  # a true relief: squeeze the kept front only as far as the thickness aim
    Vo = apply_transform(repaired.vertices, M_or)
    lo, hi = Vo.min(axis=0), Vo.max(axis=0)
    c = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])
    Vu = Vo - c  # unit scale, XY centered, bottom at z = 0
    unit_size = hi - lo

    ctx = _Ctx(Vu, repaired.faces, product, merged, target, library, gs, orientation, display, relief)
    ctx.relief_fixed = relief_param is not None
    ctx.max_thickness = float(_param(
        merged, "max_thickness_mm",
        max(MIN_FLAT_THICKNESS_MM, MAX_FLAT_THICKNESS_RATIO.get(product, 0.25) * target)))
    layout_fn = _layout_flat if orientation == "flat" else _layout_upright

    if AddOnKind.NAMEPLATE in merged and orientation == "upright":
        s, lay = _layout_nameplate(ctx, unit_size)
    else:
        def fit(s: float):
            lay = layout_fn(ctx, s)
            prev = None
            for _ in range(8):
                L = lay.measure(measure)
                if abs(L - target) < 0.05:
                    break
                if prev is not None and abs(L - prev[1]) > 1e-9:
                    s_new = s + (target - L) * (s - prev[0]) / (L - prev[1])
                else:
                    s_new = s * target / L
                prev = (s, L)
                s = max(s_new, 1e-6)
                lay = layout_fn(ctx, s)
            return s, lay

        s, lay = fit(target / float(max(unit_size[0], unit_size[1], unit_size[2] * relief)))
        if ctx.loop_seat_unit is not None:
            # The straight-hang seat was chosen at the first guess of the scale; choose it again
            # at the fitted scale (the ring does not scale with the figure) and refit.
            ctx.loop_seat_prev = (ctx.loop_seat_unit, ctx.loop_balance)
            ctx.loop_seat_unit, ctx.loop_balance, ctx.loop_light = None, None, False
            s, lay = fit(s)

    relief = lay.relief
    S = np.diag([s, s, s * relief, 1.0])
    T0 = np.eye(4)
    T0[:3, 3] = -c
    T1 = np.eye(4)
    T1[:3, 3] = lay.translate
    transform = T1 @ S @ T0 @ M_or
    fig_v = apply_transform(repaired.vertices, transform)
    if not np.allclose(fig_v, lay.fig_v, atol=1e-6):
        raise AssertionError("transform does not reproduce the placed figure")
    repaired.vertices = fig_v

    lo, hi = lay.bounds()
    metrics = {"scale_mm_per_unit": s, "relief_scale": relief, "size_mm": hi - lo,
               "size_tier": tier.value if tier else None, "target_mm": target,
               "figure_height_mm": s * figure_unit_height,
               "figure_size_mm": np.ptp(fig_v, axis=0), "form": form,
               "form_source": form_source, **lay.metrics}
    if relief_cut is not None:
        metrics["relief_cut"] = relief_cut
    form_note = (f"Form {form} ({form_source}): "
                 + ("true relief of the front view on a flat back" if form == "flat"
                    else "full 3D figure, upright, full depth"))
    notes = [
        size_note,
        form_note,
        f"Orientation {orientation}"
        + (f", display {display}, depth x{relief:.2f}" if orientation == "flat" else ""),
        *notes,
        *lay.notes,
    ]
    if base_added:
        notes.append("Magnet as a 3D figure: round base added to hold the magnet pocket")
    got = lay.measure(measure)
    if abs(got - target) > 1.0 and AddOnKind.NAMEPLATE not in merged:
        warnings.append(f"Size {got:.1f} mm, target {target:g} mm")
    geo = ProductGeometry(
        product=product, orientation=orientation, transform=transform, figure=repaired,
        parts=lay.parts, bbox_min=lo, bbox_max=hi, warnings=warnings + lay.warnings, notes=notes,
        metrics=metrics, display=display,
    )
    if colors is not None:
        from .addon_color import assign_addon_colors

        explicit = {a.kind.value: a.filament_id for a in merged.values() if a.filament_id}
        text_fixed = any(a.text is not None and a.text.filament_id for a in merged.values())
        assign_addon_colors(geo, library, colors, explicit=explicit, text_fixed=text_fixed)
    return geo


def _relief_cut(rep_fig, rot3: np.ndarray, notes: list[str], warnings: list[str]) -> dict | None:
    """True relief: cut the figure lying face up (rot3: glTF -> face-up frame) with a plane
    parallel to the bed so only its front stays; the flat back replaces the rest. The plane is
    the deepest one that still keeps RELIEF_COVER of the front silhouette (per-cell front
    heights on a 200-cell grid), and keeps at least MIN_RELIEF_KEEP of the depth. Faces are cut
    like the plinth (face_index_map; -1 for the cap)."""
    from .plinth import cut_below
    from .repair import edge_stats

    v = rep_fig.vertices @ rot3.T
    z0, z1 = float(v[:, 2].min()), float(v[:, 2].max())
    depth = z1 - z0
    if depth <= 0:
        return None
    lo, hi = v[:, :2].min(axis=0), v[:, :2].max(axis=0)
    pitch = float((hi - lo).max()) / 200.0 or 1.0
    ij = np.floor((v[:, :2] - lo) / pitch).astype(np.int64)
    n = ij.max(axis=0) + 1
    top = np.full(int(n[0] * n[1]), -np.inf)
    np.maximum.at(top, ij[:, 0] * n[1] + ij[:, 1], v[:, 2])
    cells = top[np.isfinite(top)]
    z_cover = float(np.quantile(cells, 1.0 - RELIEF_COVER))
    z_cut = min(z_cover, z1 - MIN_RELIEF_KEEP * depth)
    if z_cut <= z0 + 0.02 * depth:
        notes.append("Relief: the front view needs the whole depth; no back cut")
        return None
    v2, f2, fmap, vmap, n_cap = cut_below(v, rep_fig.faces, z_cut)
    before = rep_fig.face_source
    kept_src = set(before[fmap[fmap >= 0]].tolist())
    dropped = sorted(int(x) for x in set(before[before >= 0].tolist()) - kept_src)
    rep_fig.face_source = np.where(fmap >= 0, before[np.clip(fmap, 0, None)], -1)
    vs = rep_fig.vertex_source
    rep_fig.vertex_source = np.where(vs >= 0, vmap[np.clip(vs, 0, None)], -1)
    rep_fig.vertices = v2 @ rot3  # back to the glTF frame (rot3 is orthonormal)
    rep_fig.faces = f2
    r = rep_fig.report
    r.removed_faces["relief_back"] = dropped
    nm, bd = edge_stats(f2)
    r.nonmanifold_edges_after, r.boundary_edges_after = nm, bd
    r.watertight = nm == 0 and bd == 0
    kept = (z1 - z_cut) / depth
    notes.append(
        f"True relief: kept the front {kept:.0%} of the figure's depth (covers "
        f"{RELIEF_COVER:.0%} of the front silhouette), back cut flat and capped with {n_cap} "
        f"faces; {len(dropped)} Meshy faces behind it dropped (face_index_map)")
    if not r.watertight:
        warnings.append("Figure not watertight after the relief cut")
    return {"kept_depth_frac": round(kept, 4), "cap_faces": int(n_cap),
            "dropped_faces": len(dropped), "cover": RELIEF_COVER}


# ---------------------------------------------------------------- upright


def _pitch(size: float) -> float:
    return float(np.clip(size / 160.0, 0.2, 0.6))


# Loop positions are in the figure's own terms (upright frame: up +Z, back +Y, the viewer's
# left -X). Upright loops are built in a working frame where the position points up (+Z) and
# the hole axis is +Y: top and the sides keep the ring facing the viewer, back turns it sideways.
LOOP_POSITIONS = ("top", "rear", "left", "right")
_FIG_DIR = {"top": (0.0, 0.0, 1.0), "rear": (0.0, 1.0, 0.0), "left": (-1.0, 0.0, 0.0),
            "right": (1.0, 0.0, 0.0)}
_UPRIGHT_Q = {
    "top": np.eye(3),
    "left": np.array([[0.0, 0, 1], [0, 1, 0], [-1, 0, 0]]),
    "right": np.array([[0.0, 0, -1], [0, 1, 0], [1, 0, 0]]),
    "rear": np.array([[0.0, 0, 1], [1, 0, 0], [0, 1, 0]]),
}
TOP_PREFERENCE = 1.25  # auto: another side must be this much thicker to beat the top
# Seats this thin still mark where the head's top is (ears never hold a seat: the stem
# footprint is wider than an ear), so the head band stays on the head when the head is too thin
# for the ring at the fitted size.
HEAD_REF_THICKNESS_MM = 1.0
WEAK_SEAT_MM = 3.0  # thinnest seat a loop may take in the customer's area (with a warning)


def _rot3x4(m3: np.ndarray) -> np.ndarray:
    return np.hstack([m3, np.zeros((3, 1))])


def _loop_kind(ctx: _Ctx) -> AddOnKind | None:
    return (AddOnKind.KEYRING_LOOP if AddOnKind.KEYRING_LOOP in ctx.addons
            else AddOnKind.HANGING_LOOP if AddOnKind.HANGING_LOOP in ctx.addons else None)


# The intake answer (question_bank.yaml `loop`) names an area of the figure, not a seat:
# "Top of the head" (position top), "On the back" (back), "On the side" (left or right),
# "Wherever it hangs best" (any). The loop stays in that area; within it the seat that hangs
# least tilted wins. Only `any` lets the loop move to wherever the piece balances.
ANY_POSITIONS = ("", "auto", "any", "anywhere")
POSITION_ALIASES = {"head": "top", "top_of_head": "top", "side": "left"}
# "back" is the customer's word for the top of the back (an upright figure: loop_area back,
# top seats behind the head). The ring that sticks out of the figure's rear (+Y) is "rear";
# on a flat charm "back" means that face, which lies on the bed.
FLAT_ALIASES = {"back": "rear"}
AREA_OF_POSITION = {"top": "head", "back": "back", "left": "side", "right": "side",
                    "rear": "side"}


def loop_area(addon: AddOn) -> str:
    """head, back, side or any: where the customer allows the loop. `hang: head` (legacy)
    means the head; an unset or unknown position means any."""
    if str(addon.params.get("hang", "")).lower() == "head":
        return "head"
    pos = str(addon.params.get("position", "auto")).strip().lower().replace(" ", "_")
    pos = POSITION_ALIASES.get(pos, pos)
    if pos in ANY_POSITIONS:
        return "any"
    return AREA_OF_POSITION.get(pos, "any")


def _positions(addon: AddOn, allowed: list[str], warnings: list[str]) -> tuple[list[str], bool]:
    """Positions to try and whether the choice is automatic."""
    pos = str(addon.params.get("position", "auto")).strip().lower().replace(" ", "_")
    pos = FLAT_ALIASES.get(pos, POSITION_ALIASES.get(pos, pos))
    if pos in ANY_POSITIONS:
        return allowed, True
    if pos not in LOOP_POSITIONS:
        warnings.append(f"Loop position {pos!r} unknown; chose one automatically")
        return allowed, True
    if pos not in allowed:
        warnings.append(f"Loop position {pos!r} faces the bed in this pose; chose one automatically")
        return allowed, True
    return [pos], False


def _pick(cands: list[dict], auto: bool) -> dict | None:
    cands = [c for c in cands if c["anchor"] is not None]
    if not cands:
        return None
    if not auto:
        return cands[0]
    return max(cands, key=lambda c: c["score"] * (TOP_PREFERENCE if c["pos"] == "top" else 1.0))


def _hang_tilt(pivot: np.ndarray, com: np.ndarray, up: np.ndarray) -> float:
    """Degrees between the figure's up and the line from its center of mass to the pivot."""
    d = np.asarray(pivot, dtype=float) - np.asarray(com, dtype=float)
    d /= np.linalg.norm(d) + 1e-12
    u = np.asarray(up, dtype=float)
    u /= np.linalg.norm(u) + 1e-12
    return math.degrees(math.acos(float(np.clip(d @ u, -1, 1))))


def _upright_loop(ctx: _Ctx, lay: _Layout, fig_m: Manifold | None, solid: Solid | None = None,
                  offset: np.ndarray | None = None):
    kind = _loop_kind(ctx)
    if kind is None:
        return
    addon = ctx.addons[kind]
    heavy_spec = _loop_spec(addon, kind, ctx.gs, lay.warnings, upright=True)
    light = _loop_spec(addon, kind, ctx.gs, [], upright=False)
    has_heavy = (light.thickness_mm, light.section_mm) != (heavy_spec.thickness_mm,
                                                           heavy_spec.section_mm)
    spec = light if ctx.loop_light else heavy_spec
    ctx.loop_area = loop_area(addon) if _hang_mode(ctx, addon) != "highest" else "any"
    if ctx.loop_area in ("head", "back"):
        # "On the back" of an upright figure is its top, behind the head (the dorsal back),
        # not a ring sticking out of its rear: the same top seats, another area
        positions, auto = ["top"], False
    else:
        positions, auto = _positions(addon, list(LOOP_POSITIONS), lay.warnings)
    if auto and ctx.loop_pos:
        positions = [ctx.loop_pos]  # chosen on the first scale pass; keep it stable
    pitch = _pitch(float(np.ptp(lay.fig_v, axis=0).max()))

    def run(sp, warns, where):
        mt = max(sp.thickness_mm, ctx.gs.keychain_min_thickness_mm)
        c = _upright_candidates(ctx, lay, sp, where, auto, mt, pitch, warns)
        return c, _pick(c, auto), mt

    try_heavy = has_heavy and not ctx.loop_light
    first_warns: list[str] = []
    cands, best, min_th = run(spec, first_warns if try_heavy else lay.warnings, positions)
    if try_heavy and (best is None or best["anchor"] is None or best["weak"]
                      or best.get("synth")):
        # the heavier upright ring finds no sturdy seat (ears too close): the standard ring
        ctx.loop_seat_unit, ctx.loop_balance, ctx.loop_light = None, None, True
        spec = light
        cands, best, min_th = run(spec, lay.warnings, positions)
    else:
        lay.warnings.extend(first_warns)
    if ctx.loop_light and has_heavy:
        lay.notes.append("The heavier upright ring has no sturdy seat here; standard ring used")
    if best is None and auto and len(positions) < len(LOOP_POSITIONS):  # cached side failed
        ctx.loop_pos = None
        cands, best, min_th = run(spec, lay.warnings, list(LOOP_POSITIONS))
    if best is None:
        lay.warnings.append("No spot for the loop; none added")
        return
    _place_upright_loop(ctx, lay, fig_m, spec, kind, addon, cands, best, auto, min_th)


def _hang_mode(ctx: _Ctx, addon: AddOn) -> str:
    """straight (default): the top loop goes in the customer's area (loop_area) where the
    piece hangs least tilted; head: legacy, the same as position top; highest: the old
    highest-seat rule."""
    mode = str(addon.params.get("hang", "straight")).lower()
    return mode if mode in ("straight", "head", "highest") else "straight"


def _balanced_anchor(sol, spec, com, min_th, ctx: _Ctx | None = None,
                     offset: np.ndarray | None = None, head_only: bool = False,
                     area: str | None = None):
    """(anchor, balance info) for a top loop in the customer's area, or (None, None). The first
    scale pass chooses the seat; later passes keep the seat nearest the same spot (in the
    figure's own unit frame) so the size iteration converges. The head's top is fixed in that
    frame on the first pass too, so a head that loses its seats at another scale (the ring no
    longer fits between the ears) never makes the back count as the head; then the seat the
    last fit chose in the area is kept."""
    off = np.zeros(3) if offset is None else np.asarray(offset, float)

    def keep(unit, bal):
        a = ad.seat_at(sol, spec, unit[0] * ctx.scale + off[0], unit[1] * ctx.scale + off[1])
        if a is None:
            return None, None
        one = ad.Seats(np.array([a.x]), np.array([a.y]), np.array([a.surface]),
                       np.array([a.lowest]), np.array([a.thickness_mm]))
        return a, {**bal, "tilt_deg": float(ad.seat_tilts(one, spec, com)[0])}

    if ctx is not None and ctx.loop_seat_unit is not None and ctx.loop_balance is not None:
        a, bal = keep(ctx.loop_seat_unit, ctx.loop_balance)
        if a is not None:
            return a, bal
    seats = ad.upright_seats(sol, spec, min_th)
    zt = sol.z_top[np.isfinite(sol.z_top)]
    z_top = float(zt.max()) if zt.size else 0.0
    zb = sol.z_bottom[np.isfinite(sol.z_bottom)]
    height = z_top - (float(zb.min()) if zb.size else 0.0)
    head_top = None
    if ctx is not None and ctx.head_top_unit is not None:
        head_top = ctx.head_top_unit * ctx.scale + off[2]
    else:
        ref = ad.upright_seats(sol, spec, HEAD_REF_THICKNESS_MM)  # the head's top
        if ref is not None:
            head_top = float(ref.z_low.max())
            if ctx is not None:
                ctx.head_top_unit = (head_top - off[2]) / ctx.scale
    k = None
    if seats is not None:
        k, info = ad.pick_balanced_seat(seats, spec, com, z_top, height, head_only=head_only,
                                        area=area, head_top=head_top)
    if k is None:
        prev = getattr(ctx, "loop_seat_prev", None) if ctx is not None else None
        if prev is not None and prev[1] is not None and prev[1].get("area") == area:
            return keep(*prev)
        return None, None
    if ctx is not None:
        ctx.loop_seat_unit = (float((seats.x[k] - off[0]) / ctx.scale),
                              float((seats.y[k] - off[1]) / ctx.scale))
        ctx.loop_balance = info
    return seats.anchor(k), info


def _upright_candidates(ctx, lay, spec, positions, auto, min_th, pitch,
                        warns: list[str] | None = None) -> list[dict]:
    warns = lay.warnings if warns is None else warns
    cands = []
    kind = _loop_kind(ctx)
    mode = _hang_mode(ctx, ctx.addons[kind]) if kind is not None else "highest"
    straight = mode in ("straight", "head")
    for pos in positions:
        Q = _UPRIGHT_Q[pos]
        vw = lay.fig_v @ Q.T
        sol = voxelize(vw, ctx.F, pitch)
        com = sol.center_of_mass()
        balance = None
        if straight and pos == "top":
            area = "head" if mode == "head" else (ctx.loop_area if ctx.loop_area in
                                                   ad.LOOP_AREAS else "any")
            a, balance = _balanced_anchor(sol, spec, com, min_th, ctx, lay.translate, area=area)
            weak = False
            if a is None:
                a, balance = _balanced_anchor(sol, spec, com, min(spec.thickness_mm, WEAK_SEAT_MM),
                                              ctx, lay.translate, area=area)
                weak = a is not None
            if a is not None or area != "any":
                # the customer's area: never another one; no seat there means no loop and a
                # warning (the caller first retries the lighter ring)
                s_ = min(a.thickness_mm, 2 * min_th) if a is not None else 0.0
                cands.append({"pos": pos, "anchor": a, "Q": Q, "weak": weak, "score": s_,
                              "balance": balance})
                continue
        a = ad.find_anchor_upright(sol, spec, com[:2], min_th)
        weak = False
        if a is None:
            a = ad.find_anchor_upright(sol, spec, com[:2], spec.thickness_mm)
            weak = a is not None
        synth = False
        if a is None and not auto:
            synth = True
            zt = np.where(np.isnan(sol.z_top), -np.inf, sol.z_top)
            i, j = np.unravel_index(np.argmax(zt), zt.shape)
            a = ad.Anchor(float(sol.xs()[i]), float(sol.ys()[j]), float(zt[i, j]),
                          float(zt[i, j]), 0.0, 0)
            warns.append(f"No sturdy spot for the {pos} loop; placed on the outermost point")
        # Thickness counts up to "clearly sturdy" (2 x the minimum); past that the top wins.
        score = min(a.thickness_mm, 2 * min_th) if a is not None else 0.0
        cands.append({"pos": pos, "anchor": a, "Q": Q, "weak": weak, "score": score,
                      "balance": balance, "synth": synth})
    return cands


def _place_upright_loop(ctx, lay, fig_m, spec, kind, addon, cands, best, auto, min_th):
    if auto and not ctx.loop_pos:
        ctx.loop_scores = {c["pos"]: (round(c["score"], 2) if c["anchor"] else None) for c in cands}
    ctx.loop_pos = best["pos"] if auto else None
    a, Q, pos = best["anchor"], best["Q"], best["pos"]
    if best["weak"]:
        lay.warnings.append(f"No spot is {min_th:g} mm thick; loop placed on a thinner one")
    mw, info = ad.hanging_loop(spec, (a.x, a.y), a.surface, a.lowest)
    m = mw.transform(_rot3x4(Q.T))
    role = "hanging_loop" if kind == AddOnKind.HANGING_LOOP else "keyring"
    lay.parts.append(_mesh_part(role, role, m, ctx.filament_for(addon)))
    embed_mean = None
    if fig_m is not None:
        embed_mean = (m ^ fig_m).volume() / (2 * spec.stem_half_width * spec.thickness_mm)
    hc = Q.T @ np.array(info["hole_center"], dtype=float)
    out_dir = Q.T @ np.array([0.0, 0.0, 1.0])
    pivot = hc + out_dir * spec.inner_d_mm / 2
    com = _lay_com(ctx, lay)
    tilt = _hang_tilt(pivot, com, np.array([0.0, 0.0, 1.0]))
    area = ctx.loop_area
    lay.metrics["loop"] = {
        "kind": kind.value, "position": pos, "auto": auto, "area": area,
        "asked": str(addon.params.get("position", "auto")),
        "score_by_position": ctx.loop_scores if auto else None,
        "anchor_xy": (a.x, a.y) if pos == "top" else (float(hc[0]), float(hc[1])),
        "hole_center": hc, "surface_z": a.surface, "embed_min_mm": info["embed_min_mm"],
        "embed_mean_mm": embed_mean, "local_thickness_mm": a.thickness_mm,
        "candidates": a.candidates, "inner_d_mm": spec.inner_d_mm,
        "section_mm": spec.section_mm, "thickness_mm": spec.thickness_mm,
    }
    lay.metrics["hang_tilt_deg"] = tilt
    bal = best.get("balance")
    lay.metrics["hang"] = _hang_record(tilt, pivot, com, np.array([0.0, 0.0, 1.0]), area,
                                       bal["where"] if bal else pos, bal)
    if bal is not None:
        lay.metrics["loop"]["balance"] = bal
    if info["embed_min_mm"] < ctx.gs.loop_min_overlap_mm:
        lay.warnings.append(f"Loop overlap {info['embed_min_mm']:.1f} mm under the 1.5 mm minimum")
    asked = area in ("head", "back", "side")
    if bal is not None and asked:
        place = "on the head" if bal["where"] == "head" else "on the back"
        how = f"as asked: the {area}, at its least tilted sturdy spot"
        if bal["where"] != area:
            how = f"the figure has no seat on the {area}; the head's least tilted spot"
    elif bal is not None:
        place = {"head": "on the head", "balance": "over the center of mass",
                 "least_tilt": "at the least tilted spot", "back": "on the back"}[bal["where"]]
        how = "anywhere allowed: where it hangs straightest"
    else:
        place = f"on the {pos}"
        how = "chosen by local thickness" if auto else "as asked"
    lay.notes.append(
        f"{role} {place} ({how}), {a.thickness_mm:.1f} mm thick there, {spec.inner_d_mm:g} mm "
        f"hole, {spec.section_mm:g} x {spec.thickness_mm:g} mm ring, hangs {tilt:.0f} degrees "
        "from upright"
    )
    if bal is not None and asked and bal["any_best_tilt_deg"] < tilt - 3.0:
        lay.notes.append(
            f"The {area} is off the balance point, so it hangs {tilt:.0f} degrees tilted; the "
            f"straightest spot on top hangs {bal['any_best_tilt_deg']:.0f} degrees, which needs "
            "the answer 'Wherever it hangs best'")
    elif bal is not None and not asked and not bal["on_head"] \
            and bal["head_best_tilt_deg"] is not None:
        lay.notes.append(
            f"A loop on the head would hang {bal['head_best_tilt_deg']:.0f} degrees tilted (the "
            "head is off the center of mass); the loop sits where it balances instead")
    if tilt > 10 and not asked:  # in the customer's area it is a fact of the product (notes,
        # metrics.hang and the hanging proof view), not a warning
        lay.warnings.append(f"Hangs tilted about {tilt:.0f} degrees")


def _hang_record(tilt: float, pivot, com, up, area: str, where: str,
                 bal: dict | None = None) -> dict:
    """How the piece hangs, for the geometry report and the hanging proof view: the pivot (top
    of the loop's hole) and the center of mass in the print frame (mm), the figure's up in that
    frame and the tilt between up and the line from the center of mass to the pivot."""
    rec = {"tilt_deg": round(float(tilt), 2), "pivot_mm": [round(float(x), 3) for x in pivot],
           "com_mm": [round(float(x), 3) for x in com],
           "up": [round(float(x), 4) for x in up], "area": area, "where": where}
    if bal is not None:
        rec["best_in_area_deg"] = round(float(bal["tilt_deg"]), 2)
        rec["best_anywhere_deg"] = round(float(bal["any_best_tilt_deg"]), 2)
    return rec


def _layout_upright(ctx: _Ctx, s: float) -> _Layout:
    gs = ctx.gs
    ctx.scale = s
    V = ctx.V * s
    solid = voxelize(V, ctx.F, _pitch(float((V.max(0) - V.min(0)).max())))
    lay = _Layout(fig_v=V, parts=[])
    base_kind = next((k for k in (AddOnKind.ROUND_BASE, AddOnKind.OVAL_BASE) if k in ctx.addons), None)
    offset = np.zeros(3)
    if base_kind is not None:
        addon = ctx.addons[base_kind]
        T = max(_num(addon, "base_t_mm", 4.0), gs.plate_min_thickness_mm)
        sink, feet = _feet_sink(solid, gs.plate_min_overlap_mm)
        # Size the base from the lower 30 % of the figure (feet, Meshy's own plinth), not from
        # a head or tail that overhangs; fall back to the full shadow if that would tip.
        z = V[ctx.F][:, :, 2].min(axis=1)
        low = z < V[:, 2].min() + 0.3 * float(np.ptp(V[:, 2]))
        footprint = cross_section_to_shapely(projected_silhouette(V, ctx.F[low]))
        full = cross_section_to_shapely(projected_silhouette(V, ctx.F))
        margin = _num(addon, "margin_mm", 3.0)
        com_xy = _com([(V, ctx.F)])[:2]
        from shapely.geometry import Point

        if not shapely.buffer(footprint, margin).buffer(-2.0).contains(Point(*com_xy)):
            footprint = full
            lay.notes.append("Base sized from the whole shadow so the figure does not tip")
        if base_kind == AddOnKind.ROUND_BASE:
            base, info = ad.round_base(footprint, margin, T, 1.0)
        else:
            base, info = ad.oval_base(footprint, margin, T, 1.0)
        pocket = ctx.addons.get(AddOnKind.MAGNET_POCKET)
        offset = np.array([0.0, 0.0, T - sink])
        lay.fig_v = V + offset
        if pocket is not None:
            base = _apply_pocket(ctx, lay, base, footprint, pocket, base_top=T)
        lay.parts.append(_mesh_part("base", "base", base, ctx.filament_for(addon)))
        r = info.radius_mm
        lay.metrics["base"] = {
            "kind": base_kind.value, "thickness_mm": T, "center_xy": info.center_xy,
            "diameter_mm": 2 * r if isinstance(r, float) else (2 * r[0], 2 * r[1]),
            "figure_sink_mm": sink, **feet,
        }
        lay.notes.append(f"Figure sunk {sink:.1f} mm into a {T:g} mm {base_kind.value}")
        _tip_check(ctx, lay, shapely.buffer(footprint, margin))
    lay.translate = offset
    fig_m = try_manifold(lay.fig_v, ctx.F)
    _upright_loop(ctx, lay, fig_m, solid, offset)  # solid is unshifted; offset maps it
    if base_kind is None and AddOnKind.MAGNET_POCKET in ctx.addons:
        lay.warnings.append("Magnet pocket needs a base, plate or flat back; skipped")
    if base_kind is None:
        contact = _contact_area(solid)
        lay.metrics["bed_contact_mm2"] = contact
        if contact < 20:
            lay.warnings.append(f"Only {contact:.0f} mm2 touches the bed; use a brim")
    return lay


def _contact_area(solid: Solid) -> float:
    zb = solid.z_bottom
    zmin = np.nanmin(zb)
    return float((np.isfinite(zb) & (zb <= zmin + 0.3)).sum() * solid.pitch**2)


def _tip_check(ctx: _Ctx, lay: _Layout, footprint) -> None:
    from shapely.geometry import Point

    com = _lay_com(ctx, lay)
    lay.metrics["center_of_mass"] = com
    inside = footprint.contains(Point(com[0], com[1]))
    margin = footprint.boundary.distance(Point(com[0], com[1]))
    lay.metrics["com_margin_mm"] = margin if inside else -margin
    if not inside:
        lay.warnings.append("Center of mass is outside the footprint; it will tip over")


def _layout_nameplate(ctx: _Ctx, unit: np.ndarray) -> tuple[float, _Layout]:
    """Plate of the target width; figure standing on one end; the name fills the rest."""
    gs = ctx.gs
    addon = ctx.addons[AddOnKind.NAMEPLATE]
    W = _num(addon, "plate_w_mm", ctx.target)
    frac = _num(addon, "figure_height_frac", 0.42)
    m = 5.0
    unit = np.maximum(np.asarray(unit, dtype=float), 1e-6)  # degenerate (flat) figures
    s = frac * W / unit[2]
    s = min(s, (0.6 * W - 2 * m) / unit[1], 0.4 * W / unit[0])
    ctx.scale = s
    V = ctx.V * s
    solid = voxelize(V, ctx.F, _pitch(float((V.max(0) - V.min(0)).max())))
    lay = _Layout(fig_v=V, parts=[])
    fp = cross_section_to_shapely(projected_silhouette(V, ctx.F))
    fx0, fy0, fx1, fy1 = fp.bounds
    shape = str(addon.params.get("shape", "rounded")).lower()
    if shape not in ad.PLATE_SHAPES:
        lay.warnings.append(f"Plate shape {shape!r} unknown; used rounded")
        shape = "rounded"
    D = _num(addon, "plate_d_mm", max(0.3 * W, (fy1 - fy0) + 2 * m))
    text_h = None
    if shape == "oval":
        # The figure's footprint must sit inside the ellipse at its half depth: at most 80 % of
        # the half axis, so the figure keeps 60 % of the half width at its edge.
        D = max(D, ((fy1 - fy0) / 2 + m) / 0.8 * 2)
    T = max(_num(addon, "plate_t_mm", 4.0), gs.plate_min_thickness_mm)
    sink, feet = _feet_sink(solid, gs.plate_min_overlap_mm)
    side = str(addon.params.get("position", "left"))
    plate_fil = find(ctx.library, ctx.filament_for(addon))
    gap = 4.0
    edge = W / 2 - m  # the plate's usable half width at the figure's footprint
    if shape == "oval":
        hw = ad.ellipse_half_width(W / 2, D / 2, max(abs(fy0), abs(fy1)) + m)
        edge = hw - m
        th = 0.35 * D  # the name may use this half height of the ellipse
        text_h = 2 * th
        text_edge = ad.ellipse_half_width(W / 2, D / 2, th) - m
    else:
        text_edge = W / 2 - m
    if side == "right":
        fig_dx = edge - fx1
        region = (-text_edge, fig_dx + fx0 - gap)
    else:
        fig_dx = -edge - fx0
        region = (fig_dx + fx1 + gap, text_edge)
    corner = _num(addon, "corner_mm", -1.0)
    res = nameplate(
        addon.text, W, D, plate_fil, ctx.library, gs, thickness_mm=T, x0=-W / 2,
        text_region=region, grow="left" if side == "right" else "right", margin_mm=m,
        corner_mm=corner if corner >= 0 else None, shape=shape, text_height_mm=text_h,
    )
    offset = np.array([fig_dx, -(fy0 + fy1) / 2, T - sink])
    lay.fig_v = V + offset
    lay.translate = offset
    lay.notes.append(f"Figure on the {side} end, sunk {sink:.1f} mm into a {T:g} mm plate")
    lay.metrics["plate"] = {**res.info, "figure_sink_mm": sink, **feet}
    lay.metrics.pop("text", None)
    if "text" in res.info:
        ti = res.info["text"]
        lay.metrics["text"] = res.info["plate_text"] = ti
        lay.metrics["plate"].pop("text", None)
        tf = find(ctx.library, res.text_filament_id)
        lay.notes.append(
            f"Name {ti['text']!r}: cap {ti['cap_height_mm']:.1f} mm, relief {ti['relief_mm']:g} mm, "
            f"{tf.name} on {plate_fil.name} (dE00 {ti['delta_e_vs_plate']:.0f})"
        )
    else:
        lay.warnings.append("Nameplate without text")
    slab = res.plate
    plate_shape = ad.plate_polygon(res.width_mm, D, shape, corner if corner >= 0 else None,
                                   x0=res.x0)
    lay.metrics["plate"]["shape"] = shape
    lay.notes.append(f"Plate {shape}, {res.width_mm:.0f} x {D:.0f} x {T:g} mm")
    pocket = ctx.addons.get(AddOnKind.MAGNET_POCKET)
    if pocket is not None:
        slab = _apply_pocket(ctx, lay, slab, plate_shape, pocket, base_top=T)
    lay.parts.insert(0, _mesh_part("plate", "plate", slab, plate_fil.id))
    if res.text is not None:
        ti = res.info["text"]
        lay.parts.append(_mesh_part(
            "text", "text", res.text, res.text_filament_id,
            note=f"{ti['text']!r} cap {ti['cap_height_mm']:.1f} mm, relief {ti['relief_mm']:g} mm",
        ))
    lay.warnings.extend(res.warnings)
    lay.notes.extend(res.notes)
    _tip_check(ctx, lay, plate_shape)
    fig_m = try_manifold(lay.fig_v, ctx.F)
    _upright_loop(ctx, lay, fig_m, solid, offset)
    return s, lay


# ---------------------------------------------------------------- pocket


def _apply_pocket(ctx: _Ctx, lay: _Layout, target: Manifold, outline, addon: AddOn,
                  base_top: float) -> Manifold:
    spec = ad.PocketSpec(
        magnet_d_mm=_num(addon, "pocket_d_mm", 10.0),
        magnet_h_mm=_num(addon, "pocket_depth_mm", 2.0),
    )
    if base_top < spec.depth + spec.roof_mm:
        lay.warnings.append(
            f"Part is {base_top:g} mm thick; a {spec.depth:.1f} mm pocket needs "
            f"{spec.depth + spec.roof_mm:.1f} mm"
        )
    c = _com([(lay.fig_v, ctx.F)])
    center = ad.magnet_pocket_center(outline, spec, (float(c[0]), float(c[1])))
    if center is None:
        lay.warnings.append("Outline too small for the magnet pocket; skipped")
        return target
    cut, cutter = ad.magnet_pocket(target, center, spec)
    # The figure must not reach into the pocket (we never cut the figure).
    v = lay.fig_v
    r = np.hypot(v[:, 0] - center[0], v[:, 1] - center[1])
    intrude = (r < spec.d / 2 + 0.2) & (v[:, 2] < spec.depth + 0.4)
    if intrude.any():
        lay.parts.append(_mesh_part(
            "magnet_cutter", "cutter", cutter, None,
            note="Figure reaches into the magnet pocket; subtract this cutter from the whole object",
        ))
        lay.warnings.append("Figure reaches into the magnet pocket; cutter part added")
    lay.metrics["magnet_pocket"] = {
        "center_xy": center, "d_mm": spec.d, "depth_mm": spec.depth,
        "magnet": f"{spec.magnet_d_mm:g} x {spec.magnet_h_mm:g} mm", "figure_intrudes": bool(intrude.any()),
    }
    lay.notes.append(
        f"Magnet pocket {spec.d:.1f} x {spec.depth:.1f} mm open on the back at "
        f"({center[0]:.1f}, {center[1]:.1f}); glue the magnet in after printing"
    )
    return cut


# ---------------------------------------------------------------- flat


def _layout_flat(ctx: _Ctx, s: float) -> _Layout:
    gs = ctx.gs
    back_addon = ctx.addons.get(AddOnKind.FLAT_BACK) or AddOn(kind=AddOnKind.FLAT_BACK)
    pocket = ctx.addons.get(AddOnKind.MAGNET_POCKET)
    T = max(_num(back_addon, "back_t_mm", 3.0), gs.plate_min_thickness_mm)
    if pocket is not None:
        ps = ad.PocketSpec(magnet_d_mm=_num(pocket, "pocket_d_mm", 10.0),
                           magnet_h_mm=_num(pocket, "pocket_depth_mm", 2.0))
        T = max(T, ps.depth + ps.roof_mm + gs.plate_min_overlap_mm)
    rim = _num(back_addon, "rim_mm", 1.0)
    base_z = T - gs.plate_min_overlap_mm
    relief = ctx.relief
    depth_unit = float(ctx.V[:, 2].max())
    if not ctx.relief_fixed:
        fit = (ctx.max_thickness - base_z) / max(depth_unit * s, 1e-9)
        relief = float(np.clip(min(relief, fit), MIN_RELIEF, 1.0))
    V = ctx.V * np.array([s, s, s * relief])
    offset = np.array([0.0, 0.0, base_z])
    lay = _Layout(fig_v=V + offset, parts=[], translate=offset, relief=relief)
    thick = base_z + depth_unit * s * relief
    lay.metrics["thickness_mm"] = thick
    if thick > ctx.max_thickness + 0.05:  # a design preference, not a printability rule
        lay.notes.append(f"{thick:.1f} mm thick, over the {ctx.max_thickness:.1f} mm aim; depth "
                         f"already squeezed to x{relief:.2f}")
    fig_m = try_manifold(lay.fig_v, ctx.F)
    if fig_m is None:
        lay.warnings.append("Figure is not manifold; flat back is a plain slab and may need supports")
        outline_cs = projected_silhouette(lay.fig_v, ctx.F)
        from manifold3d import JoinType

        outline_cs = outline_cs.offset(rim, JoinType.Round, circular_segments=ad.SEG)
        back = Manifold.extrude(outline_cs, T)
    else:
        back, outline_cs = ad.flat_back(fig_m, T, rim, gs.layer_mm)
    outline = cross_section_to_shapely(outline_cs)
    lay.notes.append(
        f"Flat back {T:g} mm plus a filler under the figure, rim {rim:g} mm; print with "
        "supports off"
    )
    lay.metrics["back"] = {"t_mm": T, "rim_mm": rim, "figure_sink_mm": gs.plate_min_overlap_mm}

    if pocket is not None:
        back = _apply_pocket(ctx, lay, back, outline, pocket, base_top=T)
    lay.parts.append(_mesh_part("back", "back", back, ctx.filament_for(back_addon)))

    kind = (AddOnKind.KEYRING_LOOP if AddOnKind.KEYRING_LOOP in ctx.addons
            else AddOnKind.HANGING_LOOP if AddOnKind.HANGING_LOOP in ctx.addons else None)
    if kind is not None:
        _flat_loop(ctx, lay, kind, outline, fig_m, T)
    return lay


_FLAT_THETA = {(0, 1): 0.0, (-1, 0): -90.0, (1, 0): 90.0, (0, -1): 180.0}


def _rz(theta_deg: float) -> np.ndarray:
    t = math.radians(theta_deg)
    return np.array([[math.cos(t), -math.sin(t), 0], [math.sin(t), math.cos(t), 0], [0, 0, 1.0]])


def _flat_candidate(ctx: _Ctx, lay: _Layout, outline, spec, pos: str, T: float) -> dict | None:
    """Anchor search for one side, done in a frame rotated so that side points +Y."""
    import shapely.affinity

    d = _R_FLAT[ctx.display] @ np.array(_FIG_DIR[pos])
    if abs(d[2]) > 0.5:
        return None
    theta = _FLAT_THETA[(int(round(d[0])), int(round(d[1])))]
    rz = _rz(theta)
    outline_r = shapely.affinity.rotate(outline, theta, origin=(0, 0))
    fig_r = lay.fig_v @ rz.T
    p = 0.25
    x0, y0, x1, y1 = outline_r.bounds
    xs = np.arange(x0 - 2 * p, x1 + 2 * p, p)
    ys = np.arange(y0 - 2 * p, y1 + 2 * p, p)
    X, Y = np.meshgrid(xs, ys, indexing="ij")
    mask = shapely.contains_xy(outline_r, X, Y)
    solid = voxelize(fig_r, ctx.F, 0.5)
    height = np.full(mask.shape, np.nan)
    height[mask] = T
    zi = np.clip(np.round((X - solid.origin[0]) / solid.pitch).astype(int), 0, solid.shape[0] - 1)
    zj = np.clip(np.round((Y - solid.origin[1]) / solid.pitch).astype(int), 0, solid.shape[1] - 1)
    zt = solid.z_top[zi, zj]
    height = np.where(mask & np.isfinite(zt), np.fmax(height, zt), height)
    com = rz @ _lay_com(ctx, lay)
    min_chord = max(5.0, 1.2 * spec.stem_half_width)
    a = ad.find_anchor_flat(mask, xs, ys, height, spec, float(com[0]), min_chord)
    weak = False
    if a is None:
        a = ad.find_anchor_flat(mask, xs, ys, height, spec, float(com[0]), 0.0)
        weak = a is not None
    # Width counts up to 1.5 stem widths; past that the top wins (see TOP_PREFERENCE).
    score = min(a.width_mm, 3 * spec.stem_half_width) if a is not None else 0.0
    return {"pos": pos, "anchor": a, "theta": theta, "weak": weak, "xs": xs, "ys": ys,
            "height": height, "com": com, "score": score}


def _flat_loop(ctx: _Ctx, lay: _Layout, kind: AddOnKind, outline, fig_m, T: float) -> None:
    gs = ctx.gs
    addon = ctx.addons[kind]
    spec = _loop_spec(addon, kind, gs, lay.warnings)
    allowed = [p for p in LOOP_POSITIONS
               if abs((_R_FLAT[ctx.display] @ np.array(_FIG_DIR[p]))[2]) < 0.5]
    positions, auto = _positions(addon, allowed, lay.warnings)
    if auto and ctx.loop_pos in allowed:
        positions = [ctx.loop_pos]
    cands = [c for c in (_flat_candidate(ctx, lay, outline, spec, p, T) for p in positions) if c]
    best = _pick(cands, auto)
    if best is None and auto and len(positions) < len(allowed):  # cached side failed here
        ctx.loop_pos = None
        cands = [c for c in (_flat_candidate(ctx, lay, outline, spec, p, T) for p in allowed) if c]
        best = _pick(cands, auto)
    if best is None:
        lay.warnings.append("No spot on the outline for the keyring; none added")
        return
    if auto and not ctx.loop_pos:
        ctx.loop_scores = {c["pos"]: (round(c["score"], 2) if c["anchor"] else None) for c in cands}
    ctx.loop_pos = best["pos"] if auto else None
    a, theta, pos = best["anchor"], best["theta"], best["pos"]
    xs, ys, height, com = best["xs"], best["ys"], best["height"], best["com"]
    if best["weak"]:
        lay.warnings.append(f"No wide spot on the {pos} for the keyring; placed on a narrow one")
    mr, info = ad.keyring_loop(spec, (a.x, 0.0), a.surface, a.lowest)
    m = mr.rotate((0.0, 0.0, -theta))
    role = "keyring" if kind == AddOnKind.KEYRING_LOOP else "hanging_loop"
    lay.parts.append(_mesh_part(role, role, m, ctx.filament_for(addon)))
    # Load path: from the stem into the body toward the center of mass; thinnest column on it.
    ii = int(np.argmin(np.abs(xs - a.x)))
    jb = int(np.argmin(np.abs(ys - info["stem_bottom"])))
    jc = int(np.argmin(np.abs(ys - com[1])))
    path = height[ii, min(jc, jb) : max(jc, jb) + 1]
    path = path[np.isfinite(path)]
    thin = float(min(path.min() if len(path) else T, spec.thickness_mm))
    back_part = next(q for q in lay.parts if q.role == "back")
    back_m = try_manifold(back_part.vertices, back_part.faces)
    overlap_vol = (m ^ back_m).volume() if back_m is not None else None
    rinv = _rz(-theta)
    hc = rinv @ np.array([info["hole_center"][0], info["hole_center"][1], 0.0])
    fig_up = _R_FLAT[ctx.display] @ np.array([0.0, 0.0, 1.0])
    pivot = hc + (rinv @ np.array([0.0, 1.0, 0.0])) * spec.inner_d_mm / 2
    com_w = _lay_com(ctx, lay)
    tilt = _hang_tilt(pivot[:2], com_w[:2], fig_up[:2])
    # a flat charm hangs in its own plane: the pivot at the center of mass's depth
    lay.metrics["hang"] = _hang_record(tilt, [pivot[0], pivot[1], com_w[2]], com_w, fig_up,
                                       loop_area(addon), pos)
    lay.metrics["loop"] = {
        "kind": kind.value, "position": pos, "auto": auto,
        "score_by_position": ctx.loop_scores if auto else None,
        "anchor_x": a.x, "surface_y": a.surface, "hole_center": hc[:2],
        "embed_min_mm": info["embed_min_mm"],
        "embed_mean_mm": (overlap_vol / (2 * spec.stem_half_width * spec.thickness_mm))
        if overlap_vol is not None else None,
        "outline_width_mm": a.width_mm, "stem_region_min_height_mm": a.thickness_mm,
        "candidates": a.candidates, "load_path_min_thickness_mm": thin,
        "inner_d_mm": spec.inner_d_mm, "section_mm": spec.section_mm,
        "thickness_mm": spec.thickness_mm,
    }
    lay.metrics["hang_tilt_deg"] = tilt
    if ctx.product == ProductType.KEYCHAIN and thin < gs.keychain_min_thickness_mm:
        lay.warnings.append(
            f"Keychain is {thin:.1f} mm thick on the load path, under {gs.keychain_min_thickness_mm:g} mm"
        )
    if info["embed_min_mm"] < gs.loop_min_overlap_mm:
        lay.warnings.append(f"Loop overlap {info['embed_min_mm']:.1f} mm under the 1.5 mm minimum")
    how = "chosen by local width" if auto else "as asked"
    lay.notes.append(
        f"{role} flat on the bed at the {pos} ({how}, outline {a.width_mm:.1f} mm wide there), "
        f"hole axis Z, {spec.inner_d_mm:g} mm hole, {spec.thickness_mm:g} mm thick"
    )
    if tilt > 10:
        (lay.warnings if auto else lay.notes).append(f"Hangs tilted about {tilt:.0f} degrees")
