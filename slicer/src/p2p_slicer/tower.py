"""Prime tower (sections 7.6 and 7.7 of docs/research/native-slicer.md), as Bambu Studio
02.08.02.61 builds it on the X2D. Written from the spec and from Bambu's sliced G-code of the
calibration cases, in our own code.

Geometry (ASSUMED unless marked SOURCE):
- The tower is a W x D rectangle (wall centreline) whose lower left corner is (x, y). Two ribs
  of `prime_tower_rib_width` (8) run along its diagonals from corner to corner, so each corner
  grows an "ear": the rib's square end, centred on the corner.
- Every corner of that outline is rounded: cut back 2 mm along both edges (at most 47.5 percent
  of the shorter adjacent edge) and joined by the tangent arc. This is what Bambu's G-code
  shows: 2 mm arcs on square corners, a 1.8 mm one where the short rib side of an ear limits it
  (21.5 x 22.75 tower), concave arcs of about 4.2 and 5.0 mm where a rib meets the rectangle,
  and the brim loops' bounding box (plate_1.json) within 0.003 mm on all cases.
- The wall is printed once per layer, counter-clockwise, with a 2.5 mm gap (skip point) where
  each prime block of the layer enters the tower from the side. Layer 0 adds brim loops at
  the layer 0 line spacing: round(min(8, 8 H / 100) / spacing) loops, H = top of the last
  tower layer (SOURCE: spec 7.6 plus loop counts on all cases). Layers 1 to 5 keep
  min(loops, 6) - layer of them (the chamfer).
- Blocks: every change primes the new filament (15 mm3 in Saving mode, else the filament's
  prime volume) in lines of W - 1.0 at a 0.75 pitch, and on a nozzle switch first rams the old
  filament (`filament_change_length`) in 1.0 mm lines of W - 1.5 at 1.25 pitch. Rammings go to
  the old filament's adhesion category block and primes to the new one's; category blocks
  stack from the bottom in ascending category, each as deep as its deepest layer. Depth D =
  sum of blocks + 0.5, W = ceil to 0.5 of sqrt(D0 x W0) where D0 is the depth at W0, then D is
  re-planned at W (SOURCE: spec 7.6, checked on 19 cases).
- Unused depth of a block is filled with a sparse grid: a box plus vertical lines about 10 mm
  apart (layer 0: horizontal lines at about 0.5 mm). The wall and grids are printed by the
  wall filament: the filament used on the most layers (ties: lowest slot).

Line cross-sections are Bambu's rounded rectangles, h (w - h (1 - pi/4)); tower E per mm is
that over the filament cross-section, without the filament flow ratio (SOURCE: Bambu G-code).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .moves import Flag, Kind, MoveTable, PathWriter, Role

# ----------------------------------------------------------------------------- constants

FILAMENT_AREA = math.pi / 4 * 1.75 ** 2
LINE_W = 0.5                 # tower line width, 1.25 x nozzle (SOURCE: params json)
RAM_W = 1.0                  # ramming line width on a 0.4 nozzle (SOURCE: params json)
RAM_PITCH = 1.25             # ramming line pitch (ASSUMED from G-code)
PRIME_PITCH_L0 = 0.5         # layer 0 prime pitch (ASSUMED from G-code)
RAM_PITCH_L0 = 1.0           # layer 0 ramming pitch (ASSUMED from G-code)
FILLET = 2.0                 # convex fillet radius (SOURCE: params json)
FILLET_EDGE_SHARE = 0.475    # a fillet takes at most this share of an adjacent edge (ASSUMED)
SKIP_GAP = 2.5               # wall gap at each prime entry (SOURCE: params json)
GRID_SPACING = 10.0          # sparse grid line spacing (SOURCE: params json)
GRID_PITCH_L0 = 0.5          # layer 0 grid line pitch (ASSUMED from G-code)
CHAMFER_LOOPS = 6            # loops of the 3 mm chamfer (ASSUMED: floor(3 / 0.457))
PRIME_RAMP = (0.33, 0.375, 0.458, 0.875)   # prime line speed ramp (SOURCE: params json)
PRIME_RAMP_STEP = 50.0       # mm/min added once after the ramp (ASSUMED from G-code)
APPROACH = 2.0               # prime approach point: this far outside the tower bbox (ASSUMED)
PRIME_LEAD = 3.0             # first extrusion into the tower before the nozzle wipe (ASSUMED)
NOZZLE_WIPE = ((0.5, 0.0), (0.5, 0.5), (-0.5, 0.5), (-0.5, -0.5), (1.0, -0.5), (1.0, 1.0),
               (-1.0, 1.0), (-1.0, -1.0))   # square spiral around the prime start (ASSUMED)
SPIRAL_SLOPE = math.atan(math.pi / 60.0)     # spiral lift slope (as in api.py)
WIPE_DIST = 2.0              # wipe after a tower path (ASSUMED from G-code)
Z_HOP = 0.4


def area(w: float, h: float) -> float:
    """Cross-section of an extrusion line of width w and height h (Bambu's rounded rectangle)."""
    return h * (w - h * (1.0 - math.pi / 4.0))


def e_per_mm(w: float, h: float) -> float:
    return area(w, h) / FILAMENT_AREA


def spacing(w: float, h: float) -> float:
    """Centre distance of touching lines."""
    return w - h * (1.0 - math.pi / 4.0)


# ----------------------------------------------------------------------------- geometry


@dataclass
class Seg:
    """One piece of a wall loop: a line, or an arc (centre, radius, start and end angle, ccw)."""

    x0: float
    y0: float
    x1: float
    y1: float
    cx: float = math.nan
    cy: float = math.nan
    r: float = 0.0
    a0: float = 0.0
    a1: float = 0.0
    ccw: bool = True

    @property
    def is_arc(self) -> bool:
        return self.r > 0.0

    @property
    def length(self) -> float:
        if self.is_arc:
            return self.r * abs(self.a1 - self.a0)
        return math.hypot(self.x1 - self.x0, self.y1 - self.y0)

    def point(self, t: float) -> tuple[float, float]:
        """Point at arc length t from the start."""
        if self.is_arc:
            a = self.a0 + (t / self.r) * (1.0 if self.ccw else -1.0)
            return self.cx + self.r * math.cos(a), self.cy + self.r * math.sin(a)
        L = self.length
        u = t / L if L > 0 else 0.0
        return self.x0 + u * (self.x1 - self.x0), self.y0 + u * (self.y1 - self.y0)

    def sub(self, t0: float, t1: float) -> Seg:
        """The piece between arc lengths t0 and t1."""
        (xa, ya), (xb, yb) = self.point(t0), self.point(t1)
        if self.is_arc:
            s = 1.0 if self.ccw else -1.0
            return Seg(xa, ya, xb, yb, self.cx, self.cy, self.r, self.a0 + s * t0 / self.r,
                       self.a0 + s * t1 / self.r, self.ccw)
        return Seg(xa, ya, xb, yb)

    def extent(self) -> tuple[float, float, float, float]:
        if not self.is_arc:
            return (min(self.x0, self.x1), min(self.y0, self.y1), max(self.x0, self.x1),
                    max(self.y0, self.y1))
        xs = [self.x0, self.x1]
        ys = [self.y0, self.y1]
        lo, hi = sorted((self.a0, self.a1))
        k0 = math.ceil(lo / (math.pi / 2))
        k1 = math.floor(hi / (math.pi / 2))
        for k in range(k0, k1 + 1):
            a = k * math.pi / 2
            xs.append(self.cx + self.r * math.cos(a))
            ys.append(self.cy + self.r * math.sin(a))
        return min(xs), min(ys), max(xs), max(ys)


def _line_isect(p: np.ndarray, d: np.ndarray, q: np.ndarray, e: np.ndarray) -> np.ndarray:
    """Intersection of lines p + s d and q + t e."""
    den = d[0] * e[1] - d[1] * e[0]
    s = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / den
    return p + s * d


def rib_outline(W: float, D: float, rib_width: float = 8.0, extra: float = 0.0,
                rib_wall: bool = True, rect_y0: float = 0.0,
                rect_h: float | None = None) -> np.ndarray:
    """Vertices (CCW, local frame: the tower rectangle [0, W] x [0, D]) of the layer's wall
    rectangle [0, W] x [rect_y0, rect_y0 + rect_h] unioned with the two ribs, which run along
    the full tower's diagonals from corner to corner (plus `extra`). Collinear vertices are
    dropped. Without ribs: the rectangle."""
    import pyclipper
    rh = D if rect_h is None else rect_h
    rect = np.array([[0.0, rect_y0], [W, rect_y0], [W, rect_y0 + rh], [0.0, rect_y0 + rh]])
    if not rib_wall:
        return rect
    c = np.array([W / 2, D / 2])
    diag = math.hypot(W, D)
    half = diag / 2 + extra
    h = min(rib_width, min(W, D) / 2) / 2
    polys = [rect]
    for C in (np.array([W, D]), np.array([0.0, D])):
        u = (C - c) / np.linalg.norm(C - c)
        n = np.array([-u[1], u[0]])
        polys.append(np.array([c - u * half - n * h, c + u * half - n * h,
                               c + u * half + n * h, c - u * half + n * h]))
    scale = 1e6
    pc = pyclipper.Pyclipper()
    for q in polys:
        qi = [(round(float(x) * scale), round(float(y) * scale)) for x, y in q]
        if not pyclipper.Orientation(qi):
            qi = qi[::-1]
        pc.AddPath(qi, pyclipper.PT_SUBJECT, True)
    res = pc.Execute(pyclipper.CT_UNION, pyclipper.PFT_NONZERO, pyclipper.PFT_NONZERO)
    outer = max(res, key=lambda r: abs(pyclipper.Area(r)))
    if not pyclipper.Orientation(outer):
        outer = outer[::-1]
    pts = np.array(outer, dtype=float) / scale
    # drop collinear and duplicate vertices
    keep = []
    n = len(pts)
    for i in range(n):
        a, b, cc = pts[i - 1], pts[i], pts[(i + 1) % n]
        d1, d2 = b - a, cc - b
        if np.linalg.norm(d1) < 1e-6:
            continue
        cross = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(cross) < 1e-9 * max(1.0, np.linalg.norm(d1) * np.linalg.norm(d2)) and \
                float(d1 @ d2) > 0:
            continue
        keep.append(b)
    return np.array(keep)


@dataclass
class Corner:
    v: np.ndarray        # unrounded vertex
    din: np.ndarray      # unit direction of the incoming edge
    dout: np.ndarray     # unit direction of the outgoing edge
    turn: float          # signed turn angle (ccw positive)
    rho: float           # fillet radius at offset 0 (0 = sharp)


def round_corners(pts: np.ndarray, fillet: float = FILLET,
                  share: float = FILLET_EDGE_SHARE) -> list[Corner]:
    """Fillet radii for a CCW polygon. Every corner, convex or concave, is cut back `fillet` mm
    along both edges (at most `share` of the shorter adjacent edge) and joined by the tangent
    arc, so the radius is that tangent length over tan(turn / 2)."""
    n = len(pts)
    out = []
    for i in range(n):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
        din = (b - a) / np.linalg.norm(b - a)
        dout = (c - b) / np.linalg.norm(c - b)
        turn = math.atan2(din[0] * dout[1] - din[1] * dout[0], float(din @ dout))
        tan_half = math.tan(abs(turn) / 2)
        if tan_half < 1e-9:
            out.append(Corner(b, din, dout, turn, 0.0))
            continue
        lim = share * min(float(np.linalg.norm(b - a)), float(np.linalg.norm(c - b)))
        t = min(fillet, lim)
        out.append(Corner(b, din, dout, turn, t / tan_half))
    return out


def loop_segments(corners: list[Corner], d: float = 0.0) -> list[Seg]:
    """The rounded outline offset outward by d, as lines and arcs (CCW), starting at the first
    corner's outgoing tangent point."""
    n = len(corners)
    # offset lines: point on the line and direction, for the edge from corner i to i+1
    verts = []
    for i in range(n):
        cn = corners[i]
        # edge into corner i: from corner i-1 along cn.din; outward normal = right of direction
        e1 = cn.din
        e2 = cn.dout
        n1 = np.array([e1[1], -e1[0]])
        n2 = np.array([e2[1], -e2[0]])
        v = _line_isect(cn.v + d * n1, e1, cn.v + d * n2, e2) if abs(cn.turn) > 1e-9 \
            else cn.v + d * n1
        verts.append(v)
    pieces: list[tuple[np.ndarray, np.ndarray, Seg | None]] = []   # (tangent in, tangent out, arc)
    for i, cn in enumerate(corners):
        v = verts[i]
        if abs(cn.turn) < 1e-9:
            pieces.append((v, v, None))
            continue
        rho = cn.rho + d if cn.turn > 0 else cn.rho - d
        if rho <= 1e-9:
            pieces.append((v, v, None))
            continue
        t = rho * math.tan(abs(cn.turn) / 2)
        p_in = v - cn.din * t
        p_out = v + cn.dout * t
        left_in = np.array([-cn.din[1], cn.din[0]])
        ctr = p_in + left_in * (rho if cn.turn > 0 else -rho)
        a0 = math.atan2(p_in[1] - ctr[1], p_in[0] - ctr[0])
        a1 = math.atan2(p_out[1] - ctr[1], p_out[0] - ctr[0])
        ccw = cn.turn > 0
        if ccw and a1 < a0:
            a1 += 2 * math.pi
        if not ccw and a1 > a0:
            a1 -= 2 * math.pi
        pieces.append((p_in, p_out,
                       Seg(float(p_in[0]), float(p_in[1]), float(p_out[0]), float(p_out[1]),
                           float(ctr[0]), float(ctr[1]), rho, a0, a1, ccw)))
    segs: list[Seg] = []
    for i in range(n):
        _, p_out, arc = pieces[i]
        nxt_in = pieces[(i + 1) % n][0]
        if arc is not None:
            segs.append(arc)
        if float(np.linalg.norm(nxt_in - p_out)) > 1e-9:
            segs.append(Seg(float(p_out[0]), float(p_out[1]), float(nxt_in[0]),
                            float(nxt_in[1])))
    return segs


def loop_extent(segs: Sequence[Seg]) -> tuple[float, float, float, float]:
    ex = [s.extent() for s in segs]
    return (min(e[0] for e in ex), min(e[1] for e in ex), max(e[2] for e in ex),
            max(e[3] for e in ex))


def loop_length(segs: Sequence[Seg]) -> float:
    return sum(s.length for s in segs)


def translate(segs: Sequence[Seg], dx: float, dy: float) -> list[Seg]:
    return [Seg(s.x0 + dx, s.y0 + dy, s.x1 + dx, s.y1 + dy, s.cx + dx, s.cy + dy, s.r, s.a0,
                s.a1, s.ccw) for s in segs]


def _samples(segs: Sequence[Seg], step: float) -> list[tuple[np.ndarray, np.ndarray]]:
    """Per segment: arc-length params t (np.linspace(0, L, max(2, L // step + 1))) and the
    points there, vectorised."""
    out = []
    for sg in segs:
        L = sg.length
        n = max(2, int(L / step) + 1)
        t = np.linspace(0.0, L, n)
        if sg.is_arc:
            a = sg.a0 + (t / sg.r) * (1.0 if sg.ccw else -1.0)
            P = np.stack([sg.cx + sg.r * np.cos(a), sg.cy + sg.r * np.sin(a)], axis=1)
        else:
            u = t / L if L > 0 else np.zeros_like(t)
            P = np.stack([sg.x0 + u * (sg.x1 - sg.x0), sg.y0 + u * (sg.y1 - sg.y0)], axis=1)
        out.append((t, P))
    return out


def _nearest_arclen(segs: Sequence[Seg], p: tuple[float, float], step: float = 0.02) -> float:
    """Arc length along the loop of the sample (every `step` mm) nearest p."""
    starts = np.cumsum([0.0] + [sg.length for sg in segs])
    best, bt = 1e18, 0.0
    for k, (t, P) in enumerate(_samples(segs, step)):
        d = (P[:, 0] - p[0]) ** 2 + (P[:, 1] - p[1]) ** 2
        i = int(np.argmin(d))
        if d[i] < best:
            best, bt = float(d[i]), float(starts[k] + t[i])
    return bt


def split_at_gaps(segs: list[Seg], centres: list[tuple[float, float]], gap: float = SKIP_GAP,
                  ) -> list[list[Seg]]:
    """Cut `gap` mm of path centred at each of `centres` (points on the loop) out of a closed
    loop. Returns the printed pieces in loop order from the loop's start (or from the end of
    the gap that covers the start). Without centres: one piece, the whole loop."""
    total = loop_length(segs)
    if not centres or total <= 0:
        return [list(segs)]
    starts = np.cumsum([0.0] + [s.length for s in segs])

    def arclen(p: tuple[float, float]) -> float:
        return _nearest_arclen(segs, p)

    # cut intervals on [0, total), wrapping ones split in two
    cuts: list[tuple[float, float]] = []
    for c in centres:
        m = arclen(c)
        a, b = m - gap / 2, m + gap / 2
        if a < 0:
            cuts += [(a + total, total), (0.0, b)]
        elif b > total:
            cuts += [(a, total), (0.0, b - total)]
        else:
            cuts.append((a, b))
    cuts.sort()
    kept: list[tuple[float, float]] = []
    t = 0.0
    for a, b in cuts:
        if a - t > 1e-6:
            kept.append((t, a))
        t = max(t, b)
    if total - t > 1e-6:
        kept.append((t, total))
    def piece(t0: float, t1: float) -> list[Seg]:
        out = []
        for k, s in enumerate(segs):
            s0, s1 = starts[k], starts[k + 1]
            lo, hi = max(t0, s0), min(t1, s1)
            if hi - lo > 1e-6:
                out.append(s.sub(lo - s0, hi - s0))
        return out

    return [piece(a, b) for a, b in kept]


def rotate_loop(segs: list[Seg], near: tuple[float, float],
                candidates: list[tuple[float, float]] | None = None) -> list[Seg]:
    """Closed loop starting at the candidate point (default: segment starts) nearest `near`."""
    pts = candidates or [(s.x0, s.y0) for s in segs]
    tgt = min(pts, key=lambda p: (p[0] - near[0]) ** 2 + (p[1] - near[1]) ** 2)
    total = loop_length(segs)
    starts = np.cumsum([0.0] + [s.length for s in segs])
    bt = _nearest_arclen(segs, tgt)
    out = []
    for k, s in enumerate(segs):
        for off in (0.0, total):
            s0, s1 = starts[k] + off, starts[k + 1] + off
            lo, hi = max(bt, s0), min(bt + total, s1)
            if hi - lo > 1e-6:
                out.append((lo, s.sub(lo - s0, hi - s0)))
    out.sort(key=lambda p: p[0])
    return [s for _, s in out]


def loop_point_at_y(segs: Sequence[Seg], y: float, right: bool) -> tuple[float, float]:
    """Where the horizontal line at y meets the loop on the right (or left) half."""
    xs = []
    for _, P in _samples(segs, 0.01):
        a, b = P[:-1], P[1:]
        m = ((a[:, 1] - y) * (b[:, 1] - y) <= 0) & (b[:, 1] != a[:, 1])
        if m.any():
            u = (y - a[m, 1]) / (b[m, 1] - a[m, 1])
            xs.append(a[m, 0] + u * (b[m, 0] - a[m, 0]))
    if not xs:
        return (0.0, y)
    x = np.concatenate(xs)
    return (float(x.max() if right else x.min()), y)


# ----------------------------------------------------------------------------- settings


@dataclass
class TowerSettings:
    """What the tower reads from the resolved config."""

    w0: float
    rib_wall: bool
    rib_width: float
    extra_rib: float
    fillet_wall: bool
    skip_points: bool
    max_speed: float                  # mm/s
    brim_width: float                 # < 0: auto
    infill_gap: float                 # prime line pitch / line width (1.5)
    saving: bool
    wtx: float
    wty: float
    nozzle_of: dict[int, int]
    prime_mm3: dict[int, float]
    change_len: dict[int, float]      # filament_change_length, mm of filament
    max_vol: dict[int, float]
    category: dict[int, int]
    retract_len: dict[int, float]
    retract_speed: dict[int, float]
    deretract_speed: dict[int, float]
    toolchange_retract: dict[int, float]   # per nozzle
    accel: dict[int, float]           # print accel per nozzle, layers >= 1
    accel_l0: dict[int, float]
    travel_accel: dict[int, float]
    travel_accel_l0: dict[int, float]
    first_layer_speed: dict[int, float]
    travel_speed: dict[int, float]
    z_hop: dict[int, float]


def tower_settings(cfg: Any) -> TowerSettings:
    n_fil = max(cfg.n_filaments, len(cfg.filament_map))
    slots = range(1, n_fil + 1)

    def per_slot(key: str, default: float) -> dict[int, float]:
        return {s: cfg.fil(key, s, default) for s in slots}

    def per_nozzle(key: str, default: float) -> dict[int, float]:
        return {n: cfg.nozzle(key, n, default) for n in (0, 1)}

    retr = {s: cfg.retraction(s) for s in slots}
    pv = {s: (15.0 if cfg.s("prime_volume_mode", "Saving") == "Saving"
              else cfg.fil("filament_prime_volume", s, 45.0)) for s in slots}
    cat = {s: int(cfg.fil("filament_adhesiveness_category", s, 100.0)) for s in slots}
    gap = cfg.pct_or_abs("prime_tower_infill_gap", 100.0) / 100.0
    return TowerSettings(
        w0=cfg.f("prime_tower_width", 35.0),
        rib_wall=cfg.i("prime_tower_rib_wall", 1) == 1,
        rib_width=cfg.f("prime_tower_rib_width", 8.0),
        extra_rib=cfg.f("prime_tower_extra_rib_length", 0.0),
        fillet_wall=cfg.i("prime_tower_fillet_wall", 1) == 1,
        skip_points=cfg.i("prime_tower_skip_points", 1) == 1,
        max_speed=cfg.f("prime_tower_max_speed", 90.0),
        brim_width=cfg.f("prime_tower_brim_width", -1.0),
        infill_gap=gap if not math.isnan(gap) and gap > 0 else 1.5,
        saving=cfg.s("prime_volume_mode", "Saving") == "Saving",
        wtx=cfg.f("wipe_tower_x", 15.0), wty=cfg.f("wipe_tower_y", 220.0),
        nozzle_of={s: cfg.nozzle_of(s) for s in slots},
        prime_mm3=pv,
        change_len=per_slot("filament_change_length", 10.0),
        max_vol=per_slot("filament_max_volumetric_speed", 12.0),
        category=cat,
        retract_len={s: retr[s]["length"] for s in slots},
        retract_speed={s: retr[s]["speed"] for s in slots},
        deretract_speed={s: retr[s]["deretraction_speed"] for s in slots},
        toolchange_retract=per_nozzle("retract_length_toolchange", 2.0),
        accel=per_nozzle("default_acceleration", 10000.0),
        accel_l0=per_nozzle("initial_layer_acceleration", 500.0),
        travel_accel=per_nozzle("travel_acceleration", 10000.0),
        travel_accel_l0=per_nozzle("initial_layer_travel_acceleration", 6000.0),
        first_layer_speed=per_nozzle("initial_layer_speed", 50.0),
        travel_speed=per_nozzle("travel_speed", 1000.0),
        z_hop=per_nozzle("z_hop", Z_HOP),
    )


# ----------------------------------------------------------------------------- plan


@dataclass
class Item:
    """A ramming or prime band on one layer: the region [y0, y0 + depth] of its block."""

    kind: str            # "ram" or "prime"
    change: Any
    cat: int
    y0: float
    depth: float
    lines: int           # nominal line count (layers >= 1)


@dataclass
class LayerTower:
    layer: int
    z: float
    h: float
    changes: list[Any]
    items: list[Item]
    grids: dict[int, tuple[float, float]]    # category -> unused region [y0, y1] (absolute)
    last_prime_of: dict[int, int]            # category -> index of the change with its last prime
    shift: float = 0.0       # the layer's content and wall rectangle sit this much higher
    rect_h: float = 0.0      # height of the layer's wall rectangle (the ribs keep the full D)


@dataclass
class TowerPlan:
    enabled: bool
    x: float = 0.0             # wall rectangle origin (centreline), mm
    y: float = 0.0
    width: float = 0.0
    depth: float = 0.0
    brim: float = 0.0          # brim width setting in effect, mm
    brim_loops: int = 0
    last_layer: int = -1
    height: float = 0.0        # top of the last tower layer
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)   # with brim (plate_1.json)
    wall_slot: int = 0
    blocks: list[tuple[int, float, float]] = field(default_factory=list)  # (category, y0, depth)
    layers: list[LayerTower] = field(default_factory=list)
    corners: list[Corner] = field(default_factory=list)
    settings: TowerSettings | None = None
    spacing0: float = 0.0      # layer 0 line spacing (brim loops)
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def x1(self) -> float:
        return self.x + self.width

    @property
    def y1(self) -> float:
        return self.y + self.depth

    def layer_corners(self, layer: int) -> list[Corner]:
        """Rounded wall outline of a layer (local frame), cached by its rectangle."""
        lt = self.layers[layer]
        key = (round(lt.shift, 6), round(lt.rect_h, 6))
        cache = self.state.setdefault("outlines", {})
        if key not in cache:
            st = self.settings
            if key == (0.0, round(self.depth, 6)):
                cache[key] = self.corners
            else:
                cache[key] = round_corners(
                    rib_outline(self.width, self.depth, st.rib_width, st.extra_rib, st.rib_wall,
                                lt.shift, lt.rect_h), FILLET if st.fillet_wall else 0.0)
        return cache[key]

    def loop(self, k: int = 0, layer: int = 0) -> list[Seg]:
        """Wall loop k of a layer (0 = the wall, k >= 1 = brim or chamfer loops), absolute
        coordinates."""
        return translate(loop_segments(self.layer_corners(layer), k * self.spacing0), self.x,
                         self.y)


