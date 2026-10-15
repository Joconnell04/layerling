"""simulate_print: what the slicer and printer will actually make of a per-face paint.

The paint agent judges this result, not the ideal paint. Four rules (PLAN.md, "Print simulator
and slicer gate"), applied in the order the slicer works in. docs/research/sim.md has the
reasoning, sources and limits.

A. Layers (rule 2). Bambu Studio colors each layer in two ways (MultiMaterialSegmentation.cpp):
   walls take the color of the slice contour at the layer's mid plane, while painted up- and
   down-facing surfaces are projected straight onto the layer's exposed top or bottom area and
   override the contour colors there (top/bottom_color_penetration_layers). So faces steeper
   than flat_angle_deg take the contour color of their layer (boundaries on walls snap to layer
   mid planes) and shallower faces keep their paint (tops keep their XY shape).
   QUANTIZE_MODEL = "plan" instead applies PLAN.md's rule as written (shallow up-facing faces
   within one line width of the slice contour take its color).
B. Erosion (rule 1). A surface opening with a disk of diameter min_width_factor x nozzle_mm
   removes every part of a same-filament patch narrower than that. The metric is anisotropic:
   z distances are scaled by (min_width_factor x nozzle_mm) / (MIN_BAND_LAYERS x layer_mm), so a
   horizontal band on a wall needs only MIN_BAND_LAYERS layers of height (layers resolve z; the
   nozzle limits xy). Specks merge first, then whole thin patches, then one pass over thin parts
   of larger patches; each piece takes the filament it shares the longest boundary with.
C. Bleed (rule 3). Light filament over darker material shows the dark through. The slicer fills
   each layer's interior by nearest surface color, so the light material behind a surface point
   p (normal n) ends where the interior becomes closer to a darker surface point q:
       depth = |p - q|^2 / (2 n.(p - q))     for q behind p (n.(p - q) > 0)
   minimised over darker faces of the same part within 2 x min_light_layers x layer_mm whose own
   normal also faces away from p (solid between them, not an air gap). For multi-part prints an
   up-facing light face over another part's darker solid uses the vertical distance down to it.
   light_layers = floor(depth / layer_mm). "Light over dark" means the covering filament's
   CIELAB L* is at least BLEED_DELTA_L above the one behind it. Faces with fewer than
   min_light_layers are flagged; flags are grouped by (light region, dark region) and kept when
   they cover at least BLEED_MIN_AREA_MM2.
D. Swaps and purge (rule 4). Slice at layer_mm; a layer contains a filament if a visible face
   of it crosses the layer's mid plane, or is a painted top (bottom) within TOP_PAINT_LAYERS
   (BOTTOM_PAINT_LAYERS) above (below), or (multi-part) a part of that filament has an outline in
   the layer. Filaments are ordered per layer greedily by flush volume, starting with the one
   loaded at the end of the previous layer.
   Two nozzles (params.nozzles 2, the X2D; the default): filaments are split between the nozzles
   the way Bambu Studio's "Auto For Flush" does (every split tried, the least flush wins; on the
   three calibration prints it chose the same split as the slicer). A change to a filament
   already loaded on the other nozzle flushes nothing; a change on the same nozzle flushes
   Bambu's flush volume (flush.py) x FLUSH_CALIBRATION. The prime tower is
   TOWER_G_PER_LAYER per layer up to the last change plus TOWER_G_PER_CHANGE per change.
   One nozzle (params.nozzles 1): the old model, flush plus the 45 mm3 prime per swap, no tower.

Model grams estimate the printed part, not the solid: two walls (0.87 mm) on sides, 1.0 mm top
and 0.6 mm bottom shells, 15% sparse infill (X2D 0.20mm Standard defaults), capped at solid mass,
1.24 g/cm3. purge_pct is 100 x purge_g / model_g.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from p2p_contracts.manifest import FileRef
from p2p_contracts.reports import BleedFlag, MergedRegion, SimParams, SimReport
from scipy.spatial import cKDTree

from .color import lab_to_srgb

# Purge calibration (two nozzles). Fitted to three Bambu Studio 2.08 X2D slices of our own
# files, re-sliced with the G-code kept (SOURCE: tool changes, nozzle changes and prime tower
# grams from p2p_threemf.gcode_stats; sim/README.md "Purge calibration"):
#   Bronco nameplate s09 a2  slicer 96.65 g purge (tower 21.00)  model 96.5 g  (-0.2 %)
#   3D Frenchie keychain      slicer 14.82 g (tower 10.97)        model 15.8 g  (+6 %)
#   flat Frenchie s09 a3      slicer  3.64 g (tower 2.35)         model  6.1 g  (+68 %: the sim
#                             counts 42 tool changes where the slicer made 20)
# The old one-nozzle model said 149, 53.6 and 15.1 g.
TOWER_G_PER_LAYER = 0.0386  # prime tower grams per layer, up to the last layer with a change
TOWER_G_PER_CHANGE = 0.0236  # prime tower grams per tool change
FLUSH_CALIBRATION = 0.96  # slicer flush per same-nozzle swap / Bambu's flush matrix (least
# squares; Bambu's own volumes are about right once a swap to the other nozzle costs nothing)
from .flush import PRIME_VOLUME_MM3, flush_matrix
from .geodesic import components, propagate_nearest, thin_faces
from .mesh import Mesh

DENSITY_G_MM3 = 1.24e-3
MIN_BAND_LAYERS = 2
BLEED_DELTA_L = 30.0
BLEED_MIN_AREA_MM2 = 0.5
BLEED_VOXEL_MM = 0.1
BLEED_NEIGHBORS = 64
LAYER_EPS = 1e-6  # 1.0 mm / 0.2 mm must count as 5 layers, not 4.999
MERGE_REPORT_MIN_MM2 = 0.5
ERODE_TOL_CAP = 0.08  # face-sampling slack, at most 8% of the width limit
TOP_PAINT_LAYERS = 5  # X2D 0.20mm Standard top_color_penetration_layers
BOTTOM_PAINT_LAYERS = 3  # bottom_color_penetration_layers
QUANTIZE_MODEL = "bambu"  # or "plan"; see quantize_layers
WALL_MM = 0.87  # outer 0.42 + inner 0.45 line widths
WALL_LINE_MM = 0.42  # outer wall line width
THIN_GEOMETRY_INSET_MM = 0.05
THIN_GEOMETRY_VOXEL_MM = 0.15
TOP_SHELL_MM = 1.0
BOTTOM_SHELL_MM = 0.6
INFILL = 0.15


@dataclass(eq=False)
class SimResult:
    params: SimParams
    palette_labs: np.ndarray
    palette_rgb: np.ndarray
    face_slots_in: np.ndarray
    face_slots: np.ndarray
    face_regions_in: np.ndarray
    face_regions: np.ndarray
    face_changed: np.ndarray
    """uint8 bit flags: 1 quantized, 2 eroded."""
    face_light_layers: np.ndarray
    """float, layers of light filament behind light faces that have darker material behind."""
    face_bleed_dark_slot: np.ndarray
    layer_slots: np.ndarray
    """(n_layers, K) bool: filament present in layer."""
    layer_swaps: np.ndarray
    report: SimReport
    purge_mm3: float = 0.0
    flush: np.ndarray = field(default_factory=lambda: np.zeros((0, 0)))
    face_hidden: np.ndarray | None = None
    """bool: face lies inside another part's solid (multi-part prints)."""
    nozzle_of: np.ndarray | None = None
    """(K,) nozzle per palette slot (two-nozzle model)."""
    tower_g: float = 0.0
    flush_g: float = 0.0
    nozzle_changes: int = 0
    same_nozzle_swaps: int = 0

    @property
    def face_rgb(self) -> np.ndarray:
        return self.palette_rgb[self.face_slots - 1]

    @property
    def face_bleed_mix(self) -> np.ndarray:
        """0..0.5 blend toward the darker filament for flagged faces (render tint)."""
        n = float(self.params.min_light_layers)
        ll = self.face_light_layers
        m = np.where(np.isfinite(ll), 0.5 * np.clip(1.0 - np.floor(ll + LAYER_EPS) / n, 0, 1), 0.0)
        return m

    @property
    def face_bleed_rgb(self) -> np.ndarray:
        s = np.where(self.face_bleed_dark_slot > 0, self.face_bleed_dark_slot, self.face_slots)
        return self.palette_rgb[s - 1]


