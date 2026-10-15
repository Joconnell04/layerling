"""Region topology and boundary edits on a per-face label array, in mm on the mesh surface.

Every function takes a p2p_sim Mesh and face_regions (int, one label per triangle, 0 unlabeled)
and returns a NEW label array; nothing mutates its input. Distances are geodesic: they travel
over the welded face graph (nearest-point propagation from p2p_sim.geodesic, or Dijkstra over
face centroids where a barrier such as a crease must be respected).
"""

from __future__ import annotations

import numpy as np
from p2p_sim.geodesic import boundary_seeds, components, propagate_nearest, thin_faces
from p2p_sim.mesh import Mesh
from scipy.sparse.csgraph import dijkstra

# ---------------------------------------------------------------- basics


def region_areas(mesh: Mesh, fr: np.ndarray) -> np.ndarray:
    return np.bincount(fr, weights=mesh.face_areas, minlength=int(fr.max()) + 1)


def boundary_pairs(mesh: Mesh, fr: np.ndarray) -> np.ndarray:
    adj = mesh.face_adjacency
    return fr[adj[:, 0]] != fr[adj[:, 1]]


def shared_boundary(mesh: Mesh, keys: np.ndarray, fr: np.ndarray):
    """For each key (component id per face, -1 ignored): (key, neighbor label, shared mm) rows,
    summed over edges between a face of that key and a face of a different key."""
    adj = mesh.face_adjacency
    ln = mesh.adjacency_edge_len
    ka, kb = keys[adj[:, 0]], keys[adj[:, 1]]
    diff = ka != kb
    rows_k = np.concatenate([ka[diff], kb[diff]])
    rows_l = np.concatenate([fr[adj[diff, 1]], fr[adj[diff, 0]]])
    w = np.concatenate([ln[diff], ln[diff]])
    ok = rows_k >= 0
    rows_k, rows_l, w = rows_k[ok], rows_l[ok], w[ok]
    if rows_k.size == 0:
        return np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(0)
    key = rows_k.astype(np.int64) * (int(fr.max()) + 2) + rows_l
    uk, inv = np.unique(key, return_inverse=True)
    tot = np.bincount(inv, weights=w)
    return uk // (int(fr.max()) + 2), uk % (int(fr.max()) + 2), tot


def dominant_neighbor(mesh: Mesh, keys: np.ndarray, fr: np.ndarray, exclude=None) -> dict:
    """key -> neighbor label sharing the longest boundary (optionally excluding labels)."""
    k, lab, tot = shared_boundary(mesh, keys, fr)
    if exclude is not None:
        ex = np.isin(lab, np.asarray(list(exclude)))
        k, lab, tot = k[~ex], lab[~ex], tot[~ex]
    out: dict[int, int] = {}
    order = np.lexsort((-tot, k))
    ks = k[order]
    first = np.ones(len(ks), bool)
    first[1:] = ks[1:] != ks[:-1]
    for i in order[first]:
        out[int(k[i])] = int(lab[i])
    return out


def fill_from_neighbors(mesh: Mesh, fr: np.ndarray, mask: np.ndarray,
                        exclude_labels=()) -> np.ndarray:
    """Faces in `mask` take the label of the nearest face outside it (geodesic, through the
    masked faces). Labels in exclude_labels are never used as a source."""
    out = fr.copy()
    if not mask.any():
        return out
    adj = mesh.face_adjacency
    a_in, b_in = mask[adj[:, 0]], mask[adj[:, 1]]
    cross = a_in ^ b_in
    outside = np.where(a_in[cross], adj[cross, 1], adj[cross, 0])
    if len(exclude_labels):
        outside = outside[~np.isin(fr[outside], np.asarray(list(exclude_labels)))]
    seeds = np.unique(outside)
    if seeds.size == 0:
        return out
    trav = np.where(mask, 1, 0)
    trav[seeds] = 1
    _, src, _ = propagate_nearest(mesh, trav, seeds, mesh.face_centroids[seeds])
    got = mask & (src >= 0)
    out[got] = fr[src[got]]
    return out


# ---------------------------------------------------------------- boundary edits


