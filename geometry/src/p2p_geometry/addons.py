"""Exact-geometry add-ons. Every builder returns closed manifold3d solids in mm.

None of these touch the figure mesh. The figure is only ever read (as a boolean tool for the
flat back, or as a voxel solid for placement), so its triangles and colors stay untouched.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from manifold3d import CrossSection, JoinType, Manifold, OpType
from scipy import ndimage

from .solid import Solid

SEG = 96  # circular segments for round features


# ------------------------------------------------------------------ primitives


def rounded_rect(w: float, d: float, r: float) -> CrossSection:
    """Rounded rectangle centered on the origin."""
    r = max(0.0, min(r, w / 2 - 1e-3, d / 2 - 1e-3))
    core = CrossSection.square((w - 2 * r, d - 2 * r), center=True)
    return core.offset(r, JoinType.Round, circular_segments=SEG) if r > 0 else core


def chamfered_prism(section: CrossSection, height: float, chamfer: float, bottom: float = 0.0):
    """Prism of a CONVEX section with a 45 degree chamfer on the top edge (and bottom if > 0)."""
    chamfer = max(0.0, min(chamfer, height / 3))
    bottom = max(0.0, min(bottom, height / 3))
    if chamfer == 0 and bottom == 0:
        return Manifold.extrude(section, height)
    pieces = [Manifold.extrude(section, height - chamfer - bottom).translate((0, 0, bottom))]
    pieces.append(
        Manifold.extrude(section.offset(-chamfer, JoinType.Round, circular_segments=SEG), 1e-3)
        .translate((0, 0, height - 1e-3))
    )
    if bottom > 0:
        pieces.append(
            Manifold.extrude(section.offset(-bottom, JoinType.Round, circular_segments=SEG), 1e-3)
        )
    return Manifold.batch_hull(pieces)


# ------------------------------------------------------------------ bases and plates


@dataclass
class BaseInfo:
    center_xy: tuple[float, float]
    radius_mm: float | tuple[float, float]
    thickness_mm: float


def round_base(
    footprint, margin_mm: float = 3.0, thickness_mm: float = 4.0, chamfer_mm: float = 1.0,
    min_diameter_mm: float = 0.0,
) -> tuple[Manifold, BaseInfo]:
    """Disc under a figure: smallest circle around the footprint (shapely) plus a margin."""
    import shapely

    circle = shapely.minimum_bounding_circle(footprint)
    c = circle.centroid
    r = max(shapely.minimum_bounding_radius(footprint) + margin_mm, min_diameter_mm / 2)
    disc = CrossSection.circle(r, SEG * 2)
    solid = chamfered_prism(disc, thickness_mm, chamfer_mm, bottom=0.4).translate((c.x, c.y, 0))
    return solid, BaseInfo((c.x, c.y), r, thickness_mm)


def oval_base(
    footprint, margin_mm: float = 3.0, thickness_mm: float = 4.0, chamfer_mm: float = 1.0
) -> tuple[Manifold, BaseInfo]:
    """Ellipse around the footprint's minimum rotated rectangle, plus a margin."""
    rect = footprint.minimum_rotated_rectangle
    pts = np.asarray(rect.exterior.coords)[:4]
    e1, e2 = pts[1] - pts[0], pts[2] - pts[1]
    a, b = np.linalg.norm(e1) / 2, np.linalg.norm(e2) / 2
    ang = math.degrees(math.atan2(e1[1], e1[0]))
    c = pts.mean(axis=0)
    ra, rb = a * math.sqrt(2) + margin_mm, b * math.sqrt(2) + margin_mm
    ell = CrossSection.circle(1.0, SEG * 2).scale((ra, rb)).rotate(ang)
    solid = chamfered_prism(ell, thickness_mm, chamfer_mm, bottom=0.4).translate((c[0], c[1], 0))
    return solid, BaseInfo((float(c[0]), float(c[1])), (ra, rb), thickness_mm)


PLATE_SHAPES = ("rectangle", "rounded", "oval")
# Corner radius per shape (the intake question's options, question_bank.yaml plate_shape).
# ASSUMED: a "rectangle" keeps a 1 mm radius so the corner does not lift or catch.
PLATE_CORNER_MM = {"rectangle": 1.0, "rounded": 5.0}


