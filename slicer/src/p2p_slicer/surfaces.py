"""Top, bottom, solid, sparse and bridge areas per region (section 5.3).

Tops and bottoms compare a region with the whole layer above and below (all filaments), so
internal colour boundaries make no skins. The infill area of a region (inside its walls) is split
into:
- top: not covered by the layer above; bottom: not supported by the layer below (layer 0 is all
  bottom; a one-layer membrane counts as bottom). A bottom above layer 0 is an external bridge.
- solid: within `top_shell_layers` (or as many as `top_shell_thickness` needs) below a top or
  `bottom_shell_layers` above a bottom, or where a neighbour layer in that range has no infill
  (sloped walls: ensure_vertical_shell_thickness);
- sparse: the rest; sparse pieces up to `minimum_sparse_infill_area` (15 mm2) become solid;
- internal bridge: solid printed over sparse infill of the layer below.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import geom as G


@dataclass
class RegionSurfaces:
    top: G.Paths = field(default_factory=list)
    bottom: G.Paths = field(default_factory=list)        # layer 0 bottom
    bridge: G.Paths = field(default_factory=list)        # bottoms above layer 0 (over air)
    internal_bridge: G.Paths = field(default_factory=list)
    solid: G.Paths = field(default_factory=list)
    sparse: G.Paths = field(default_factory=list)


def _drop_small(paths: G.Paths, min_area_mm2: float) -> G.Paths:
    if not paths:
        return []
    ex = G.expolygons(paths)
    return G.flatten([e for e in ex if G.ex_area(e) > min_area_mm2])


def classify(slices: list[G.Paths], fills: list[dict[int, G.Paths]], heights: list[float], *,
             top_layers: int, top_thickness: float, bottom_layers: int,
             bottom_thickness: float, min_sparse_area: float = 15.0,
             density: float = 0.15, envelopes: list[G.Paths] | None = None
             ) -> list[dict[int, RegionSurfaces]]:
    """`slices[i]`: the whole layer i; `fills[i][slot]`: that region's infill area;
    `envelopes[i]`: the infill area the whole layer would have in one filament. The sloped-wall
    test compares envelopes, so walls along internal colour boundaries make no shells (Bambu
    computes vertical shells over all regions of a multi-material layer together)."""
    n = len(slices)
    top_raw: list[G.Paths] = []
    bot_raw: list[G.Paths] = []
    for i in range(n):
        above = slices[i + 1] if i + 1 < n else []
        below = slices[i - 1] if i > 0 else []
        t = G.difference(slices[i], above) if above else list(slices[i])
        b = G.difference(slices[i], below) if i > 0 else list(slices[i])
        top_raw.append(G.opening(t, 0.021) if i + 1 < n else t)
        bot_raw.append(G.opening(b, 0.021) if i > 0 else b)
    fill_all: list[G.Paths] = envelopes if envelopes is not None else \
        [G.union([p for ps in f.values() for p in ps]) for f in fills]
    out: list[dict[int, RegionSurfaces]] = []
    for i in range(n):
        h = heights[i]
        n_top = max(top_layers, math.ceil(top_thickness / h - 1e-9) if top_thickness else 0)
        n_bot = max(bottom_layers,
                    math.ceil(bottom_thickness / h - 1e-9) if bottom_thickness else 0)
        top_src: G.Paths = []
        bot_src: G.Paths = []
        for k in range(1, n_top):
            if i + k < n:
                top_src.extend(top_raw[i + k])
        for k in range(1, n_bot):
            if i - k >= 0:
                bot_src.extend(bot_raw[i - k])
        shell_top = G.union(top_src) if top_src else []
        shell = G.union(top_src + bot_src) if (top_src or bot_src) else []
        # sloped walls: infill here that is not infill on every neighbour in range
        lo = max(0, i - (n_bot - 1))
        hi = min(n - 1, i + (n_top - 1))
        common = None
        for k in range(lo, hi + 1):
            if k == i:
                continue
            common = fill_all[k] if common is None else G.intersection(common, fill_all[k])
            if not common:
                break
        layer_res: dict[int, RegionSurfaces] = {}
        for slot, F in fills[i].items():
            rs = RegionSurfaces()
            if not F:
                layer_res[slot] = rs
                continue
            bottom = G.intersection(F, bot_raw[i])
            top = G.difference(G.intersection(F, top_raw[i]), bottom)
            rest = G.difference(F, G.union(top, bottom)) if (top or bottom) else list(F)
            solid = G.intersection(rest, shell) if shell else []
            if common is not None:
                slope = G.difference(rest, common) if common else list(rest)
                if slope:
                    solid = G.union(solid, _drop_small(G.opening(slope, 0.2), 0.5))
            sparse = G.difference(rest, solid) if solid else list(rest)
            if density <= 0:
                solid = G.union(solid, sparse)
                sparse = []
            else:
                ex = G.expolygons(sparse)
                small = [e for e in ex if G.ex_area(e) <= min_sparse_area]
                if small:
                    solid = G.union(solid, G.flatten(small))
                    sparse = G.flatten([e for e in ex if G.ex_area(e) > min_sparse_area])
            if i == 0:
                rs.bottom = bottom
            else:
                rs.bridge = bottom
            rs.top = top
            rs.solid = solid
            rs.sparse = sparse
            # candidates for internal bridges: top-shell solid only (not sloped-wall shells)
            rs.internal_bridge = G.intersection(solid, shell_top) if shell_top else []
            layer_res[slot] = rs
        out.append(layer_res)
    # internal bridges: top-shell solid over the sparse infill of the layer below, wide enough
    # to span (narrow rings on domes stay solid; the 1 mm opening is ASSUMED)
    for i in range(n):
        below_sparse = G.union([p for rs in out[i - 1].values() for p in rs.sparse]) \
            if i > 0 else []
        for rs in out[i].values():
            cand = rs.internal_bridge
            rs.internal_bridge = []
            if not cand or not below_sparse:
                continue
            br = G.opening(G.intersection(cand, below_sparse), 1.0)
            br = _drop_small(br, 1.0)
            if br:
                rs.internal_bridge = br
                rs.solid = G.difference(rs.solid, br)
    return out
