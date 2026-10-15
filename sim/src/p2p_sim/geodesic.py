"""Surface distance fields on the face graph.

Faces are nodes; faces sharing a welded edge are neighbors. A label boundary is an edge between
faces with different labels.

Distances use nearest-point propagation (a discrete feature transform): every face carries the
boundary point nearest to it, found by passing points between neighbors inside the same label
region, and its distance is the straight-line distance to that point. Plain shortest paths over
face centroids zig-zag and overestimate by up to ~35% on strips that are not aligned with the
triangulation; this does not. Straight-line distance is accurate while the surface is roughly
flat over the distance measured (the widths that matter for printing are 0.3 to 3 mm).

metric_z scales the z component of every distance (an anisotropic metric; see simulate.py).
"""

from __future__ import annotations

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

from .mesh import Mesh

EPS = 1e-12


def _scale(d: np.ndarray, metric_z: float) -> np.ndarray:
    if metric_z == 1.0:
        return d
    return d * np.array([1.0, 1.0, metric_z])


def same_label_pairs(mesh: Mesh, labels: np.ndarray) -> np.ndarray:
    adj = mesh.face_adjacency
    return labels[adj[:, 0]] == labels[adj[:, 1]]


def _neighbors_csr(mesh: Mesh, labels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    adj = mesh.face_adjacency[same_label_pairs(mesh, labels)]
    f = mesh.n_faces
    rows = np.concatenate([adj[:, 0], adj[:, 1]])
    cols = np.concatenate([adj[:, 1], adj[:, 0]])
    order = np.argsort(rows, kind="stable")
    indptr = np.zeros(f + 1, dtype=np.int64)
    np.add.at(indptr, rows + 1, 1)
    return np.cumsum(indptr), cols[order]


def _gather(indptr: np.ndarray, indices: np.ndarray, faces: np.ndarray):
    """(owner face, neighbor face) for every neighbor of `faces`."""
    starts = indptr[faces]
    counts = indptr[faces + 1] - starts
    total = int(counts.sum())
    if total == 0:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    owner = np.repeat(faces, counts)
    offs = np.repeat(starts - np.concatenate([[0], np.cumsum(counts)[:-1]]), counts)
    return owner, indices[offs + np.arange(total)]


def propagate_nearest(
    mesh: Mesh, labels: np.ndarray, seed_faces: np.ndarray, seed_points: np.ndarray,
    metric_z: float = 1.0, limit: float = np.inf,
):
    """Nearest seed point for every face, travelling only through same-label neighbors.

    Returns (dist, src_face, point): dist is the (scaled) straight-line distance from the face
    centroid to its nearest seed point, src_face the seed face that owns that point.
    """
    f = mesh.n_faces
    c = mesh.face_centroids
    dist = np.full(f, np.inf)
    src = np.full(f, -1, dtype=np.int64)
    pts = np.full((f, 3), np.nan)
    if len(seed_faces) == 0:
        return dist, src, pts
    seed_faces = np.asarray(seed_faces, dtype=np.int64)
    d0 = np.linalg.norm(_scale(c[seed_faces] - seed_points, metric_z), axis=1)
    dist[seed_faces] = d0
    src[seed_faces] = seed_faces
    pts[seed_faces] = seed_points
    indptr, indices = _neighbors_csr(mesh, labels)
    frontier = seed_faces
    for _ in range(100000):
        if frontier.size == 0:
            break
        owner, nb = _gather(indptr, indices, frontier)
        # the neighbor nb receives the owner's point
        cand = pts[owner]
        cd = np.linalg.norm(_scale(c[nb] - cand, metric_z), axis=1)
        better = (cd < dist[nb] - 1e-9) & (cd <= limit)
        if not better.any():
            break
        nb, cd, owner = nb[better], cd[better], owner[better]
        order = np.lexsort((cd, nb))
        nbs = nb[order]
        first = np.ones(len(nbs), dtype=bool)
        first[1:] = nbs[1:] != nbs[:-1]
        pick = order[first]
        tgt = nb[pick]
        dist[tgt] = cd[pick]
        src[tgt] = src[owner[pick]]
        pts[tgt] = pts[owner[pick]]
        frontier = tgt
    return dist, src, pts


def boundary_seeds(mesh: Mesh, labels: np.ndarray, metric_z: float = 1.0):
    """Faces touching a label boundary: (faces, nearest point on their boundary edges)."""
    adj = mesh.face_adjacency
    diff = ~same_label_pairs(mesh, labels)
    if not diff.any():
        return np.zeros(0, np.int64), np.zeros((0, 3))
    e = mesh.adjacency_edges[diff]
    wv = mesh.welded_vertices
    p0, p1 = wv[e[:, 0]], wv[e[:, 1]]
    faces = np.concatenate([adj[diff, 0], adj[diff, 1]])
    p0 = np.concatenate([p0, p0])
    p1 = np.concatenate([p1, p1])
    c = mesh.face_centroids[faces]
    seg = p1 - p0
    t = np.clip(np.einsum("ij,ij->i", c - p0, seg) / np.maximum((seg * seg).sum(1), EPS), 0, 1)
    q = p0 + t[:, None] * seg
    off = np.linalg.norm(_scale(c - q, metric_z), axis=1)
    order = np.lexsort((off, faces))
    fs = faces[order]
    first = np.ones(len(fs), dtype=bool)
    first[1:] = fs[1:] != fs[:-1]
    pick = order[first]
    return faces[pick], q[pick]


def distance_to_boundary(
    mesh: Mesh, labels: np.ndarray, metric_z: float = 1.0, limit: float = np.inf
):
    """Per face: distance to the nearest boundary of its own label region.

    Returns (dist (F,), source_face (F,), source_point (F, 3)). Faces farther than `limit`, or in
    a label region with no boundary, get inf and source -1.
    """
    seeds, q = boundary_seeds(mesh, labels, metric_z)
    return propagate_nearest(mesh, labels, seeds, q, metric_z, limit)


def distance_from(
    mesh: Mesh, labels: np.ndarray, sources: np.ndarray, metric_z: float = 1.0,
    limit: float = np.inf,
) -> np.ndarray:
    """Distance from the nearest source face centroid, travelling inside each label region."""
    src = np.nonzero(sources)[0] if sources.dtype == bool else np.asarray(sources)
    d, _, _ = propagate_nearest(mesh, labels, src, mesh.face_centroids[src], metric_z, limit)
    return d


def _tolerance(mesh: Mesh, width: float, tol_frac: float, tol_cap: float) -> np.ndarray:
    """Per-face slack for face-level sampling: tol_frac x local edge length, at most
    tol_cap x width (so a coarse mesh cannot let everything through)."""
    return np.minimum(tol_frac * mesh.face_edge_len, tol_cap * width)


def thin_faces(
    mesh: Mesh, labels: np.ndarray, min_width: float,
    metric_z: float = 1.0, tol_frac: float = 0.5, tol_cap: float = 0.15,
) -> np.ndarray:
    """Morphological opening on the surface: faces a disk of diameter min_width cannot reach.

    Erode each label region by r = min_width / 2 (keep faces at least r from its boundary), then
    dilate the survivors by r inside the same region. Faces not recovered are too thin to print.
    Face-level labels have jagged edges (one triangle in or out), so a slack of half the local
    edge length (at most 15% of min_width) is allowed on both steps.
    """
    r = min_width / 2.0
    tol = _tolerance(mesh, min_width, tol_frac, tol_cap)
    lim = r + float(tol.max()) + 1e-6
    d, _, _ = distance_to_boundary(mesh, labels, metric_z, limit=lim)
    return opening_from_distance(mesh, labels, d, min_width, metric_z, tol_frac, tol_cap)


def opening_from_distance(
    mesh: Mesh, labels: np.ndarray, dist: np.ndarray, width: float, metric_z: float = 1.0,
    tol_frac: float = 0.5, tol_cap: float = 0.15,
) -> np.ndarray:
    """Faces removed by an opening of diameter `width`, given distance_to_boundary output."""
    r = width / 2.0
    tol = _tolerance(mesh, width, tol_frac, tol_cap)
    core = dist >= r - tol
    if core.all():
        return np.zeros(mesh.n_faces, dtype=bool)
    lim = r + float(tol.max()) + 1e-6
    dc = distance_from(mesh, labels, core, metric_z, limit=lim)
    return ~(core | (dc <= r + tol))


def components(mesh: Mesh, mask: np.ndarray, labels: np.ndarray | None = None):
    """Connected components of faces in `mask` (optionally only through same-label edges).

    Returns (n, comp) where comp is -1 outside the mask.
    """
    adj = mesh.face_adjacency
    keep = mask[adj[:, 0]] & mask[adj[:, 1]]
    if labels is not None:
        keep &= labels[adj[:, 0]] == labels[adj[:, 1]]
    a = adj[keep]
    f = mesh.n_faces
    g = csr_matrix((np.ones(len(a)), (a[:, 0], a[:, 1])), shape=(f, f))
    _, comp_all = connected_components(g, directed=False)
    comp = np.full(f, -1, dtype=np.int64)
    uniq, inv = np.unique(comp_all[mask], return_inverse=True)
    comp[mask] = inv
    return len(uniq), comp
