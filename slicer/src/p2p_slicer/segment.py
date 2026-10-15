"""Paint segmentation: per layer, the area each filament prints (section 4 of the spec).

1. Contour colour: every contour segment takes the filament of the triangle that produced it.
2. Clean-up along each contour: runs of 0.2 mm or less after a run of another filament take that
   filament; runs of 0.5 mm or less between two runs of the same filament take that filament.
3. Interior: an island whose contours carry one filament prints in it; otherwise every point takes
   the filament of the nearest contour point (Voronoi on points sampled every 0.1 mm).
4. Tops and bottoms: the up-facing triangles between this slice plane and the next, projected,
   minus the layer above (exposed tops only), opened by 0.25 x outer wall width, carried down
   top_color_penetration_layers - 1 more layers, each inset a further 0.80 mm (bottoms mirror
   this upwards). Overlaps: paint beats the part filament, then the lower slot wins.
5. Merge: side regions lose the top and bottom areas; each filament's top/bottom area is added.
"""

from __future__ import annotations

import itertools
import math
import os as _os

import numpy as np

from . import geom as G
from .section import LayerLoops

SAMPLE_MM = 0.1


def _runs(slots: np.ndarray, seglen: np.ndarray) -> list[list]:
    """Consecutive equal-slot runs on a closed loop: [slot, length, first, last]."""
    n = len(slots)
    if n == 0:
        return []
    # rotate so a run starts at index 0
    change = np.nonzero(slots != np.roll(slots, 1))[0]
    if len(change) == 0:
        return [[int(slots[0]), float(seglen.sum()), 0, n - 1]]
    start = int(change[0])
    s = np.roll(slots, -start)
    ln = np.roll(seglen, -start)
    idx = np.roll(np.arange(n), -start)
    runs = []
    b = np.nonzero(s != np.roll(s, 1))[0].tolist() + [n]
    for a, c in itertools.pairwise(b):
        runs.append([int(s[a]), float(ln[a:c].sum()), int(idx[a]), int(idx[c - 1])])
    return runs


def _set_run(out: np.ndarray, run: list, new: int) -> None:
    a, b = run[2], run[3]
    if a <= b:
        out[a:b + 1] = new
    else:
        out[a:] = new
        out[:b + 1] = new


def clean_contour(states: np.ndarray, seglen: np.ndarray, min_painted: float = 0.0
                  ) -> np.ndarray:
    """Run rules along a closed contour (step 2). `states`: filament per segment, 0 where the
    triangle is unpainted (it prints the part filament). Passes, each until nothing changes:
    1. a run of 0.1 mm or less between two runs of the same state takes that state (pieces of
       one colour closer than 0.1 mm merge);
    2. a run of 0.5 mm or less between two runs of the same painted state takes that state;
    3. a run of 0.2 mm or less right after a run of another painted state takes that state.
    Unpainted runs never take over painted ones except by rule 1."""
    out = states.copy()
    for rule in (1, 2, 3, 4):
        if rule == 4 and min_painted <= 0:
            break
        for _ in range(len(out) + 1):
            runs = _runs(out, seglen)
            m = len(runs)
            if m <= 1:
                return out
            changed = False
            k = 0
            while k < m:
                prev = runs[(k - 1) % m]
                nxt = runs[(k + 1) % m]
                cur = runs[k]
                between = prev[0] == nxt[0] != cur[0]
                take_prev = (
                    (rule == 1 and between and cur[1] <= 0.1)
                    or (rule == 2 and between and prev[0] != 0 and cur[1] <= 0.5)
                    or (rule == 3 and prev[0] != 0 and prev[0] != cur[0] and cur[1] <= 0.2))
                drop = (rule == 4 and cur[0] != 0 and prev[0] == 0 and nxt[0] == 0
                        and cur[1] <= min_painted)
                new = prev[0] if take_prev else (0 if drop else None)
                if new is not None:
                    _set_run(out, cur, new)
                    changed = True
                    k += 2        # neighbours changed length: recompute runs next pass
                    continue
                k += 1
            if not changed:
                break
    return out


