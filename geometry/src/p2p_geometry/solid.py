"""Voxel solid by vertical ray parity, with exact per-column surface heights.

Used for local thickness (loop placement), feet detection, overlap checks and pocket checks.
Rays run along +Z through column centers, so `z_top` and `z_bottom` are exact surface heights.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property

import numpy as np
from scipy import ndimage

# Irrational sub-pitch offsets keep rays off edges and vertices of axis-aligned meshes.
_JX = 0.000913 * np.sqrt(2)
_JY = 0.000877 * np.sqrt(3)


@dataclass
class Solid:
    occ: np.ndarray  # (nx, ny, nz) bool, voxel centers inside the mesh
    origin: np.ndarray  # world coords of voxel (0, 0, 0) center (before column jitter)
    pitch: float
    z_top: np.ndarray  # (nx, ny) highest surface hit per column, nan if none
    z_bottom: np.ndarray  # (nx, ny) lowest surface hit per column, nan if none
    z_top_entry: np.ndarray  # (nx, ny) where the topmost solid run starts, nan if none
    odd_columns: int = 0  # columns with an odd number of hits (leaks)
    _cache: dict = field(default_factory=dict, repr=False)

    @property
    def shape(self) -> tuple[int, int, int]:
        return self.occ.shape  # type: ignore[return-value]

    def xs(self) -> np.ndarray:
        return self.origin[0] + np.arange(self.shape[0]) * self.pitch + _JX * self.pitch

    def ys(self) -> np.ndarray:
        return self.origin[1] + np.arange(self.shape[1]) * self.pitch + _JY * self.pitch

    def zs(self) -> np.ndarray:
        return self.origin[2] + np.arange(self.shape[2]) * self.pitch

    def index(self, pts: np.ndarray) -> np.ndarray:
        idx = np.round((np.atleast_2d(pts) - self.origin) / self.pitch).astype(np.int64)
        return idx

    def inside_grid(self, idx: np.ndarray) -> np.ndarray:
        return np.all((idx >= 0) & (idx < np.array(self.shape)), axis=1)

    def contains(self, pts: np.ndarray) -> np.ndarray:
        idx = self.index(pts)
        ok = self.inside_grid(idx)
        out = np.zeros(len(idx), dtype=bool)
        out[ok] = self.occ[idx[ok, 0], idx[ok, 1], idx[ok, 2]]
        return out

    @cached_property
    def edt(self) -> np.ndarray:
        """Distance (mm) from each solid voxel center to the nearest empty voxel center."""
        return ndimage.distance_transform_edt(self.occ, sampling=self.pitch)

    def thickness_at(self, pts: np.ndarray, radius_mm: float = 1.0) -> np.ndarray:
        """Local thickness: diameter of the largest inscribed ball within radius of each point."""
        r = max(1, int(round(radius_mm / self.pitch)))
        mx = ndimage.maximum_filter(self.edt, size=2 * r + 1)
        idx = self.index(pts)
        ok = self.inside_grid(idx)
        out = np.zeros(len(idx))
        out[ok] = 2 * mx[idx[ok, 0], idx[ok, 1], idx[ok, 2]]
        return out

    @property
    def volume_mm3(self) -> float:
        return float(self.occ.sum()) * self.pitch**3

    def center_of_mass(self) -> np.ndarray:
        ijk = np.argwhere(self.occ)
        return self.origin + ijk.mean(axis=0) * self.pitch


def voxelize(vertices: np.ndarray, faces: np.ndarray, pitch: float, pad: int = 3,
             bounds: tuple[np.ndarray, np.ndarray] | None = None) -> Solid:
    """bounds: fix the grid to (lo, hi) so several meshes share one grid."""
    v = np.asarray(vertices, dtype=np.float64)
    tri = v[np.asarray(faces)]
    blo, bhi = (v.min(axis=0), v.max(axis=0)) if bounds is None else map(np.asarray, bounds)
    lo = blo - pad * pitch
    hi = bhi + pad * pitch
    n = np.maximum(np.ceil((hi - lo) / pitch).astype(int), 1)
    origin = lo
    ox, oy, oz = origin
    jx, jy = _JX * pitch, _JY * pitch

    xmin, xmax = tri[:, :, 0].min(1), tri[:, :, 0].max(1)
    ymin, ymax = tri[:, :, 1].min(1), tri[:, :, 1].max(1)
    i0 = np.ceil((xmin - ox - jx) / pitch).astype(np.int64)
    i1 = np.floor((xmax - ox - jx) / pitch).astype(np.int64)
    j0 = np.ceil((ymin - oy - jy) / pitch).astype(np.int64)
    j1 = np.floor((ymax - oy - jy) / pitch).astype(np.int64)
    ni = np.maximum(i1 - i0 + 1, 0)
    nj = np.maximum(j1 - j0 + 1, 0)
    cnt = ni * nj
    sel = np.nonzero(cnt > 0)[0]
    cnt_s = cnt[sel]
    total = int(cnt_s.sum())
    z_top = np.full(n[:2], np.nan)
    z_bot = np.full(n[:2], np.nan)
    z_entry = np.full(n[:2], np.nan)
    occ = np.zeros(n, dtype=bool)
    if total == 0:
        return Solid(occ, origin, pitch, z_top, z_bot, z_entry)

    t_idx = np.repeat(sel, cnt_s)
    starts = np.repeat(np.cumsum(cnt_s) - cnt_s, cnt_s)
    local = np.arange(total) - starts
    njr = nj[t_idx]
    I = i0[t_idx] + local // njr
    J = j0[t_idx] + local % njr
    px = ox + I * pitch + jx
    py = oy + J * pitch + jy

    a = tri[t_idx, 0]
    b = tri[t_idx, 1]
    c = tri[t_idx, 2]
    v0x, v0y = b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]
    v1x, v1y = c[:, 0] - a[:, 0], c[:, 1] - a[:, 1]
    v2x, v2y = px - a[:, 0], py - a[:, 1]
    den = v0x * v1y - v1x * v0y
    with np.errstate(divide="ignore", invalid="ignore"):
        s = (v2x * v1y - v1x * v2y) / den
        t = (v0x * v2y - v2x * v0y) / den
        inside = (den != 0) & (s >= 0) & (t >= 0) & (s + t <= 1)
    I, J = I[inside], J[inside]
    s, t = s[inside], t[inside]
    z = a[inside, 2] + s * (b[inside, 2] - a[inside, 2]) + t * (c[inside, 2] - a[inside, 2])

    col = I * n[1] + J
    order = np.lexsort((z, col))
    col, z, I, J = col[order], z[order], I[order], J[order]
    # Drop near-duplicate hits in a column (ray through a shared edge counted twice).
    dup = np.zeros(len(z), dtype=bool)
    dup[1:] = (col[1:] == col[:-1]) & (np.abs(z[1:] - z[:-1]) < 1e-9)
    keep = ~dup
    col, z, I, J = col[keep], z[keep], I[keep], J[keep]

    first = np.ones(len(col), dtype=bool)
    first[1:] = col[1:] != col[:-1]
    grp_start = np.maximum.accumulate(np.where(first, np.arange(len(col)), 0))
    rank = np.arange(len(col)) - grp_start
    counts = np.bincount(np.searchsorted(np.unique(col), col))
    col_count = np.repeat(counts, counts)
    odd = int((counts % 2 == 1).sum())

    np.fmax.at(z_top, (I, J), z)
    np.fmin.at(z_bot, (I, J), z)

    # Pair hits (0,1), (2,3) ... ; an odd leftover hit is ignored.
    is_in = (rank % 2 == 0) & (rank + 1 < col_count)
    k_in = np.nonzero(is_in)[0]
    zin, zout = z[k_in], z[k_in + 1]
    Ii, Ji = I[k_in], J[k_in]
    ks = np.ceil((zin - oz) / pitch).astype(np.int64)
    ke = np.ceil((zout - oz) / pitch).astype(np.int64) - 1  # half-open [zin, zout)
    good = ke >= ks
    ks, ke, Ii, Ji = ks[good], ke[good], Ii[good], Ji[good]
    ks = np.clip(ks, 0, n[2])
    ke = np.clip(ke + 1, 0, n[2])
    diff = np.zeros((n[0], n[1], n[2] + 1), dtype=np.int32)
    np.add.at(diff, (Ii, Ji, ks), 1)
    np.add.at(diff, (Ii, Ji, ke), -1)
    occ = np.cumsum(diff, axis=2)[:, :, : n[2]] > 0

    # Entry height of the topmost solid run per column.
    last_in = np.full(n[:2], np.nan)
    np.fmax.at(last_in, (Ii, Ji), zin[good])
    z_entry = last_in
    return Solid(occ, origin, pitch, z_top, z_bot, z_entry, odd_columns=odd)


def auto_pitch(size_mm: float, target_cells: int = 160, lo: float = 0.25, hi: float = 1.0) -> float:
    return float(np.clip(size_mm / target_cells, lo, hi))