def plate_section(width_mm: float, depth_mm: float, shape: str = "rounded",
                  corner_mm: float | None = None) -> CrossSection:
    """Plate outline centered on the origin: rectangle, rounded rectangle or oval (ellipse)."""
    if shape == "oval":
        return CrossSection.circle(1.0, SEG * 2).scale((width_mm / 2, depth_mm / 2))
    r = PLATE_CORNER_MM.get(shape, 5.0) if corner_mm is None else corner_mm
    return rounded_rect(width_mm, depth_mm, r)


def plate_polygon(width_mm: float, depth_mm: float, shape: str = "rounded",
                  corner_mm: float | None = None, x0: float | None = None):
    """The same outline as a shapely polygon (tip check, magnet pocket), left edge at x0."""
    import shapely
    import shapely.affinity

    cx = 0.0 if x0 is None else x0 + width_mm / 2
    if shape == "oval":
        disk = shapely.Point(0.0, 0.0).buffer(1.0, 64)
        return shapely.affinity.translate(
            shapely.affinity.scale(disk, width_mm / 2, depth_mm / 2, origin=(0, 0)), cx)
    r = PLATE_CORNER_MM.get(shape, 5.0) if corner_mm is None else corner_mm
    r = max(0.0, min(r, width_mm / 2 - 1e-3, depth_mm / 2 - 1e-3))
    box = shapely.box(cx - width_mm / 2 + r, -depth_mm / 2 + r, cx + width_mm / 2 - r,
                      depth_mm / 2 - r)
    return box.buffer(r, 16) if r > 0 else box


def ellipse_half_width(a: float, b: float, y: float) -> float:
    """x extent of the ellipse (x/a)^2 + (y/b)^2 = 1 at height y (0 past the tip)."""
    t = 1.0 - (y / b) ** 2
    return a * math.sqrt(t) if t > 0 else 0.0


def plate(
    width_mm: float, depth_mm: float, thickness_mm: float = 4.0, corner_mm: float | None = 5.0,
    chamfer_mm: float = 0.8, shape: str = "rounded",
) -> Manifold:
    """Nameplate slab centered on the origin in XY, bottom on z = 0."""
    return chamfered_prism(plate_section(width_mm, depth_mm, shape, corner_mm), thickness_mm,
                           chamfer_mm, 0.4)


# ------------------------------------------------------------------ loops


@dataclass
class LoopSpec:
    inner_d_mm: float = 5.0
    section_mm: float = 3.0  # radial wall of the ring
    thickness_mm: float = 4.0  # along the hole axis
    clearance_mm: float = 1.0  # gap between the hole and the figure surface
    embed_mm: float = 2.0  # stem depth below the lowest surface point under it (>= overlap)

    @property
    def outer_r(self) -> float:
        return self.inner_d_mm / 2 + self.section_mm

    @property
    def stem_half_width(self) -> float:
        return 0.85 * self.outer_r


def loop_solid(spec: LoopSpec, v_bottom: float, v_center: float, chamfer_both: bool) -> Manifold:
    """Tab with a ring, in local coords: ring center (0, v_center), stem down to v_bottom,
    extruded 0..thickness along local z. The hole gets small countersinks on both faces."""
    ro, ri, t = spec.outer_r, spec.inner_d_mm / 2, spec.thickness_mm
    hw = spec.stem_half_width
    outer = (
        CrossSection.circle(ro, SEG).translate((0, v_center))
        + CrossSection.square((2 * hw, 0.01)).translate((-hw, v_bottom))
    ).hull()
    c = min(0.6, t / 6, spec.section_mm / 4)
    bottom = c if chamfer_both else 0.0
    body = chamfered_prism(outer, t, c, bottom=bottom)
    cs = min(0.4, spec.section_mm / 5)
    hole = Manifold.cylinder(t + 2, ri, ri, SEG).translate((0, 0, -1))
    sink_top = Manifold.cylinder(cs + 0.01, ri, ri + cs + 0.01, SEG).translate((0, 0, t - cs))
    sink_bot = Manifold.cylinder(cs + 0.01, ri + cs + 0.01, ri, SEG).translate((0, 0, -0.01))
    hole = Manifold.batch_boolean([hole, sink_top, sink_bot], OpType.Add).translate((0, v_center, 0))
    return body - hole


