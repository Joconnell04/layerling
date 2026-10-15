"""Overhang degrees for wall speeds, and support checks (section 6 of the spec).

Overhang: a wall point's signed distance to the layer below's outline (negative inside) against
two offsets of that outline, inner -0.5 w + 0.5 (0.5 nozzle + 0.5 w) / 5 and outer +0.5 nozzle
(-0.169 and +0.2 mm for a 0.42 mm wall). Inside the inner offset the degree is 0; between the
two it rises linearly to 5 (pieces of at most 0.6 mm); beyond the outer offset the piece is an
"Overhang wall". Speed: degree 0-1 normal, 1-2 normal to overhang_2_4_speed, 2-3 to
overhang_3_4_speed, 3-4 to overhang_4_4_speed, 4 and up overhang_4_4_speed; overhang walls run at
overhang_totally_speed.
"""

from __future__ import annotations

import numpy as np

from . import geom as G
from .config import SliceConfig

PIECE_MM = 0.6


class OverhangMap:
    def __init__(self, lower: G.Paths, width: float, nozzle: float = 0.4,
                 current: G.Paths | None = None) -> None:
        import shapely
        self.ok = bool(lower)
        self.d_in = -0.5 * width + 0.5 * (0.5 * nozzle + 0.5 * width) / 5.0
        self.d_out = 0.5 * nozzle
        # nothing on this layer reaches past the inner offset of the layer below: no overhang
        self.none = False
        if self.ok and current is not None:
            reach = G.difference(G.offset(current, -0.5 * width), G.offset(lower, self.d_in))
            self.none = G.total_area(reach) < 1e-4
        if self.ok and not self.none:
            ex = G.expolygons(lower)
            polys = [shapely.Polygon(G.to_mm(o), [G.to_mm(h) for h in hs]) for o, hs in ex]
            geom = shapely.MultiPolygon(polys) if len(polys) > 1 else polys[0]
            self.poly = shapely.make_valid(geom)
            self.boundary = shapely.boundary(self.poly)
            shapely.prepare(self.poly)

    def signed_distance(self, pts: np.ndarray) -> np.ndarray:
        import shapely
        if not self.ok:
            return np.full(len(pts), 1e9)
        p = shapely.points(pts)
        d = shapely.distance(p, self.boundary)
        inside = shapely.contains_xy(self.poly, pts[:, 0], pts[:, 1])
        return np.where(inside, -d, d)

    def degrees(self, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Split a polyline (mm) into pieces of at most PIECE_MM where it overhangs; returns
        (points, per-segment degree with 6 meaning a full overhang)."""
        if self.none:
            return pts, np.zeros(len(pts) - 1)
        sd_v = self.signed_distance(pts)
        if not (sd_v > self.d_in).any():
            return pts, np.zeros(len(pts) - 1)
        out = [pts[:1]]
        for k in range(len(pts) - 1):
            a, b = pts[k], pts[k + 1]
            L = float(np.hypot(*(b - a)))
            n = max(1, int(np.ceil(L / PIECE_MM))) if (sd_v[k] > self.d_in or
                                                        sd_v[k + 1] > self.d_in) else 1
            t = np.arange(1, n + 1)[:, None] / n
            out.append(a + t * (b - a))
        q = np.vstack(out)
        mid = 0.5 * (q[1:] + q[:-1])
        sd = self.signed_distance(mid)
        deg = np.clip(5.0 * (sd - self.d_in) / (self.d_out - self.d_in), 0.0, 5.0)
        deg = np.where(sd > self.d_out, 6.0, deg)
        return q, deg


def overhang_speed(cfg: SliceConfig, n: int, deg: np.ndarray, v_normal: float) -> np.ndarray:
    s2 = cfg.nozzle("overhang_2_4_speed", n, 50.0)
    s3 = cfg.nozzle("overhang_3_4_speed", n, 30.0)
    s4 = cfg.nozzle("overhang_4_4_speed", n, 10.0)
    st = cfg.nozzle("overhang_totally_speed", n, 10.0)
    v = np.full(len(deg), v_normal)
    m = (deg > 1) & (deg <= 2)
    v[m] = v_normal + (s2 - v_normal) * (deg[m] - 1)
    m = (deg > 2) & (deg <= 3)
    v[m] = s2 + (s3 - s2) * (deg[m] - 2)
    m = (deg > 3) & (deg <= 4)
    v[m] = s3 + (s4 - s3) * (deg[m] - 3)
    v[(deg > 4) & (deg <= 5)] = s4
    v[deg > 5] = st
    return np.minimum(v, v_normal)