# ---------------------------------------------------------------------- helpers


def _patches(mesh: Mesh, slots: np.ndarray) -> np.ndarray:
    """Connected same-slot patches, numbered from 1 (pseudo regions when none are given)."""
    _, comp = components(mesh, np.ones(mesh.n_faces, dtype=bool), slots)
    return comp + 1


def _edge_weights_by_key(keys_a, keys_b, w, na, nb):
    """Sum w over (keys_a, keys_b) pairs -> dict via a flat bincount."""
    flat = keys_a.astype(np.int64) * nb + keys_b.astype(np.int64)
    uniq, inv = np.unique(flat, return_inverse=True)
    tot = np.bincount(inv, weights=w)
    return uniq // nb, uniq % nb, tot


def _argmax_per_group(group, value):
    """For each group id, index of the max value. Returns (groups, idx)."""
    order = np.lexsort((-value, group))
    g = group[order]
    first = np.ones(len(g), dtype=bool)
    first[1:] = g[1:] != g[:-1]
    return g[first], order[first]


# ---------------------------------------------------------------------- rule A: quantize


def quantize_layers(mesh: Mesh, slots: np.ndarray, params: SimParams,
                    model: str | None = None) -> np.ndarray:
    """Rule 2: snap color boundaries to layer steps. Returns new slots.

    model "bambu" (default, what Bambu Studio's MultiMaterialSegmentation.cpp does): faces
    steeper than flat_angle_deg take the color of their layer's slice contour, so boundaries on
    walls snap to layer mid planes; shallower up- and down-facing faces keep their color because
    painted tops and bottoms are projected straight onto each layer's exposed area.
    model "plan" (PLAN.md as written): shallow up-facing faces within one line width of the
    slice contour take the contour color; boundaries along gentle slopes snap to layer steps.
    """
    model = model or QUANTIZE_MODEL
    if model == "bambu":
        return _quantize_walls(mesh, slots, params)
    if model != "plan":
        raise ValueError(f"unknown quantize model {model}")
    L = params.layer_mm
    n = mesh.face_normals
    shallow = n[:, 2] >= np.cos(np.radians(params.flat_angle_deg))
    if not shallow.any():
        return slots.copy()
    tri_z = mesh.triangles[:, :, 2]
    zmin, zmax = tri_z.min(axis=1), tri_z.max(axis=1)
    zc = mesh.face_centroids[:, 2]
    key = np.floor(zc / L - 0.5).astype(np.int64)
    k_hi = np.floor(zmax / L - 0.5).astype(np.int64)
    seed = shallow & ((k_hi + 0.5) * L >= zmin)
    key[seed] = k_hi[seed]
    f = mesh.n_faces
    lab = np.where(shallow, key, -(np.arange(f, dtype=np.int64) + 1) - (1 << 40))
    seeds = np.nonzero(seed)[0]
    if seeds.size == 0:
        return slots.copy()
    sp = mesh.face_centroids[seeds].copy()
    sp[:, 2] = (key[seeds] + 0.5) * L  # point on the slice plane
    # horizontal distance to the slice contour: propagate within each terrace, z ignored
    d, src, _ = propagate_nearest(mesh, lab, seeds, sp, metric_z=0.0, limit=4 * params.nozzle_mm)
    out = slots.copy()
    take = shallow & ~seed & (src >= 0) & (d < params.nozzle_mm)
    out[take] = slots[src[take]]
    return out


def _quantize_walls(mesh: Mesh, slots: np.ndarray, params: SimParams) -> np.ndarray:
    """Each wall face (steeper than flat_angle_deg) takes the color the slice contour of its
    layer has at the same place: the face crossing that layer's mid plane, found by horizontal
    distance within the layer band."""
    L = params.layer_mm
    nz = mesh.face_normals[:, 2]
    steep = np.abs(nz) < np.cos(np.radians(params.flat_angle_deg))
    if not steep.any():
        return slots.copy()
    tri_z = mesh.triangles[:, :, 2]
    zmin, zmax = tri_z.min(axis=1), tri_z.max(axis=1)
    band = np.floor(mesh.face_centroids[:, 2] / L).astype(np.int64)
    plane = (band + 0.5) * L
    seed = steep & (zmin <= plane) & (plane <= zmax)
    seeds = np.nonzero(seed)[0]
    if seeds.size == 0:
        return slots.copy()
    f = mesh.n_faces
    lab = np.where(steep, band, -(np.arange(f, dtype=np.int64) + 1) - (1 << 40))
    sp = mesh.face_centroids[seeds].copy()
    sp[:, 2] = plane[seeds]
    # the contour point directly above or below: allow half a triangle of sideways slack so a
    # face never takes a color from across a vertical boundary
    h = float(np.median(mesh.face_edge_len[steep]))
    lim = float(np.clip(0.5 * h, 0.02, params.nozzle_mm))
    _, src, _ = propagate_nearest(mesh, lab, seeds, sp, metric_z=0.0, limit=lim)
    out = slots.copy()
    take = steep & ~seed & (src >= 0)
    out[take] = slots[src[take]]
    return out