def keyring_loop(
    spec: LoopSpec, anchor_xy: tuple[float, float], surface_y: float, lowest_y: float,
) -> tuple[Manifold, dict]:
    """In-plane keyring tab for a flat product (hole axis Z, bottom on z = 0, up is +Y).

    surface_y: highest edge of the product under the tab; lowest_y: lowest edge under it.
    """
    ri = spec.inner_d_mm / 2
    v_center = surface_y + spec.clearance_mm + ri
    v_bottom = lowest_y - spec.embed_mm
    m = loop_solid(spec, v_bottom, v_center, chamfer_both=False).translate((anchor_xy[0], 0, 0))
    info = {
        "hole_center": (anchor_xy[0], v_center),
        "stem_bottom": v_bottom,
        "embed_min_mm": lowest_y - v_bottom,
        "top": v_center + spec.outer_r,
    }
    return m, info


def hanging_loop(
    spec: LoopSpec, anchor_xy: tuple[float, float], surface_z: float, lowest_z: float,
) -> tuple[Manifold, dict]:
    """Upright ring on top of a standing figure: hole axis along Y (faces the viewer)."""
    ri = spec.inner_d_mm / 2
    v_center = surface_z + spec.clearance_mm + ri
    v_bottom = lowest_z - spec.embed_mm
    local = loop_solid(spec, v_bottom, v_center, chamfer_both=True)
    # local (u, v, w) -> world (u, -w, v): rotate +90 degrees about X.
    m = local.rotate((90, 0, 0)).translate(
        (anchor_xy[0], anchor_xy[1] + spec.thickness_mm / 2, 0)
    )
    info = {
        "hole_center": (anchor_xy[0], anchor_xy[1], v_center),
        "stem_bottom": v_bottom,
        "embed_min_mm": lowest_z - v_bottom,
        "top": v_center + spec.outer_r,
    }
    return m, info


@dataclass
class Anchor:
    x: float
    y: float
    surface: float  # highest surface under the footprint (z upright, y flat)
    lowest: float  # lowest surface under the footprint
    thickness_mm: float
    candidates: int
    width_mm: float = 0.0  # flat: outline chord at the stem; upright: same as thickness_mm


@dataclass
class Seats:
    """Every sturdy seat for an upright Y-axis ring (arrays, one entry per seat)."""

    x: np.ndarray
    y: np.ndarray
    z_high: np.ndarray  # highest surface under the stem footprint (the ring sits above it)
    z_low: np.ndarray  # lowest surface under the footprint
    thickness_mm: np.ndarray

    def __len__(self) -> int:
        return len(self.x)

    @property
    def score(self) -> np.ndarray:
        """Highest flush seat: the stem's lowest contact counts, and a footprint that straddles
        a spike (an ear tip) is penalised because the ring would float above it on a long stem."""
        return self.z_low - 0.5 * (self.z_high - self.z_low)

    def anchor(self, k: int) -> Anchor:
        return Anchor(float(self.x[k]), float(self.y[k]), float(self.z_high[k]),
                      float(self.z_low[k]), float(self.thickness_mm[k]), len(self),
                      float(self.thickness_mm[k]))


def upright_seats(solid: Solid, spec: LoopSpec, min_thickness_mm: float) -> Seats | None:
    """Spots on top of a standing figure where a Y-axis ring's stem fits.

    A spot passes when the whole stem footprint sits over the figure, the stem bottom stays
    inside the topmost solid run of every column (no poking out under an ear or a drooping
    part), and the local thickness there is at least min_thickness_mm (rules out thin ears).
    """
    p = solid.pitch
    zt = np.where(np.isnan(solid.z_top), -np.inf, solid.z_top)
    ze = np.where(np.isnan(solid.z_top_entry), np.inf, solid.z_top_entry)
    wi = int(math.ceil(spec.stem_half_width / p))
    wj = int(math.ceil(spec.thickness_mm / 2 / p))
    size = (2 * wi + 1, 2 * wj + 1)
    zlow = ndimage.minimum_filter(zt, size=size, mode="constant", cval=-np.inf)
    zhigh = ndimage.maximum_filter(zt, size=size, mode="constant", cval=-np.inf)
    zent = ndimage.maximum_filter(ze, size=size, mode="constant", cval=np.inf)
    zb = zlow - spec.embed_mm
    valid = np.isfinite(zlow) & (zent <= zb - 0.5 * p)
    if not valid.any():
        return None
    ii, jj = np.nonzero(valid)
    xs, ys = solid.xs(), solid.ys()
    pts = np.stack([xs[ii], ys[jj], zb[ii, jj] - 1.0], axis=1)
    th = solid.thickness_at(pts, radius_mm=1.5)
    ok = th >= min_thickness_mm
    if not ok.any():
        return None
    ii, jj, th = ii[ok], jj[ok], th[ok]
    return Seats(xs[ii].astype(float), ys[jj].astype(float), zhigh[ii, jj].astype(float),
                 zlow[ii, jj].astype(float), np.asarray(th, float))


