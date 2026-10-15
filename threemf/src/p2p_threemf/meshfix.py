"""Mesh cleanup before writing: drop degenerate and duplicate triangles, weld equal vertices.

Bambu repairs meshes on load anyway (admesh removes degenerate facets), so this changes nothing
visible; it keeps what we write equal to what Bambu slices, so the read-back compares like with
like and `mesh_stat degenerate_facets="0"` in model_settings is true.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class CleanReport:
    faces_in: int
    faces_out: int
    repeated_index: int  # a vertex used twice in one triangle
    zero_area: int  # three distinct but collinear (or coincident) vertices
    duplicate: int  # the same three vertices as an earlier triangle
    vertices_welded: int
    area_removed_mm2: float


def clean_mesh(vertices, faces, face_slots, min_area_mm2: float = 1e-10
               ) -> tuple[np.ndarray, np.ndarray, np.ndarray, CleanReport]:
    v = np.asarray(vertices, dtype=np.float64)
    f = np.asarray(faces, dtype=np.int64)
    s = np.asarray(face_slots)
    n_in = len(f)
    # Weld vertices with identical float32 coordinates (what the 3MF stores).
    key = np.ascontiguousarray(v.astype(np.float32)).view(np.dtype((np.void, 12))).ravel()
    _, first, inv = np.unique(key, return_index=True, return_inverse=True)
    welded = len(v) - len(first)
    f = inv.reshape(-1)[f]
    v = v[first]
    rep = (f[:, 0] == f[:, 1]) | (f[:, 1] == f[:, 2]) | (f[:, 0] == f[:, 2])
    tri = v[f]
    area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    zero = ~rep & (area < min_area_mm2)
    keep = ~(rep | zero)
    srt = np.sort(f, axis=1)
    _, uniq_idx = np.unique(srt[keep], axis=0, return_index=True)
    dup_mask = np.zeros(len(f), dtype=bool)
    kept_idx = np.flatnonzero(keep)
    dup_mask[kept_idx] = True
    dup_mask[kept_idx[uniq_idx]] = False
    keep &= ~dup_mask
    removed_area = float(area[~keep].sum())
    f, s = f[keep], s[keep]
    used = np.unique(f)
    remap = np.full(len(v), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    return v[used], remap[f], s, CleanReport(
        faces_in=n_in, faces_out=int(keep.sum()), repeated_index=int(rep.sum()),
        zero_area=int(zero.sum()), duplicate=int(dup_mask.sum()), vertices_welded=int(welded),
        area_removed_mm2=round(removed_area, 6))
