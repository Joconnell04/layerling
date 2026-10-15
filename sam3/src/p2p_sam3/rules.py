"""Label rules applied after the multi-view vote (backproject.py), per subject.

cap_parts      A subject has at most 2 eyes and 2 ears (MAX_PER_SUBJECT), 1 nose and 1 mouth.
               Extra eye and ear components lose by confidence x area. Nose and mouth are
               ranked by facing front and by distance to the eyes (then eyes and nose), so a
               big false "nose" SAM 3 finds on the rear from the back view loses to the real one.
head_fallback  SAM 3 often scores "head" only from the sides, and the face (front view) falls
               into body. Body or unlabeled faces inside the convex hull of the subject's face
               parts (eyes, nose, mouth, head, muzzle, face; not ears, which can hang to the
               chest), slightly expanded, and connected to those parts become head. With a
               collar, nothing below the collar's lowest point changes.
subject_frame  Forward (tail to nose) and left axes of the subject in XY; -Y and +X for a
               subject facing front, its own axes for one sitting turned (Bronco, about 45 deg).
foot_groups    SAM 3 returns paws in pieces (an ankle seen from one side). A piece that does not
               reach the ground, or a speck, joins the nearest piece within reach; pieces on the
               ground stay separate feet (a sitting dog's feet can be 4 mm apart).
foot_names     Feet named by position in the subject frame: _front_left, _front_right,
               _back_left, _back_right, for any number of feet (extra ones get _2, _3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

MAX_PER_SUBJECT: dict[str, int] = {
    "eye": 2, "pupil": 2, "iris": 2, "ear": 2, "inner_ear": 2, "eyebrow": 2,
    "nose": 1, "mouth": 1,
}  # fmt: skip
ANCHORED = ("nose", "mouth")  # ranked by position on the face, in this order
EYE_PARTS = ("eye", "pupil", "iris")
HULL_PARTS = frozenset(
    {"eye", "pupil", "iris", "eye_highlight", "eyebrow", "nose", "mouth", "tongue", "teeth",
     "muzzle", "face", "head"}
)  # fmt: skip


def _components(adj: csr_matrix, idx: np.ndarray):
    nc, comp = connected_components(adj[idx][:, idx], directed=False)
    return nc, comp


def _centroid(g, areas, centroids):
    return (areas[g] @ centroids[g]) / max(float(areas[g].sum()), 1e-12)


def cap_parts(
    label: np.ndarray,
    keys: Sequence[tuple[str | None, str]],
    adj: csr_matrix,
    areas: np.ndarray,
    centroids: np.ndarray,
    normals: np.ndarray,
    mass: np.ndarray,
    caps: Mapping[str, int],
) -> tuple[np.ndarray, int]:
    """Faces to refill (bool (F,)) and the number of components dropped.

    mass: (F, K) per-face weight of each label (area x seen x confidence).
    """
    n = len(label)
    extra = np.zeros(n, dtype=bool)
    dropped = 0
    kidx = {k: i for i, k in enumerate(keys)}
    subjects = sorted({s for s, _ in keys if s is not None})
    order = [p for p in caps if p not in ANCHORED] + [p for p in ANCHORED if p in caps]
    for subject in subjects:
        diag = _subject_diag(label, keys, subject, centroids)
        for part in order:
            k = kidx.get((subject, part))
            if k is None:
                continue
            idx = np.flatnonzero((label == k) & ~extra)
            if idx.size == 0:
                continue
            nc, comp = _components(adj, idx)
            if nc <= caps[part]:
                continue
            m = np.bincount(comp, weights=mass[idx, k], minlength=nc)
            if part in ANCHORED:
                anchor_parts = EYE_PARTS if part == "nose" else (*EYE_PARTS, "nose")
                score = m * _face_factor(
                    idx, comp, nc, label, extra, keys, kidx, subject, anchor_parts, areas,
                    centroids, normals, diag,
                )  # fmt: skip
            else:
                score = m
            losers = np.argsort(-score, kind="stable")[caps[part] :]
            dropped += int(losers.size)
            extra[idx[np.isin(comp, losers)]] = True
    return extra, dropped


def _subject_diag(label, keys, subject, centroids) -> float:
    ks = [i for i, (s, _) in enumerate(keys) if s == subject]
    pts = centroids[np.isin(label, ks)]
    if len(pts) < 2:
        return 1.0
    return float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0))) or 1.0


def _face_factor(idx, comp, nc, label, extra, keys, kidx, subject, anchor_parts, areas,
                 centroids, normals, diag):  # fmt: skip
    """Per component: facing front (normal -Y) and near the anchor parts score higher."""
    w = areas[idx]
    nrm = np.stack([np.bincount(comp, weights=w * normals[idx, a], minlength=nc) for a in range(3)])
    nrm = nrm.T / np.maximum(np.linalg.norm(nrm.T, axis=1, keepdims=True), 1e-12)
    facing = 0.25 + 0.75 * np.clip(-nrm[:, 1], 0.0, 1.0)
    anchors = []
    for p in anchor_parts:
        k = kidx.get((subject, p))
        if k is None:
            continue
        a = np.flatnonzero((label == k) & ~extra)
        if a.size:
            anchors.append(_centroid(a, areas, centroids))
    if not anchors:
        return facing
    anchor = np.mean(anchors, axis=0)
    cen = np.stack(
        [np.bincount(comp, weights=w * centroids[idx, a], minlength=nc) for a in range(3)]
    )
    cen = cen.T / np.maximum(np.bincount(comp, weights=w, minlength=nc)[:, None], 1e-12)
    d = np.linalg.norm(cen - anchor, axis=1)
    scale = 0.1 * diag
    return facing / (1.0 + (d / scale) ** 2)


def head_fallback(
    label: np.ndarray,
    keys: Sequence[tuple[str | None, str]],
    adj: csr_matrix,
    vertices: np.ndarray,
    faces: np.ndarray,
    centroids: np.ndarray,
    expand: float = 1.1,
    max_points: int = 4000,
) -> dict[str, np.ndarray]:
    """subject -> faces that become that subject's head (see module docstring)."""
    from scipy.spatial import Delaunay, QhullError

    kidx = {k: i for i, k in enumerate(keys)}
    out: dict[str, np.ndarray] = {}
    for subject in sorted({s for s, _ in keys if s is not None}):
        head_k = kidx.get((subject, "head"))
        if head_k is None:
            continue
        anchor_ks = [kidx[(subject, p)] for p in ("eye", "nose") if (subject, p) in kidx]
        if not np.isin(label, anchor_ks).any():
            continue
        part_ks = [kidx[(subject, p)] for p in HULL_PARTS if (subject, p) in kidx]
        part_faces = np.flatnonzero(np.isin(label, part_ks))
        pts = vertices[np.unique(faces[part_faces])]
        if len(pts) > max_points:
            pts = pts[:: int(np.ceil(len(pts) / max_points))]
        center = pts.mean(axis=0)
        try:
            hull = Delaunay(center + (pts - center) * expand)
        except (QhullError, ValueError):
            continue  # flat or too few points
        body_k = kidx.get((subject, "body"), -2)
        cand = np.flatnonzero((label == body_k) | (label == -1))
        if cand.size == 0:
            continue
        cand = cand[hull.find_simplex(centroids[cand]) >= 0]
        collar_k = kidx.get((subject, "collar"))
        if collar_k is not None and cand.size:
            collar = np.flatnonzero(label == collar_k)
            if collar.size:
                cand = cand[centroids[cand, 2] >= centroids[collar, 2].min()]
        if cand.size == 0:
            continue
        # Keep candidates connected to the face parts.
        sub = np.concatenate([part_faces, cand])
        _, comp = _components(adj, sub)
        touching = np.unique(comp[: part_faces.size])
        keep = cand[np.isin(comp[part_faces.size :], touching)]
        if keep.size:
            out[subject] = keep
    return out


