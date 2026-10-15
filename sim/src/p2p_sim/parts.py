"""Multi-part prints: parts that overlap, each with its own filament.

A Bambu 3MF can carry one object as several parts (figure, key loop, base plate, raised text),
each assigned a filament. The parts overlap; the slicer prints their union. For the simulator:
- faces of one part that lie inside another part's solid are hidden: they are never visible,
  never contribute a color to a layer, and never count for bleed (interior_faces);
- each part's solid is printed in its own filament in every layer it spans (the infill and
  the inner walls), so its filament is present in those layers (swap counting);
- light material over another part's darker solid is limited by the distance down to that
  solid (part_depth_below), not by surface paint.

All tests use vertical lines (the build direction), bucketed on an XY grid, so they stay fast
at 200k faces and tolerate open meshes (parity may be wrong right at holes).
"""

from __future__ import annotations

import numpy as np

from .mesh import Mesh

_JITTER = np.array([1.37e-5, 0.91e-5])  # keeps vertical lines off shared edges and vertices


def column_hits(tri: np.ndarray, qxy: np.ndarray, max_pairs: int = 4_000_000):
    """Every (query, triangle, z) where the vertical line through qxy crosses a triangle.

    tri: (M, 3, 3) triangles, qxy: (N, 2). Returns (q_idx, t_idx, z) arrays.
    """
    tri = np.asarray(tri, dtype=np.float64)
    q = np.asarray(qxy, dtype=np.float64) + _JITTER
    if len(tri) == 0 or len(q) == 0:
        e = np.zeros(0, np.int64)
        return e, e, np.zeros(0)
    lo = tri[:, :, :2].min(axis=1)
    hi = tri[:, :, :2].max(axis=1)
    ext = np.median(hi - lo) if len(tri) > 1 else 1.0
    cell = max(float(ext) * 2.0, 1e-3)
    origin = np.minimum(lo.min(axis=0), q.min(axis=0)) - cell
    nx = int(np.ceil((max(hi[:, 0].max(), q[:, 0].max()) - origin[0]) / cell)) + 2
    c0 = np.floor((lo - origin) / cell).astype(np.int64)
    c1 = np.floor((hi - origin) / cell).astype(np.int64)
    span = (c1 - c0 + 1)
    # expand triangles to the grid cells their bbox covers
    cnt = span[:, 0] * span[:, 1]
    t_rep = np.repeat(np.arange(len(tri)), cnt)
    k = np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt)
    sx = span[t_rep, 0]
    cx = c0[t_rep, 0] + k % sx
    cy = c0[t_rep, 1] + k // sx
    cell_id = cy * nx + cx
    order = np.argsort(cell_id, kind="stable")
    cell_sorted = cell_id[order]
    tri_sorted = t_rep[order]
    qc = np.floor((q - origin) / cell).astype(np.int64)
    q_cell = qc[:, 1] * nx + qc[:, 0]
    start = np.searchsorted(cell_sorted, q_cell, side="left")
    stop = np.searchsorted(cell_sorted, q_cell, side="right")
    n_c = stop - start
    out_q, out_t, out_z = [], [], []
    chunk = max(1, int(max_pairs // max(1.0, float(n_c.mean()) * 2)))
    for s in range(0, len(q), chunk):
        qi = np.arange(s, min(s + chunk, len(q)))
        m = n_c[qi]
        tot = int(m.sum())
        if tot:
            qr = np.repeat(qi, m)
            kk = np.arange(tot) - np.repeat(np.cumsum(m) - m, m)
            ti = tri_sorted[np.repeat(start[qi], m) + kk]
            p = q[qr]
            a, bb, c = tri[ti, 0], tri[ti, 1], tri[ti, 2]
            det = (bb[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (bb[:, 1] - a[:, 1])
            ok = np.abs(det) > 1e-14
            l1 = ((p[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (p[:, 1] - a[:, 1]))
            l2 = ((bb[:, 0] - a[:, 0]) * (p[:, 1] - a[:, 1]) - (p[:, 0] - a[:, 0]) * (bb[:, 1] - a[:, 1]))
            l1 = np.where(ok, l1 / np.where(ok, det, 1), -1)
            l2 = np.where(ok, l2 / np.where(ok, det, 1), -1)
            l0 = 1 - l1 - l2
            inside = ok & (l0 >= 0) & (l1 >= 0) & (l2 >= 0)
            z = l0 * a[:, 2] + l1 * bb[:, 2] + l2 * c[:, 2]
            out_q.append(qr[inside])
            out_t.append(ti[inside])
            out_z.append(z[inside])
    if not out_q:
        e = np.zeros(0, np.int64)
        return e, e, np.zeros(0)
    return np.concatenate(out_q), np.concatenate(out_t), np.concatenate(out_z)


def interior_faces(mesh: Mesh, face_parts: np.ndarray) -> np.ndarray:
    """Faces whose centroid lies inside the solid of a different part (hidden in the union)."""
    parts = np.asarray(face_parts)
    out = np.zeros(mesh.n_faces, dtype=bool)
    ids = np.unique(parts)
    if len(ids) < 2:
        return out
    c = mesh.face_centroids
    tri = mesh.triangles
    for b in ids:
        tb = np.nonzero(parts == b)[0]
        qf = np.nonzero(parts != b)[0]
        lo, hi = tri[tb].reshape(-1, 3).min(0), tri[tb].reshape(-1, 3).max(0)
        near = np.all((c[qf] >= lo - 1e-6) & (c[qf] <= hi + 1e-6), axis=1)
        qf = qf[near]
        if qf.size == 0:
            continue
        qi, _, z = column_hits(tri[tb], c[qf, :2])
        above = z > c[qf[qi], 2]
        n_above = np.bincount(qi[above], minlength=len(qf))
        out[qf[n_above % 2 == 1]] = True
    return out


def part_depth_below(mesh: Mesh, face_parts: np.ndarray, faces: np.ndarray,
                     candidate_parts: dict[int, np.ndarray]):
    """For each face (on part A), the vertical distance down to the top of another part's solid.

    candidate_parts maps a part id A to the part ids whose solids count (e.g. darker ones).
    Only solids reached without crossing air count: the part A's own solid must continue at
    least down to the other solid's top. Returns (depth (n,), hit_face (n,), -1 if none).
    """
    parts = np.asarray(face_parts)
    faces = np.asarray(faces, dtype=np.int64)
    depth = np.full(len(faces), np.inf)
    hit = np.full(len(faces), -1, dtype=np.int64)
    c = mesh.face_centroids
    tri = mesh.triangles
    for a, others in candidate_parts.items():
        sel = np.nonzero(parts[faces] == a)[0]
        if sel.size == 0 or len(others) == 0:
            continue
        qf = faces[sel]
        # A's own exit below each face (its first crossing below the face centroid)
        ta = np.nonzero(parts == a)[0]
        qi, ti, z = column_hits(tri[ta], c[qf, :2])
        zq = c[qf, 2]
        below = z < zq[qi] - 1e-6
        exit_a = np.full(len(qf), -np.inf)
        np.maximum.at(exit_a, qi[below], z[below])
        for b in others:
            tb = np.nonzero(parts == b)[0]
            qi, ti, z = column_hits(tri[tb], c[qf, :2])
            below = z < zq[qi] - 1e-6
            if not below.any():
                continue
            qi, ti, z = qi[below], ti[below], z[below]
            top_b = np.full(len(qf), -np.inf)
            np.maximum.at(top_b, qi, z)
            is_top = z == top_b[qi]
            top_face = np.full(len(qf), -1, dtype=np.int64)
            top_face[qi[is_top]] = tb[ti[is_top]]
            d = zq - top_b
            # A's solid must reach B's top: A exits at or below B's top (overlap or contact)
            ok = np.isfinite(d) & (exit_a <= top_b + 0.05)
            better = ok & (d < depth[sel])
            depth[sel[better]] = d[better]
            hit[sel[better]] = top_face[better]
    return depth, hit


def horizontal_thickness(mesh: Mesh, faces: np.ndarray, points: np.ndarray,
                         exterior: np.ndarray | None = None, n_dirs: int = 4):
    """Width of the solid through each point, measured horizontally.

    For each of n_dirs horizontal directions the mesh is rotated so that direction is vertical
    and column_hits finds the solid interval containing the point; the narrowest direction wins
    (within 1/cos(90/n_dirs) of the true width). Returns (width (n,), end_face_a, end_face_b):
    the faces bounding the narrowest interval, -1 where the point is not inside the solid."""
    tri_idx = np.arange(mesh.n_faces) if exterior is None else np.nonzero(exterior)[0]
    tri = mesh.triangles[tri_idx]
    n = len(points)
    best = np.full(n, np.inf)
    ea = np.full(n, -1, np.int64)
    eb = np.full(n, -1, np.int64)
    for k in range(n_dirs):
        a = np.pi * k / n_dirs
        d = np.array([np.cos(a), np.sin(a), 0.0])
        u = np.array([-np.sin(a), np.cos(a), 0.0])
        rot = np.stack([u, np.array([0.0, 0.0, 1.0]), d])  # rows: new x, y, z (z along d)
        rt = tri @ rot.T
        rp = points @ rot.T
        qi, ti, z = column_hits(rt, rp[:, :2])
        dz = z - rp[qi, 2]
        up = dz > 1e-6
        dn = dz < -1e-6
        hi = np.full(n, np.inf)
        hf = np.full(n, -1, np.int64)
        lo = np.full(n, np.inf)
        lf = np.full(n, -1, np.int64)
        for sel, arr, far in ((up, hi, hf), (dn, lo, lf)):
            q, t, dd = qi[sel], ti[sel], np.abs(dz[sel])
            order = np.lexsort((dd, q))
            q, t, dd = q[order], t[order], dd[order]
            first = np.ones(len(q), dtype=bool)
            first[1:] = q[1:] != q[:-1]
            arr[q[first]] = dd[first]
            far[q[first]] = tri_idx[t[first]]
        w = hi + lo
        better = w < best
        best[better] = w[better]
        ea[better] = hf[better]
        eb[better] = lf[better]
    return best, ea, eb


def part_z_ranges(mesh: Mesh, face_parts: np.ndarray) -> dict[int, tuple[float, float]]:
    parts = np.asarray(face_parts)
    z = mesh.triangles[:, :, 2]
    return {int(p): (float(z[parts == p].min()), float(z[parts == p].max()))
            for p in np.unique(parts)}
