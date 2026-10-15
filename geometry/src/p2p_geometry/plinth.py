"""Meshy's own plinth: detect the bottom slab and cut it off with a capped plane.

Meshy figurines usually stand on a thin disc. On a flat product (keychain, magnet) the disc
lies on its edge and shows as a bar under the paws, so it goes. Face correspondence survives:
faces wholly below the plane are dropped, faces crossing it are clipped (the pieces keep their
source face), and the cap is appended. Everything is recorded in face_source.

Detection works on the upright figure (Z up) from the horizontal cross-section area profile:
a plinth is a flat-bottomed slab that covers much of the figure's footprint, keeps a nearly
constant section for a short height, and then the section drops sharply (only the paws stand on
it). The SAM 3 "base" label does not exist yet at stage 6, so this is geometric only.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .meshio import projected_silhouette
from .solid import voxelize


@dataclass
class Plinth:
    found: bool
    reason: str
    z_bottom: float = 0.0
    z_top: float = 0.0  # plinth top plane (upright frame, same units as the input)
    area_ratio: float = 0.0  # plinth section / figure shadow
    drop_ratio: float = 0.0  # section just above the top / plinth section
    height_frac: float = 0.0  # plinth height / figure height


def detect_plinth(vertices: np.ndarray, faces: np.ndarray) -> Plinth:
    """vertices in an upright Z-up frame (any units)."""
    v = np.asarray(vertices, dtype=np.float64)
    f = np.asarray(faces)
    zmin, zmax = float(v[:, 2].min()), float(v[:, 2].max())
    height = zmax - zmin
    pitch = height / 220.0
    sol = voxelize(v, f, pitch)
    area = sol.occ.sum(axis=(0, 1)) * pitch**2
    zs = sol.zs()
    ks = np.nonzero(area > 0)[0]
    if len(ks) == 0:
        return Plinth(False, "empty figure")
    k0 = int(ks[0])
    shadow = projected_silhouette(v, f).area()
    nb = max(2, int(round(0.01 * height / pitch)))
    base = float(np.median(area[k0 : k0 + nb]))
    ratio = base / max(shadow, 1e-12)
    if ratio < 0.25:
        return Plinth(False, f"bottom section is {ratio:.0%} of the shadow; no plinth",
                      area_ratio=ratio)
    k = k0
    limit = k0 + int(0.25 * height / pitch)
    while k < min(limit, len(area)) and area[k] >= 0.6 * base:
        k += 1
    if k >= limit:
        return Plinth(False, "bottom section never drops; the figure is a block", area_ratio=ratio)
    slab = area[k0:k]
    if len(slab) < 2 or slab.std() / max(slab.mean(), 1e-12) > 0.25:
        return Plinth(False, "bottom section is not a slab", area_ratio=ratio)
    z_est = float(zs[k - 1] + pitch / 2)
    # Refine to the plinth's flat top: area-weighted median height of upward faces near it.
    tri = v[f]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    a = np.linalg.norm(n, axis=1)
    nz = n[:, 2] / np.maximum(a, 1e-30)
    zc = tri[:, :, 2].mean(axis=1)
    sel = (nz > 0.95) & (np.abs(zc - z_est) < 3 * pitch)
    z_top = z_est
    if sel.any():
        order = np.argsort(zc[sel])
        w = np.cumsum(a[sel][order])
        z_top = float(zc[sel][order][np.searchsorted(w, w[-1] / 2)])
    above = area[min(k + 1, len(area) - 1)]
    return Plinth(
        True, "flat slab under the figure", z_bottom=zmin, z_top=z_top, area_ratio=ratio,
        drop_ratio=float(above / base), height_frac=(z_top - zmin) / height,
    )


def cut_below(vertices: np.ndarray, faces: np.ndarray, z_cut: float
              ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    """Remove everything below z = z_cut and cap the opening.

    Returns (vertices, faces, face_map, vertex_map, cap_faces): face_map[i] is the input face
    that output face i came from (-1 for cap faces); vertex_map[j] is the output index of input
    vertex j (-1 if dropped). Kept faces stay in input order; a clipped face keeps its slot with
    its first piece, second pieces and the cap are appended.
    """
    from manifold3d import triangulate

    v = np.asarray(vertices, dtype=np.float64)
    f = np.asarray(faces, dtype=np.int64)
    span = float(np.ptp(v[:, 2])) or 1.0
    eps = 1e-7 * span
    while np.any(np.abs(v[:, 2] - z_cut) < eps):
        z_cut += 3 * eps
    s = v[:, 2] - z_cut
    up = s > 0
    n_up = up[f].sum(axis=1)
    new_v: list[np.ndarray] = []
    edge_vid: dict[tuple[int, int], int] = {}
    nv = len(v)

    def cross(i: int, j: int) -> int:
        nonlocal nv
        key = (i, j) if i < j else (j, i)
        if key not in edge_vid:
            t = s[i] / (s[i] - s[j])
            new_v.append(v[i] + t * (v[j] - v[i]))
            edge_vid[key] = nv
            nv += 1
        return edge_vid[key]

    out_f: list[list[int]] = []
    out_src: list[int] = []
    extra_f: list[list[int]] = []
    extra_src: list[int] = []
    for fi in range(len(f)):
        if n_up[fi] == 3:
            out_f.append(list(f[fi]))
            out_src.append(fi)
        elif n_up[fi] == 0:
            continue
        else:
            poly = []
            tri = f[fi]
            for k in range(3):
                p, q = int(tri[k]), int(tri[(k + 1) % 3])
                if up[p]:
                    poly.append(p)
                if up[p] != up[q]:
                    poly.append(cross(p, q))
            out_f.append([poly[0], poly[1], poly[2]])
            out_src.append(fi)
            if len(poly) == 4:
                extra_f.append([poly[0], poly[2], poly[3]])
                extra_src.append(fi)
    allv = np.concatenate([v, np.array(new_v).reshape(-1, 3)])
    faces_out = np.array(out_f + extra_f, dtype=np.int64).reshape(-1, 3)
    src = np.array(out_src + extra_src, dtype=np.int64)

    # Cap: boundary loops lie on the plane. Reverse them (the cap walks each edge the other
    # way) and triangulate in mirrored XY so the cap faces point down, out of the solid.
    from .repair import _boundary_loops

    loops = _boundary_loops(faces_out)
    cap = np.zeros((0, 3), dtype=np.int64)
    if loops:
        rev = [lp[::-1] for lp in loops]
        polys = [allv[lp][:, :2] * np.array([1.0, -1.0]) for lp in rev]
        idx = np.concatenate([np.array(lp) for lp in rev])
        tris = np.asarray(triangulate(polys), dtype=np.int64)
        cap = idx[tris] if len(tris) else cap
        if len(cap):
            t = allv[cap]
            nz = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])[:, 2]
            if nz.sum() > 0:  # make sure the cap faces down
                cap = cap[:, ::-1]
    faces_out = np.concatenate([faces_out, cap])
    src = np.concatenate([src, -np.ones(len(cap), dtype=np.int64)])

    used = np.zeros(len(allv), dtype=bool)
    used[faces_out.ravel()] = True
    remap = -np.ones(len(allv), dtype=np.int64)
    remap[used] = np.arange(used.sum())
    return allv[used], remap[faces_out], src, remap[: len(v)], len(cap)