def _ramming_mm(c: Any, st: TowerSettings) -> float:
    if not c.nozzle_switch:
        return 0.0
    r = float(getattr(c, "ramming_mm", 0.0) or 0.0)
    return r if r > 0 else st.change_len.get(c.old, 10.0)


def _prime_mm3(c: Any, st: TowerSettings) -> float:
    p = float(getattr(c, "prime_mm3", 0.0) or 0.0)
    return p if p > 0 else st.prime_mm3.get(c.new, 15.0)


def _item_depths(c: Any, W: float, h: float, st: TowerSettings) -> tuple[int, int]:
    """(ramming lines, prime lines) of a change at width W and layer height h."""
    n_r = 0
    rm = _ramming_mm(c, st)
    if rm > 0:
        n_r = math.ceil(rm * FILAMENT_AREA / area(RAM_W, h) / (W - LINE_W - RAM_W) - 1e-9)
    n_p = math.ceil(_prime_mm3(c, st) / area(LINE_W, h) / (W - 2 * LINE_W) - 1e-9)
    return n_r, n_p


def plan_tower(changes: list[Any], plan: Any, cfg: Any, *,
               layer_slots: Sequence[set[int] | Sequence[int]] | None = None) -> TowerPlan:
    """Size and place the tower and lay out every layer's blocks.

    `changes`: all tool changes to a different filament after the first load (purge.Change),
    in print order. `layer_slots`: per layer, the filaments that print on it (for the wall
    filament); without it the filaments seen in the changes are counted instead."""
    st = tower_settings(cfg)
    chs = sorted((c for c in changes if c.old > 0 and c.old != c.new),
                 key=lambda c: (c.layer, c.order))
    if not chs or cfg.i("enable_prime_tower", 1) != 1:
        return TowerPlan(enabled=False, settings=st)
    last = max(c.layer for c in chs)
    heights = np.asarray(plan.height, dtype=float)
    tops = np.asarray(plan.print_z, dtype=float)
    by_layer: dict[int, list[Any]] = {}
    for c in chs:
        by_layer.setdefault(c.layer, []).append(c)
    pitch = LINE_W * st.infill_gap

    def layout(W: float) -> dict[int, dict[int, float]]:
        per: dict[int, dict[int, float]] = {}
        for L, cl in by_layer.items():
            h = float(heights[L])
            d: dict[int, float] = {}
            for c in cl:
                n_r, n_p = _item_depths(c, W, h, st)
                if n_r:
                    k = st.category.get(c.old, 100)
                    d[k] = d.get(k, 0.0) + n_r * RAM_PITCH
                k = st.category.get(c.new, 100)
                d[k] = d.get(k, 0.0) + n_p * pitch
            per[L] = d
        return per

    def blocks_of(per: dict[int, dict[int, float]]) -> dict[int, float]:
        out: dict[int, float] = {}
        for d in per.values():
            for k, v in d.items():
                out[k] = max(out.get(k, 0.0), v)
        return out

    D0 = sum(blocks_of(layout(st.w0)).values()) + LINE_W
    W = math.ceil(math.sqrt(D0 * st.w0) * 2.0 - 1e-9) / 2.0
    per = layout(W)
    blk = blocks_of(per)
    D = sum(blk.values()) + LINE_W

    corners = round_corners(rib_outline(W, D, st.rib_width, st.extra_rib, st.rib_wall),
                            FILLET if st.fillet_wall else 0.0)
    e0 = loop_extent(loop_segments(corners, 0.0))
    x0 = st.wtx - e0[0]
    y0 = st.wty - e0[1]
    h0 = float(heights[0])
    s0 = spacing(LINE_W, h0)
    H = float(tops[last])
    brim = st.brim_width if st.brim_width >= 0 else min(8.0, 8.0 * H / 100.0)
    n_brim = math.floor(brim / s0 + 0.5)
    eb = loop_extent(loop_segments(corners, n_brim * s0))
    bbox = (x0 + eb[0], y0 + eb[1], x0 + eb[2], y0 + eb[3])

    # blocks bottom to top in ascending category
    base = y0 + LINE_W / 2 - (pitch - LINE_W) / 2
    blocks = []
    yb = base
    for k in sorted(blk):
        blocks.append((k, yb, blk[k]))
        yb += blk[k]
    size_of = {k: d for k, _, d in blocks}

    # wall filament: the one used on the most layers, ties to the lowest slot
    counts: dict[int, int] = {}
    if layer_slots is not None:
        for L, ss in enumerate(layer_slots):
            for s in set(ss):
                counts[s] = counts.get(s, 0) + 1
    else:
        seen: dict[int, set[int]] = {}
        for c in chs:
            seen.setdefault(c.layer, set()).update((c.old, c.new))
        for ss in seen.values():
            for s in ss:
                counts[s] = counts.get(s, 0) + 1
    wall_slot = min(counts, key=lambda s: (-counts[s], s)) if counts else chs[0].new

    # A single-category tower gets shallower where no layer above needs its full depth: the
    # layer's rectangle and content move up, the ribs stay (ASSUMED rule fitted on three cases:
    # shift 0.5 (floor(gap) + 1) for a gap D - 0.5 - P > 0, P the deepest layer at or above;
    # rectangle height P + 0.5 rounded up to 0.5).
    own = [sum(per.get(L, {}).values()) for L in range(last + 1)]
    prop = own[:]
    for L in range(last - 1, -1, -1):
        prop[L] = max(prop[L], prop[L + 1])
    layers = []
    for L in range(last + 1):
        cl = by_layer.get(L, [])
        h = float(heights[L])
        shift, rect_h = 0.0, D
        if len(blocks) == 1:
            gap = D - LINE_W - prop[L]
            if gap > 1e-6:
                shift = 0.5 * (math.floor(gap + 1e-6) + 1)
                rect_h = math.ceil((prop[L] + LINE_W) / 0.5 - 1e-6) * 0.5
        start_of = {k: y + shift for k, y, _ in blocks}
        used = {k: 0.0 for k in start_of}
        items = []
        last_prime: dict[int, int] = {}
        for i, c in enumerate(cl):
            n_r, n_p = _item_depths(c, W, h, st)
            if n_r:
                k = st.category.get(c.old, 100)
                items.append(Item("ram", c, k, start_of[k] + used[k], n_r * RAM_PITCH, n_r))
                used[k] += n_r * RAM_PITCH
            k = st.category.get(c.new, 100)
            items.append(Item("prime", c, k, start_of[k] + used[k], n_p * pitch, n_p))
            used[k] += n_p * pitch
            last_prime[k] = i
        top = {k: (start_of[k] + size_of[k] if len(blocks) > 1 else start_of[k] + prop[L])
               for k in start_of}
        grids = {k: (start_of[k] + used[k], top[k]) for k in start_of
                 if top[k] - start_of[k] - used[k] > 1e-6}
        layers.append(LayerTower(L, float(tops[L]), h, cl, items, grids, last_prime, shift,
                                 rect_h))

    return TowerPlan(enabled=True, x=x0, y=y0, width=W, depth=D, brim=brim, brim_loops=n_brim,
                     last_layer=last, height=H, bbox=bbox, wall_slot=wall_slot, blocks=blocks,
                     layers=layers, corners=corners, settings=st, spacing0=s0)