def _columns(mesh: Mesh, queries: np.ndarray, exterior: np.ndarray):
    """For each query point: nearest visible surface crossing straight above and below.

    Returns (face_above, dz_above, face_below, dz_below); -1 / inf where there is none."""
    from .parts import column_hits

    n = len(queries)
    fa, da = np.full(n, -1, np.int64), np.full(n, np.inf)
    fb, db = np.full(n, -1, np.int64), np.full(n, np.inf)
    tri_idx = np.nonzero(exterior)[0]
    if n == 0 or tri_idx.size == 0:
        return fa, da, fb, db
    qi, ti, z = column_hits(mesh.triangles[tri_idx], queries[:, :2])
    dz = z - queries[qi, 2]
    for sel, f_out, d_out, sign in ((dz > 1e-6, fa, da, 1.0), (dz < -1e-6, fb, db, -1.0)):
        q, t, d = qi[sel], ti[sel], np.abs(dz[sel])
        order = np.lexsort((d, q))
        q, t, d = q[order], t[order], d[order]
        first = np.ones(len(q), dtype=bool)
        first[1:] = q[1:] != q[:-1]
        f_out[q[first]] = tri_idx[t[first]]
        d_out[q[first]] = d[first]
    return fa, da, fb, db


def paint_penetration(mesh: Mesh, slots: np.ndarray, params: SimParams,
                      exterior: np.ndarray | None = None) -> np.ndarray:
    """Top and bottom paint penetration (Bambu top/bottom_color_penetration_layers).

    A painted top is projected TOP_PAINT_LAYERS layers down through the solid under it and wins
    over wall colors there. So a wall face whose outer wall line (half a line width inside the
    surface) lies under a top surface within TOP_PAINT_LAYERS x layer_mm takes that top's
    color; likewise BOTTOM_PAINT_LAYERS above a bottom surface. The nearer surface wins.
    Visible on raised features: a 1 mm letter with a painted top prints its upper sides in the
    top color too. On sloped walls the effect is about one line width and changes little."""
    L = params.layer_mm
    ext = np.ones(mesh.n_faces, dtype=bool) if exterior is None else exterior
    n = mesh.face_normals
    steep = ext & (np.abs(n[:, 2]) < np.cos(np.radians(params.flat_angle_deg)))
    walls = np.nonzero(steep)[0]
    if walls.size == 0:
        return slots.copy()
    nh = n[walls].copy()
    nh[:, 2] = 0
    nh /= np.maximum(np.linalg.norm(nh, axis=1, keepdims=True), 1e-12)
    q = mesh.face_centroids[walls] - nh * (0.5 * WALL_LINE_MM)
    fa, da, fb, db = _columns(mesh, q, ext)
    nz = n[:, 2]
    top_ok = (fa >= 0) & (da <= TOP_PAINT_LAYERS * L + 1e-6)
    top_ok &= nz[np.maximum(fa, 0)] > 0
    bot_ok = (fb >= 0) & (db <= BOTTOM_PAINT_LAYERS * L + 1e-6)
    bot_ok &= nz[np.maximum(fb, 0)] < 0
    use_top = top_ok & (~bot_ok | (da <= db))
    use_bot = bot_ok & ~use_top
    out = slots.copy()
    out[walls[use_top]] = slots[fa[use_top]]
    out[walls[use_bot]] = slots[fb[use_bot]]
    return out


# ---------------------------------------------------------------------- rule B: erode


def thin_geometry(mesh: Mesh, params: SimParams, exterior: np.ndarray | None = None):
    """Solid thickness across each face, measured horizontally just inside the surface.

    Returns (faces, width, end_a, end_b). A face is on thin geometry when the solid there is
    narrower than min_width_factor x nozzle; if both ends of that narrow section carry the
    face's own filament, the whole cross-section in those layers is one thin color region
    (a 0.5 mm letter whose sides took its top's color, a thin fin painted all over)."""
    from .geodesic import distance_from
    from .parts import horizontal_thickness

    w = params.min_width_factor * params.nozzle_mm
    ext = np.ones(mesh.n_faces, dtype=bool) if exterior is None else exterior
    empty = (np.zeros(0, np.int64), np.zeros(0), np.zeros(0, np.int64), np.zeros(0, np.int64))
    idx = np.nonzero(ext)[0]
    if idx.size == 0:
        return empty
    c, n = mesh.face_centroids, mesh.face_normals
    # 1. cheap filter: faces with an opposite-facing face less than w behind them
    pick = idx[_voxel_pick(c[idx], BLEED_VOXEL_MM)]
    tree = cKDTree(c[pick])
    k = min(16, len(pick))
    walls = np.zeros(mesh.n_faces, dtype=bool)
    for s0 in range(0, idx.size, 50000):
        ch = idx[s0 : s0 + 50000]
        # search around a point w/2 inside the surface: the far wall of a thin section is
        # nearer to it than the face's own wall is
        mid = c[ch] - n[ch] * (w / 2)
        _, j = tree.query(mid, k=k, distance_upper_bound=w / 2 + 0.05)
        j = j.reshape(len(ch), k)
        ok = j < len(pick)
        qf = pick[np.where(ok, j, 0)]
        pq = c[ch][:, None, :] - c[qf]
        depth = np.einsum("nj,nkj->nk", n[ch], pq)
        opp = np.einsum("nj,nkj->nk", n[ch], n[qf]) < -0.5
        walls[ch] = (ok & opp & (depth > 1e-6) & (depth < w)).any(axis=1)
    if not walls.any():
        return empty
    # 2. widen over the surface (tops and ends of thin features), then measure exactly
    near = distance_from(mesh, np.zeros(mesh.n_faces, dtype=np.int64), walls, limit=w)
    faces = np.nonzero(ext & (near <= w))[0]
    local = np.zeros(mesh.n_faces, dtype=bool)
    lo = c[faces].min(axis=0) - 2 * w
    hi = c[faces].max(axis=0) + 2 * w
    reach = cKDTree(c[faces]).query(c[idx], k=1, distance_upper_bound=2 * w)[0]
    local[idx[np.isfinite(reach)]] = True
    local &= np.all((c >= lo) & (c <= hi), axis=1)
    pts = c[faces] - n[faces] * THIN_GEOMETRY_INSET_MM
    # measure at one face per THIN_GEOMETRY_VOXEL_MM cell, copy to the faces around it
    rep = _voxel_pick(pts, THIN_GEOMETRY_VOXEL_MM)
    rw, rea, reb = horizontal_thickness(mesh, faces[rep], pts[rep], local)
    _, j = cKDTree(pts[rep]).query(pts, k=1)
    width, ea, eb = rw[j], rea[j], reb[j]
    keep = (width < w) & (ea >= 0) & (eb >= 0)
    return faces[keep], width[keep], ea[keep], eb[keep]