def _sample(loop: np.ndarray, slots: np.ndarray, step: float) -> tuple[np.ndarray, np.ndarray]:
    """Points every `step` mm along a closed loop (mm coords) with the slot of their segment."""
    p = loop
    q = np.roll(p, -1, axis=0)
    d = q - p
    L = np.hypot(d[:, 0], d[:, 1])
    cnt = np.where(L > step, (L // step).astype(np.int64), 0)
    if cnt.sum() == 0:
        return p.copy(), slots.copy()
    seg = np.repeat(np.arange(len(p)), cnt)
    k = np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt) + 1
    t = (k * step / L[seg])[:, None]
    extra = p[seg] + t * d[seg]
    return np.vstack([p, extra]), np.concatenate([slots, slots[seg]])


def _thin(pts: np.ndarray, sl: np.ndarray, far: float = 1.5, step: float = 0.5
          ) -> tuple[np.ndarray, np.ndarray]:
    """Keep every contour sample within `far` mm of another filament's sample, and one every
    `step` mm elsewhere: Voronoi boundaries between filaments depend only on the samples near
    them (ASSUMED: moves a boundary by well under 0.1 mm)."""
    from scipy.spatial import cKDTree
    uniq = np.unique(sl)
    if len(uniq) <= 1:
        return pts, sl
    near = np.zeros(len(pts), dtype=bool)
    for s in uniq:
        other = pts[sl != s]
        if len(other) == 0:
            continue
        d, _ = cKDTree(other).query(pts[sl == s], distance_upper_bound=far)
        near[np.nonzero(sl == s)[0][np.isfinite(d)]] = True
    keep = near.copy()
    grid = np.floor(pts / step).astype(np.int64)
    key = (grid[:, 0] * 1_000_003 + grid[:, 1]) * 64 + sl.astype(np.int64)
    _, first = np.unique(key, return_index=True)
    keep[first] = True
    return pts[keep], sl[keep]


def voronoi_split(island: G.Paths, pts: np.ndarray, sl: np.ndarray) -> dict[int, G.Paths]:
    """Split an island by nearest sampled contour point (GEOS Voronoi and cascaded union)."""
    import shapely
    _, ui = np.unique(np.round(pts, 5), axis=0, return_index=True)
    keep = np.sort(ui)
    pts = pts[keep]
    sl = sl[keep]
    mp = shapely.multipoints(pts)
    lo = pts.min(0) - 5
    hi = pts.max(0) + 5
    env = shapely.box(lo[0], lo[1], hi[0], hi[1])
    cells = shapely.get_parts(shapely.voronoi_polygons(mp, extend_to=env, ordered=True))
    if len(cells) != len(pts):
        return _voronoi_split_scipy(island, pts, sl)
    # cells to Clipper paths: neighbouring cells share vertices exactly, so a non-zero
    # intersection with the island merges each filament's cells in one pass
    xy, ci = shapely.get_coordinates(cells, return_index=True)
    xy = G.to_int(xy)
    cuts = np.flatnonzero(np.diff(ci)) + 1
    cell_paths = [p[:-1] for p in np.split(xy, cuts)]
    out: dict[int, G.Paths] = {}
    # the filament with the most cells takes what the others leave (one clip fewer)
    order = sorted(np.unique(sl).tolist(), key=lambda s: int((sl == s).sum()))
    claimed = []
    for s in order[:-1]:
        mine = [cell_paths[k] for k in np.flatnonzero(sl == s)]
        paths = G.intersection(mine, island)
        if paths:
            out[int(s)] = paths
            claimed.extend(paths)
    rest = G.difference(island, claimed) if claimed else list(island)
    if rest:
        out[int(order[-1])] = rest
    return out


def _to_shapely(paths: G.Paths):
    import shapely
    ex = G.expolygons(paths)
    polys = [shapely.Polygon(G.to_mm(o), [G.to_mm(h) for h in hs]) for o, hs in ex]
    return shapely.make_valid(shapely.MultiPolygon(polys)) if len(polys) != 1 else \
        shapely.make_valid(polys[0])


def _from_shapely(g) -> G.Paths:
    import shapely
    out: G.Paths = []
    for poly in shapely.get_parts(g):
        if poly.geom_type != "Polygon" or poly.is_empty:
            continue
        ext = G.to_int(np.asarray(poly.exterior.coords)[:-1])
        if len(ext) >= 3:
            out.append(ext if G.area(ext) > 0 else ext[::-1])
        for ring in poly.interiors:
            h = G.to_int(np.asarray(ring.coords)[:-1])
            if len(h) >= 3:
                out.append(h if G.area(h) < 0 else h[::-1])
    return G.union(out) if out else []


def _voronoi_split_scipy(island: G.Paths, pts: np.ndarray, sl: np.ndarray) -> dict[int, G.Paths]:
    from scipy.spatial import Voronoi
    lo = pts.min(0) - 50
    hi = pts.max(0) + 50
    far = np.array([[lo[0], lo[1]], [hi[0], lo[1]], [hi[0], hi[1]], [lo[0], hi[1]]])
    _, ui = np.unique(np.round(pts, 6), axis=0, return_index=True)
    pts = pts[np.sort(ui)]
    sl = sl[np.sort(ui)]
    vor = Voronoi(np.vstack([pts, far]))
    cells: dict[int, list[np.ndarray]] = {}
    for i in range(len(pts)):
        reg = vor.regions[vor.point_region[i]]
        if not reg or -1 in reg:
            continue
        poly = vor.vertices[reg]
        ip = G.to_int(poly)
        if G.area(ip) < 0:
            ip = ip[::-1]
        cells.setdefault(int(sl[i]), []).append(ip)
    out: dict[int, G.Paths] = {}
    for s, cs in cells.items():
        u = G.intersection(G.union(cs), island)
        if u:
            out[s] = u
    return out


def _clip_tri_slab(tri: np.ndarray, z0: float, z1: float) -> np.ndarray | None:
    """Part of triangle (3, 3) with z0 <= z <= z1, projected to XY."""
    poly = [tuple(v) for v in tri]
    for zc, keep_above in ((z0, True), (z1, False)):
        if not poly:
            return None
        out = []
        n = len(poly)
        for k in range(n):
            a = poly[k]
            b = poly[(k + 1) % n]
            ina = (a[2] >= zc) if keep_above else (a[2] <= zc)
            inb = (b[2] >= zc) if keep_above else (b[2] <= zc)
            if ina:
                out.append(a)
            if ina != inb:
                t = (zc - a[2]) / (b[2] - a[2])
                out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), zc))
        poly = out
    if len(poly) < 3:
        return None
    return np.array([(x, y) for x, y, _ in poly])


