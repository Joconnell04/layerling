"""Triangle-plane sectioning with the triangle id kept per contour segment (section 3 and 13.1).

For every triangle and every slice plane between its lowest and highest vertex we cut one
segment. A vertex counts as above the plane when z >= plane (so a vertex on the plane never
produces a zero-length pair of crossings). The crossing point on a mesh edge is computed once per
(edge, plane) from the edge's lower-index vertex, so the two triangles sharing the edge agree bit
for bit, and segments chain into loops through exact integer keys (plane, edge): no tolerance.
An unmatched key means an open mesh; such chains are kept as open polylines and reported.

Loops are oriented counter-clockwise around material (outer contours CCW, holes CW).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

try:
    from numba import njit
except ImportError:  # pragma: no cover
    def njit(*a, **k):  # type: ignore[misc]
        if a and callable(a[0]):
            return a[0]
        return lambda f: f


@dataclass
class LayerLoops:
    loops: list[np.ndarray] = field(default_factory=list)   # float64 (n, 2) mm, closed implied
    faces: list[np.ndarray] = field(default_factory=list)   # int32 (n,), face of segment i -> i+1
    open_chains: int = 0


@njit(cache=True)
def _cycles(nxt: np.ndarray, order: np.ndarray, out_start: np.ndarray, out_len: np.ndarray,
            out_closed: np.ndarray, out_idx: np.ndarray) -> int:
    """Follow next pointers. Segments are visited in `order`; returns the number of chains.
    out_idx receives segment indices chain after chain."""
    n = nxt.shape[0]
    seen = np.zeros(n, dtype=np.bool_)
    # a chain can start in the middle of an open run: find heads (no predecessor) first
    has_prev = np.zeros(n, dtype=np.bool_)
    for s in range(n):
        if nxt[s] >= 0:
            has_prev[nxt[s]] = True
    k = 0
    nc = 0
    for pass_ in range(2):
        for oi in range(n):
            s0 = order[oi]
            if seen[s0]:
                continue
            if pass_ == 0 and has_prev[s0]:
                continue   # first pass: open chains from their heads
            out_start[nc] = k
            s = s0
            closed = False
            while True:
                seen[s] = True
                out_idx[k] = s
                k += 1
                t = nxt[s]
                if t < 0:
                    break
                if t == s0:
                    closed = True
                    break
                if seen[t]:
                    break
                s = t
            out_len[nc] = k - out_start[nc]
            out_closed[nc] = closed
            nc += 1
    return nc


def section_mesh(v: np.ndarray, f: np.ndarray, slice_z: np.ndarray) -> list[LayerLoops]:
    """Loops per slice plane (planes must be ascending)."""
    v = np.asarray(v, dtype=np.float64)
    f = np.asarray(f, dtype=np.int64)
    slice_z = np.asarray(slice_z, dtype=np.float64)
    n_layers = len(slice_z)
    out = [LayerLoops() for _ in range(n_layers)]
    if len(f) == 0 or n_layers == 0:
        return out
    # unique edges, edge k of a triangle joins vertex k and k+1
    tri_e = np.stack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]], axis=1)   # (F, 3, 2)
    lo = tri_e.min(axis=2)
    hi = tri_e.max(axis=2)
    ekey = lo * (int(v.shape[0]) + 1) + hi
    uniq, inv = np.unique(ekey.ravel(), return_inverse=True)
    edge_id = inv.reshape(-1, 3)
    n_edges = len(uniq)
    e_lo = uniq // (int(v.shape[0]) + 1)
    e_hi = uniq % (int(v.shape[0]) + 1)

    z = v[f][:, :, 2]                      # (F, 3)
    zmin = z.min(axis=1)
    zmax = z.max(axis=1)
    i0 = np.searchsorted(slice_z, zmin, side="right")
    i1 = np.searchsorted(slice_z, zmax, side="right")
    cnt = np.maximum(i1 - i0, 0)
    tri = np.repeat(np.arange(len(f)), cnt)
    if len(tri) == 0:
        return out
    starts = np.repeat(i0, cnt)
    offs = np.arange(len(tri)) - np.repeat(np.cumsum(cnt) - cnt, cnt)
    lay = starts + offs
    s = slice_z[lay]
    below = z[tri] < s[:, None]            # (P, 3)
    # edge k crosses when its two vertices differ; enter = below -> above along a->b->c
    b0, b1, b2 = below[:, 0], below[:, 1], below[:, 2]
    cross = np.stack([b0 != b1, b1 != b2, b2 != b0], axis=1)
    enter = np.stack([b0 & ~b1, b1 & ~b2, b2 & ~b0], axis=1)
    exit_ = cross & ~enter
    k_enter = np.argmax(enter, axis=1)
    k_exit = np.argmax(exit_, axis=1)
    e_enter = edge_id[tri, k_enter]
    e_exit = edge_id[tri, k_exit]
    # outer contours come out CCW with segments running from the exit edge to the enter edge
    seg_a = e_exit
    seg_b = e_enter
    # crossing points per (layer, edge), computed from the canonical edge orientation
    def point(e: np.ndarray, layer: np.ndarray) -> np.ndarray:
        pa = v[e_lo[e]]
        pb = v[e_hi[e]]
        t = (slice_z[layer] - pa[:, 2]) / (pb[:, 2] - pa[:, 2])
        return pa[:, :2] + t[:, None] * (pb[:, :2] - pa[:, :2])
    pa = point(seg_a, lay)
    # chain: next segment starts at the edge where this one ends
    key_a = lay.astype(np.int64) * n_edges + seg_a
    key_b = lay.astype(np.int64) * n_edges + seg_b
    order = np.argsort(key_a, kind="stable")
    ka_sorted = key_a[order]
    pos = np.searchsorted(ka_sorted, key_b)
    pos_c = np.minimum(pos, len(ka_sorted) - 1)
    found = ka_sorted[pos_c] == key_b
    nxt = np.where(found, order[pos_c], -1).astype(np.int64)
    # visit layer by layer so chains come out grouped
    visit = np.argsort(lay, kind="stable").astype(np.int64)
    P = len(tri)
    out_start = np.zeros(P + 1, dtype=np.int64)
    out_len = np.zeros(P + 1, dtype=np.int64)
    out_closed = np.zeros(P + 1, dtype=np.bool_)
    out_idx = np.zeros(P, dtype=np.int64)
    nc = _cycles(nxt, visit, out_start, out_len, out_closed, out_idx)
    for c in range(nc):
        idx = out_idx[out_start[c]:out_start[c] + out_len[c]]
        L = int(lay[idx[0]])
        if not out_closed[c]:
            out[L].open_chains += 1
            if len(idx) < 3:
                continue
        out[L].loops.append(pa[idx])
        out[L].faces.append(tri[idx].astype(np.int32))
    return out


def loop_area(p: np.ndarray) -> float:
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))