# ----------------------------------------------------------------------------- from G-code


def changes_from_moves(mt: MoveTable) -> list[Any]:
    """The tool changes of a read G-code (Bambu's or ours) as purge.Change records, for
    calibration: one per T to a different filament after the first load."""
    from .purge import Change
    r = mt.rows
    out: list[Any] = []
    per_layer: dict[int, int] = {}
    for k in np.nonzero(r["kind"] == Kind.TOOL_CHANGE)[0]:
        old = int(r["i"][k])
        new = int(r["slot"][k])
        if old <= 0 or old == new:
            continue
        L = int(r["layer"][k]) - 1
        fl = int(r["flags"][k])
        noz = mt.meta.get("nozzle_of", {})
        on, nn = int(noz.get(old, 0)), int(noz.get(new, 0))
        order = per_layer.get(L, 0)
        per_layer[L] = order + 1
        out.append(Change(layer=L, order=order, old=old, new=new, old_nozzle=on, new_nozzle=nn,
                          nozzle_switch=bool(fl & Flag.SWITCH) or on != nn,
                          ams_swap=bool(fl & Flag.AMS_SWAP), first_load=bool(fl & Flag.FIRST_LOAD),
                          held_old=int(r["p"][k])))
    return out


def layer_slots_from_moves(mt: MoveTable) -> list[set[int]]:
    """Per layer (0-based), the filaments that extrude outside the tower."""
    r = mt.rows
    m = (r["kind"] == Kind.EXTRUDE) & (r["role"] != Role.PRIME_TOWER) & (r["layer"] > 0)
    n = int(r["layer"].max()) if len(r) else 0
    out: list[set[int]] = [set() for _ in range(n)]
    for L, s in zip(r["layer"][m], r["slot"][m]):
        out[int(L) - 1].add(int(s))
    return out