def grow(mesh: Mesh, fr: np.ndarray, rid: int, mm: float, protect=()) -> np.ndarray:
    """Faces outside region `rid` within `mm` of its boundary join it."""
    inside = fr == rid
    if not inside.any():
        raise ValueError(f"region {rid} has no faces")
    binary = inside.astype(np.int64)
    faces, pts = boundary_seeds(mesh, binary)
    keep = ~inside[faces]
    faces, pts = faces[keep], pts[keep]
    d, _, _ = propagate_nearest(mesh, binary, faces, pts, limit=mm)
    take = (~inside) & (d <= mm)
    if len(protect):
        take &= ~np.isin(fr, np.asarray(list(protect)))
    out = fr.copy()
    out[take] = rid
    return out


def shrink(mesh: Mesh, fr: np.ndarray, rid: int, mm: float) -> np.ndarray:
    """Faces of `rid` within `mm` of its boundary go to the neighbors across that boundary.
    Refuses (ValueError) when nothing of the region would be left."""
    inside = fr == rid
    if not inside.any():
        raise ValueError(f"region {rid} has no faces")
    binary = inside.astype(np.int64)
    faces, pts = boundary_seeds(mesh, binary)
    keep = inside[faces]
    d, _, _ = propagate_nearest(mesh, binary, faces[keep], pts[keep], limit=mm)
    drop = inside & (d <= mm)
    if drop.sum() >= inside.sum():
        raise ValueError(f"region {rid} is narrower than {2 * mm:.2f} mm everywhere; "
                         "shrinking would delete it (use merge_regions or despeckle)")
    return fill_from_neighbors(mesh, fr, drop, exclude_labels=[rid])


def smooth_boundary(mesh: Mesh, fr: np.ndarray, rid: int, mm: float) -> np.ndarray:
    """Morphological closing then opening of region `rid` with a disk of radius `mm`: notches
    and gaps narrower than 2 mm fill in, spurs narrower than 2 mm go to their neighbors."""
    inside = fr == rid
    if not inside.any():
        raise ValueError(f"region {rid} has no faces")
    out = fr.copy()
    width = 2.0 * mm
    thin_out = thin_faces(mesh, inside.astype(np.int64), width) & ~inside
    out[thin_out] = rid
    inside2 = out == rid
    thin_in = thin_faces(mesh, inside2.astype(np.int64), width) & inside2
    if thin_in.sum() >= inside2.sum():
        return out  # the whole region is thinner than the disk: keep it, closing only
    return fill_from_neighbors(mesh, out, thin_in, exclude_labels=[rid])


def crease_pairs(mesh: Mesh, angle_deg: float) -> np.ndarray:
    """bool (E,): adjacency pairs whose dihedral angle is at least angle_deg."""
    n = mesh.face_normals
    adj = mesh.face_adjacency
    cos = np.einsum("ij,ij->i", n[adj[:, 0]], n[adj[:, 1]])
    return cos <= np.cos(np.radians(angle_deg))


def snap_to_creases(mesh: Mesh, fr: np.ndarray, region_ids=None, max_shift_mm: float = 1.0,
                    crease_angle_deg: float = 30.0, min_area_mm2: float = 1.0) -> np.ndarray:
    """Move label boundaries onto nearby creases.

    Band = faces within max_shift_mm of a boundary of the selected regions. Every band face
    takes the label of the nearest face outside the band, once travelling freely and once
    never crossing a crease edge. Only faces where the crease changes the answer are relabeled,
    so smooth stretches of boundary stay where they are and a boundary that runs next to a
    crease moves onto it. Pieces under min_area_mm2 the move creates merge into a neighbor."""
    fr = np.asarray(fr)
    adj = mesh.face_adjacency
    diff = fr[adj[:, 0]] != fr[adj[:, 1]]
    if region_ids is not None:
        sel = np.isin(fr[adj[:, 0]], region_ids) | np.isin(fr[adj[:, 1]], region_ids)
        diff &= sel
    if not diff.any():
        return fr.copy()
    edge_faces = np.unique(adj[diff].ravel())
    crease = crease_pairs(mesh, crease_angle_deg)
    g_all = mesh.face_graph()
    d_band = dijkstra(g_all, indices=edge_faces, min_only=True, limit=max_shift_mm)
    band = np.isfinite(d_band)
    core = ~band
    # cores adjacent to the band are the sources
    touch = (band[adj[:, 0]] & core[adj[:, 1]]) | (band[adj[:, 1]] & core[adj[:, 0]])
    src = np.unique(np.where(band[adj[touch, 0]], adj[touch, 1], adj[touch, 0]))
    if src.size == 0:
        return fr.copy()
    within = band[adj[:, 0]] | band[adj[:, 1]]
    lim = 3.0 * max_shift_mm + 1.0
    _, _, s_free = dijkstra(mesh.face_graph(within), indices=src, min_only=True, limit=lim,
                            return_predecessors=True)
    _, _, s_crs = dijkstra(mesh.face_graph(within & ~crease), indices=src, min_only=True,
                           limit=lim, return_predecessors=True)
    out = fr.copy()
    ok = band & (s_free >= 0) & (s_crs >= 0)
    lab_free = np.where(s_free >= 0, fr[np.maximum(s_free, 0)], -1)
    lab_crs = np.where(s_crs >= 0, fr[np.maximum(s_crs, 0)], -1)
    change = ok & (lab_free != lab_crs)
    out[change] = lab_crs[change]
    return _drop_new_slivers(mesh, fr, out, min_area_mm2)