def subject_frame(
    label: np.ndarray,
    keys: Sequence[tuple[str | None, str]],
    subject: str | None,
    areas: np.ndarray,
    centroids: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """(forward, left) unit vectors in XY for the subject's own body.

    Forward runs from the tail (else the subject's centroid) to the nose (else eyes, else
    head). Left is forward turned 90 degrees about +Z. A subject facing -Y gets forward -Y and
    left +X, the contract's convention; a Meshy model sitting turned 45 degrees gets its own
    axes, so its paws and eyes are named by its own sides. Default when there is no face or
    the axis is too short: (-Y, +X).
    """
    default = (np.array([0.0, -1.0]), np.array([1.0, 0.0]))
    if subject is None:
        return default
    kidx = {k: i for i, k in enumerate(keys)}

    def cen(parts):
        ks = [kidx[(subject, p)] for p in parts if (subject, p) in kidx]
        g = np.flatnonzero(np.isin(label, ks)) if ks else np.array([], np.int64)
        return _centroid(g, areas, centroids) if g.size else None

    front = next((c for c in (cen(("nose",)), cen(EYE_PARTS), cen(("head",))) if c is not None),
                 None)  # fmt: skip
    if front is None:
        return default
    back = cen(("tail",))
    if back is None:
        back = cen([p for s, p in keys if s == subject])
    d = (front - back)[:2]
    ks = [i for i, (s, _) in enumerate(keys) if s == subject]
    pts = centroids[np.isin(label, ks), :2]
    extent = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0))) if len(pts) else 0.0
    if np.linalg.norm(d) < 0.1 * max(extent, 1e-9):
        return default
    f = d / np.linalg.norm(d)
    return f, np.array([-f[1], f[0]])