def seat_at(solid: Solid, spec: LoopSpec, x: float, y: float) -> Anchor | None:
    """The seat for a Y-axis ring's stem centered at (x, y), without the validity tests of
    upright_seats (used to keep a chosen spot while the product is rescaled)."""
    p = solid.pitch
    xs, ys = solid.xs(), solid.ys()
    i = int(np.clip(np.round((x - xs[0]) / p), 0, len(xs) - 1))
    j = int(np.clip(np.round((y - ys[0]) / p), 0, len(ys) - 1))
    wi = int(math.ceil(spec.stem_half_width / p))
    wj = int(math.ceil(spec.thickness_mm / 2 / p))
    win = solid.z_top[max(0, i - wi):i + wi + 1, max(0, j - wj):j + wj + 1]
    win = win[np.isfinite(win)]
    if not win.size:
        return None
    zh, zl = float(win.max()), float(win.min())
    th = float(solid.thickness_at(np.array([[xs[i], ys[j], zl - spec.embed_mm - 1.0]]),
                                  radius_mm=1.5)[0])
    return Anchor(float(xs[i]), float(ys[j]), zh, zl, th, 1, th)


def find_anchor_upright(
    solid: Solid, spec: LoopSpec, com_xy: np.ndarray, min_thickness_mm: float
) -> Anchor | None:
    """Highest sturdy spot on top of a standing figure for a Y-axis ring (see upright_seats).
    Among passing spots the highest wins; near-ties go to the one closest to the center of mass
    so the ornament hangs straight. pick_balanced_seat chooses for a straight hang instead."""
    seats = upright_seats(solid, spec, min_thickness_mm)
    if seats is None:
        return None
    score = seats.score
    near = score >= score.max() - 0.75
    d = np.hypot(seats.x - com_xy[0], seats.y - com_xy[1])
    d[~near] = np.inf
    return seats.anchor(int(np.argmin(d)))


# Hanging: a keychain or ornament hangs with its center of mass straight below the top of the
# loop's hole. ASSUMED tolerances, to confirm with a test print: under 8 degrees reads as
# straight; the head is the top 12 % of the figure's height (below its highest seat).
STRAIGHT_TILT_DEG = 8.0
HEAD_BAND_FRAC = 0.12


def seat_tilts(seats: Seats, spec: LoopSpec, com: np.ndarray) -> np.ndarray:
    """Hang tilt in degrees per seat: the angle between up (+Z) and the line from the center of
    mass to the pivot (the top of the hole: seat surface + clearance + hole diameter)."""
    pz = seats.z_high + spec.clearance_mm + spec.inner_d_mm
    d = np.stack([seats.x - com[0], seats.y - com[1], pz - com[2]], axis=1)
    n = np.linalg.norm(d, axis=1) + 1e-12
    return np.degrees(np.arccos(np.clip(d[:, 2] / n, -1.0, 1.0)))


LOOP_AREAS = ("head", "back", "any")  # where a top loop may sit (the customer's answer)