def _drop_new_slivers(mesh: Mesh, before: np.ndarray, after: np.ndarray,
                      min_area_mm2: float) -> np.ndarray:
    """Pieces under min_area_mm2 that an edit created (they contain a changed face) merge into
    the neighbor they share the most edge with, so a boundary move never leaves specks."""
    changed = before != after
    if not changed.any():
        return after
    n, comp = components(mesh, np.ones(mesh.n_faces, bool), after)
    ca = np.bincount(comp, weights=mesh.face_areas, minlength=n)
    touched = np.zeros(n, bool)
    touched[comp[changed]] = True
    adj = mesh.face_adjacency  # pieces cut off next to a moved boundary count too
    near = changed[adj[:, 0]] | changed[adj[:, 1]]
    touched[comp[adj[near].ravel()]] = True
    small = touched & (ca < min_area_mm2)
    if not small.any():
        return after
    dom = dominant_neighbor(mesh, np.where(small[comp], comp, -1), after)
    out = after.copy()
    target = np.full(n, -1, np.int64)
    for c, lab in dom.items():
        target[c] = lab
    mv = small[comp] & (target[comp] >= 0)
    out[mv] = target[comp[mv]]
    return out


def boundary_on_crease_share(mesh: Mesh, fr: np.ndarray, angle_deg: float = 30.0) -> float:
    diff = boundary_pairs(mesh, fr)
    if not diff.any():
        return 1.0
    ln = mesh.adjacency_edge_len
    cr = crease_pairs(mesh, angle_deg)
    return float(ln[diff & cr].sum() / max(ln[diff].sum(), 1e-12))


# ---------------------------------------------------------------- islands


def despeckle(mesh: Mesh, fr: np.ndarray, min_area_mm2: float, region_ids=None,
              face_filament: np.ndarray | None = None, max_pass: int = 6) -> np.ndarray:
    """Merge connected pieces under min_area_mm2 into the neighbor they share the most edge
    with. Pieces are same-region components; with face_filament (region -> filament resolved
    per face) a second pass also merges same-filament islands (a speck made of two tiny
    regions). region_ids limits which regions may lose pieces."""
    out = np.asarray(fr).copy()
    area = mesh.face_areas
    allowed = None if region_ids is None else np.asarray(list(region_ids))
    for _ in range(max_pass):
        n, comp = components(mesh, np.ones(mesh.n_faces, bool), out)
        ca = np.bincount(comp, weights=area, minlength=n)
        small = ca < min_area_mm2
        comp_lab = np.zeros(n, np.int64)
        comp_lab[comp] = out
        if allowed is not None:
            small &= np.isin(comp_lab, allowed)
        if not small.any():
            break
        keys = np.where(small[comp], comp, -1)
        dom = dominant_neighbor(mesh, keys, out)
        if not dom:
            break
        # biggest specks first would not matter: each keeps its own target
        target = np.full(n, -1, np.int64)
        for c, lab in dom.items():
            target[c] = lab
        moved = small & (target >= 0)
        if not moved.any():
            break
        mf = moved[comp]
        out[mf] = target[comp[mf]]
    if face_filament is not None:
        out = _despeckle_filament(mesh, out, face_filament, min_area_mm2, allowed)
    return out