def _patch_loops(v: np.ndarray, f: np.ndarray, tris: np.ndarray) -> list[np.ndarray]:
    """Outline of a set of mesh triangles projected to XY: its boundary edges (edges whose
    reverse is not in the set), chained into loops. Linear time; the union of the loops is the
    union of the projected triangles when they do not fold over each other."""
    if len(tris) == 0:
        return []
    ff = f[tris]
    a = ff.reshape(-1)
    b = ff[:, [1, 2, 0]].reshape(-1)
    nv = int(v.shape[0]) + 1
    key = a.astype(np.int64) * nv + b
    rkey = b.astype(np.int64) * nv + a
    ks = np.sort(key)
    pos = np.searchsorted(ks, rkey)
    pos = np.minimum(pos, len(ks) - 1)
    has_rev = ks[pos] == rkey
    ea = a[~has_rev]
    eb = b[~has_rev]
    if len(ea) == 0:
        return []
    order = np.argsort(ea, kind="stable")
    sa = ea[order]
    nxt_pos = np.searchsorted(sa, eb)
    nxt_pos = np.minimum(nxt_pos, len(sa) - 1)
    ok = sa[nxt_pos] == eb
    nxt = np.where(ok, order[nxt_pos], -1)
    seen = np.zeros(len(ea), dtype=bool)
    loops = []
    for s0 in range(len(ea)):
        if seen[s0]:
            continue
        idx = []
        s = s0
        while s >= 0 and not seen[s]:
            seen[s] = True
            idx.append(s)
            s = int(nxt[s])
        if len(idx) >= 3:
            loops.append(G.to_int(v[ea[idx], :2]))
    return loops