def pick_balanced_seat(seats: Seats, spec: LoopSpec, com: np.ndarray, z_top: float,
                       height: float, head_only: bool = False, area: str | None = None,
                       head_top: float | None = None) -> tuple[int | None, dict]:
    """Seat for a top loop in the area the customer chose, placed where it hangs least tilted.

    The head is the seats within HEAD_BAND_FRAC of the figure's height below the highest seat
    (thin ears never hold a seat, so the highest seats are the head); the back is every other
    top seat (the neck, back and rump of a standing animal). Every seat already passed the
    sturdiness test (upright_seats: local thickness, stem inside the topmost solid run).
    - area head (the answer "Top of the head"): the least tilted head seat, however tilted.
    - area back ("On the back"): the least tilted back seat (over the center of mass on a
      standing animal); the head when the figure has no back seat.
    - area any (the answer allows anywhere): the head when a head seat hangs within
      STRAIGHT_TILT_DEG, else the highest seat that does, else the least tilted one.
    Among seats within 1 degree of the best tilt in the area, the highest wins. head_only is
    the old name for area head. head_top: the highest flush seat of any thickness (the top of
    the head), so a head too thin for this ring at this size does not move the band down to the
    back; default the highest of these seats. Returns (None, info) when the area has no seat.
    z_top is kept for callers."""
    area = area or ("head" if head_only else "any")
    tilt = seat_tilts(seats, spec, com)
    top = float(seats.z_low.max()) if head_top is None else max(float(head_top),
                                                                 float(seats.z_low.max()))
    head = seats.z_low >= top - HEAD_BAND_FRAC * height
    back = ~head
    head_best = float(tilt[head].min()) if head.any() else None
    back_best = float(tilt[back].min()) if back.any() else None
    info = {"area": area, "head_best_tilt_deg": head_best, "back_best_tilt_deg": back_best,
            "any_best_tilt_deg": float(tilt.min()), "seats": len(seats),
            "head_seats": int(head.sum())}
    if area == "head" and not head.any():
        return None, {**info, "where": None}
    if area == "head":
        pool, where = head, "head"
    elif area == "back" and back.any():
        pool, where = back, "back"
    elif area == "back":
        pool, where = head, "head"  # no seat behind the head (a bust): the head
    elif head_best is not None and head_best <= STRAIGHT_TILT_DEG:
        pool, where = head, "head"
    elif (tilt <= STRAIGHT_TILT_DEG).any():
        pool, where = tilt <= STRAIGHT_TILT_DEG, "balance"
    else:
        pool, where = np.ones(len(seats), bool), "least_tilt"
    best = float(tilt[pool].min())
    near = pool & (tilt <= best + 1.0)
    score = np.where(near, seats.score, -np.inf)
    k = int(np.argmax(score))
    return k, {"where": where, "on_head": bool(head[k]), "tilt_deg": float(tilt[k]), **info}


def find_anchor_flat(
    mask: np.ndarray, xs: np.ndarray, ys: np.ndarray, height: np.ndarray, spec: LoopSpec,
    com_x: float, min_width_mm: float,
) -> Anchor | None:
    """Highest sturdy spot on the top (+Y) edge of a flat product's outline for a Z-axis ring.

    mask: (nx, ny) product footprint; height: (nx, ny) product top z per column.
    A spot passes when the stem footprint is fully inside the outline down to the stem bottom
    and the outline there is at least min_width_mm wide (not a thin ear or tail).
    """
    p = float(xs[1] - xs[0])
    nx, ny = mask.shape
    jidx = np.arange(ny)[None, :].repeat(nx, 0)
    y_top = np.where(mask.any(axis=1), np.where(mask, jidx, -1).max(axis=1), -1)
    # start of the topmost run in each column
    y_entry = np.full(nx, -1)
    for i in np.nonzero(y_top >= 0)[0]:
        col = mask[i, : y_top[i] + 1][::-1]
        gap = np.argmin(col) if not col.all() else len(col)
        y_entry[i] = y_top[i] - gap + 1
    wi = int(math.ceil(spec.stem_half_width / p))
    ytop_f = np.where(y_top >= 0, y_top, -10**9).astype(float)
    ylow = ndimage.minimum_filter1d(ytop_f, 2 * wi + 1, mode="constant", cval=-10**9)
    yhigh = ndimage.maximum_filter1d(ytop_f, 2 * wi + 1, mode="constant", cval=-10**9)
    yent = ndimage.maximum_filter1d(
        np.where(y_entry >= 0, y_entry, 10**9).astype(float), 2 * wi + 1, mode="constant",
        cval=10**9,
    )
    emb = spec.embed_mm / p
    yb = ylow - emb
    valid = (ylow > -1) & (yent <= yb)
    if not valid.any():
        return None
    cand = []
    for i in np.nonzero(valid)[0]:
        jb = int(max(0, math.floor(yb[i])))
        # Chord of the outline one mm below the stem bottom: a thin ear or tail is narrow there.
        row = mask[:, max(0, jb - int(round(1.0 / p)))]
        lo_i = i
        while lo_i > 0 and row[lo_i - 1]:
            lo_i -= 1
        hi_i = i
        while hi_i < nx - 1 and row[hi_i + 1]:
            hi_i += 1
        w = (hi_i - lo_i + 1) * p if row[i] else 0.0
        if w < min_width_mm:
            continue
        cols = slice(max(0, i - wi), min(nx, i + wi + 1))
        th = height[cols, jb : int(ylow[i]) + 1]
        th = float(th[np.isfinite(th)].min()) if np.isfinite(th).any() else 0.0
        score = ylow[i] - 0.5 * (yhigh[i] - ylow[i])
        cand.append((score, -abs(xs[i] - com_x), i, w, th))
    if not cand:
        return None
    best = max(c[0] for c in cand)
    near = [c for c in cand if c[0] >= best - 0.75 / p]
    _, _, i, w, th = max(near, key=lambda c: c[1])
    return Anchor(
        float(xs[i]), float(ys[int(yhigh[i])]), float(ys[int(yhigh[i])]), float(ys[int(ylow[i])]),
        th, len(cand), float(w),
    )