def erode(mesh: Mesh, slots: np.ndarray, params: SimParams, max_pass: int = 6,
          geometry=None):
    """Rule 1. Returns (new slots, receiver face per changed face (-1 if unchanged)).

    Phases, so a face-level opening cannot cascade:
    1. patches smaller than the disk itself (specks, highlights) merge, repeated until none;
    2. whole patches with no printable core (hairlines, small dots) merge, repeated;
    3. one pass removes the thin parts of larger patches (tails, necks, slivers);
    4. phase 1 again for specks that phase 3 split off.
    Each merged piece takes the filament it shares the longest boundary with."""
    w = params.min_width_factor * params.nozzle_mm
    metric_z = w / (MIN_BAND_LAYERS * params.layer_mm)
    out = slots.copy()
    receiver = np.full(mesh.n_faces, -1, dtype=np.int64)
    everything = np.ones(mesh.n_faces, dtype=bool)
    disk_area = np.pi * (w / 2) ** 2

    def classify():
        thin = thin_faces(mesh, out, w, metric_z=metric_z, tol_cap=ERODE_TOL_CAP)
        if geometry is not None and len(geometry[0]):
            gf, _, ga, gb = geometry
            one_color = (out[ga] == out[gf]) & (out[gb] == out[gf])
            thin[gf[one_color]] = True
        if not thin.any():
            return thin, thin, thin
        n_patch, patch = components(mesh, everything, out)
        cnt = np.bincount(patch, minlength=n_patch)
        whole = thin & (np.bincount(patch, weights=thin, minlength=n_patch) == cnt)[patch]
        area = np.bincount(patch, weights=mesh.face_areas, minlength=n_patch)
        tiny = whole & (area[patch] < disk_area)
        return thin, whole, tiny

    for phase in ("tiny", "whole", "part", "tiny"):
        for _ in range(max_pass if phase != "part" else 1):
            thin, whole, tiny = classify()
            pick = {"tiny": tiny, "whole": whole, "part": thin}[phase]
            if not pick.any() or not _merge_pieces(mesh, out, receiver, pick):
                break
    return out, receiver


def _merge_pieces(mesh: Mesh, out: np.ndarray, receiver: np.ndarray, thin: np.ndarray) -> bool:
    """Reassign each connected thin piece to the neighboring filament it shares the longest
    boundary with (neighbors that are not thin preferred). Returns True if anything changed."""
    adj = mesh.face_adjacency
    elen = mesh.adjacency_edge_len
    k, comp = components(mesh, thin, out)
    fa = np.concatenate([adj[:, 0], adj[:, 1]])
    fb = np.concatenate([adj[:, 1], adj[:, 0]])
    el = np.concatenate([elen, elen])
    m = thin[fa] & (out[fa] != out[fb])
    fa, fb, el = fa[m], fb[m], el[m]
    stable = ~thin[fb]
    assign_slot = np.zeros(k, dtype=np.int64)
    assign_face = np.full(k, -1, dtype=np.int64)
    for prefer in (stable, ~stable):
        sel = prefer & (assign_slot[comp[fa]] == 0)
        if not sel.any():
            continue
        ca, sb, tot = _edge_weights_by_key(comp[fa[sel]], out[fb[sel]], el[sel], k,
                                           int(out.max()) + 1)
        g, idx = _argmax_per_group(ca, tot)
        assign_slot[g] = sb[idx]
        rep_mask = sel & (out[fb] == assign_slot[comp[fa]])
        gg, ii = _argmax_per_group(comp[fa[rep_mask]], el[rep_mask])
        assign_face[gg] = fb[rep_mask][ii]
    tf = np.nonzero(thin)[0]
    new = assign_slot[comp[tf]]
    ok = new > 0
    tf, new = tf[ok], new[ok]
    if tf.size == 0:
        return False
    out[tf] = new
    receiver[tf] = assign_face[comp[tf]]
    return True


# ---------------------------------------------------------------------- rule C: bleed


def bleed_depths(mesh: Mesh, slots: np.ndarray, palette_labs: np.ndarray, params: SimParams,
                 exterior: np.ndarray | None = None, face_parts: np.ndarray | None = None,
                 part_slots: np.ndarray | None = None):
    """Per face: layers of light filament behind it (inf when nothing darker is behind) and the
    darker face that limits it (-1 if none).

    Surface paint: nearest-surface depth against darker faces of the same part. Parts: for
    up-facing light faces, the vertical distance down to a darker part's solid (parts.py)."""
    L = params.layer_mm
    f = mesh.n_faces
    layers = np.full(f, np.inf)
    dark_face = np.full(f, -1, dtype=np.int64)
    lstar = np.asarray(palette_labs, dtype=np.float64)[:, 0]
    c = mesh.face_centroids
    nrm = mesh.face_normals
    radius = 2.0 * params.min_light_layers * L
    ext = np.ones(f, dtype=bool) if exterior is None else exterior
    parts = np.zeros(f, dtype=np.int64) if face_parts is None else np.asarray(face_parts)
    for s in np.unique(slots):
        darker = np.nonzero(lstar[s - 1] - lstar >= BLEED_DELTA_L)[0] + 1
        if darker.size == 0:
            continue
        lf = np.nonzero((slots == s) & ext)[0]
        df = np.nonzero(np.isin(slots, darker) & ext)[0]
        if lf.size and part_slots is not None:
            _part_bleed(mesh, parts, part_slots, lf, darker, L, layers, dark_face)
        if lf.size == 0 or df.size == 0:
            continue
        # thin the darker faces to one per BLEED_VOXEL_MM cell, then test the nearest
        # BLEED_NEIGHBORS of them: bounded work however finely a region is triangulated
        df = df[_voxel_pick(c[df], BLEED_VOXEL_MM)]
        tree = cKDTree(c[df])
        near, _ = tree.query(c[lf], k=1, distance_upper_bound=radius)
        lf = lf[np.isfinite(near)]
        k = min(BLEED_NEIGHBORS, len(df))
        for s0 in range(0, lf.size, 20000):
            chunk = lf[s0 : s0 + 20000]
            dist, idx = tree.query(c[chunk], k=k, distance_upper_bound=radius)
            dist, idx = dist.reshape(len(chunk), k), idx.reshape(len(chunk), k)
            valid = idx < len(df)
            q = df[np.where(valid, idx, 0)]  # (n, k)
            pq = c[chunk][:, None, :] - c[q]
            behind = np.einsum("nj,nkj->nk", nrm[chunk], pq)
            # solid between p and q: q is behind p and p is behind q (not across an air gap);
            # painted colors fill a part's own solid, so q must be on the same part
            ok = valid & (behind > 1e-6) & (np.einsum("nkj,nkj->nk", nrm[q], pq) < -1e-6)
            ok &= parts[chunk][:, None] == parts[q]
            depth = np.where(ok, np.einsum("nkj,nkj->nk", pq, pq) / (2 * np.maximum(behind, 1e-12)),
                             np.inf)
            j = np.argmin(depth, axis=1)
            dl = depth[np.arange(len(chunk)), j] / L
            better = dl < layers[chunk]
            layers[chunk[better]] = dl[better]
            dark_face[chunk[better]] = q[np.arange(len(chunk)), j][better]
    _top_penetration_floor(mesh, slots, lstar, params, ext, face_parts, part_slots, layers,
                           dark_face)
    return layers, dark_face


