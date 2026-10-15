"""Classic walls, gap areas and the infill boundary for one region (section 5.2).

Widths come in as line widths w; a line's centre-to-centre spacing with its neighbour is
s = w - (1 - pi/4) h (rounded-rectangle cross-section, section 5.1).

- The outer wall's centreline sits w_outer / 2 inside the outline. The first inner wall is a
  further (s_outer + s_inner) / 2 in, the next ones s_inner apart.
- Every inset is followed by an opening of 0.6 x spacing (INSET_OVERLAP_TOLERANCE 0.4), which
  drops parts too narrow for another wall.
- The infill boundary is s_inner / 2 minus the 15 percent infill_wall_overlap inside the innermost
  wall centreline (0.1447 mm at the 0.20 mm Standard widths, SOURCE: spec 5.2).
- `one_wall`: the region is a top with nothing above (only_one_wall_top): outer wall only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import geom as G
from .moves import Role

ROUND_FACTOR = 0.21460183660255172   # 1 - pi/4
INSET_OVERLAP_TOLERANCE = 0.4
# Narrow islands print one outer wall this much thinner: 0.378512 mm for a 0.42 mm outer wall
# (SOURCE: Bambu G-code, keychain_thin_3c_020 red nose); the ratio is ASSUMED to scale.
NARROW_WALL_FACTOR = 0.378512 / 0.42


def spacing(w: float, h: float) -> float:
    return w - ROUND_FACTOR * h


def cross_section(w: float, h: float) -> float:
    return h * (w - ROUND_FACTOR * h)


@dataclass
class Loop:
    path: np.ndarray          # int64 (n, 2), closed, CCW for contours and CW for holes
    role: int                 # Role.OUTER_WALL or Role.INNER_WALL
    width: float
    depth: int                # 0 outer, 1 first inner, ...
    is_hole: bool


@dataclass
class Walls:
    loops: list[Loop] = field(default_factory=list)
    infill_area: G.Paths = field(default_factory=list)
    gap_area: G.Paths = field(default_factory=list)
    outer_area: G.Paths = field(default_factory=list)   # area enclosed by the outer wall centreline


def make_walls(region: G.Paths, *, loops: int, w_outer: float, w_inner: float, h: float,
               one_wall: bool = False, infill_overlap: float = 0.15) -> Walls:
    out = Walls()
    if not region:
        return out
    s_e = spacing(w_outer, h)
    s_i = spacing(w_inner, h)
    m_e = s_e * (1 - INSET_OVERLAP_TOLERANCE)
    m_i = s_i * (1 - INSET_OVERLAP_TOLERANCE)
    n = 1 if one_wall else max(loops, 1)
    last = G.offset2(region, -(w_outer / 2 + m_e / 2), m_e / 2)
    if not last:
        # narrow island: one thinner outer wall when the island is small (spec 5.2)
        w_n = NARROW_WALL_FACTOR * w_outer
        if G.total_area(region) < (w_outer + 0.78 * s_e) * 10.0:
            thin = G.offset2(region, -(w_n / 2 + 0.05 * s_e), 0.05 * s_e)
            if thin:
                out.outer_area = thin
                for p in thin:
                    out.loops.append(Loop(path=p, role=Role.OUTER_WALL, width=w_n, depth=0,
                                          is_hole=G.area(p) < 0))
        return out
    out.outer_area = last
    levels = [last]
    for i in range(1, n):
        d = (s_e + s_i) / 2 if i == 1 else s_i
        nxt = G.offset2(last, -(d + m_i / 2), m_i / 2)
        if not nxt:
            # gap between the last wall and the centre: gap fill candidate
            out.gap_area = G.offset(last, -(s_e if i == 1 else s_i) / 2)
            break
        # parts too narrow for this wall become gap area
        gap = G.difference(G.offset(last, -d / 2), G.offset(nxt, d / 2 + 0.001))
        if gap:
            out.gap_area.extend(gap)
        levels.append(nxt)
        last = nxt
    for depth, lv in enumerate(levels):
        role = Role.OUTER_WALL if depth == 0 else Role.INNER_WALL
        w = w_outer if depth == 0 else w_inner
        for p in lv:
            out.loops.append(Loop(path=p, role=role, width=w, depth=depth,
                                  is_hole=G.area(p) < 0))
    s_last = s_e if len(levels) == 1 else s_i
    inset = s_last / 2 - infill_overlap * (s_e + s_i) / 2
    out.infill_area = G.offset(last, -inset)
    return out