# ----------------------------------------------------------------------------- layer order


def layer_events(tp: TowerPlan, layer: int, changes: Sequence[Any] | None = None,
                 slot: int | None = None) -> list[tuple]:
    """Bambu's order of tower blocks and filament turns on one tower layer:
    ("objects", slot), ("finish", slot) (a layer without changes: grids and wall, printed
    before the objects), ("wall", slot), ("grid", category, slot), ("change", change).

    The first tower visit of a layer comes at its first change: the wall first if the wall
    filament is printing, then grids of blocks that get no prime on this layer, then the change.
    After each change: the grid of its block if that was the block's last prime on the layer,
    then the wall if the new filament is the wall filament. A wall filament that is not used on
    a layer with changes: its wall goes after the last change (ASSUMED, never seen)."""
    if not tp.enabled or layer > tp.last_layer:
        return [("objects", slot)] if slot else []
    lt = tp.layers[layer]
    chs = list(changes) if changes is not None else list(lt.changes)
    if not chs:
        return [("finish", slot), ("objects", slot)]
    w = tp.wall_slot
    order = [chs[0].old] + [c.new for c in chs]
    ev: list[tuple] = [("objects", chs[0].old)]
    primed = {lt.items[i].cat for i in range(len(lt.items)) if lt.items[i].kind == "prime"}
    if w == chs[0].old:
        ev.append(("wall", w))
    for k in sorted(lt.grids):
        if k not in primed:
            ev.append(("grid", k, chs[0].old))
    wall_done = w == chs[0].old
    for i, c in enumerate(chs):
        ev.append(("change", c))
        k = tp.settings.category.get(c.new, 100) if tp.settings else 100
        if lt.last_prime_of.get(k) == i and k in lt.grids:
            ev.append(("grid", k, c.new))
        if c.new == w and not wall_done:
            ev.append(("wall", w))
            wall_done = True
        if i == len(chs) - 1 and not wall_done and w not in order:
            ev.append(("wall", c.new))
            wall_done = True
        ev.append(("objects", c.new))
    return ev