def _despeckle_filament(mesh, fr, face_filament, min_area_mm2, allowed, max_pass: int = 4):
    out = fr.copy()
    area = mesh.face_areas
    fil = np.asarray(face_filament)
    for _ in range(max_pass):
        n, comp = components(mesh, np.ones(mesh.n_faces, bool), fil)
        ca = np.bincount(comp, weights=area, minlength=n)
        small = ca < min_area_mm2
        if allowed is not None:
            ok = np.zeros(n, bool)
            np.logical_or.at(ok, comp, np.isin(out, allowed))
            small &= ok
        if not small.any():
            break
        keys = np.where(small[comp], comp, -1)
        # neighbor region with a DIFFERENT filament, by shared edge length
        k, lab, tot = shared_boundary(mesh, keys, out)
        if k.size == 0:
            break
        # a neighbor face's filament: take any face of that region
        reg_fil = {}
        for r in np.unique(lab):
            reg_fil[int(r)] = fil[np.argmax(out == r)]
        comp_fil = np.zeros(n, dtype=fil.dtype)
        comp_fil[comp] = fil
        good = np.array([reg_fil[int(lb)] != comp_fil[int(c)] for c, lb in zip(k, lab)])
        k, lab, tot = k[good], lab[good], tot[good]
        if k.size == 0:
            break
        order = np.lexsort((-tot, k))
        ks = k[order]
        first = np.ones(len(ks), bool)
        first[1:] = ks[1:] != ks[:-1]
        target = np.full(n, -1, np.int64)
        target[k[order[first]]] = lab[order[first]]
        mf = (target[comp] >= 0) & small[comp]
        if not mf.any():
            break
        out[mf] = target[comp[mf]]
        fil = fil.copy()
        for c in np.unique(comp[mf]):
            fil[comp == c] = reg_fil[int(target[c])]
    return out


def islands(mesh: Mesh, face_filament: np.ndarray, min_area_mm2: float):
    """Same-filament connected pieces under min_area_mm2: (count, total area)."""
    n, comp = components(mesh, np.ones(mesh.n_faces, bool), np.asarray(face_filament))
    ca = np.bincount(comp, weights=mesh.face_areas, minlength=n)
    small = ca < min_area_mm2
    return int(small.sum()), float(ca[small].sum())


# ---------------------------------------------------------------- broken bands
#
# live2 sombrero: the hat's striped band came out of Meshy's 5-color paint as red and black
# dashes with straw gaps; the agent kept one color and the final check recolored it red, so the
# print showed 15 red pieces (largest 12%, gaps 3.7 mm median and 8.3 mm at most, pieces 1.7 to
# 3.8 mm wide; derived from the job files). No piece was too thin to print, so despeckle and the
# islands check (pieces under 1 mm2) never saw it. A band must print as one strip, or not at all.

FRAG_MIN_AREA_MM2 = 10.0  # a region this big or more can read as a band (ASSUMED)
FRAG_MIN_PIECES = 4  # more than 3 separate pieces (ASSUMED)
FRAG_LARGEST_MAX = 0.6  # and no piece holds 60% of it (ASSUMED)
BRIDGE_GAP_MM = 10.0  # gaps up to this close when a band is bridged (ASSUMED; covers 8.3)
BRIDGE_MAX_GROWTH = 1.5  # the bridges may add at most 1.5x the band's own area (ASSUMED): a
#                          dashed band about doubles; more means it was not a band
# Words that say a region is a band-like part (hat band, trim, collar...), in its label or in
# the final check's note about it. Coat stripes, spots and patches are markings: they are never
# bridged or merged away here (the markings check keeps them).
BAND_WORDS = ("band", "trim", "strap", "collar", "ribbon", "brim", "belt", "bandana", "scarf",
              "sash", "hem", "rim", "border", "harness", "leash", "headband", "bow")