def _top_penetration_floor(mesh, slots, lstar, params, ext, face_parts, part_slots, layers,
                           dark_face):
    """Painted tops keep TOP_PAINT_LAYERS of their color under them (Bambu top paint
    penetration), limited by the solid below: its full thickness, or half of it when the
    surface underneath is darker (its bottom paint penetrates up to meet it). Part filaments
    are volumes, not paint, so a face showing its own part's filament gets no floor."""
    L = params.layer_mm
    cand = np.nonzero(np.isfinite(layers) & ext
                      & (mesh.face_normals[:, 2] >= np.cos(np.radians(params.flat_angle_deg))))[0]
    if part_slots is not None and face_parts is not None:
        cand = cand[slots[cand] != np.asarray(part_slots)[np.asarray(face_parts)[cand]]]
    if cand.size == 0:
        return
    _, _, fb, db = _columns(mesh, mesh.face_centroids[cand], ext)
    has = fb >= 0
    darker_below = np.zeros(len(cand), dtype=bool)
    darker_below[has] = lstar[slots[cand[has]] - 1] - lstar[slots[fb[has]] - 1] >= BLEED_DELTA_L
    thick = np.where(has, np.where(darker_below, db / 2, db), 0.0)
    floor = np.minimum(TOP_PAINT_LAYERS * L, thick) / L
    raise_ = floor > layers[cand]
    idx = cand[raise_]
    layers[idx] = floor[raise_]
    # what limits it now is the surface underneath, when that is darker
    lim = raise_ & darker_below
    dark_face[cand[lim]] = fb[lim]


def _voxel_pick(points: np.ndarray, cell: float) -> np.ndarray:
    """Indices of one point per cubic cell (the first in input order)."""
    key = np.floor(points / cell).astype(np.int64)
    _, first = np.unique(key, axis=0, return_index=True)
    return np.sort(first)


def _part_bleed(mesh, parts, part_slots, light_faces, darker_slots, L, layers, dark_face):
    """Light up-facing faces over another part whose filament is darker: depth = vertical
    distance down to that part's solid."""
    from .parts import part_depth_below

    up = light_faces[mesh.face_normals[light_faces, 2] > 0.3]
    if up.size == 0:
        return
    part_slots = np.asarray(part_slots)
    dark_parts = [int(p) for p in range(len(part_slots)) if part_slots[p] in set(darker_slots)]
    cands = {int(a): [b for b in dark_parts if b != a] for a in np.unique(parts[up])}
    cands = {a: b for a, b in cands.items() if b}
    if not cands:
        return
    depth, hit = part_depth_below(mesh, parts, up, cands)
    dl = depth / L
    better = np.isfinite(dl) & (dl < layers[up])
    layers[up[better]] = dl[better]
    dark_face[up[better]] = hit[better]


# ---------------------------------------------------------------------- rule D: swaps


@dataclass
class LayerSpans:
    """Per face, the layers its filament is present in (layer_presence without the slots), so
    many palettes can be scored on one mesh cheaply (PurgeEstimator)."""

    n_layers: int
    lo: np.ndarray  # painted span (paint penetration included), first layer
    hi: np.ndarray
    ok: np.ndarray
    raw_lo: np.ndarray  # the face's own z span (a part's body)
    raw_hi: np.ndarray
    raw_ok: np.ndarray


def layer_spans(mesh: Mesh, params: SimParams,
                exterior: np.ndarray | None = None) -> LayerSpans:
    L = params.layer_mm
    tri_z = mesh.triangles[:, :, 2]
    zmin, zmax = tri_z.min(axis=1), tri_z.max(axis=1)
    top = float(zmax.max()) if zmax.size else 0.0
    n_layers = max(1, int(np.ceil(top / L - 1e-9)))
    lo = np.ceil(zmin / L - 0.5).astype(np.int64)
    hi = np.floor(zmax / L - 0.5).astype(np.int64)
    ext = np.ones(mesh.n_faces, dtype=bool) if exterior is None else exterior
    raw_ok = ext & (np.clip(hi, -1, n_layers - 1) >= np.clip(lo, 0, n_layers - 1))
    raw_lo, raw_hi = np.clip(lo, 0, n_layers - 1), np.clip(hi, -1, n_layers - 1)
    nz = mesh.face_normals[:, 2]
    flat = np.abs(nz) >= np.cos(np.radians(params.flat_angle_deg))
    zc = mesh.face_centroids[:, 2]
    up = flat & (nz > 0)
    dn = flat & (nz < 0)
    own_up = np.ceil(zc / L - 1e-9).astype(np.int64) - 1
    own_dn = np.floor(zc / L + 1e-9).astype(np.int64)
    lo = np.where(up, np.minimum(lo, own_up - (TOP_PAINT_LAYERS - 1)), lo)
    hi = np.where(up, np.maximum(hi, own_up), hi)
    lo = np.where(dn, np.minimum(lo, own_dn), lo)
    hi = np.where(dn, np.maximum(hi, own_dn + (BOTTOM_PAINT_LAYERS - 1)), hi)
    lo = np.clip(lo, 0, n_layers - 1)
    hi = np.clip(hi, -1, n_layers - 1)
    return LayerSpans(n_layers, lo, hi, (hi >= lo) & ext, raw_lo, raw_hi, raw_ok)


def presence_from_spans(sp: LayerSpans, slots: np.ndarray, k: int,
                        face_parts: np.ndarray | None = None,
                        part_slots=None) -> np.ndarray:
    """(n_layers, k) bool, exactly layer_presence for the spans' mesh."""
    n_layers = sp.n_layers
    slots = np.asarray(slots, np.int64)
    diff = np.zeros((n_layers + 1, k), dtype=np.int64)
    ok = sp.ok
    np.add.at(diff, (sp.lo[ok], slots[ok] - 1), 1)
    np.add.at(diff, (sp.hi[ok] + 1, slots[ok] - 1), -1)
    if part_slots is not None and face_parts is not None:
        ps = np.asarray(part_slots, np.int64)[np.asarray(face_parts)]
        okp = sp.raw_ok
        np.add.at(diff, (sp.raw_lo[okp], ps[okp] - 1), 1)
        np.add.at(diff, (sp.raw_hi[okp] + 1, ps[okp] - 1), -1)
    return np.cumsum(diff, axis=0)[:n_layers] > 0


