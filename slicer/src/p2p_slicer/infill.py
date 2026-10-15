"""Fill patterns (sections 5.3 and 5.4): parallel hatching clipped to an area, then chained.

- Solid (top, bottom, internal solid, bridge): lines at the solid angle (45 degrees, turned 90
  every layer), pitch = spacing stretched by up to 20 percent so whole lines fit; the line width
  grows with it, so solid volume = area x h (x flow).
- Sparse grid: two sweeps, 45 and 135 degrees, every layer, pitch = spacing / (density / 2), on a
  grid anchored at the object's bounding-box centre so lines stack layer to layer.

A `Chain` is a polyline printed in one go: `extrude[i]` says whether segment i extrudes (True) or
is a travel inside the fill (False, for monotonic line changes).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import geom as G


@dataclass
class Chain:
    pts: np.ndarray            # int64 (n, 2)
    extrude: np.ndarray        # bool (n - 1,)
    spacing: float             # mm, line pitch (decides the E per mm)
    role: int = 0

    @property
    def length_mm(self) -> float:
        d = np.diff(self.pts.astype(np.float64), axis=0)
        L = np.hypot(d[:, 0], d[:, 1]) * G.UNIT
        return float(L[self.extrude].sum())


def _rot(p: np.ndarray, c: float, s: float) -> np.ndarray:
    x = p[:, 0].astype(np.float64)
    y = p[:, 1].astype(np.float64)
    return np.stack([x * c - y * s, x * s + y * c], axis=1)


def hatch(area: G.Paths, angle_deg: float, pitch_mm: float,
          origin: tuple[float, float] | None = None, stretch: bool = False
          ) -> tuple[list[np.ndarray], float, list[int]]:
    """Clip parallel lines to `area`. Returns (segments, pitch used in mm, scanline index per
    segment). Lines run along the angle; `origin` (int units) fixes the grid phase."""
    if not area:
        return [], pitch_mm, []
    a = math.radians(angle_deg)
    c, s = math.cos(-a), math.sin(-a)
    rot = [_rot(p, c, s) for p in area]
    allp = np.concatenate(rot)
    ymin, ymax = allp[:, 1].min(), allp[:, 1].max()
    xmin, xmax = allp[:, 0].min() - 10, allp[:, 0].max() + 10
    pitch = pitch_mm * G.SCALE
    if stretch:
        extent = ymax - ymin
        n = max(1, round(extent / pitch))
        p2 = extent / n
        if p2 > pitch * 1.2:
            p2 = pitch
        pitch = p2
        y0 = ymin + pitch / 2
    else:
        if origin is not None:
            o = _rot(np.array([origin], dtype=np.float64), c, s)[0]
            y0 = o[1] + math.ceil((ymin - o[1]) / pitch) * pitch
        else:
            y0 = ymin + pitch / 2
    ys = np.arange(y0, ymax, pitch)
    if len(ys) == 0:
        return [], pitch * G.UNIT, []
    ci, si = math.cos(a), math.sin(a)
    # both ends of every scanline rotated back at once
    ax = np.round(xmin * ci - ys * si).astype(np.int64)
    ay = np.round(xmin * si + ys * ci).astype(np.int64)
    bx = np.round(xmax * ci - ys * si).astype(np.int64)
    by = np.round(xmax * si + ys * ci).astype(np.int64)
    lines = [np.array([[ax[k], ay[k]], [bx[k], by[k]]], dtype=np.int64) for k in range(len(ys))]
    clipped = G.clip_lines(lines, area)
    if not clipped:
        return [], pitch * G.UNIT, []
    # scanline index of each piece (nearest line by rotated y), and order along +x
    first = np.array([seg[0] for seg in clipped], dtype=np.float64)
    last = np.array([seg[-1] for seg in clipped], dtype=np.float64)
    ry = first[:, 0] * s + first[:, 1] * c
    rx0 = first[:, 0] * c - first[:, 1] * s
    rx1 = last[:, 0] * c - last[:, 1] * s
    kk = np.round((ry - y0) / pitch).astype(np.int64)
    flip = rx0 > rx1
    out = [seg[::-1] if f else seg for seg, f in zip(clipped, flip)]
    return out, pitch * G.UNIT, kk.tolist()


def _order_scan(segs: list[np.ndarray], idx: list[int], angle_deg: float) -> list[np.ndarray]:
    """Boustrophedon: scanlines in order, alternate direction, pieces along each line."""
    if not segs:
        return []
    a = math.radians(angle_deg)
    c, s = math.cos(-a), math.sin(-a)
    first = np.array([seg[0] for seg in segs], dtype=np.float64)
    x0 = first[:, 0] * c - first[:, 1] * s
    order = np.lexsort((x0, np.asarray(idx)))
    out = []
    flip = False
    last_k = None
    for j in order:
        k, seg = idx[j], segs[j]
        if k != last_k:
            flip = not flip if last_k is not None else False
            last_k = k
        out.append(seg[::-1] if flip else seg)
    return out


def chain_lines(segs: list[np.ndarray], max_link_mm: float, link_extrudes: bool,
                spacing_mm: float) -> list[Chain]:
    """Join consecutive segments into chains when the gap between them is short."""
    chains: list[Chain] = []
    cur: list[np.ndarray] = []
    ext: list[bool] = []
    lim = max_link_mm * G.SCALE
    for seg in segs:
        if cur:
            gap = float(np.hypot(*(seg[0] - cur[-1][-1]).astype(np.float64)))
            if gap <= lim:
                cur.append(seg)
                ext.append(link_extrudes)
                continue
            chains.append(_mk(cur, ext, spacing_mm))
        cur, ext = [seg], []
    if cur:
        chains.append(_mk(cur, ext, spacing_mm))
    return chains


def _mk(parts: list[np.ndarray], links: list[bool], spacing_mm: float) -> Chain:
    pts = [parts[0]]
    ext = [np.ones(len(parts[0]) - 1, dtype=bool)]
    for p, lk in zip(parts[1:], links):
        pts.append(p)
        ext.append(np.array([lk], dtype=bool))
        ext.append(np.ones(len(p) - 1, dtype=bool))
    return Chain(pts=np.concatenate(pts), extrude=np.concatenate(ext), spacing=spacing_mm)


def solid_fill(area: G.Paths, width: float, h: float, angle_deg: float,
               monotonic: bool = False) -> list[Chain]:
    from .walls import spacing as spc
    s = spc(width, h)
    segs, pitch, idx = hatch(area, angle_deg, s, stretch=True)
    ordered = _order_scan(segs, idx, angle_deg)
    if monotonic:
        return chain_lines(ordered, 3.0 * pitch, False, pitch)
    return chain_lines(ordered, 2.5 * pitch, True, pitch)


NARROW_SOLID_MM = 3.0    # an internal solid piece gone after this inward offset is "narrow"


def is_narrow(ex: G.ExPoly) -> bool:
    """Bambu's detect_narrow_internal_solid_infill test: nothing left after a 3 mm inset
    (spec 5.3: narrow solid under 6 mm prints concentric)."""
    return not G.offset([ex[0], *ex[1]], -NARROW_SOLID_MM)


def concentric_fill(area: G.Paths, width: float, h: float) -> list[Chain]:
    """Loops at half a spacing inside the area, then one spacing apart, outside in. Each loop
    is a closed polyline (first point repeated)."""
    from .walls import spacing as spc
    s = spc(width, h)
    out: list[Chain] = []
    cur = G.offset(area, -s / 2) if area else []
    while cur:
        for p in cur:
            if len(p) < 3:
                continue
            q = np.vstack([p, p[:1]])
            out.append(Chain(pts=q, extrude=np.ones(len(q) - 1, dtype=bool), spacing=s))
        cur = G.offset(cur, -s)
    return out


def sparse_grid(area: G.Paths, width: float, h: float, density: float,
                anchor: tuple[float, float], layer: int = 0) -> list[Chain]:
    """Grid: 45 and 135 degree sweeps; `anchor` = bbox centre in int units."""
    from .walls import spacing as spc
    if density <= 0 or not area:
        return []
    s = spc(width, h)
    pitch = s / (density / 2.0)
    chains: list[Chain] = []
    for ang in (45.0, 135.0):
        segs, _, idx = hatch(area, ang, pitch, origin=anchor)
        ordered = _order_scan(segs, idx, ang)
        chains.extend(chain_lines(ordered, min(20.0, 1.6 * pitch), True, s))
    return chains
