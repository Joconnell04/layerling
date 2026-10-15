"""Coat markings from texture color clusters, inside the body and head regions SAM 3 found.

SAM 3 finds parts (eye, ear, head, body) but not named coat markings ("white blaze", "black
saddle"). The GLB base-color texture has them, so:

1. face_colors_from_texture: CIELAB per face, the median of 7 bilinear texture samples inside the
   triangle (times baseColorFactor).
2. split_markings, restricted to faces of host regions (part in hosts, default body and head):
   a. edge-preserving smoothing of face colors over mesh adjacency (neighbors within about
      smooth_de count, so fur noise fades and marking edges stay);
   b. area-weighted k-means in CIELAB over all host faces together (k clusters), so the same
      white on the face and on the chest is one color;
   c. merge clusters whose centers are within merge_de CIEDE2000, then fold clusters under
      min_cluster_frac of the host area into the nearest color;
   d. dissolve blend bands: a cluster whose color lies between two others, ramps from one to
      the other, and sits in a band between them (baked shading, e.g. black fading into tan)
      goes to the nearer of the two colors face by face (_blend_clusters);
   e. per host region: area-weighted majority filter over neighbors, then connected components
      of each color; components under min_marking_frac of the host region's area (or under
      min_marking_mm2) take the color of the nearest larger component;
   f. a color covering at least min_host_share of a host region stays that region; every
      other component becomes a new Region(part="marking", source="texture_cluster",
      parent_ids=[host id]), labeled <subject>.marking_<color>_<position>, e.g.
      s1.marking_white_face, s1.marking_black_back. When no color covers min_host_share (a
      tricolor body), every component becomes a marking and the host region is retired
      (MarkingResult.retired_ids); its id is not reused. A piece that touches the subject's
      collar (absorb_parts) and matches its color within absorb_de joins the collar instead:
      the dark band SAM 3's collar mask missed is collar, not a marking.

Positions come from the component's area-weighted mean normal (and height when the normal
averages out): head -> face, crown, nape, chin, cheek_left, cheek_right, head; body -> chest,
back, belly, left, right, legs, sides. The two sets do not overlap, so a head marking never
takes a body marking's name. left/right are the subject's own sides (+X is its left). Colors
are color.NAMED_COLORS entries, distinct per cluster (color.distinct_color_names). Confidence is
the share of the component's area whose raw (unsmoothed) color is nearest its own cluster.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
from p2p_contracts.regions import Region
from scipy.sparse import csr_matrix, diags
from scipy.sparse.csgraph import connected_components, dijkstra

from .backproject import _fill_from_nearest, face_adjacency, face_geometry
from .color import ciede2000, color_name, distinct_color_names, lab_to_hex, srgb_to_lab
from .glb import GlbMesh, sample_texture

# Barycentric sample points: centroid, three toward the corners, three toward the edges.
_BARY = np.array(
    [
        [1 / 3, 1 / 3, 1 / 3],
        [2 / 3, 1 / 6, 1 / 6],
        [1 / 6, 2 / 3, 1 / 6],
        [1 / 6, 1 / 6, 2 / 3],
        [1 / 6, 5 / 12, 5 / 12],
        [5 / 12, 1 / 6, 5 / 12],
        [5 / 12, 5 / 12, 1 / 6],
    ]
)


def face_colors_from_texture(mesh: GlbMesh) -> np.ndarray:
    """(F, 3) CIELAB per face: median of 7 texture samples, times the material's color factor.

    Faces with no texture use the factor alone; faces with no material are mid gray.
    """
    n = len(mesh.faces)
    rgb = np.full((n, len(_BARY), 3), 128.0)
    for mi, mat in enumerate(mesh.materials):
        sel = np.flatnonzero(mesh.face_material == mi)
        if sel.size == 0:
            continue
        factor = np.asarray(mat.base_color_factor[:3], float)
        if mat.texture is not None and mesh.uvs is not None:
            tex = np.asarray(mat.texture.convert("RGB"))
            uv = np.einsum("sk,fkd->fsd", _BARY, mesh.uvs[mesh.faces[sel]])
            rgb[sel] = sample_texture(tex, uv) * factor
        else:
            rgb[sel] = 255.0 * factor
    return np.median(srgb_to_lab(rgb), axis=1)


@dataclass
class ColorCluster:
    index: int
    lab: tuple[float, float, float]
    hex: str
    name: str
    area_mm2: float


@dataclass
class MarkingResult:
    face_regions: np.ndarray  # int32 (F,), host faces reassigned to marking regions
    regions: list[Region]  # hosts updated, markings appended
    next_region_id: int
    clusters: list[ColorCluster]
    face_cluster: np.ndarray  # int32 (F,), cluster index for host faces, -1 elsewhere
    region_colors: dict[int, dict] = field(default_factory=dict)  # region id -> {lab, hex, name}
    marking_ids: list[int] = field(default_factory=list)
    retired_ids: list[int] = field(default_factory=list)  # hosts split entirely into markings
    stats: dict = field(default_factory=dict)


# ---------------------------------------------------------------- clustering


def _smooth(adj: csr_matrix, lab: np.ndarray, areas: np.ndarray, de: float, iters: int):
    """Edge-preserving smoothing: neighbors weighted by area * exp(-(dE76 / de)^2)."""
    if iters <= 0 or adj.nnz == 0:
        return lab
    coo = adj.tocoo()
    r, c = coo.row, coo.col
    out = lab.copy()
    for _ in range(iters):
        d2 = ((out[r] - out[c]) ** 2).sum(axis=1)
        w = areas[c] * np.exp(-d2 / (de * de))
        wm = csr_matrix((w, (r, c)), shape=adj.shape)
        num = wm @ out + areas[:, None] * out
        den = np.asarray(wm.sum(axis=1)).ravel() + areas
        out = num / den[:, None]
    return out


def _kmeans(x: np.ndarray, w: np.ndarray, k: int, iters: int = 40, seed: int = 0):
    """Area-weighted k-means with deterministic k-means++ seeding. Returns (centers, labels)."""
    rng = np.random.default_rng(seed)
    k = min(k, len(x))
    centers = [x[int(np.argmax(w))]]
    for _ in range(1, k):
        d2 = np.min(((x[:, None, :] - np.array(centers)[None]) ** 2).sum(axis=2), axis=1)
        p = w * d2
        if p.sum() <= 0:
            break
        centers.append(x[rng.choice(len(x), p=p / p.sum())])
    centers = np.array(centers)
    labels = np.zeros(len(x), np.int64)
    for _ in range(iters):
        labels = np.argmin(((x[:, None, :] - centers[None]) ** 2).sum(axis=2), axis=1)
        new = centers.copy()
        for j in range(len(centers)):
            m = labels == j
            if w[m].sum() > 0:
                new[j] = (w[m, None] * x[m]).sum(axis=0) / w[m].sum()
        if np.allclose(new, centers, atol=1e-4):
            centers = new
            break
        centers = new
    return centers, labels


def _merge_clusters(centers, weights, merge_de: float, min_frac: float):
    """Merge centers closer than merge_de (CIEDE2000), then fold light clusters into the nearest.

    Returns (final centers (K', 3), mapping old index -> new index).
    """
    groups = [[i] for i in range(len(centers))]
    cen = [np.asarray(c, float) for c in centers]
    wt = [float(x) for x in weights]
    while len(cen) > 1:
        d = ciede2000(np.array(cen)[:, None], np.array(cen)[None])
        np.fill_diagonal(d, np.inf)
        i, j = np.unravel_index(np.argmin(d), d.shape)
        total = sum(wt)
        light = [q for q in range(len(cen)) if wt[q] < min_frac * total]
        if d[i, j] < merge_de:
            pass
        elif light:
            i = min(light, key=lambda q: wt[q])
            j = int(np.argmin(d[i]))
        else:
            break
        i, j = int(i), int(j)
        a, b = (i, j) if wt[i] >= wt[j] else (j, i)  # b folds into a
        s = wt[a] + wt[b]
        if s > 0:
            cen[a] = (cen[a] * wt[a] + cen[b] * wt[b]) / s
        wt[a] = s
        groups[a] += groups[b]
        del cen[b], wt[b], groups[b]
    mapping = np.zeros(len(centers), np.int64)
    for gi, g in enumerate(groups):
        mapping[g] = gi
    return np.array(cen), mapping


def _majority(adj: csr_matrix, labels: np.ndarray, areas: np.ndarray, n_labels: int, iters: int):
    """Area-weighted mode of each face and its neighbors (adj restricted to one host)."""
    if iters <= 0 or adj.nnz == 0 or n_labels <= 1:
        return labels
    a = (adj > 0).astype(np.float64) + diags(np.ones(adj.shape[0]))
    a = a @ diags(areas)
    for _ in range(iters):
        onehot = csr_matrix(
            (np.ones(len(labels)), (np.arange(len(labels)), labels)), shape=(len(labels), n_labels)
        )
        votes = (a @ onehot).toarray()
        new = np.argmax(votes, axis=1)
        keep = votes[np.arange(len(labels)), labels] >= votes[np.arange(len(labels)), new]
        new = np.where(keep, labels, new)
        if np.array_equal(new, labels):
            break
        labels = new
    return labels


def _wquantile(x: np.ndarray, w: np.ndarray, q: float) -> float:
    order = np.argsort(x)
    cum = np.cumsum(w[order])
    return float(x[order][min(np.searchsorted(cum, q * cum[-1]), len(x) - 1)])


def _blend_clusters(
    centers, labels, raw_lab, areas, adj, max_width_mm: float, stats: dict, min_spread=0.3
):
    """Clusters that are blend bands: {c: (a, b)}.

    c is a blend of a and b when all three hold:
    - its CIELAB center lies near the segment between theirs (0.15 <= t <= 0.85, off the line
      by at most 0.25 of the segment);
    - its colors ramp along that segment: the 10th to 90th percentile spread of the faces' raw
      colors projected on it is at least min_spread (a flat gray patch between white and black
      spreads about 0.1; Meshy's black-into-tan shading on the beagle spreads 0.45);
    - it is a band between them: the area-weighted median over c's faces of max(surface
      distance to a, to b) is at most max_width_mm.
    """
    present = [c for c in range(len(centers)) if (labels == c).any()]
    dist_to: dict[int, np.ndarray] = {}

    def dist(c):
        if c not in dist_to:
            seeds = np.flatnonzero(labels == c)
            dist_to[c] = dijkstra(adj, directed=False, indices=seeds, min_only=True)
        return dist_to[c]

    out: dict[int, tuple[int, int]] = {}
    order = sorted(present, key=lambda c: float(areas[labels == c].sum()))
    for c in order:
        best = None
        for i, a in enumerate(present):
            for b in present[i + 1 :]:
                if c in (a, b) or a in out or b in out:
                    continue
                ab = centers[b] - centers[a]
                length = float(np.linalg.norm(ab))
                if length < 1e-6:
                    continue
                t = float((centers[c] - centers[a]) @ ab) / length**2
                perp = float(np.linalg.norm(centers[c] - centers[a] - t * ab)) / length
                if 0.15 <= t <= 0.85 and perp <= 0.25 and (best is None or perp < best[0]):
                    best = (perp, a, b)
        if best is None:
            continue
        _, a, b = best
        m = labels == c
        width = _wquantile(np.maximum(dist(a)[m], dist(b)[m]), areas[m], 0.5)
        ab = centers[b] - centers[a]
        tf = (raw_lab[m] - centers[a]) @ ab / float(ab @ ab)
        spread = _wquantile(tf, areas[m], 0.9) - _wquantile(tf, areas[m], 0.1)
        dissolved = spread >= min_spread and width <= max_width_mm
        stats.setdefault("blends", []).append(
            {"cluster": int(c), "between": [int(a), int(b)], "width_mm": round(width, 2),
             "max_width_mm": round(max_width_mm, 2), "t_spread": round(spread, 3),
             "dissolved": bool(dissolved)}
        )  # fmt: skip
        if dissolved:
            out[c] = (a, b)
    return out


# ---------------------------------------------------------------- naming


def _position(host_part: str, normal: np.ndarray, rel_height: float) -> str:
    """Position word from a component's mean normal (unnormalized) and relative height."""
    mag = float(np.linalg.norm(normal))
    if mag < 0.25:
        if host_part == "head":
            return "head"
        return "legs" if rel_height < 0.35 else "sides"
    x, y, z = normal / mag
    axis = int(np.argmax(np.abs([x, y, z])))
    if host_part == "head":
        if axis == 1:
            return "face" if y < 0 else "nape"
        if axis == 2:
            return "crown" if z > 0 else "chin"
        return "cheek_left" if x > 0 else "cheek_right"
    if axis == 1:
        return "chest" if y < 0 else "back"
    if axis == 2:
        return "back" if z > 0 else "belly"
    return "left" if x > 0 else "right"


def _region_for(
    rid, label, subject, part, g, vertices, faces, areas, centroids, confidence, parents, views=0
) -> Region:
    area = float(areas[g].sum())
    cent = (areas[g] @ centroids[g]) / max(area, 1e-12)
    vs = vertices[np.unique(faces[g])]
    return Region(
        id=rid,
        label=label,
        subject_id=subject,
        part=part,
        source="texture_cluster",
        confidence=None if confidence is None else round(float(np.clip(confidence, 0, 1)), 4),
        n_faces=int(g.size),
        area_mm2=round(area, 4),
        centroid_mm=tuple(round(float(v), 4) for v in cent),
        bbox_min_mm=tuple(round(float(v), 4) for v in vs.min(axis=0)),
        bbox_max_mm=tuple(round(float(v), 4) for v in vs.max(axis=0)),
        min_width_mm=None,
        views_seen=views,
        parent_ids=list(parents),
    )


# ---------------------------------------------------------------- main


def split_markings(
    vertices: np.ndarray,
    faces: np.ndarray,
    face_regions: np.ndarray,
    regions: Sequence[Region],
    face_lab: np.ndarray,
    *,
    hosts: Sequence[str] = ("body", "head"),
    next_region_id: int | None = None,
    k: int = 8,
    smooth_de: float = 6.0,
    smooth_iters: int = 3,
    merge_de: float = 12.0,
    min_cluster_frac: float = 0.03,
    majority_iters: int = 3,
    min_marking_frac: float = 0.03,
    min_marking_mm2: float = 4.0,
    min_host_share: float = 0.5,
    dissolve_blends: bool = True,
    blend_width_frac: float = 0.15,
    absorb_parts: Sequence[str] = ("collar",),
    absorb_de: float = 15.0,
    seed: int = 0,
) -> MarkingResult:
    vertices = np.asarray(vertices, np.float64)
    faces = np.asarray(faces, np.int64)
    face_regions = np.asarray(face_regions, np.int32).copy()
    face_lab = np.asarray(face_lab, np.float64)
    n = len(faces)
    if face_lab.shape != (n, 3):
        raise ValueError(f"face_lab must be ({n}, 3), got {face_lab.shape}")
    regions = list(regions)
    next_id = next_region_id or (max((r.id for r in regions), default=0) + 1)
    centroids, areas, normals = face_geometry(vertices, faces)
    host_regions = [r for r in regions if r.part in hosts]
    host_ids = np.array([r.id for r in host_regions], np.int64)
    in_host = np.isin(face_regions, host_ids) if host_ids.size else np.zeros(n, bool)
    face_cluster = np.full(n, -1, np.int32)
    region_colors: dict[int, dict] = {}
    if not in_host.any():
        return MarkingResult(face_regions, regions, next_id, [], face_cluster, region_colors)

    adj = face_adjacency(faces, centroids, vertices)
    hidx = np.flatnonzero(in_host)
    # Only smooth across edges inside the same host region.
    sub = adj[hidx][:, hidx].tocoo()
    same = face_regions[hidx[sub.row]] == face_regions[hidx[sub.col]]
    hadj = csr_matrix((sub.data[same], (sub.row[same], sub.col[same])), shape=(hidx.size,) * 2)
    lab_s = _smooth(hadj, face_lab[hidx], areas[hidx], smooth_de, smooth_iters)

    centers, km = _kmeans(lab_s, areas[hidx], k, seed=seed)
    cw = np.bincount(km, weights=areas[hidx], minlength=len(centers))
    centers, _ = _merge_clusters(centers, cw, merge_de, min_cluster_frac)
    lab_h = np.argmin(ciede2000(lab_s[:, None], centers[None]), axis=1)
    stats: dict = {"blends": []}
    if dissolve_blends and len(centers) >= 3:
        hv = vertices[np.unique(faces[hidx])]
        max_w = blend_width_frac * float(np.linalg.norm(hv.max(axis=0) - hv.min(axis=0)))
        blends = _blend_clusters(
            centers, lab_h, face_lab[hidx], areas[hidx], adj[hidx][:, hidx], max_w, stats
        )
        if blends:
            for c, (a, b) in blends.items():
                m = lab_h == c
                da = ((lab_s[m] - centers[a]) ** 2).sum(axis=1)
                db = ((lab_s[m] - centers[b]) ** 2).sum(axis=1)
                lab_h[m] = np.where(da <= db, a, b)
            keep_c = [c for c in range(len(centers)) if c not in blends]
            remap = np.full(len(centers), -1, np.int64)
            remap[keep_c] = np.arange(len(keep_c))
            centers, lab_h = centers[keep_c], remap[lab_h]
    nk = len(centers)
    names = distinct_color_names(centers)  # two grays: silver and gray, not silver twice
    raw_nearest = np.argmin(ciede2000(face_lab[hidx][:, None], centers[None]), axis=1)

    new_regions: list[Region] = []
    marking_ids: list[int] = []
    updated: dict[int, Region] = {}
    used_labels = {r.label for r in regions}
    # Regions (a collar) that take matching marking pieces touching them.
    absorbers = {r.id: r for r in regions if r.part in absorb_parts}
    absorber_lab = {
        rid: np.median(face_lab[face_regions == rid], axis=0)
        for rid in absorbers
        if (face_regions == rid).any()
    }
    absorbed: dict[int, list[np.ndarray]] = {}
    for host in host_regions:
        loc = np.flatnonzero(face_regions[hidx] == host.id)  # positions within hidx
        if loc.size == 0:
            continue
        g_adj = hadj[loc][:, loc]
        lbl = _majority(g_adj, lab_h[loc], areas[hidx[loc]], nk, majority_iters)
        host_area = float(areas[hidx[loc]].sum())
        floor = max(min_marking_frac * host_area, min_marking_mm2)
        # Fold small components of each color into the nearest larger one.
        for _ in range(3):
            small = np.zeros(loc.size, bool)
            for c in np.unique(lbl):
                idx = np.flatnonzero(lbl == c)
                ncomp, comp = connected_components(g_adj[idx][:, idx], directed=False)
                comp_area = np.bincount(comp, weights=areas[hidx[loc[idx]]], minlength=ncomp)
                small[idx[comp_area[comp] < floor]] = True
            if not small.any() or small.all():
                break
            # A small piece not connected to any larger one (an ear tip cut off by the host
            # boundary) keeps its own color.
            filled = _fill_from_nearest(g_adj, lbl, ~small)
            lbl = np.where(filled < 0, lbl, filled)
        face_cluster[hidx[loc]] = lbl
        by_color = np.bincount(lbl, weights=areas[hidx[loc]], minlength=nk)
        dominant = int(np.argmax(by_color))
        if by_color[dominant] < min_host_share * host_area:
            dominant = -1  # patchwork host: every color becomes a marking, the host retires
        else:
            region_colors[host.id] = _color_entry(centers[dominant], names[dominant])

        keep = hidx[loc[lbl == dominant]]
        rel_h_lo = float(vertices[np.unique(faces[hidx[loc]]), 2].min())
        rel_h_hi = float(vertices[np.unique(faces[hidx[loc]]), 2].max())
        comps = []
        for c in range(nk):
            if c == dominant:
                continue
            idx = np.flatnonzero(lbl == c)
            if idx.size == 0:
                continue
            ncomp, comp = connected_components(g_adj[idx][:, idx], directed=False)
            for ci in range(ncomp):
                comps.append((c, hidx[loc[idx[comp == ci]]], loc[idx[comp == ci]]))
        comps.sort(key=lambda t: (-float(areas[t[1]].sum()), int(t[1].min())))
        for c, g, gl in comps:
            target = _absorber(g, c, host, adj, face_regions, absorbers, centers, absorb_de,
                               absorber_lab)  # fmt: skip
            if target is not None:
                face_regions[g] = target
                absorbed.setdefault(target, []).append(g)
                continue
            wn = (areas[g, None] * normals[g]).sum(axis=0) / max(float(areas[g].sum()), 1e-12)
            cz = float((areas[g] @ centroids[g, 2]) / max(float(areas[g].sum()), 1e-12))
            rel = (cz - rel_h_lo) / max(rel_h_hi - rel_h_lo, 1e-9)
            name = f"marking_{names[c]}_{_position(host.part, wn, rel)}"
            label = f"{host.subject_id}.{name}" if host.subject_id else name
            base, i = label, 2
            while label in used_labels:
                label = f"{base}_{i}"
                i += 1
            used_labels.add(label)
            purity = float(areas[g] @ (raw_nearest[gl] == c)) / max(float(areas[g].sum()), 1e-12)
            reg = _region_for(
                next_id, label, host.subject_id, "marking", g, vertices, faces, areas,
                centroids, purity, [host.id],
            )  # fmt: skip
            new_regions.append(reg)
            marking_ids.append(next_id)
            region_colors[next_id] = _color_entry(centers[c], names[c])
            face_regions[g] = next_id
            next_id += 1
        if keep.size and keep.size != loc.size:
            updated[host.id] = _region_for(
                host.id, host.label, host.subject_id, host.part, keep, vertices, faces, areas,
                centroids, host.confidence, host.parent_ids, host.views_seen,
            ).model_copy(update={"source": host.source})  # fmt: skip
        elif keep.size == 0:
            updated[host.id] = None  # every face went to markings

    for rid in absorbed:
        r = absorbers[rid]
        g = np.flatnonzero(face_regions == rid)
        updated[rid] = _region_for(
            rid, r.label, r.subject_id, r.part, g, vertices, faces, areas, centroids,
            r.confidence, r.parent_ids, r.views_seen,
        ).model_copy(update={"source": r.source})  # fmt: skip
    stats["absorbed_faces"] = {rid: int(sum(len(g) for g in gs)) for rid, gs in absorbed.items()}
    retired = [rid for rid, r in updated.items() if r is None]
    out_regions = [updated.get(r.id, r) for r in regions if r.id not in retired]
    out_regions += new_regions
    clusters = [
        ColorCluster(
            index=i,
            lab=tuple(round(float(v), 2) for v in centers[i]),
            hex=lab_to_hex(centers[i]),
            name=names[i],
            area_mm2=round(float(areas[face_cluster == i].sum()), 3),
        )
        for i in range(nk)
    ]
    return MarkingResult(
        face_regions=face_regions,
        regions=out_regions,
        next_region_id=next_id,
        clusters=clusters,
        face_cluster=face_cluster,
        region_colors=region_colors,
        marking_ids=marking_ids,
        retired_ids=retired,
        stats=stats,
    )


def _absorber(g, c, host, adj, face_regions, absorbers, centers, max_de, absorber_lab):
    """Id of a same-subject absorbing region (a collar) that this piece touches and matches in
    color (CIEDE2000 under max_de), or None."""
    if not absorbers:
        return None
    touching = set(np.unique(face_regions[adj[g].indices]).tolist()) & set(absorbers)
    best, best_de = None, max_de
    for rid in sorted(touching):
        if absorbers[rid].subject_id != host.subject_id or rid not in absorber_lab:
            continue
        de = float(ciede2000(centers[c], absorber_lab[rid]))
        if de < best_de:
            best, best_de = rid, de
    return best


def _color_entry(lab, name: str | None = None) -> dict:
    return {
        "lab": [round(float(v), 2) for v in lab],
        "hex": lab_to_hex(lab),
        "name": name or color_name(lab),
    }