def _slab_areas(v: np.ndarray, f: np.ndarray, fslot: np.ndarray, fpaint: np.ndarray,
                cand: np.ndarray, z0: float, z1: float, zmin: np.ndarray,
                zmax: np.ndarray) -> dict[tuple[int, int], G.Paths]:
    """Projected slab parts of candidate triangles, grouped by (priority, slot). Triangles
    wholly inside the slab are outlined as mesh patches; the few crossing a slab plane are
    clipped one by one."""
    groups: dict[tuple[int, int], list[np.ndarray]] = {}
    inside = (zmin[cand] >= z0) & (zmax[cand] <= z1)
    ins = cand[inside]
    if len(ins):
        pr = np.where(fpaint[ins], 0, 1)
        sl = fslot[ins].astype(np.int64)
        for key in set(zip(pr.tolist(), sl.tolist())):
            m = (pr == key[0]) & (sl == key[1])
            groups.setdefault(key, []).extend(_patch_loops(v, f, ins[m]))
    cross = cand[~inside]
    if len(cross):
        # small crossing triangles (fine meshes): whole triangle when its centroid is in the slab,
        # outlined as patches; large ones are clipped exactly
        tri = v[f[cross]]
        ext = np.max(tri.max(axis=1)[:, :2] - tri.min(axis=1)[:, :2], axis=1)
        small = ext < 0.5
        cz = tri[:, :, 2].mean(axis=1)
        sm = cross[small & (cz >= z0) & (cz <= z1)]
        if len(sm):
            pr = np.where(fpaint[sm], 0, 1)
            sl = fslot[sm].astype(np.int64)
            for key in set(zip(pr.tolist(), sl.tolist())):
                m = (pr == key[0]) & (sl == key[1])
                groups.setdefault(key, []).extend(_patch_loops(v, f, sm[m]))
        for t in cross[~small]:
            p = _clip_tri_slab(v[f[t]], z0, z1)
            if p is None:
                continue
            ip = G.to_int(p)
            a = G.area(ip)
            if abs(a) < 1e-6:
                continue
            if a < 0:
                ip = ip[::-1]
            key = (0 if fpaint[t] else 1, int(fslot[t]))
            groups.setdefault(key, []).append(ip)
    return {k: G.union(ps) for k, ps in groups.items()}


_PARALLEL = float(_os.environ.get("P2P_PAR", "0.9"))
_UNION_CLEAN = _os.environ.get("P2P_UCLEAN", "1") == "1"
_MIN_RUN = float(_os.environ.get("P2P_MINRUN", "0"))


def _edge_states(tree, src_states: np.ndarray, src_dir: np.ndarray, pm: np.ndarray,
                 d: np.ndarray) -> np.ndarray:
    """State of each layer-contour edge: the nearest original segment running along it (paint
    on a part's contour that only crosses the layer contour does not colour it)."""
    mid = pm + 0.5 * d
    k = min(8, len(src_states))
    dist, nn = tree.query(mid, k=k)
    if k == 1:
        return src_states[nn].astype(np.int64)
    L = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-12)
    u = d / L[:, None]
    cos = np.abs((src_dir[nn] * u[:, None, :]).sum(-1))
    ok = cos >= _PARALLEL
    # nearest parallel candidate within 0.05 mm of the nearest one, else the nearest
    ok &= dist <= dist[:, :1] + 0.05
    pick = np.where(ok.any(1), ok.argmax(1), 0)
    return src_states[nn[np.arange(len(nn)), pick]].astype(np.int64)


