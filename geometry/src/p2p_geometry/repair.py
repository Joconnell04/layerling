"""Local repair of the Meshy figure mesh.

Keeps every triangle in its original order where possible. Only three things remove a face:
it collapsed to a line after welding, it duplicates another face, or it is a dangling fin on a
non-manifold edge (the Meshy leftovers). Holes are closed by appending faces at the end.
`face_source` maps every output face to its source face (-1 = added).
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np

from .types import RepairedFigure, RepairReport


def weld(vertices: np.ndarray, tol_rel: float = 1e-7) -> tuple[np.ndarray, np.ndarray]:
    """Merge coincident vertices (UV seams). Returns (new_vertices, source->new index)."""
    v = np.asarray(vertices, dtype=np.float64)
    diag = float(np.linalg.norm(v.max(0) - v.min(0))) or 1.0
    key = np.round(v / (diag * tol_rel)).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    return v[first], inv.reshape(-1)


def _edge_table(faces: np.ndarray):
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    fid = np.tile(np.arange(len(faces)), 3)
    es = np.sort(e, axis=1).astype(np.int64)
    # One int64 key per undirected edge sorts exactly like the (lo, hi) rows, so uniq, inv and
    # counts match np.unique(es, axis=0) at a fraction of its time (1.3 s a call on 1.29M faces).
    n = np.int64(int(es.max()) + 1) if len(es) else np.int64(1)
    keys, inv, counts = np.unique(es[:, 0] * n + es[:, 1], return_inverse=True,
                                  return_counts=True)
    uniq = np.stack([keys // n, keys % n], axis=1)
    return e, fid, uniq, inv.reshape(-1), counts


def edge_stats(faces: np.ndarray) -> tuple[int, int]:
    """(non-manifold edges, boundary edges)."""
    if len(faces) == 0:
        return 0, 0
    _, _, _, _, counts = _edge_table(faces)
    return int((counts > 2).sum()), int((counts == 1).sum())


def _boundary_loops(faces: np.ndarray) -> list[list[int]]:
    """Directed boundary loops (edge direction as in the owning face)."""
    e, _, _, inv, counts = _edge_table(faces)
    bmask = counts[inv] == 1
    nxt: dict[int, list[int]] = defaultdict(list)
    for a, b in e[bmask]:
        nxt[int(a)].append(int(b))
    loops = []
    used: set[tuple[int, int]] = set()
    for start in list(nxt):
        for b0 in nxt[start]:
            if (start, b0) in used:
                continue
            loop = [start]
            a, b = start, b0
            ok = True
            while True:
                used.add((a, b))
                if b == start:
                    break
                if len(nxt[b]) != 1 or len(loop) > 100000:
                    ok = False
                    break
                loop.append(b)
                a, b = b, nxt[b][0]
            if ok and len(loop) >= 3:
                loops.append(loop)
    return loops


def repair_figure(
    vertices: np.ndarray,
    faces: np.ndarray,
    max_hole_edges: int = 200,
    max_hole_frac: float = 0.15,
) -> RepairedFigure:
    """Weld, drop collapsed/duplicate/fin faces, fix winding, fill small holes.

    max_hole_frac: largest hole perimeter to fill, as a fraction of the bbox diagonal.
    """
    import trimesh

    v_src = np.asarray(vertices, dtype=np.float64)
    f_src = np.asarray(faces, dtype=np.int64)
    rep = RepairReport(source_vertices=len(v_src), source_faces=len(f_src))

    v, vmap = weld(v_src)
    rep.welded_vertices = len(v_src) - len(v)
    f = vmap[f_src]
    rep.nonmanifold_edges_before, rep.boundary_edges_before = edge_stats(f)
    keep = np.ones(len(f), dtype=bool)

    collapsed = (f[:, 0] == f[:, 1]) | (f[:, 1] == f[:, 2]) | (f[:, 0] == f[:, 2])
    if collapsed.any():
        rep.removed_faces["collapsed"] = np.nonzero(collapsed)[0].tolist()
        keep &= ~collapsed

    srt = np.sort(f, axis=1)
    _, first = np.unique(srt[keep], axis=0, return_index=True)
    alive = np.nonzero(keep)[0]
    dup = np.ones(len(alive), dtype=bool)
    dup[first] = False
    if dup.any():
        rep.removed_faces["duplicate"] = alive[dup].tolist()
        keep[alive[dup]] = False

    # Dangling fins: faces on a non-manifold edge that also own a boundary edge.
    fins: list[int] = []
    for _ in range(4):
        alive = np.nonzero(keep)[0]
        _, fid, _, inv, counts = _edge_table(f[alive])
        nm = counts[inv] > 2
        if not nm.any():
            break
        bnd = counts[inv] == 1
        on_nm = np.zeros(len(alive), dtype=bool)
        on_nm[fid[nm]] = True
        has_b = np.zeros(len(alive), dtype=bool)
        has_b[fid[bnd]] = True
        cand = alive[on_nm & has_b]
        if len(cand) == 0:
            break
        fins.extend(cand.tolist())
        keep[cand] = False
    if fins:
        rep.removed_faces["fin"] = sorted(fins)

    face_source = np.nonzero(keep)[0]
    f = f[keep]

    # Winding: flip faces in place (order kept) so the mesh is consistently oriented, outward.
    tm = trimesh.Trimesh(v, f, process=False)
    before = tm.faces.copy()
    try:
        trimesh.repair.fix_winding(tm)
    except Exception:  # noqa: BLE001, S110 winding fix is best effort; watertight check follows
        pass
    rep.faces_flipped = int((np.any(tm.faces != before, axis=1)).sum())
    f = np.asarray(tm.faces, dtype=np.int64)

    # Fill holes by fanning from the loop centroid; new faces appended at the end.
    diag = float(np.linalg.norm(v.max(0) - v.min(0))) or 1.0
    new_v = [v]
    new_f = [f]
    n_v = len(v)
    for loop in _boundary_loops(f):
        pts = v[loop]
        perim = float(np.linalg.norm(pts - np.roll(pts, -1, axis=0), axis=1).sum())
        if len(loop) > max_hole_edges or perim > max_hole_frac * diag:
            rep.holes_skipped.append(len(loop))
            continue
        if len(loop) == 3:
            a, b, c = loop
            tris = np.array([[c, b, a]])
        else:
            c_idx = n_v
            new_v.append(pts.mean(axis=0, keepdims=True))
            n_v += 1
            a = np.array(loop)
            b = np.roll(a, -1)
            tris = np.stack([b, a, np.full_like(a, c_idx)], axis=1)
        new_f.append(tris)
        rep.holes_filled += 1
        rep.hole_faces_added += len(tris)
    v = np.concatenate(new_v)
    f = np.concatenate(new_f)
    face_source = np.concatenate([face_source, -np.ones(len(f) - len(face_source), dtype=np.int64)])

    tm = trimesh.Trimesh(v, f, process=False)
    if tm.is_watertight and tm.volume < 0:
        f = f[:, ::-1].copy()
        rep.inverted = True
    rep.nonmanifold_edges_after, rep.boundary_edges_after = edge_stats(f)
    rep.watertight = rep.nonmanifold_edges_after == 0 and rep.boundary_edges_after == 0

    comps = trimesh.graph.connected_components(
        trimesh.Trimesh(v, f, process=False).face_adjacency, nodes=np.arange(len(f)), min_len=1
    )
    rep.components = len(comps)
    sizes = sorted((len(c) for c in comps), reverse=True)
    rep.small_components = [s for s in sizes[1:] if s < 0.01 * len(f)]

    return RepairedFigure(
        vertices=v, faces=f, face_source=face_source, vertex_source=vmap, report=rep
    )