def band_like(*texts: str | None) -> bool:
    import re

    words = set()
    for t in texts:
        words |= set(re.findall(r"[a-z]+", (t or "").lower()))
    return any(w in words for w in BAND_WORDS)


def pieces(mesh: Mesh, fr: np.ndarray, rid: int) -> dict:
    """How region rid lies on the surface: separate pieces, total area, the largest share."""
    inside = np.asarray(fr) == rid
    if not inside.any():
        return {"pieces": 0, "area_mm2": 0.0, "largest_share": 0.0}
    n, comp = components(mesh, inside)
    area = np.bincount(comp[inside], weights=mesh.face_areas[inside], minlength=n)
    tot = float(area.sum())
    return {"pieces": int(n), "area_mm2": round(tot, 3),
            "largest_share": round(float(area.max()) / max(tot, 1e-12), 3)}


def fragmented(info: dict) -> bool:
    """A region big enough to read as a shape that prints as scattered pieces."""
    return (info["area_mm2"] >= FRAG_MIN_AREA_MM2 and info["pieces"] >= FRAG_MIN_PIECES
            and info["largest_share"] < FRAG_LARGEST_MAX)


def band_width(mesh: Mesh, fr: np.ndarray, rid: int) -> float:
    """The band's own width: twice the median over its pieces of the deepest point's distance
    to the piece's edge, clamped to what prints and still reads as a band (0.8 to 4 mm)."""
    from p2p_sim.geodesic import distance_to_boundary

    inside = np.asarray(fr) == rid
    d, _, _ = distance_to_boundary(mesh, inside.astype(np.int64), limit=4.0)
    n, comp = components(mesh, inside)
    deep = np.zeros(n)
    dd = np.where(np.isfinite(d), d, 2.0)
    np.maximum.at(deep, comp[inside], dd[inside])
    return float(np.clip(2.0 * np.median(deep), 0.8, 4.0))


def bridge_pieces(mesh: Mesh, fr: np.ndarray, rid: int, max_gap_mm: float,
                  width_mm: float | None = None, protect=()) -> tuple[np.ndarray, dict]:
    """Join the pieces of region rid across gaps up to max_gap_mm with strips as wide as the
    band. Every two neighboring pieces (they meet in the surface's nearest-piece map) whose
    gap is short enough get the shortest face path between them, widened to the band's width.
    Faces of labels in `protect` are never crossed or painted. Returns (labels, info)."""
    fr = np.asarray(fr)
    inside = fr == rid
    if not inside.any():
        raise ValueError(f"region {rid} has no faces")
    n, comp = components(mesh, inside)
    width = width_mm or band_width(mesh, fr, rid)
    info = {"width_mm": round(width, 2), "bridges": 0}
    if n <= 1:
        return fr.copy(), info
    allowed = np.ones(mesh.n_faces, bool)
    if len(protect):
        allowed &= ~np.isin(fr, np.asarray(list(protect)))
    allowed |= inside
    adj = mesh.face_adjacency
    ok = allowed[adj[:, 0]] & allowed[adj[:, 1]]
    dist, pred, src = dijkstra(mesh.face_graph(ok), indices=np.nonzero(inside)[0],
                               min_only=True, limit=max_gap_mm, return_predecessors=True)
    reached = np.isfinite(dist) & (src >= 0)
    piece = np.full(mesh.n_faces, -1, np.int64)
    piece[reached] = comp[src[reached]]
    a, b = adj[ok, 0], adj[ok, 1]
    meet = reached[a] & reached[b] & (piece[a] != piece[b])
    a, b = a[meet], b[meet]
    c = mesh.face_centroids
    gap = dist[a] + dist[b] + np.linalg.norm(c[a] - c[b], axis=1)
    short = gap <= max_gap_mm
    a, b, gap = a[short], b[short], gap[short]
    if a.size == 0:
        return fr.copy(), info
    # one bridge per pair of neighboring pieces: the shortest meeting
    pa, pb = np.minimum(piece[a], piece[b]), np.maximum(piece[a], piece[b])
    order = np.lexsort((gap, pb, pa))
    key = pa[order] * n + pb[order]
    first = np.ones(len(key), bool)
    first[1:] = key[1:] != key[:-1]
    path = np.zeros(mesh.n_faces, bool)
    for i in order[first]:
        for f in (int(a[i]), int(b[i])):
            while f >= 0 and not inside[f] and not path[f]:
                path[f] = True
                f = int(pred[f])
    info["bridges"] = int(first.sum())
    seeds = np.nonzero(path)[0]
    trav = np.where(allowed, 1, 0).astype(np.int64)
    d, _, _ = propagate_nearest(mesh, trav, seeds, c[seeds], limit=width / 2.0)
    add = allowed & (path | (d <= width / 2.0))
    out = fr.copy()
    out[add] = rid
    return out, info