def purge_from_presence(presence: np.ndarray, rgb: np.ndarray, labs: np.ndarray,
                        params: SimParams) -> dict:
    """The simulator's purge model (rule D) on a layer presence table: grams of flush and
    prime tower on the X2D's two nozzles (sim/README.md, Purge calibration)."""
    flush = flush_matrix(np.asarray(rgb, np.uint8), np.asarray(labs, float),
                         dark_to_light_weight=2.0 * params.dark_to_light_weight)
    if params.nozzles >= 2:
        plan = assign_nozzles(presence, flush, params.nozzles)
        n = plan.changes
        flush_g = plan.flush_mm3 * FLUSH_CALIBRATION * DENSITY_G_MM3
        tower_g = (TOWER_G_PER_LAYER * plan.last_change_layer + TOWER_G_PER_CHANGE * n
                   if n else 0.0)
        return {"purge_g": flush_g + tower_g, "flush_g": flush_g, "tower_g": tower_g,
                "changes": int(n), "nozzle_changes": int(plan.nozzle_changes),
                "same_nozzle": int(plan.same_nozzle), "layers": int(presence.shape[0])}
    swaps, flush_mm3, _ = sequence_swaps(presence, flush)
    n = int(swaps.sum())
    g = (flush_mm3 + PRIME_VOLUME_MM3 * n) * DENSITY_G_MM3
    return {"purge_g": g, "flush_g": g, "tower_g": 0.0, "changes": n, "nozzle_changes": 0,
            "same_nozzle": n, "layers": int(presence.shape[0])}


class PurgeEstimator:
    """Purge for many candidate palettes on one product: the layer spans are computed once,
    then each palette costs one presence table and the nozzle split (no erosion or bleed;
    those move few faces). Within a few percent of simulate_print's purge on our jobs, which
    is within 11 % of the slicer (sim/README.md)."""

    def __init__(self, mesh: Mesh, params: SimParams | None = None,
                 face_parts: np.ndarray | None = None):
        self.params = params or SimParams()
        self.spans = layer_spans(mesh, self.params)
        self.face_parts = face_parts
        self.model_g = printed_grams(mesh)

    def estimate(self, face_slots: np.ndarray, palette_rgb, palette_labs,
                 part_slots=None) -> dict:
        k = len(palette_rgb)
        pres = presence_from_spans(self.spans, face_slots, k, self.face_parts, part_slots)
        out = purge_from_presence(pres, palette_rgb, palette_labs, self.params)
        out["model_g"] = self.model_g
        out["colors_per_layer_max"] = int(pres.sum(axis=1).max()) if pres.size else 0
        return out


def layer_presence(mesh: Mesh, slots: np.ndarray, k: int, params: SimParams,
                   exterior: np.ndarray | None = None, face_parts: np.ndarray | None = None,
                   part_slots: np.ndarray | None = None) -> np.ndarray:
    """(n_layers, k) bool: filament used in layer. Hidden faces (inside another part) do not
    count; with parts, each part's own filament is present wherever its outline is (its infill
    and inner walls), so every part is counted within the same single print."""
    L = params.layer_mm
    tri_z = mesh.triangles[:, :, 2]
    zmin, zmax = tri_z.min(axis=1), tri_z.max(axis=1)
    top = float(zmax.max()) if zmax.size else 0.0
    n_layers = max(1, int(np.ceil(top / L - 1e-9)))
    lo = np.ceil(zmin / L - 0.5).astype(np.int64)
    hi = np.floor(zmax / L - 0.5).astype(np.int64)
    ext = np.ones(mesh.n_faces, dtype=bool) if exterior is None else exterior
    part_rows = []
    if part_slots is not None and face_parts is not None:
        ps = np.asarray(part_slots)[np.asarray(face_parts)]
        okp = ext & (np.clip(hi, -1, n_layers - 1) >= np.clip(lo, 0, n_layers - 1))
        part_rows = [(np.clip(lo[okp], 0, n_layers - 1), np.clip(hi[okp], -1, n_layers - 1),
                      ps[okp])]
    nz = mesh.face_normals[:, 2]
    flat = np.abs(nz) >= np.cos(np.radians(params.flat_angle_deg))
    zc = mesh.face_centroids[:, 2]
    up = flat & (nz > 0)
    dn = flat & (nz < 0)
    own_up = np.ceil(zc / L - 1e-9).astype(np.int64) - 1
    own_dn = np.floor(zc / L + 1e-9).astype(np.int64)
    lo = np.where(up, np.minimum(lo, own_up - (TOP_PAINT_LAYERS - 1)), lo)
    hi = np.where(up, np.maximum(hi, own_up), hi)
    lo = np.where(dn, np.minimum(lo, own_dn), lo)
    hi = np.where(dn, np.maximum(hi, own_dn + (BOTTOM_PAINT_LAYERS - 1)), hi)
    lo = np.clip(lo, 0, n_layers - 1)
    hi = np.clip(hi, -1, n_layers - 1)
    ok = (hi >= lo) & ext
    diff = np.zeros((n_layers + 1, k), dtype=np.int64)
    np.add.at(diff, (lo[ok], slots[ok] - 1), 1)
    np.add.at(diff, (hi[ok] + 1, slots[ok] - 1), -1)
    for plo, phi, pslot in part_rows:
        np.add.at(diff, (plo, pslot - 1), 1)
        np.add.at(diff, (phi + 1, pslot - 1), -1)
    return np.cumsum(diff, axis=0)[:n_layers] > 0


def sequence_swaps(presence: np.ndarray, flush: np.ndarray):
    """Greedy per-layer order. Returns (swaps per layer, total flush mm3, list of (from, to))."""
    n_layers = presence.shape[0]
    cur = -1
    swaps = np.zeros(n_layers, dtype=np.int64)
    total = 0.0
    seq = []
    for li in range(n_layers):
        todo = set(np.nonzero(presence[li])[0].tolist())
        if not todo:
            continue
        if cur in todo:
            todo.discard(cur)
        elif cur < 0:
            # first loaded filament is free: start with the one that makes the rest cheapest
            cur = min(todo, key=lambda s: sum(flush[s, t] for t in todo if t != s))
            todo.discard(cur)
        while todo:
            nxt = min(sorted(todo), key=lambda s: flush[cur, s])
            total += flush[cur, nxt]
            seq.append((cur, nxt))
            swaps[li] += 1
            cur = nxt
            todo.discard(nxt)
    return swaps, total, seq


@dataclass
class NozzlePlan:
    nozzle_of: np.ndarray  # (K,) nozzle per slot
    swaps: np.ndarray  # tool changes per layer
    flush_mm3: float  # same-nozzle flush, before FLUSH_CALIBRATION
    changes: int
    nozzle_changes: int
    same_nozzle: int
    last_change_layer: int  # 1-based layer of the last change (0: none)


