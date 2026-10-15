"""Region geometry for p2p_contracts.Region: n_faces, area, centroid, bbox, min_width, views_seen.

min_width_mm (thinnest local width that matters for printing):
Face-level labels have jagged boundaries: one-triangle notches are real but tiny narrowings, so
a plain "minimum over the medial axis" reports the face size for every region. Instead:
1. Geodesic distance from every face to its region boundary (distance_to_boundary).
2. For each width w on a ladder (0.3 .. 10 mm), a surface opening with a disk of diameter w
   (erode by w/2, dilate back by w/2 inside the region) removes the parts narrower than w.
   A region "fails" at w when that removes >= 5% of its area or one connected piece of at least
   1 mm2 (a thin tail or neck).
3. The answer lies between the last passing and the first failing ladder width. Where the
   medial-axis width (adjacent faces whose nearest boundary points face each other at >= 140
   degrees; the chord between those points) falls inside that bracket it is used for precision,
   otherwise the last passing width is reported.
A region that never fails reports twice its largest boundary distance (its widest inscribed disk).
A region with no boundary (covers a whole closed component) reports None.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geodesic import components, distance_to_boundary, opening_from_distance
from .mesh import Mesh

OBJECT_ANGLE_DEG = 140.0
WIDTH_LADDER = (0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.2, 1.6, 2.0, 2.5, 3.2, 4.0, 5.0, 6.4, 8.0, 10.0)
FAIL_AREA_FRAC = 0.05
FAIL_CHUNK_MM2 = 1.0
TOL_FRAC = 0.2


@dataclass
class RegionGeometry:
    id: int
    n_faces: int
    area_mm2: float
    centroid_mm: tuple[float, float, float]
    bbox_min_mm: tuple[float, float, float]
    bbox_max_mm: tuple[float, float, float]
    min_width_mm: float | None
    views_seen: int | None = None

    def fields(self) -> dict:
        """Keyword arguments for p2p_contracts.Region (geometry part only)."""
        d = {
            "n_faces": self.n_faces,
            "area_mm2": round(self.area_mm2, 4),
            "centroid_mm": tuple(round(x, 4) for x in self.centroid_mm),
            "bbox_min_mm": tuple(round(x, 4) for x in self.bbox_min_mm),
            "bbox_max_mm": tuple(round(x, 4) for x in self.bbox_max_mm),
            "min_width_mm": None if self.min_width_mm is None else round(self.min_width_mm, 4),
        }
        if self.views_seen is not None:
            d["views_seen"] = self.views_seen
        return d


def medial_widths(mesh: Mesh, labels: np.ndarray, src: np.ndarray, pts: np.ndarray,
                  object_angle_deg: float = OBJECT_ANGLE_DEG):
    """(label per medial pair, chord width per medial pair)."""
    adj = mesh.face_adjacency
    a, b = adj[:, 0], adj[:, 1]
    ok = (labels[a] == labels[b]) & (src[a] >= 0) & (src[b] >= 0) & (src[a] != src[b])
    a, b = a[ok], b[ok]
    c = mesh.face_centroids
    u = pts[a] - c[a]
    v = pts[b] - c[b]
    nu = np.linalg.norm(u, axis=1)
    nv = np.linalg.norm(v, axis=1)
    cosang = np.einsum("ij,ij->i", u, v) / np.maximum(nu * nv, 1e-12)
    med = cosang <= np.cos(np.radians(object_angle_deg))
    return labels[a[med]], np.linalg.norm(pts[a[med]] - pts[b[med]], axis=1)


def min_widths(mesh: Mesh, labels: np.ndarray, ladder=WIDTH_LADDER,
               fail_frac: float = FAIL_AREA_FRAC, fail_chunk_mm2: float = FAIL_CHUNK_MM2):
    """Label -> thinnest printing-relevant width in mm (None when the label has no boundary)."""
    labels = np.asarray(labels).astype(np.int64)
    n = int(labels.max()) + 1 if labels.size else 1
    dist, src, pts = distance_to_boundary(mesh, labels)
    area = mesh.face_areas
    reg_area = np.bincount(labels, weights=area, minlength=n)
    present = np.bincount(labels, minlength=n) > 0
    finite = np.isfinite(dist)
    dmax = np.full(n, -np.inf)
    np.maximum.at(dmax, labels[finite], dist[finite])
    has_boundary = np.isfinite(dmax) & (dmax > -np.inf)

    w_lo = np.zeros(n)
    w_hi = np.full(n, np.inf)
    active = present & has_boundary
    for w in ladder:
        if not active.any():
            break
        removed = opening_from_distance(mesh, labels, dist, w, tol_frac=TOL_FRAC,
                                        tol_cap=10.0) & active[labels]
        rem_area = np.bincount(labels[removed], weights=area[removed], minlength=n)
        fail = rem_area >= fail_frac * np.maximum(reg_area, 1e-12)
        if removed.any():
            k, comp = components(mesh, removed, labels)
            chunk = np.bincount(comp[removed], weights=area[removed], minlength=k)
            chunk_label = np.zeros(k, dtype=np.int64)
            chunk_label[comp[removed]] = labels[removed]
            big = np.zeros(n)
            np.maximum.at(big, chunk_label, chunk)
            fail |= big >= fail_chunk_mm2
        fail &= active
        w_hi[fail] = w
        passing = active & ~fail
        w_lo[passing] = w
        active &= ~fail

    lab_p, w_p = medial_widths(mesh, labels, src, pts)
    out: dict[int, float | None] = {}
    for lab in np.nonzero(present)[0]:
        lab = int(lab)
        if not has_boundary[lab]:
            out[lab] = None
            continue
        if not np.isfinite(w_hi[lab]):  # never failed: widest inscribed disk
            out[lab] = float(max(2 * dmax[lab], w_lo[lab]))
            continue
        lo, hi = w_lo[lab], w_hi[lab]
        if lo <= 0:  # fails at the first rung: thinner than the ladder starts
            out[lab] = float(min(2 * dmax[lab], ladder[0]))
            continue
        cand = w_p[(lab_p == lab) & (w_p >= 0.9 * lo) & (w_p <= 1.1 * hi)]
        out[lab] = float(cand.min()) if cand.size else float(lo)
    return out


def region_geometry(
    mesh: Mesh, face_regions: np.ndarray, region_ids=None, with_min_width: bool = True,
) -> dict[int, RegionGeometry]:
    """Geometry for every region id present (or the requested ones). Id 0 (unlabeled) included
    only if requested explicitly."""
    fr = np.asarray(face_regions).astype(np.int64)
    if fr.shape != (mesh.n_faces,):
        raise ValueError(f"face_regions must be ({mesh.n_faces},)")
    ids = np.unique(fr) if region_ids is None else np.asarray(list(region_ids))
    if region_ids is None:
        ids = ids[ids != 0]
    n = int(fr.max()) + 1
    area = mesh.face_areas
    nf = np.bincount(fr, minlength=n)
    ar = np.bincount(fr, weights=area, minlength=n)
    cen = np.stack([np.bincount(fr, weights=area * mesh.face_centroids[:, k], minlength=n)
                    for k in range(3)], 1)
    tri = mesh.triangles
    lo = np.full((n, 3), np.inf)
    hi = np.full((n, 3), -np.inf)
    np.minimum.at(lo, fr, tri.min(axis=1))
    np.maximum.at(hi, fr, tri.max(axis=1))
    widths = min_widths(mesh, fr) if with_min_width else {}
    out = {}
    for rid in ids:
        rid = int(rid)
        if rid >= n or nf[rid] == 0:
            continue
        c = cen[rid] / max(ar[rid], 1e-12)
        out[rid] = RegionGeometry(
            id=rid, n_faces=int(nf[rid]), area_mm2=float(ar[rid]),
            centroid_mm=tuple(float(x) for x in c),
            bbox_min_mm=tuple(float(x) for x in lo[rid]),
            bbox_max_mm=tuple(float(x) for x in hi[rid]),
            min_width_mm=widths.get(rid),
        )
    return out


def views_seen(
    mesh: Mesh, face_regions: np.ndarray, min_px: int = 12, size_px: int = 256,
    backend: str | None = None,
) -> dict[int, int]:
    """Region id -> number of contract views in which at least min_px pixels are visible."""
    from p2p_contracts.regions import VIEWS

    from .camera import camera_matrices
    from .raster import rasterize

    fr = np.asarray(face_regions).astype(np.int64)
    n = int(fr.max()) + 1
    seen = np.zeros(n, dtype=np.int64)
    for spec in VIEWS.values():
        r = rasterize(mesh, camera_matrices(spec, mesh, size_px), backend)
        cnt = np.bincount(fr[r.face_id[r.mask]], minlength=n)
        seen += cnt >= min_px
    return {int(i): int(seen[i]) for i in np.unique(fr)}


def fill_regions(region_map, mesh: Mesh, face_regions: np.ndarray, with_views: bool = True):
    """Return a copy of a p2p_contracts.RegionMap with geometry fields recomputed."""
    geo = region_geometry(mesh, face_regions)
    vs = views_seen(mesh, face_regions) if with_views else {}
    regions = []
    for reg in region_map.regions:
        g = geo.get(reg.id)
        if g is None:
            upd = {"n_faces": 0, "area_mm2": 0.0, "min_width_mm": None}
        else:
            g.views_seen = vs.get(reg.id, reg.views_seen) if with_views else None
            upd = g.fields()
        regions.append(reg.model_copy(update=upd))
    return region_map.model_copy(update={"regions": regions})