def consolidate(mesh: Mesh, fr: np.ndarray, rid: int, *, gap_mm: float = BRIDGE_GAP_MM,
                max_growth: float = BRIDGE_MAX_GROWTH, protect=(),
                merge: bool = True) -> tuple[np.ndarray, dict]:
    """A band that prints as scattered pieces becomes one readable strip, or leaves.

    Not fragmented: unchanged. Else neighboring pieces are bridged across gaps up to gap_mm
    (bridge_pieces, strips at the band's own width); kept when the result is no longer
    fragmented and the bridges add at most max_growth x the band's area. Otherwise, with
    merge, every piece goes to the neighbor it shares the most edge with (the band takes its
    surroundings' color), so the print never shows the band as spots. Returns (new labels,
    what happened)."""
    fr = np.asarray(fr)
    before = pieces(mesh, fr, rid)
    if not fragmented(before):
        return fr.copy(), {"region": rid, "action": "none", "before": before}
    closed, how = bridge_pieces(mesh, fr, rid, gap_mm, protect=protect)
    after = pieces(mesh, closed, rid)
    growth = after["area_mm2"] / max(before["area_mm2"], 1e-12) - 1.0
    info = {"region": rid, "before": before, "bridged": after, "growth": round(growth, 3),
            **how}
    if not fragmented(after) and growth <= max_growth:
        return closed, {**info, "action": "bridged"}
    if not merge:
        return fr.copy(), {**info, "action": "left"}
    inside = fr == rid
    n, comp = components(mesh, inside)
    dom = dominant_neighbor(mesh, np.where(inside, comp, -1), fr, exclude=[rid])
    out = fr.copy()
    target = np.full(n, -1, np.int64)
    for c, lab in dom.items():
        target[c] = lab
    mv = inside & (target[np.maximum(comp, 0)] >= 0)
    out[mv] = target[comp[mv]]
    into = sorted({int(x) for x in target[target >= 0]})
    return out, {**info, "action": "merged", "into": into}


# ---------------------------------------------------------------- topology


def split_components(mesh: Mesh, fr: np.ndarray, rid: int) -> list[np.ndarray]:
    """Face masks of each connected piece of region rid, largest first."""
    inside = fr == rid
    n, comp = components(mesh, inside)
    area = np.bincount(comp[inside], weights=mesh.face_areas[inside], minlength=n)
    return [comp == c for c in np.argsort(-area)]


def split_plane(mesh: Mesh, fr: np.ndarray, rid: int, point, normal) -> list[np.ndarray]:
    inside = fr == rid
    nrm = np.asarray(normal, float)
    nrm = nrm / max(np.linalg.norm(nrm), 1e-12)
    s = (mesh.face_centroids - np.asarray(point, float)) @ nrm
    return [inside & (s >= 0), inside & (s < 0)]


def split_seeds(mesh: Mesh, fr: np.ndarray, rid: int, seeds) -> list[np.ndarray]:
    """Each face of rid goes to the geodesically nearest seed face (inside the region)."""
    inside = fr == rid
    seeds = [int(s) for s in seeds if 0 <= int(s) < mesh.n_faces and inside[int(s)]]
    if len(seeds) < 2:
        raise ValueError("geodesic_seed needs at least 2 seed faces inside the region")
    adj = mesh.face_adjacency
    g = mesh.face_graph(inside[adj[:, 0]] & inside[adj[:, 1]])
    _, _, src = dijkstra(g, indices=seeds, min_only=True, return_predecessors=True)
    masks = [inside & (src == s) for s in seeds]
    stray = inside & (src < 0)
    if stray.any():
        masks[0] |= stray
    return masks