def sequence_nozzles(presence: np.ndarray, flush: np.ndarray, nozzle_of) -> NozzlePlan:
    """Greedy per-layer order with each nozzle keeping its own loaded filament. A change to a
    filament loaded on its nozzle costs nothing; else the flush from that nozzle's filament."""
    nozzle_of = np.asarray(nozzle_of, dtype=np.int64)
    n_layers = presence.shape[0]
    swaps = np.zeros(n_layers, dtype=np.int64)
    loaded: dict[int, int] = {}
    cur, active = -1, -1
    fl = 0.0
    changes = nchg = same = last = 0

    def cost(t: int) -> float:
        held = loaded.get(int(nozzle_of[t]), -1)
        return 0.0 if held in (-1, t) else float(flush[held, t])

    for li in range(n_layers):
        todo = set(np.nonzero(presence[li])[0].tolist())
        if not todo:
            continue
        if cur in todo:
            todo.discard(cur)
        elif cur < 0:
            cur = min(todo)
            loaded[int(nozzle_of[cur])] = cur
            active = int(nozzle_of[cur])
            todo.discard(cur)
        while todo:
            nxt = min(sorted(todo), key=cost)
            n = int(nozzle_of[nxt])
            held = loaded.get(n, -1)
            if held not in (-1, nxt):
                fl += float(flush[held, nxt])
                same += 1
            nchg += int(n != active)
            changes += 1
            swaps[li] += 1
            loaded[n], active, cur = nxt, n, nxt
            todo.discard(nxt)
            last = li + 1
    return NozzlePlan(nozzle_of, swaps, fl, changes, nchg, same, last)


def assign_nozzles(presence: np.ndarray, flush: np.ndarray, nozzles: int) -> NozzlePlan:
    """Split the filaments between the nozzles for the least flush ("Auto For Flush"): every
    split is tried up to 10 filaments (slot 1 fixed on nozzle 0), else greedy by co-presence."""
    k = presence.shape[1]
    if nozzles <= 1 or k <= 1:
        return sequence_nozzles(presence, flush, np.zeros(k, np.int64))
    best = None
    if k <= 10:
        for code in range(nozzles ** (k - 1)):
            nz, c = [0], code
            for _ in range(k - 1):
                nz.append(c % nozzles)
                c //= nozzles
            plan = sequence_nozzles(presence, flush, nz)
            if best is None or (plan.flush_mm3, plan.changes) < (best.flush_mm3, best.changes):
                best = plan
        return best
    co = presence.T.astype(np.int64) @ presence.astype(np.int64)
    order = np.argsort(-np.diag(co))
    nz = np.zeros(k, np.int64)
    for i, s in enumerate(order):
        nz[s] = i % nozzles
    return sequence_nozzles(presence, flush, nz)


def printed_grams(mesh: Mesh, exterior: np.ndarray | None = None) -> float:
    """Shell + sparse infill estimate. With overlapping parts, only visible faces count and the
    union volume is the divergence sum over them (exact but for the seams where parts cut)."""
    ext = np.ones(mesh.n_faces, dtype=bool) if exterior is None else exterior
    nz = mesh.face_normals[ext, 2]
    t = np.where(nz > 0.7, TOP_SHELL_MM, np.where(nz < -0.7, BOTTOM_SHELL_MM, WALL_MM))
    tri = mesh.triangles[ext]
    vol = float(abs(np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum()) / 6)
    shell = min(float((mesh.face_areas[ext] * t).sum()), vol)
    return (shell + INFILL * max(0.0, vol - shell)) * DENSITY_G_MM3


# ---------------------------------------------------------------------- main


def _fileref(arr: np.ndarray, out_dir: Path | None, job_dir: Path | None, name: str) -> FileRef:
    buf = io.BytesIO()
    np.save(buf, arr)
    data = buf.getvalue()
    rel = name
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / name
        p.write_bytes(data)
        rel = p.relative_to(job_dir).as_posix() if job_dir is not None else p.name
    return FileRef(path=rel, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), kind="npy",
                   role="sim_face_filaments")