def _paint_split(S: G.Paths, loops: list[np.ndarray], states: list[np.ndarray]
                 ) -> dict[int, G.Paths]:
    """Colour an area from the paint states on its contour (steps 1 to 3). `states`: per contour
    segment of the original loops, the painted filament or 0 (unpainted). Returns state -> area,
    state 0 included."""
    cleaned = []
    for lp, sl in zip(loops, states):
        d = np.roll(lp, -1, axis=0) - lp
        cleaned.append(clean_contour(sl, np.hypot(d[:, 0], d[:, 1])))
    all_states = {int(x) for c in cleaned for x in np.unique(c)}
    if len(all_states) <= 1:
        return {next(iter(all_states)) if all_states else 0: S}
    # colours live on the layer's contour (the union of all parts): each contour sample takes
    # the state of the nearest original segment
    from scipy.spatial import cKDTree
    src_p, src_s, src_d = [], [], []
    for lp, sl in zip(loops, cleaned):
        pts, ss = _sample(lp, sl, SAMPLE_MM / 2)
        d = np.roll(lp, -1, axis=0) - lp
        L = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-12)
        u = d / L[:, None]
        # _sample returns the vertices first, then the extra points of each segment in order
        cnt = np.where(L > SAMPLE_MM / 2, (L // (SAMPLE_MM / 2)).astype(np.int64), 0)
        seg = np.concatenate([np.arange(len(lp)), np.repeat(np.arange(len(lp)), cnt)]) \
            if cnt.sum() else np.arange(len(lp))
        src_p.append(pts)
        src_s.append(ss)
        src_d.append(u[seg])
    tree = cKDTree(np.concatenate(src_p))
    src_states = np.concatenate(src_s)
    src_dir = np.concatenate(src_d)
    res: dict[int, G.Paths] = {}
    for outer, holes in G.expolygons(S):
        isl = [outer] + holes
        pts_l, sl_l = [], []
        for path in isl:
            pm = G.to_mm(path)
            d = np.roll(pm, -1, axis=0) - pm
            st = _edge_states(tree, src_states, src_dir, pm, d)
            if _UNION_CLEAN:
                st = clean_contour(st, np.hypot(d[:, 0], d[:, 1]), min_painted=_MIN_RUN)
            pts, ss = _sample(pm, st, SAMPLE_MM)
            pts_l.append(pts)
            sl_l.append(ss)
        sl_all = np.concatenate(sl_l)
        isl_states = {int(x) for x in np.unique(sl_all)}
        if len(isl_states) <= 1:
            res.setdefault(next(iter(isl_states)), []).extend(isl)
            continue
        vp, vs = _thin(np.concatenate(pts_l), sl_all)
        for st, ps in voronoi_split(isl, vp, vs).items():
            res.setdefault(st, []).extend(ps)
    return {st: G.union(ps) for st, ps in res.items() if ps}


def paint_regions(v: np.ndarray, f: np.ndarray, face_slot: np.ndarray, face_painted: np.ndarray,
                  face_part: np.ndarray, loops: list[LayerLoops], slices: list[G.Paths],
                  slice_z: np.ndarray, *, top_layers: int, bottom_layers: int,
                  outer_width: float = 0.42, layer_height: float = 0.2
                  ) -> list[dict[int, G.Paths]]:
    """Per layer: slot -> area.

    Ownership without paint: every part prints in its own filament; where parts overlap, the later
    part (3MF order) keeps the area. Paint is segmented over the whole layer (the union of the
    parts): painted areas (state k) are taken from whichever part owns them and print in k;
    unpainted areas (state 0) stay with their owner."""
    n = len(slices)
    parts = list(dict.fromkeys(face_part.tolist()))
    part_default: dict[int, int] = {}
    for pid in parts:
        m = face_part == pid
        unp = m & ~face_painted
        src = face_slot[unp] if unp.any() else face_slot[m]
        part_default[pid] = int(np.bincount(src).argmax())
    any_paint = bool(face_painted.any())
    base: list[dict[int, G.Paths]] = []
    paint: list[dict[int, G.Paths]] = []       # per layer: painted state -> area (no 0)
    for i in range(n):
        ll = loops[i]
        by_part: dict[int, list[int]] = {}
        for k, fc in enumerate(ll.faces):
            by_part.setdefault(int(face_part[fc[0]]), []).append(k)
        raw = {pid: G.union([G.to_int(ll.loops[k]) for k in ks]) for pid, ks in by_part.items()}
        own: dict[int, G.Paths] = {}
        later: G.Paths = []
        for pid in reversed(parts):
            a = raw.get(pid)
            if not a:
                continue
            c = G.difference(a, later) if later else a
            if c:
                own.setdefault(part_default[pid], []).extend(c)
            later = G.union(later, a)
        base.append({s: G.union(ps) for s, ps in own.items()})
        pl: dict[int, G.Paths] = {}
        if any_paint and slices[i]:
            states = [np.where(face_painted[fc], face_slot[fc], 0).astype(np.int64)
                      for fc in ll.faces]
            if any((st != 0).any() for st in states):
                split = _paint_split(slices[i], ll.loops, states)
                pl = {st: a for st, a in split.items() if st != 0 and a}
        paint.append(pl)
    if any_paint and (top_layers > 0 or bottom_layers > 0):
        _top_bottom(v, f, face_slot, face_painted, slices, slice_z, paint, top_layers,
                    bottom_layers, outer_width, layer_height)
    out: list[dict[int, G.Paths]] = []
    for i in range(n):
        res = base[i]
        if paint[i]:
            claimed = G.union([p for a in paint[i].values() for p in a])
            new: dict[int, G.Paths] = {}
            for s, ps in res.items():
                d = G.difference(ps, claimed)
                if d:
                    new[s] = d
            for s, ps in paint[i].items():
                new[s] = G.union(new.get(s, []), ps)
            res = new
        out.append({s: ps for s, ps in ((s, [p for p in ps if abs(G.area(p)) > 0.01])
                                        for s, ps in res.items()) if ps})
    return out


_TB_MODE = _os.environ.get("P2P_TB_MODE", "trim")


def _top_bottom(v, f, face_slot, face_painted, slices, slice_z, paint, top_layers,
                bottom_layers, outer_width, layer_height) -> None:
    """Step 4 and 5: exposed tops and bottoms carried into the layers below and above; they
    replace the side paint where they lie (unpainted tops remove paint). Modifies `paint`."""
    n = len(slices)
    tri = v[f]
    e1 = tri[:, 1] - tri[:, 0]
    e2 = tri[:, 2] - tri[:, 0]
    nrm = np.cross(e1, e2)
    nl = np.linalg.norm(nrm, axis=1)
    nz = np.where(nl > 0, nrm[:, 2] / np.maximum(nl, 1e-30), 0.0)
    zmin = tri[:, :, 2].min(axis=1)
    zmax = tri[:, :, 2].max(axis=1)
    up = nz > 0.5
    down = nz < -0.5
    open_r = 0.25 * outer_width
    inset = outer_width + (outer_width - layer_height * (1 - math.pi / 4))
    tb: list[dict[tuple[int, int], G.Paths]] = [{} for _ in range(n)]
    zcap = float(zmax.max()) + 1.0 if len(zmax) else 1.0
    fstate = np.where(face_painted, face_slot, 0)

    def carry(a: G.Paths, key, start: int, count: int, step: int) -> None:
        trimmed: G.Paths = []
        for k in range(count):
            j = start + step * k
            if j < 0 or j >= n or not slices[j]:
                break
            mode = _TB_MODE
            if not k:
                piece = G.intersection(a, slices[j])
                trimmed = slices[j]
            elif mode in ("prusa", "trim"):
                # carried inside the sections of every layer it passes (from its own layer on),
                # shrunk by k insets
                if mode == "prusa" and not G.offset(a, -k * inset):
                    break
                trimmed = G.intersection(trimmed, slices[j])
                piece = G.intersection(a, G.offset(trimmed, -k * inset))
            elif mode == "area":
                piece = G.intersection(G.offset(a, -k * inset), slices[j])
            else:
                piece = G.intersection(a, G.offset(slices[j], -k * inset))
            if piece:
                tb[j].setdefault(key, []).extend(piece)

    for i in range(n):
        S = slices[i]
        if not S:
            continue
        if top_layers > 0:
            z0 = slice_z[i]
            z1 = slice_z[i + 1] if i + 1 < n else zcap
            cand = np.nonzero(up & (zmax >= z0) & (zmin <= z1))[0]
            if len(cand):
                above = slices[i + 1] if i + 1 < n else []
                for key, area in _slab_areas(v, f, fstate, face_painted, cand, z0, z1,
                                             zmin, zmax).items():
                    a = G.difference(area, above) if above else area
                    a = G.opening(G.intersection(a, S), open_r)
                    a = [p for p in a if abs(G.area(p)) > 0.01]
                    if a:
                        carry(a, key, i, top_layers, -1)
        if bottom_layers > 0 and i > 0:
            z0b = slice_z[i - 1]
            z1b = slice_z[i]
            candb = np.nonzero(down & (zmax >= z0b) & (zmin <= z1b))[0]
            if len(candb):
                below = slices[i - 1]
                for key, area in _slab_areas(v, f, fstate, face_painted, candb, z0b, z1b,
                                             zmin, zmax).items():
                    a = G.difference(area, below) if below else area
                    a = G.opening(G.intersection(a, S), open_r)
                    a = [p for p in a if abs(G.area(p)) > 0.01]
                    if a:
                        carry(a, key, i, bottom_layers, 1)
    for i in range(n):
        if not tb[i]:
            continue
        claimed: G.Paths = []
        adds: dict[int, G.Paths] = {}
        for (prio, st) in sorted(tb[i]):     # paint first (lower slot wins), unpainted last
            a = G.union(tb[i][(prio, st)])
            if claimed and a:
                a = G.difference(a, claimed)
            if not a:
                continue
            if st != 0:
                adds.setdefault(st, []).extend(a)
            claimed = G.union(claimed, a)
        new: dict[int, G.Paths] = {}
        for st, ps in paint[i].items():
            d = G.difference(ps, claimed) if claimed else ps
            if d:
                new[st] = d
        for st, ps in adds.items():
            new[st] = G.union(new.get(st, []), ps)
        paint[i] = new
