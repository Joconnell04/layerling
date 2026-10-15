"""Back-project 2D part masks to mesh triangles through face-ID buffers and vote across views.

Per view the renderer (sim workstream) gives a face-ID buffer: (H, W) int32, the triangle index
seen at each pixel, -1 for background. For every face and label we accumulate

    votes[f, k] = sum over views of  w(f, v) * (pixels of f inside label k's masks in v)
    seen[f]     = sum over views of  w(f, v) * (pixels of f visible in v)

where w(f, v) = |cos| of the angle between the face normal and the direction to the camera,
clipped to [min_angle_weight, 1], so head-on views count more than grazing ones. The vote share
votes / seen is the weighted fraction of the face's observations that call it label k; a label
survives on a face if its share is at least min_vote. Views where a face is hidden do not count
against it.

Conflicts (several labels pass on one face): the most specific part wins (parts.SPECIFICITY:
pupil < eye < ear < head < body) as long as its confidence (share x mean SAM score) is at least
min_specific_confidence; otherwise the label with the highest confidence wins.

Faces no view saw take the value of the nearest seen face over mesh adjacency (Dijkstra on face
centroid distances; faces sharing an edge are neighbors after coincident vertices are welded, so
the UV seams of GLB meshes do not cut the surface), which may be "unlabeled". Speckle left where
mask edges cut triangles (a component under min_component_frac of its label's largest component,
or under min_component_area_mm2) is refilled the same way. So are components beyond
max_per_subject for a subject's part (default MAX_PER_SUBJECT: 2 eyes, 2 ears, 1 nose, 1 mouth;
nose and mouth ranked by facing front and closeness to the eyes), which drops one view's false
positive such as a "nose" on the rear. With head_fallback, body faces inside the hull of the
subject's face parts become head (rules.head_fallback). Each label's faces then split into
connected components; each component becomes one Region. A paired part with exactly two
components is named part_left / part_right by the SUBJECT's own sides (regions.Region.label): the
model's front faces -Y, so the subject's left is +X. The sides come from rules.subject_frame
(tail to nose), which is -Y/+X for a subject facing front and follows a subject sitting turned.
A subject's paws are grouped into feet (floating pieces join the nearest foot) and named by
position in that frame: paw_front_left, paw_front_right, paw_back_left, paw_back_right
(rules.foot_groups, rules.foot_names).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from p2p_contracts.manifest import FileRef
from p2p_contracts.payloads import PART_VOCAB
from p2p_contracts.regions import MeshRef, Region, RegionMap
from scipy.sparse import coo_matrix, csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra

from .parts import PAIRED_PARTS, part_for_prompt, specificity
from .rules import MAX_PER_SUBJECT, cap_parts, foot_groups, foot_names, subject_frame
from .rules import head_fallback as rules_head_fallback
from .types import SegmentResult

__all__ = ["MAX_PER_SUBJECT", "BackprojectResult", "LabelMask", "ViewLabels", "backproject"]


@dataclass
class LabelMask:
    part: str
    mask: np.ndarray  # (H, W) bool, same size as the view's face-ID buffer
    score: float = 1.0
    subject_id: str | None = None


@dataclass
class ViewLabels:
    face_ids: np.ndarray  # (H, W) int32, triangle index, -1 = background
    masks: list[LabelMask]
    camera_pos_mm: Sequence[float] | None = None  # perspective camera position
    view_dir: Sequence[float] | None = None  # orthographic: direction from model toward camera
    name: str = ""


@dataclass
class BackprojectResult:
    face_regions: np.ndarray  # int32 (F,), region id, 0 = unlabeled
    regions: list[Region]
    next_region_id: int
    label_keys: list[tuple[str | None, str]]  # (subject_id, part), index k in the arrays below
    face_label: np.ndarray  # int32 (F,), index into label_keys, -1 = unlabeled
    face_confidence: np.ndarray  # float32 (F,), confidence of the chosen label
    share: np.ndarray  # float32 (F, K) vote share per label
    seen: np.ndarray  # bool (F,), visible in at least one view
    filled: np.ndarray  # bool (F,), unseen or cleaned up, label taken from a neighbor
    stats: dict = field(default_factory=dict)

    def region_map(self, mesh: MeshRef, face_regions: FileRef) -> RegionMap:
        return RegionMap(
            mesh=mesh,
            face_regions=face_regions,
            regions=self.regions,
            next_region_id=self.next_region_id,
        )

    def save(self, out_dir: Path, rel_dir: str, mesh: MeshRef) -> RegionMap:
        """Write face_regions.npy and region_map.json into out_dir (a stage attempt folder).

        rel_dir is out_dir relative to the job folder, POSIX, used for the FileRef path.
        """
        return save_region_map(
            self.face_regions, self.regions, self.next_region_id, out_dir, rel_dir, mesh
        )


def save_region_map(
    face_regions: np.ndarray,
    regions: list[Region],
    next_region_id: int,
    out_dir: Path,
    rel_dir: str,
    mesh: MeshRef,
) -> RegionMap:
    """Write face_regions.npy and region_map.json into out_dir; rel_dir is job-relative POSIX."""
    import hashlib

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    npy = out_dir / "face_regions.npy"
    np.save(npy, np.asarray(face_regions).astype(np.int32))
    data = npy.read_bytes()
    ref = FileRef(
        path=f"{rel_dir.rstrip('/')}/face_regions.npy",
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        kind="npy",
        role="face_regions",
    )
    rmap = RegionMap(mesh=mesh, face_regions=ref, regions=regions, next_region_id=next_region_id)
    (out_dir / "region_map.json").write_text(json.dumps(rmap.model_dump(mode="json"), indent=2))
    return rmap


# ---------------------------------------------------------------- mesh helpers


def face_geometry(vertices: np.ndarray, faces: np.ndarray):
    """(centroids (F,3), areas (F,), unit normals (F,3), zero for degenerate faces)."""
    tri = vertices[faces]
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    norm = np.linalg.norm(cross, axis=1)
    normals = np.divide(cross, norm[:, None], out=np.zeros_like(cross), where=norm[:, None] > 0)
    return tri.mean(axis=1), 0.5 * norm, normals


def welded_faces(vertices: np.ndarray, faces: np.ndarray, rel_tol: float = 1e-7) -> np.ndarray:
    """faces re-indexed so vertices at the same position (within rel_tol of the bbox diagonal)
    share one index. GLB exports duplicate vertices along UV seams; without welding, faces on
    either side of a seam would not be neighbors. Face order is unchanged."""
    vertices = np.asarray(vertices, np.float64)
    if len(vertices) == 0:
        return np.asarray(faces)
    diag = float(np.linalg.norm(vertices.max(axis=0) - vertices.min(axis=0))) or 1.0
    q = np.round(vertices / (diag * rel_tol)).astype(np.int64)
    _, inverse = np.unique(q, axis=0, return_inverse=True)
    return inverse.ravel()[np.asarray(faces)]


def face_adjacency(
    faces: np.ndarray, centroids: np.ndarray, vertices: np.ndarray | None = None
) -> csr_matrix:
    """Symmetric (F, F) graph of faces sharing an edge, weighted by centroid distance.

    With vertices given, coincident vertices are welded first (see welded_faces).
    """
    if vertices is not None:
        faces = welded_faces(vertices, faces)
    n = len(faces)
    edges = np.sort(faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2), axis=1)
    owner = np.repeat(np.arange(n), 3)
    order = np.lexsort((edges[:, 1], edges[:, 0]))
    e, f = edges[order], owner[order]
    same = (e[1:] == e[:-1]).all(axis=1)
    a, b = f[:-1][same], f[1:][same]
    keep = a != b
    a, b = a[keep], b[keep]
    w = np.linalg.norm(centroids[a] - centroids[b], axis=1) + 1e-9
    g = coo_matrix(
        (np.concatenate([w, w]), (np.concatenate([a, b]), np.concatenate([b, a]))), shape=(n, n)
    ).tocsr()
    g.sum_duplicates()
    return g


def _fill_from_nearest(adj: csr_matrix, values: np.ndarray, known: np.ndarray) -> np.ndarray:
    """values for unknown faces from the nearest known face; -2 where none is reachable."""
    out = values.copy()
    unknown = ~known
    if not unknown.any():
        return out
    seeds = np.flatnonzero(known)
    if seeds.size == 0:
        out[unknown] = -2
        return out
    _, _, src = dijkstra(
        adj, directed=False, indices=seeds, min_only=True, return_predecessors=True
    )
    reach = unknown & (src >= 0)
    out[reach] = values[src[reach]]
    out[unknown & (src < 0)] = -2
    return out


# ---------------------------------------------------------------- inputs


def _norm_part(part: str) -> str:
    p = part.strip().lower()
    return p if p in PART_VOCAB else part_for_prompt(p)


def masks_from_result(
    result: SegmentResult,
    image_index: int,
    prompt_map: Mapping[str, tuple[str | None, str]] | None = None,
) -> list[LabelMask]:
    """LabelMasks for one image of a SegmentResult.

    prompt_map: prompt -> (subject_id, part). Default: part from parts.part_for_prompt, no subject.
    """
    img = next(im for im in result.images if im.image_index == image_index)
    out: list[LabelMask] = []
    for pm in img.prompts:
        subject, part = (prompt_map or {}).get(pm.prompt, (None, part_for_prompt(pm.prompt)))
        for m, s in zip(pm.decoded(), pm.scores, strict=True):
            out.append(LabelMask(part=part, mask=m, score=s, subject_id=subject))
    return out


def _angle_weights(view: ViewLabels, centroids, normals, min_w: float) -> np.ndarray:
    if view.camera_pos_mm is not None:
        d = np.asarray(view.camera_pos_mm, float)[None, :] - centroids
        d /= np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-12)
        cos = np.abs((normals * d).sum(axis=1))
    elif view.view_dir is not None:
        v = np.asarray(view.view_dir, float)
        cos = np.abs(normals @ (v / max(np.linalg.norm(v), 1e-12)))
    else:
        cos = np.ones(len(centroids))
    return np.clip(cos, min_w, 1.0)


# ---------------------------------------------------------------- main


def backproject(
    vertices: np.ndarray,
    faces: np.ndarray,
    views: Iterable[ViewLabels],
    *,
    min_vote: float = 0.5,
    min_specific_confidence: float = 0.35,
    min_angle_weight: float = 0.1,
    min_component_area_mm2: float = 0.0,
    min_component_frac: float = 0.02,
    max_per_subject: Mapping[str, int] | None = None,
    head_fallback: bool = True,
    foot_gap_frac: float = 0.06,
    fill_unseen: bool = True,
    first_region_id: int = 1,
    source: str = "sam3",
) -> BackprojectResult:
    vertices = np.asarray(vertices, dtype=np.float64)
    faces = np.asarray(faces, dtype=np.int64)
    views = list(views)
    n = len(faces)
    centroids, areas, normals = face_geometry(vertices, faces)

    found = {(m.subject_id, _norm_part(m.part)) for v in views for m in v.masks}
    # A subject with eyes or a nose gets a head label even if SAM 3 found no head (fallback).
    found |= {(s, "head") for s, p in found if s is not None and p in ("eye", "nose")}
    keys = sorted(found, key=lambda k: (k[0] or "", k[1]))
    kidx = {k: i for i, k in enumerate(keys)}
    nk = len(keys)

    denom = np.zeros(n)
    votes = np.zeros((n, nk))
    score_sum = np.zeros((n, nk))
    labeled_views: list[list[np.ndarray]] = [[] for _ in range(nk)]

    for vi, view in enumerate(views):
        fid = np.asarray(view.face_ids)
        if fid.ndim != 2:
            raise ValueError(f"view {vi}: face_ids must be 2D")
        if fid.max(initial=-1) >= n or fid.min(initial=-1) < -1:
            raise ValueError(f"view {vi}: face ids out of range for {n} faces")
        valid = fid >= 0
        w = _angle_weights(view, centroids, normals, min_angle_weight)
        denom += w * np.bincount(fid[valid], minlength=n)

        by_key: dict[int, list[LabelMask]] = {}
        for m in view.masks:
            if np.shape(m.mask) != fid.shape:
                raise ValueError(f"view {vi}: mask {np.shape(m.mask)} != face_ids {fid.shape}")
            by_key.setdefault(kidx[(m.subject_id, _norm_part(m.part))], []).append(m)
        for k, ms in by_key.items():
            # Per pixel, the best score among this label's masks (overlaps count once).
            best = np.zeros(fid.shape, dtype=np.float32)
            for m in ms:
                np.maximum(
                    best, np.where(np.asarray(m.mask, bool), np.float32(m.score), 0), out=best
                )
            hit = valid & (best > 0)
            if not hit.any():
                continue
            ids = fid[hit]
            cnt = np.bincount(ids, minlength=n)
            votes[:, k] += w * cnt
            score_sum[:, k] += w * np.bincount(ids, weights=best[hit], minlength=n)
            labeled_views[k].append(np.flatnonzero(cnt))

    seen = denom > 0
    share = np.divide(votes, denom[:, None], out=np.zeros_like(votes), where=seen[:, None])
    mean_score = np.divide(score_sum, votes, out=np.zeros_like(votes), where=votes > 0)
    conf = share * mean_score

    label = np.full(n, -1, dtype=np.int64)
    if nk:
        rank = np.array([specificity(p) for _, p in keys], dtype=np.float64)
        cand = share >= min_vote
        confident = cand & (conf >= min_specific_confidence)
        # rank steps of 2 dominate conf in [0, 1]; argmin ties go to the lower key index.
        c1 = np.argmin(np.where(confident, rank[None, :] * 2.0 - conf, np.inf), axis=1)
        c2 = np.argmin(np.where(cand, rank[None, :] * 1e-6 - conf, np.inf), axis=1)
        label = np.where(confident.any(axis=1), c1, np.where(cand.any(axis=1), c2, -1))

    adj = face_adjacency(faces, centroids, vertices)
    filled = np.zeros(n, dtype=bool)
    if fill_unseen and not seen.all():
        label = _fill_from_nearest(adj, label, seen)
        filled = ~seen & (label != -2)
        label[label == -2] = -1

    def despeckle(label: np.ndarray) -> np.ndarray:
        # Speckle: mask edges cut through triangles, leaving lone faces of a label (or lone
        # unlabeled faces inside a part). Refill components that are tiny next to that label's
        # largest component, or under the absolute floor.
        nonlocal filled
        if not ((min_component_area_mm2 > 0 or min_component_frac > 0) and nk):
            return label
        small = np.zeros(n, dtype=bool)
        for k in range(-1, nk):
            idx = np.flatnonzero(label == k)
            if idx.size == 0:
                continue
            nc, comp = connected_components(adj[idx][:, idx], directed=False)
            comp_area = np.bincount(comp, weights=areas[idx], minlength=nc)
            floor = max(min_component_area_mm2, min_component_frac * comp_area.max())
            small[idx[comp_area[comp] < floor]] = True
        if small.any():
            label = _fill_from_nearest(adj, label, ~small)
            label[label == -2] = -1
            filled |= small
        return label

    label = despeckle(label)

    dropped_components = 0
    caps = MAX_PER_SUBJECT if max_per_subject is None else max_per_subject
    if caps and nk:
        # Per subject: 2 eyes, 2 ears, 1 nose, 1 mouth (rules.cap_parts). Extra components are
        # refilled from their neighbors. Only labels with a subject id are capped.
        mass = (areas * seen)[:, None] * conf
        extra, dropped_components = cap_parts(
            label, keys, adj, areas, centroids, normals, mass, caps
        )
        if extra.any():
            label = _fill_from_nearest(adj, label, ~extra)
            label[label == -2] = -1
            filled |= extra

    head_fallback_faces = 0
    if head_fallback and nk:
        for subject, idx in rules_head_fallback(
            label, keys, adj, vertices, faces, centroids
        ).items():
            k = keys.index((subject, "head"))
            have = label == k
            fill_conf = float(conf[have, k].mean()) if have.any() else 0.5
            label[idx] = k
            conf[idx, k] = np.maximum(conf[idx, k], fill_conf)
            head_fallback_faces += int(idx.size)
        if head_fallback_faces:
            label = despeckle(label)  # carving the head out of the body can leave specks

    face_conf = np.zeros(n, dtype=np.float32)
    has = label >= 0
    face_conf[has] = conf[np.flatnonzero(has), label[has]]

    regions: list[Region] = []
    face_regions = np.zeros(n, dtype=np.int32)
    next_id = first_region_id
    frames: dict[str | None, tuple[np.ndarray, np.ndarray]] = {}
    for k, (subject, part) in enumerate(keys):
        idx = np.flatnonzero(label == k)
        if idx.size == 0:
            continue
        nc, comp = connected_components(adj[idx][:, idx], directed=False)
        groups = [idx[comp == c] for c in range(nc)]
        cents = [(areas[g] @ centroids[g]) / max(areas[g].sum(), 1e-12) for g in groups]
        order = sorted(
            range(nc),
            key=lambda c: (
                round(cents[c][0], 6),
                round(cents[c][2], 6),
                round(cents[c][1], 6),
                int(groups[c][0]),
            ),
        )
        if subject not in frames:
            frames[subject] = subject_frame(label, keys, subject, areas, centroids)
        fwd, left = frames[subject]
        if part == "paw" and subject is not None and nc >= 2:
            # Feet: floating pieces join the nearest foot, then names by position in the
            # subject's own frame (x' = toward its left, y' = toward its back).
            sub_faces = np.flatnonzero(np.isin(label, [i for i, kk in enumerate(keys)
                                                       if kk[0] == subject]))  # fmt: skip
            sub_pts = vertices[faces[sub_faces]].reshape(-1, 3)
            lo, hi = sub_pts.min(0), sub_pts.max(0)
            feet = foot_groups(
                groups, centroids, areas, gap_mm=foot_gap_frac * float(np.linalg.norm(hi - lo)),
                ground_tol_mm=0.05 * float(hi[2] - lo[2]),
            )  # fmt: skip
            groups = [np.concatenate([groups[c] for c in m]) for m in feet]
            nc = len(groups)
            cents = [(areas[g] @ centroids[g]) / max(areas[g].sum(), 1e-12) for g in groups]
            local = np.array([[c[:2] @ left, -(c[:2] @ fwd), c[2]] for c in cents])
            side = centroids[sub_faces, :2] @ left
            quals = foot_names(
                local,
                center_x=float((side.min() + side.max()) / 2),
                weights=np.array([areas[g].sum() for g in groups]),
            )
            order = list(range(nc))
            if nc > 1:
                rank = {"_front_left": 0, "_front_right": 1, "_back_left": 2, "_back_right": 3}
                order = sorted(order, key=lambda c: (rank.get(quals[c], 9), quals[c]))
                quals = [quals[c] for c in order]
        elif part in PAIRED_PARTS and nc == 2:
            # Subject's own sides: facing -Y its left is +X; a turned subject uses its frame.
            order = sorted(range(nc), key=lambda c: -float(cents[c][:2] @ left))
            quals = ["_left", "_right"]
        elif nc == 1:
            quals = [""]
        else:
            by_area = sorted(order, key=lambda c: (-areas[groups[c]].sum(), order.index(c)))
            order = by_area
            quals = [f"_{i + 1}" for i in range(nc)]
        for c, qual in zip(order, quals, strict=True):
            g = groups[c]
            area = float(areas[g].sum())
            seen_g = g[seen[g]]
            wts = areas[seen_g]
            confidence = (
                float(np.clip((wts @ conf[seen_g, k]) / wts.sum(), 0.0, 1.0))
                if wts.size and wts.sum() > 0
                else None
            )
            vs = vertices[np.unique(faces[g])]
            gset = np.zeros(n, dtype=bool)
            gset[g] = True
            views_seen = sum(bool(gset[fl].any()) for fl in labeled_views[k])
            name = f"{part}{qual}"
            regions.append(
                Region(
                    id=next_id,
                    label=f"{subject}.{name}" if subject else name,
                    subject_id=subject,
                    part=part,
                    source=source,
                    confidence=None if confidence is None else round(confidence, 4),
                    n_faces=int(g.size),
                    area_mm2=round(area, 4),
                    centroid_mm=tuple(round(float(x), 4) for x in cents[c]),
                    bbox_min_mm=tuple(round(float(x), 4) for x in vs.min(axis=0)),
                    bbox_max_mm=tuple(round(float(x), 4) for x in vs.max(axis=0)),
                    min_width_mm=None,
                    views_seen=views_seen,
                )
            )
            face_regions[g] = next_id
            next_id += 1

    stats = {
        "n_faces": n,
        "n_views": len(views),
        "seen_faces": int(seen.sum()),
        "filled_faces": int(filled.sum()),
        "dropped_extra_components": dropped_components,
        "head_fallback_faces": head_fallback_faces,
        "labeled_faces": int((face_regions > 0).sum()),
        "labeled_area_pct": round(
            100.0 * float(areas[face_regions > 0].sum()) / max(float(areas.sum()), 1e-12), 3
        ),
    }
    return BackprojectResult(
        face_regions=face_regions,
        regions=regions,
        next_region_id=next_id,
        label_keys=keys,
        face_label=label.astype(np.int32),
        face_confidence=face_conf,
        share=share.astype(np.float32),
        seen=seen,
        filled=filled,
        stats=stats,
    )