def simulate_print_detailed(
    mesh: Mesh,
    face_slots: np.ndarray,
    palette_labs,
    params: SimParams | None = None,
    face_regions: np.ndarray | None = None,
    out_dir: str | Path | None = None,
    job_dir: str | Path | None = None,
    face_parts: np.ndarray | None = None,
    part_slots=None,
    quantize_model: str | None = None,
) -> SimResult:
    params = params or SimParams()
    labs = np.asarray(palette_labs, dtype=np.float64).reshape(-1, 3)
    k = len(labs)
    if face_parts is not None:
        face_parts = np.asarray(face_parts).astype(np.int64)
        if face_parts.shape != (mesh.n_faces,):
            raise ValueError(f"face_parts must be ({mesh.n_faces},)")
    if part_slots is not None:
        part_slots = np.asarray(part_slots).astype(np.int64)
        if face_parts is None:
            raise ValueError("part_slots needs face_parts")
        if len(part_slots) <= face_parts.max():
            raise ValueError("part_slots must have one slot per part id")
    if face_slots is None:
        if part_slots is None:
            raise ValueError("give face_slots, or face_parts with part_slots")
        face_slots = part_slots[face_parts]
    slots_in = np.asarray(face_slots).astype(np.int64)
    if slots_in.shape != (mesh.n_faces,):
        raise ValueError(f"face_slots must be ({mesh.n_faces},)")
    if slots_in.min() < 1 or slots_in.max() > k:
        raise ValueError(f"face_slots must be 1..{k} (resolve unlabeled faces first)")
    regions_in = (_patches(mesh, slots_in) if face_regions is None
                  else np.asarray(face_regions).astype(np.int64))
    rgb = lab_to_srgb(labs)
    hidden = np.zeros(mesh.n_faces, dtype=bool)
    if face_parts is not None and len(np.unique(face_parts)) > 1:
        from .parts import interior_faces

        hidden = interior_faces(mesh, face_parts)
    exterior = ~hidden
    area = np.where(exterior, mesh.face_areas, 0.0)

    # A. quantize, B. erode
    q = quantize_layers(mesh, slots_in, params, quantize_model)
    if (quantize_model or QUANTIZE_MODEL) == "bambu":
        q = paint_penetration(mesh, q, params, exterior)
    geometry = thin_geometry(mesh, params, exterior)
    e, _ = erode(mesh, q, params, geometry=geometry)
    changed = np.zeros(mesh.n_faces, dtype=np.uint8)
    changed[q != slots_in] |= 1
    changed[e != q] |= 2

    # region bookkeeping: changed faces join the region of the face whose color they took
    # (moved faces cut off from every unchanged face of their new color, which multi-pass
    # erosion can produce, take the nearest one in space instead)
    regions = regions_in.copy()
    moved = e != slots_in
    if moved.any():
        seeds = np.nonzero(~moved)[0]
        _, src, _ = propagate_nearest(mesh, e, seeds, mesh.face_centroids[seeds])
        ok = moved & (src >= 0)
        regions[ok] = regions_in[src[ok]]
        for s in np.unique(e[moved & ~ok]):
            lost_f = np.nonzero(moved & ~ok & (e == s))[0]
            pool = np.nonzero(~moved & (e == s))[0]
            if pool.size:
                _, j = cKDTree(mesh.face_centroids[pool]).query(mesh.face_centroids[lost_f])
                regions[lost_f] = regions_in[pool[j]]

    merged = []
    n_reg = int(max(regions_in.max(), regions.max())) + 1
    reg_area = np.bincount(regions_in, weights=area, minlength=n_reg)
    lost = moved
    if lost.any():
        la = np.bincount(regions_in[lost], weights=area[lost], minlength=n_reg)
        other = lost & (regions != regions_in)
        ra, rb, tot = _edge_weights_by_key(regions_in[other], regions[other], area[other], n_reg,
                                           n_reg)
        g, idx = _argmax_per_group(ra, tot)
        into = dict(zip(g.tolist(), rb[idx].tolist()))
        qa = np.bincount(regions_in[changed == 1], weights=area[changed == 1], minlength=n_reg)
        w_min = params.min_width_factor * params.nozzle_mm
        for rid in np.nonzero(la > 0)[0]:
            rid = int(rid)
            gone = la[rid] >= reg_area[rid] * 0.999
            if not gone and la[rid] < MERGE_REPORT_MIN_MM2:
                continue
            if gone and qa[rid] >= la[rid] * 0.5:
                reason = "quantized_away"
            elif gone and reg_area[rid] < (2 * w_min) ** 2:
                reason = "island"
            else:
                reason = "too_thin"
            merged.append(MergedRegion(region_id=rid, into_region_id=int(into.get(rid, 0)),
                                       area_mm2=round(float(la[rid]), 3), reason=reason))
        merged.sort(key=lambda m: -m.area_mm2)

    # C. bleed
    light_layers, dark_face = bleed_depths(mesh, e, labs, params, exterior, face_parts,
                                           part_slots)
    flagged = np.floor(light_layers + LAYER_EPS) < params.min_light_layers
    dark_slot = np.zeros(mesh.n_faces, dtype=np.int64)
    dark_slot[flagged] = e[dark_face[flagged]]
    flags = []
    if flagged.any():
        fl = np.nonzero(flagged)[0]
        ra, rb = regions[fl], regions[dark_face[fl]]
        key = ra * n_reg + rb
        for kk in np.unique(key):
            sel = fl[key == kk]
            a_tot = float(area[sel].sum())
            if a_tot < BLEED_MIN_AREA_MM2:
                continue
            ll = np.floor(light_layers[sel] + LAYER_EPS)
            order = np.argsort(ll)
            cum = np.cumsum(area[sel][order])
            med = int(ll[order][np.searchsorted(cum, cum[-1] / 2)])
            flags.append(BleedFlag(region_id=int(kk // n_reg), over_region_id=int(kk % n_reg),
                                   light_layers=med, area_mm2=round(a_tot, 3)))
        flags.sort(key=lambda b: -b.area_mm2)
    light_layers = np.where(flagged, light_layers, np.inf)

    # D. swaps and purge
    presence = layer_presence(mesh, e, k, params, exterior, face_parts, part_slots)
    # SimParams.dark_to_light_weight is a plain multiplier (contract, default 1.0);
    # flush_matrix takes weight / 2, so 2.0 there leaves Bambu's volumes unchanged.
    flush = flush_matrix(rgb, labs, dark_to_light_weight=2.0 * params.dark_to_light_weight)
    plan = None
    tower_g = flush_g = 0.0
    if params.nozzles >= 2:
        plan = assign_nozzles(presence, flush, params.nozzles)
        swaps = plan.swaps
        n_swaps = plan.changes
        flush_g = plan.flush_mm3 * FLUSH_CALIBRATION * DENSITY_G_MM3
        tower_g = (TOWER_G_PER_LAYER * plan.last_change_layer + TOWER_G_PER_CHANGE * n_swaps
                   if n_swaps else 0.0)
        purge_g = flush_g + tower_g
        purge_mm3 = purge_g / DENSITY_G_MM3
    else:
        swaps, flush_mm3, _ = sequence_swaps(presence, flush)
        n_swaps = int(swaps.sum())
        purge_mm3 = flush_mm3 + PRIME_VOLUME_MM3 * n_swaps
        purge_g = purge_mm3 * DENSITY_G_MM3
        flush_g = purge_g
    model_g = printed_grams(mesh, exterior)

    out_dir = Path(out_dir) if out_dir is not None else None
    job_dir = Path(job_dir) if job_dir is not None else None
    ref = _fileref(e.astype(np.int16), out_dir, job_dir, "sim_face_filaments.npy")
    report = SimReport(
        params=params,
        merged=merged,
        bleed_flags=flags,
        layers=int(presence.shape[0]),
        swaps_total=n_swaps,
        swaps_max_per_layer=int(swaps.max()) if swaps.size else 0,
        purge_g_est=round(purge_g, 3),
        model_g_est=round(model_g, 3),
        purge_pct=round(100.0 * purge_g / model_g, 2) if model_g > 0 else 0.0,
        face_filaments=ref,
    )
    return SimResult(
        params=params, palette_labs=labs, palette_rgb=rgb, face_slots_in=slots_in.astype(np.int16),
        face_slots=e.astype(np.int16), face_regions_in=regions_in, face_regions=regions,
        face_changed=changed, face_light_layers=light_layers, face_bleed_dark_slot=dark_slot,
        layer_slots=presence, layer_swaps=swaps, report=report, purge_mm3=purge_mm3, flush=flush,
        face_hidden=hidden, nozzle_of=plan.nozzle_of if plan is not None else None,
        tower_g=round(tower_g, 3), flush_g=round(flush_g, 3),
        nozzle_changes=plan.nozzle_changes if plan is not None else 0,
        same_nozzle_swaps=plan.same_nozzle if plan is not None else n_swaps,
    )


def simulate_print(
    mesh: Mesh,
    face_slots: np.ndarray | None,
    palette_labs,
    params: SimParams | None = None,
    face_regions: np.ndarray | None = None,
    out_dir: str | Path | None = None,
    job_dir: str | Path | None = None,
    face_parts: np.ndarray | None = None,
    part_slots=None,
) -> tuple[np.ndarray, SimReport]:
    """Apply the slicer limits to a per-face paint.

    face_slots: (F,) 3MF slot per face (palette index + 1, all faces resolved). May be None when
        face_parts and part_slots are given (every face takes its part's filament).
    palette_labs: (K, 3) CIELAB per slot (measured filament Lab when available).
    face_regions: optional (F,) region ids for reporting; default: connected same-slot patches.
    face_parts: optional (F,) part index per face for multi-part prints (parts may overlap;
        faces hidden inside another part are ignored). part_slots: (P,) filament slot per part,
        used for each part's solid (infill, inner walls) in swap counting and bleed.
    out_dir/job_dir: when given, sim_face_filaments.npy is written to out_dir and the report's
        FileRef path is relative to job_dir.
    Returns (sim_face_slots int16 (F,), SimReport).
    """
    res = simulate_print_detailed(mesh, face_slots, palette_labs, params, face_regions, out_dir,
                                  job_dir, face_parts, part_slots)
    return res.face_slots, res.report