# ------------------------------------------------------------------ flat back


def flat_back(
    figure: Manifold, thickness_mm: float, rim_mm: float = 1.0, layer_mm: float = 0.2,
) -> tuple[Manifold, CrossSection]:
    """Backing for a figure lying face up: a slab plus a filler that makes the product a height
    field, so no layer overhangs the one below and nothing needs supports.

    Slab: the figure's XY shadow grown by rim_mm, from z = 0 to thickness_mm (3.0 min).
    Filler: one layer-tall slice per layer from the slab top up, each the shadow of the figure
    above the slice's top. It sits inside the figure almost everywhere (the figure part comes
    first in the object, so its paint wins overlaps) and fills only real gaps under the figure:
    its underside, and pockets under a chin or ear. It stays below the figure's top surface.
    Returns (solid, outline section).
    """
    shadow = figure.project()
    outline = shadow.offset(rim_mm, JoinType.Round, circular_segments=SEG) if rim_mm > 0 else shadow
    slab = Manifold.extrude(outline, thickness_mm)
    if rim_mm >= 0.8:
        step = min(0.4, rim_mm / 2)
        inner = outline.offset(-step, JoinType.Round, circular_segments=SEG)
        slab = Manifold.extrude(outline, thickness_mm - step) + Manifold.extrude(
            inner, thickness_mm
        )
    z_top = figure.bounding_box()[5]
    pieces = [slab]
    z = thickness_mm
    while z + layer_mm < z_top:
        above = figure.trim_by_plane((0.0, 0.0, 1.0), z + layer_mm + 0.02)  # stay under skin
        if above.is_empty():
            break
        sec = above.project()
        if sec.is_empty() or sec.area() < 1e-3:
            break
        pieces.append(Manifold.extrude(sec, layer_mm + 1e-3).translate((0, 0, z)))
        z += layer_mm
    return Manifold.batch_boolean(pieces, OpType.Add), outline


# ------------------------------------------------------------------ magnet pocket


@dataclass
class PocketSpec:
    magnet_d_mm: float = 10.0
    magnet_h_mm: float = 2.0
    clearance_d_mm: float = 0.3  # diameter clearance for a glue-in fit
    clearance_h_mm: float = 0.2
    wall_mm: float = 1.5  # to the outline edge
    roof_mm: float = 0.8  # above the pocket (4 layers)

    @property
    def d(self) -> float:
        return self.magnet_d_mm + self.clearance_d_mm

    @property
    def depth(self) -> float:
        return self.magnet_h_mm + self.clearance_h_mm


def magnet_pocket_center(outline, spec: PocketSpec, prefer_xy) -> tuple[float, float] | None:
    """Point inside the outline (shapely) at least wall_mm from its edge, nearest prefer_xy."""
    from shapely.geometry import Point
    from shapely.ops import nearest_points

    allowed = outline.buffer(-(spec.d / 2 + spec.wall_mm))
    if allowed.is_empty:
        return None
    p = Point(prefer_xy)
    if allowed.contains(p):
        return (p.x, p.y)
    q = nearest_points(allowed, p)[0]
    return (q.x, q.y)


def magnet_pocket(
    target: Manifold, center_xy: tuple[float, float], spec: PocketSpec
) -> tuple[Manifold, Manifold]:
    """Cut an open-back pocket (from z = 0 up) into `target`. Returns (cut target, cutter).

    The pocket opens on the bed side so the magnet is glued in after printing; the roof bridges.
    A 0.3 mm entry chamfer keeps the first layer from closing the pocket (elephant foot).
    """
    r = spec.d / 2
    cyl = Manifold.cylinder(spec.depth + 1.0, r, r, SEG).translate((0, 0, -1.0))
    lip = Manifold.cylinder(0.31, r + 0.3, r, SEG).translate((0, 0, -0.01))
    cutter = (cyl + lip).translate((center_xy[0], center_xy[1], 0))
    return target - cutter, cutter