def foot_groups(
    groups: Sequence[np.ndarray],
    centroids: np.ndarray,
    areas: np.ndarray,
    gap_mm: float,
    ground_tol_mm: float,
) -> list[list[int]]:
    """Components joined into feet (lists of component indices).

    A foot reaches the ground (its lowest face within ground_tol_mm of the lowest paw face).
    A piece that does not (an ankle or toe line seen from one side) joins the nearest other
    piece whose closest face is within gap_mm. A speck under a tenth of the median piece's
    area joins the nearest piece within 3 x gap_mm. Other pieces on the ground never merge,
    since the feet of a sitting animal can be a few mm apart.
    """
    from scipy.spatial import cKDTree

    nc = len(groups)
    zmin = np.array([centroids[g, 2].min() for g in groups])
    area = np.array([float(areas[g].sum()) for g in groups])
    speck = area < 0.1 * np.median(area)
    grounded = (zmin <= zmin.min() + ground_tol_mm) & ~speck
    parent = list(range(nc))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    trees = [cKDTree(centroids[g]) for g in groups]
    floating = sorted(np.flatnonzero(~grounded), key=lambda i: area[i])
    for i in floating:
        best, best_d = None, gap_mm * (3.0 if speck[i] else 1.0)
        for j in range(nc):
            if find(j) == find(i):
                continue
            d = float(trees[j].query(centroids[groups[i]], k=1)[0].min())
            if d < best_d:
                best, best_d = j, d
        if best is not None:
            parent[find(i)] = find(best)
    feet: dict[int, list[int]] = {}
    for i in range(nc):
        feet.setdefault(find(i), []).append(i)
    return sorted(feet.values(), key=min)


def foot_names(cents: np.ndarray, center_x: float, weights: np.ndarray | None = None) -> list[str]:
    """Qualifiers for feet at cents (G, 3), in input order: '', '_left', '_front_left', ...

    Front is smaller y: with 4 feet the two with the smallest y; otherwise the area-weighted
    two-class split of y (Otsu), so a tiny piece cannot set the boundary. Within a front or
    back pair, the larger x is the subject's left; in a group of another size, x is compared
    with the subject's center.
    """
    g = len(cents)
    if g == 1:
        return [""]
    x, y = cents[:, 0], cents[:, 1]
    if g == 2:
        if abs(y[0] - y[1]) > abs(x[0] - x[1]):
            return ["_front", "_back"] if y[0] < y[1] else ["_back", "_front"]
        return ["_left", "_right"] if x[0] > x[1] else ["_right", "_left"]
    by_y = np.argsort(y, kind="stable")
    if g == 4:
        cut = 1
    else:
        w = np.ones(g) if weights is None else np.asarray(weights, float)[by_y]
        ys = y[by_y]
        best = -1.0
        for i in range(g - 1):
            wa, wb = w[: i + 1].sum(), w[i + 1 :].sum()
            ma, mb = (w[: i + 1] @ ys[: i + 1]) / wa, (w[i + 1 :] @ ys[i + 1 :]) / wb
            between = wa * wb * (ma - mb) ** 2
            if between > best:
                best, cut = between, i
    front = set(by_y[: cut + 1].tolist())
    names = [""] * g
    for members in (sorted(front), sorted(set(range(g)) - front)):
        end = "_front" if members and members[0] in front else "_back"
        if len(members) == 2:
            a, b = members
            sides = {a: "_left", b: "_right"} if x[a] > x[b] else {a: "_right", b: "_left"}
        else:
            sides = {i: "_left" if x[i] > center_x else "_right" for i in members}
        for i in members:
            names[i] = f"{end}{sides[i]}"
    seen: dict[str, int] = {}
    out = []
    for nm in names:
        seen[nm] = seen.get(nm, 0) + 1
        out.append(nm if seen[nm] == 1 else f"{nm}_{seen[nm]}")
    return out