# ----------------------------------------------------------------------------- emission


def _r3(v: float) -> float:
    return round(v + 0.0, 3)


class _Out:
    """Writes one layer's tower rows through a PathWriter."""

    def __init__(self, tp: TowerPlan, layer: int, pw: PathWriter) -> None:
        self.tp = tp
        self.st: TowerSettings = tp.settings  # type: ignore[assignment]
        self.lt = tp.layers[layer]
        self.layer = layer
        self.pw = pw
        self.z = self.lt.z
        self.h = self.lt.h
        self.l0 = layer == 0
        self.e_line = e_per_mm(LINE_W, self.h)
        self.state = tp.state

    # -- per nozzle values
    def noz(self, slot: int) -> int:
        return self.st.nozzle_of.get(slot, 0)

    def acc_print(self, slot: int) -> float:
        n = self.noz(slot)
        return self.st.accel_l0[n] if self.l0 else self.st.accel[n]

    def acc_travel(self, slot: int) -> float:
        n = self.noz(slot)
        return self.st.travel_accel_l0[n] if self.l0 else self.st.travel_accel[n]

    def speed(self, slot: int) -> float:
        """mm/min of tower lines."""
        v = self.st.max_speed
        if self.l0:
            v = min(v, self.st.first_layer_speed[self.noz(slot)])
        return v * 60.0

    def hop(self, slot: int) -> float:
        return self.st.z_hop[self.noz(slot)]

    # -- flags
    def tower_on(self, on: bool = True) -> None:
        if on:
            self.pw.flags |= Flag.TOWER
        else:
            self.pw.flags &= ~Flag.TOWER

    # -- primitives
    def retract(self, slot: int, length: float | None = None) -> None:
        L = self.st.retract_len[slot] if length is None else length
        have = self.state.get("retracted", 0.0)
        if have >= L - 1e-9:
            return
        self.pw.retract(-(L - have), self.st.retract_speed[slot] * 60.0)
        self.state["retracted"] = L

    def unretract(self, slot: int, amount: float | None = None) -> None:
        have = self.state.get("retracted", 0.0)
        a = have if amount is None else amount
        if a > 0:
            self.pw.retract(a, self.st.deretract_speed[slot] * 60.0)
        self.state["retracted"] = 0.0

    def spiral(self, slot: int, f: float, i: float | None = None, j: float | None = None,
               towards: tuple[float, float] | None = None) -> None:
        hop = self.hop(slot)
        if hop <= 0:
            return
        R = round(hop / (2 * math.pi * SPIRAL_SLOPE), 3)
        if i is None or j is None:
            x, y, _ = self.pw.pos
            dx, dy = (towards[0] - x, towards[1] - y) if towards else (1.0, 0.0)
            dl = math.hypot(dx, dy) or 1.0
            i, j = round(-R * dy / dl, 3), round(R * dx / dl, 3)
        x, y, _ = self.pw.pos
        self.pw.arc(x, y, i, j, 0.0, cw=False, f=f, z=_r3(self.z + hop), p=1)

    def line(self, x: float, y: float, e_per: float, f: float | None = None) -> None:
        cx, cy, _ = self.pw.pos
        x, y = _r3(x), _r3(y)
        L = math.hypot(x - cx, y - cy)
        if L < 1e-9:
            return
        self.pw.extrude(x, y, L * e_per, f)

    def seg(self, s: Seg, e_per: float, f: float | None = None) -> None:
        cx, cy, _ = self.pw.pos
        if s.is_arc:
            x1, y1 = _r3(s.x1), _r3(s.y1)
            self.pw.arc(x1, y1, _r3(s.cx - cx), _r3(s.cy - cy), s.length * e_per,
                        cw=not s.ccw, f=f)
        else:
            self.line(s.x1, s.y1, e_per, f)

    def travel(self, x: float, y: float, f: float | None = None, z: float | None = None) -> None:
        self.pw.travel(_r3(x), _r3(y), None if z is None else _r3(z), f)

    # -- entering the tower
    def anchor(self) -> tuple[float, float]:
        """Where a wall or grid visit starts from: the current position within a visit, else
        Bambu's block start point: the top of the no-change box on the layer's prime side
        (the right side on layers without changes)."""
        if self.state.get("at_tower"):
            x, y, _ = self.pw.pos
            return x, y
        tp, lt = self.tp, self.lt
        left = bool(lt.changes) and ((self.layer + 1) // 2) % 2 == 0
        return (tp.x + LINE_W if left else tp.x1 - LINE_W), tp.y + lt.shift + lt.rect_h - 2 * LINE_W

    def enter(self, slot: int) -> None:
        """From the objects: retract, lift, travel to the anchor, drop, unretract (outside the
        tower block, in the objects' role). Within a visit: nothing (the block travels)."""
        pw = self.pw
        if self.state.get("at_tower"):
            return
        x, y = self.anchor()
        self.tower_on(False)
        self.retract(slot)
        pw.accel(self.acc_travel(slot))
        self.spiral(slot, self.st.travel_speed[self.noz(slot)] * 60.0, towards=(x, y))
        self.travel(x, y, self.st.travel_speed[self.noz(slot)] * 60.0, z=self.z + self.hop(slot))
        self.travel(x, y, z=self.z)
        self.unretract(slot)
        self.state["at_tower"] = True

    def leave(self, slot: int) -> None:
        """After the last tower block of a visit: wipe back along the last path, retracting
        (outside the tower block, in the tower's role)."""
        self.tower_on(False)
        path = self.state.get("last_path") or []
        L = self.st.retract_len[slot]
        if len(path) >= 2 and L > 0:
            pw = self.pw
            pw.wipe_start()
            pts = list(reversed(path))
            left = WIPE_DIST
            done = 0.0
            for a, b in itertools.pairwise(pts):
                d = math.hypot(b[0] - a[0], b[1] - a[1])
                if d <= 1e-9:
                    continue
                t = min(1.0, left / d)
                x, y = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
                de = 0.95 * L * (t * d) / WIPE_DIST
                pw.wipe(_r3(x), _r3(y), -de)
                done += de
                left -= t * d
                if left <= 1e-9:
                    break
            pw.wipe_end()
            self.state["retracted"] = done
        self.retract(slot)
        self.state["at_tower"] = False
        self.state["last_path"] = []

    # -- blocks
    def box_and_grid(self, slot: int, y0: float, y1: float, variant: str) -> None:
        """Sparse fill of [y0, y1]: box, then grid lines (layer 0: dense horizontal lines)."""
        tp, pw = self.tp, self.pw
        if variant == "nochange1":
            B, T = y0 + 0.375, y1 - 0.625
        elif variant == "nochange2":
            B, T = y0 + 0.875, y1 - 0.125
        else:
            B, T = y0 + 0.375, y1 - 0.125
        if T - B < 1e-6:
            return
        L, R = tp.x + LINE_W, tp.x1 - LINE_W
        f = self.speed(slot)
        cx = self.anchor()[0]
        right = abs(cx - R) < abs(cx - L)
        start = (R, T) if right else (L, T)
        self.enter(slot)
        pw.feature(Role.PRIME_TOWER, LINE_W)
        self.tower_on()
        pw.accel(self.acc_travel(slot))
        self.travel(*start, f=f)
        pw.accel(self.acc_print(slot))
        ring = [(L, T), (L, B), (R, B), (R, T)] if right else [(L, B), (R, B), (R, T), (L, T)]
        for p in ring:
            self.line(p[0], p[1], self.e_line, f)
        pw.accel(self.acc_travel(slot))
        self.travel(L + 1.0, B, f)
        if self.l0:
            pw.accel(self.acc_print(slot))
            lo, hi = B + GRID_PITCH_L0, T - GRID_PITCH_L0
            n = max(1, math.floor((hi - lo) / GRID_PITCH_L0 + 1e-9)) if hi > lo else 0
            ys = [lo + k * (hi - lo) / n for k in range(n + 1)] if n else [lo]
            xa, xb = L + GRID_PITCH_L0, R - GRID_PITCH_L0
            to_right = True
            for yv in ys:
                self.line(self.pw.pos[0], yv, self.e_line)
                self.line(xb if to_right else xa, yv, self.e_line)
                to_right = not to_right
            self.line(self.pw.pos[0], T, self.e_line)
        else:
            span = (R - 1.0) - (L + 1.0)
            n = max(1, math.ceil(span / GRID_SPACING - 1e-9))
            xs = [L + 1.0 + k * span / n for k in range(n + 1)]
            up = True
            for k, xv in enumerate(xs):
                if k:
                    pw.accel(self.acc_travel(slot))
                    self.travel(xv, T if not up else B)
                pw.accel(self.acc_print(slot))
                self.line(xv, T if up else B, self.e_line)
                up = not up
        x, y, _ = pw.pos
        self.state["last_path"] = [(x, B if y == T else T), (x, y)]

    def wall(self, slot: int, gaps: list[tuple[float, float]]) -> None:
        """The wall loop (with skip gaps) and this layer's brim or chamfer loops."""
        tp, pw = self.tp, self.pw
        f = self.speed(slot)
        loop0 = tp.loop(0, self.layer)
        corners = [(tp.x, tp.y), (tp.x1, tp.y), (tp.x1, tp.y1), (tp.x, tp.y1)]
        loop0 = rotate_loop(loop0, self.anchor(), corners)
        pieces = split_at_gaps(loop0, gaps if self.st.skip_points else [])
        start = (pieces[0][0].x0, pieces[0][0].y0)
        self.enter(slot)
        pw.feature(Role.PRIME_TOWER, LINE_W)
        self.tower_on()
        pw.accel(self.acc_travel(slot))
        self.travel(*start)
        pw.accel(self.acc_print(slot))
        e = self.e_line
        for k, pc in enumerate(pieces):
            if k:
                self.retract(slot)
                pw.accel(self.acc_travel(slot))
                self.travel(pc[0].x0, pc[0].y0, 600.0)
                self.unretract(slot)
                pw.accel(self.acc_print(slot))
            for j, s in enumerate(pc):
                self.seg(s, e, f if j == 0 else None)
        n_extra = tp.brim_loops if self.l0 else max(0, min(tp.brim_loops, CHAMFER_LOOPS)
                                                    - self.layer)
        last = pieces[-1]
        for k in range(1, n_extra + 1):
            cx, cy, _ = pw.pos
            lp = rotate_loop(tp.loop(k, self.layer), (cx, cy))
            pw.accel(self.acc_travel(slot))
            self.travel(lp[0].x0, lp[0].y0)
            pw.accel(self.acc_print(slot))
            for j, s in enumerate(lp):
                self.seg(s, e, f if j == 0 else None)
            last = lp
        # remember the tail of the last loop for the wipe
        tail = []
        for s in reversed(last):
            n = max(2, int(s.length / 0.2) + 1)
            pts = [s.point(t) for t in np.linspace(s.length, 0.0, n)]
            tail = list(reversed(pts)) + tail
            if sum(x.length for x in last[last.index(s):]) > WIPE_DIST + 0.5:
                break
        self.state["last_path"] = tail

    def ramming(self, c: Any, item: Item) -> None:
        """Part of a nozzle switch before the change G-code: the old filament's ramming lines,
        then a wipe, the rest of the retraction and a lift."""
        pw, st = self.pw, self.st
        old = c.old
        pitch_n = RAM_PITCH
        if self.l0:
            n = math.floor((item.depth - pitch_n) / RAM_PITCH_L0 + 1e-9) + 1
            ys = [item.y0 + pitch_n / 2 + k * RAM_PITCH_L0 for k in range(n)]
        else:
            ys = [item.y0 + pitch_n / 2 + k * pitch_n for k in range(item.lines)]
        xl, xr = self.tp.x + LINE_W / 2 + RAM_W / 2, self.tp.x1 - LINE_W / 2 - RAM_W / 2
        # travel in: from the objects the retract and lift sit outside the block
        if not self.state.get("at_tower"):
            self.retract(old)
            pw.accel(self.acc_travel(old))
            self.spiral(old, st.travel_speed[self.noz(old)] * 60.0, towards=(xl, ys[0]))
            pw.feature(Role.PRIME_TOWER, LINE_W)
            self.tower_on()
            self.travel(xl, ys[0], st.travel_speed[self.noz(old)] * 60.0)
        else:
            pw.feature(Role.PRIME_TOWER, LINE_W)
            self.tower_on()
            self.retract(old)
            # Bambu's travel between two tower blocks sets no acceleration: it runs at
            # whatever the previous block left (usually its print acceleration)
            self.spiral(old, st.travel_speed[self.noz(old)] * 60.0, i=round(
                self.hop(old) / (2 * math.pi * SPIRAL_SLOPE), 3), j=0.0)
            self.travel(xl, ys[0])
        self.travel(xl, ys[0], z=self.z)
        self.unretract(old)
        self.state["at_tower"] = True
        pw.feature(Role.PRIME_TOWER, RAM_W)
        pw.flags |= Flag.NOZZLE_CHANGE
        pw.accel(self.acc_print(old))
        e = e_per_mm(RAM_W, self.h)
        f = st.max_vol.get(old, 12.0) / area(RAM_W, self.h) * 60.0
        f = math.floor(f + 0.5)
        right = True
        for k, yv in enumerate(ys):
            if k:
                self.line(pw.pos[0], yv, e)
            self.line(xr if right else xl, yv, e, f if k == 0 else None)
            right = not right
        pw.flags &= ~Flag.NOZZLE_CHANGE
        # wipe 2 mm back along the last line, 95 percent of the tool change retraction
        x, y, _ = pw.pos
        tcr = st.toolchange_retract[self.noz(old)]
        back = -WIPE_DIST if not right else WIPE_DIST
        pw.accel(self.acc_print(old))
        pw.wipe_start()
        pw.wipe(_r3(x + back), _r3(y), -0.95 * tcr, self.speed(old) if not self.l0
                else st.max_speed * 60.0)
        pw.wipe_end()
        pw.retract(-0.05 * tcr, st.retract_speed[old] * 60.0)
        self.state["retracted"] = tcr
        pw.accel(self.acc_travel(old))
        self.spiral(old, st.max_speed * 60.0, i=round(self.hop(old) / (2 * math.pi
                                                                        * SPIRAL_SLOPE), 3), j=0.0)

    def pre_change(self, c: Any) -> None:
        """Part of a change without ramming before the change G-code: retract to the tool
        change length and lift."""
        pw, st = self.pw, self.st
        pw.feature(Role.PRIME_TOWER, LINE_W)
        self.tower_on()
        old = c.old
        tcr = st.toolchange_retract[self.noz(old)]
        path = self.state.get("last_path") or []
        if self.state.get("at_tower") and len(path) >= 2 and self.state.get("retracted", 0) == 0:
            x0, y0 = path[-1]
            x1, y1 = path[-2]
            d = math.hypot(x1 - x0, y1 - y0) or 1.0
            t = min(1.0, WIPE_DIST / d)
            pw.accel(self.acc_print(old))
            pw.wipe_start()
            pw.wipe(_r3(x0 + t * (x1 - x0)), _r3(y0 + t * (y1 - y0)), -0.95 * tcr,
                    self.speed(old))
            pw.wipe_end()
            self.state["retracted"] = 0.95 * tcr
        self.retract(old, tcr)
        pw.accel(self.acc_travel(old))
        self.spiral(old, st.max_speed * 60.0, i=round(self.hop(old) / (2 * math.pi
                                                                        * SPIRAL_SLOPE), 3), j=0.0)
        self.state["at_tower"] = False

    def prime(self, c: Any, item: Item, filament_start: Callable[[Any], None] | None) -> None:
        """Part of a change after the change G-code: approach from the side, nozzle wipe,
        prime lines."""
        tp, pw, st = self.tp, self.pw, self.st
        new = c.new
        n = self.noz(new)
        pitch = LINE_W * st.infill_gap
        if self.l0:
            m = math.floor((item.depth - pitch) / PRIME_PITCH_L0 + 1e-9) + 1
            ys = [item.y0 + pitch / 2 + k * PRIME_PITCH_L0 for k in range(m)]
        else:
            ys = [item.y0 + pitch / 2 + k * pitch for k in range(item.lines)]
        up = self.layer % 2 == 0
        left = ((self.layer + 1) // 2) % 2 == 0
        if not up:
            ys = ys[::-1]
        xl, xr = tp.x + LINE_W, tp.x1 - LINE_W
        sx = xl if left else xr
        inward = 1.0 if left else -1.0
        ax = tp.bbox[0] - APPROACH if left else tp.bbox[2] + APPROACH
        pw.feature(Role.PRIME_TOWER, LINE_W)
        self.tower_on()
        pw.accel(self.acc_travel(new))
        self.travel(ax, ys[0], st.travel_speed[n] * 60.0)
        self.travel(ax, ys[0], z=self.z)
        if filament_start is not None:
            filament_start(c)
        self.travel(sx, ys[0], z=self.z + self.hop(new))
        self.travel(sx, ys[0], z=self.z)
        self.state["retracted"] = st.toolchange_retract[n]
        self.unretract(new)
        pw.dwell(0.0)
        f_max = self.speed(new)
        ramp = [r * f_max for r in PRIME_RAMP]
        ramp.append(ramp[-1] + PRIME_RAMP_STEP)

        def speed_of(k: int) -> float:
            return math.floor(ramp[min(k, len(ramp) - 1)] + 0.5)

        e = self.e_line
        pw.accel(self.acc_print(new))
        self.line(sx + inward * PRIME_LEAD, ys[0], e, speed_of(0))
        rl = st.retract_len[new]
        pw.retract(-rl, st.retract_speed[new] * 60.0)
        pw.accel(self.acc_travel(new))
        self.travel(sx - inward * 1.5, ys[0], 600.0)
        self.travel(sx, ys[0], 240.0)
        for k, (dx, dy) in enumerate(NOZZLE_WIPE):
            self.travel(sx + dx, ys[0] + dy, 600.0 if k == 0 else None)
        self.travel(sx + inward * PRIME_LEAD, ys[0], speed_of(0))
        pw.retract(rl, st.deretract_speed[new] * 60.0)
        pw.accel(self.acc_print(new))
        far = xr if left else xl
        to_far = True
        for k, yv in enumerate(ys):
            if k:
                self.line(pw.pos[0], yv, e)
            self.line(far if to_far else sx, yv, e, speed_of(k))
            to_far = not to_far
        self.tower_on(False)
        pw.dwell(0.0)
        x, y, _ = pw.pos
        self.state["last_path"] = [(sx if not to_far else far, y), (x, y)]
        self.state["retracted"] = 0.0
        self.state["at_tower"] = True

    def entries(self) -> list[tuple[float, float]]:
        """Skip-gap centres on the wall: where each prime of the layer enters from the side."""
        tp = self.tp
        pitch = LINE_W * self.st.infill_gap
        up = self.layer % 2 == 0
        left = ((self.layer + 1) // 2) % 2 == 0
        loop0 = tp.loop(0, self.layer)
        out = []
        for it in self.lt.items:
            if it.kind != "prime":
                continue
            if self.l0:
                m = math.floor((it.depth - pitch) / PRIME_PITCH_L0 + 1e-9) + 1
                ys = [it.y0 + pitch / 2 + k * PRIME_PITCH_L0 for k in range(m)]
            else:
                ys = [it.y0 + pitch / 2 + k * pitch for k in range(it.lines)]
            y = ys[0] if up else ys[-1]
            out.append(loop_point_at_y(loop0, y, right=not left))
        return out


def tower_layer(tp: TowerPlan, layer: int, changes: Sequence[Any] | None, writer: PathWriter,
                cfg: Any = None, *, slot: int | None = None,
                template: Callable[[Any], None] | None = None,
                filament_start: Callable[[Any], None] | None = None,
                objects: Callable[[int], None] | None = None) -> None:
    """Append one layer's tower rows in Bambu's print order.

    `changes`: the layer's changes in order (default: the plan's). `slot`: the filament
    printing when the layer starts (needed on layers without changes). Callbacks, all optional:
    `objects(slot)` prints that filament's objects, `template(change)` emits the change G-code
    (called with Flag.TOWER set, as Bambu's file has it inside the tower block),
    `filament_start(change)` emits the new filament's filament_start_gcode.
    Rows inside Bambu's WIPE_TOWER_START/END markers carry Flag.TOWER."""
    if not tp.enabled or layer > tp.last_layer:
        if objects is not None and slot:
            objects(slot)
        return
    out = _Out(tp, layer, writer)
    lt = tp.layers[layer]
    evs = layer_events(tp, layer, changes, slot)
    entries = out.entries()
    items_of: dict[int, list[Item]] = {}
    for it in lt.items:
        items_of.setdefault(id(it.change), []).append(it)
    multi = len(tp.blocks) > 1
    for i, ev in enumerate(evs):
        kind = ev[0]
        nxt = evs[i + 1][0] if i + 1 < len(evs) else "objects"
        if kind == "objects":
            if objects is not None and ev[1]:
                objects(ev[1])
            continue
        if kind == "finish":
            s = ev[1] or tp.wall_slot
            for k in sorted(lt.grids):
                g = lt.grids[k]
                out.box_and_grid(s, g[0], g[1], "nochange2" if multi else "nochange1")
            out.wall(s, [])
            out.leave(s)
            continue
        if kind == "wall":
            out.wall(ev[1], entries)
        elif kind == "grid":
            k, s = ev[1], ev[2]
            g = lt.grids[k]
            out.box_and_grid(s, g[0], g[1], "post")
        elif kind == "change":
            c = ev[1]
            its = items_of.get(id(c), [])
            ram = next((it for it in its if it.kind == "ram"), None)
            pri = next((it for it in its if it.kind == "prime"), None)
            if ram is not None:
                out.ramming(c, ram)
            else:
                out.pre_change(c)
            writer.flags |= Flag.TOWER
            if template is not None:
                template(c)
            writer.flags &= ~Flag.TOWER
            out.state["retracted"] = 0.0
            if pri is not None:
                out.prime(c, pri, filament_start)
        if nxt == "objects":
            out.leave({"wall": lambda e: e[1], "grid": lambda e: e[2],
                       "change": lambda e: e[1].new}[kind](ev))
