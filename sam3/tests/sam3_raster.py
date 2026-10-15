"""Minimal numpy rasterizer: face-ID buffer and textured, shaded render for one camera.

A stand-in for p2p_sim.render (sim workstream, not merged yet) so the sam3 tests and
scripts/beagle_regions.py can run a full label pass. Perspective camera from p2p_contracts.VIEWS
(azimuth around +Z from +X, elevation, vertical fov, framing the bounding sphere times
fit_margin), z-buffer with perspective-correct barycentrics, no culling (Meshy GLBs are double
sided). Replace with the sim renderer once it has a face-ID buffer.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from p2p_contracts.regions import CameraSpec


@dataclass
class Camera:
    pos: np.ndarray  # (3,) mm
    right: np.ndarray
    up: np.ndarray
    forward: np.ndarray
    f_px: float
    size: int

    def project(self, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(N, 3) world -> (N, 2) pixel coords (x right, y down) and (N,) camera depth."""
        d = pts - self.pos
        x, y, z = d @ self.right, d @ self.up, d @ self.forward
        c = self.size / 2
        return np.stack([c + self.f_px * x / z, c - self.f_px * y / z], axis=1), z


def camera_for(spec: CameraSpec, vertices: np.ndarray, size: int) -> Camera:
    lo, hi = vertices.min(axis=0), vertices.max(axis=0)
    center = (lo + hi) / 2
    radius = float(np.linalg.norm(hi - lo) / 2)
    half = np.radians(spec.fov_deg) / 2
    dist = spec.fit_margin * radius / np.sin(half)
    az, el = np.radians(spec.azimuth_deg), np.radians(spec.elevation_deg)
    pos = center + dist * np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
    fwd = (center - pos) / np.linalg.norm(center - pos)
    world_up = np.array([0.0, 0.0, 1.0]) if abs(fwd[2]) < 0.999 else np.array([0.0, 1.0, 0.0])
    right = np.cross(fwd, world_up)
    right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
    return Camera(pos, right, up, fwd, (size / 2) / np.tan(half), size)


def _edge(p, q, cx, cy):
    """Edge function of p -> q at pixel centers (cx, cy), per triangle."""
    px, py = p[:, 0, None, None], p[:, 1, None, None]
    return (q[:, 0, None, None] - px) * (cy - py) - (q[:, 1, None, None] - py) * (cx - px)


def rasterize(vertices: np.ndarray, faces: np.ndarray, cam: Camera):
    """(face_ids (H, W) int32 with -1 background, bary (H, W, 3) float32 perspective-correct)."""
    size = cam.size
    p2, z = cam.project(vertices)
    tri, tz = p2[faces], z[faces]  # (F, 3, 2), (F, 3)
    ok = (tz > 1e-6).all(axis=1)
    lo = np.floor(tri.min(axis=1) - 0.5).astype(np.int64)
    hi = np.ceil(tri.max(axis=1) - 0.5).astype(np.int64)
    lo, hi = np.clip(lo, 0, size - 1), np.clip(hi, 0, size - 1)
    ok &= (hi >= lo).all(axis=1)
    ext = (hi - lo).max(axis=1) + 1
    frag_pix, frag_depth, frag_face, frag_b = [], [], [], []
    for k in (2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048):
        sel = np.flatnonzero(ok & (ext <= k) & (ext > k // 2 if k > 2 else True))
        if sel.size == 0:
            continue
        step = max(1, (1 << 22) // (k * k))
        for s in range(0, sel.size, step):
            f = sel[s : s + step]
            ox, oy = np.meshgrid(np.arange(k), np.arange(k))
            px = lo[f, 0][:, None, None] + ox[None]
            py = lo[f, 1][:, None, None] + oy[None]
            inside_box = (px <= hi[f, 0][:, None, None]) & (py <= hi[f, 1][:, None, None])
            cx, cy = px + 0.5, py + 0.5
            a, b, c = tri[f, 0], tri[f, 1], tri[f, 2]
            area = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (
                c[:, 0] - a[:, 0]
            )
            area = np.where(np.abs(area) < 1e-12, np.nan, area)[:, None, None]

            w0 = _edge(b, c, cx, cy) / area
            w1 = _edge(c, a, cx, cy) / area
            w2 = _edge(a, b, cx, cy) / area
            inside = inside_box & (w0 >= -1e-7) & (w1 >= -1e-7) & (w2 >= -1e-7)
            ti, yy, xx = np.nonzero(inside)
            if ti.size == 0:
                continue
            fz = tz[f[ti]]
            l0, l1, l2 = (
                w0[ti, yy, xx] / fz[:, 0],
                w1[ti, yy, xx] / fz[:, 1],
                w2[ti, yy, xx] / fz[:, 2],
            )
            inv = l0 + l1 + l2
            frag_pix.append(py[ti, yy, xx] * size + px[ti, yy, xx])
            frag_depth.append(1.0 / inv)
            frag_face.append(f[ti])
            frag_b.append(np.stack([l0, l1, l2], axis=1) / inv[:, None])
    face_ids = np.full(size * size, -1, np.int32)
    bary = np.zeros((size * size, 3), np.float32)
    if frag_pix:
        pix = np.concatenate(frag_pix)
        depth = np.concatenate(frag_depth)
        order = np.lexsort((depth, pix))
        pix, first = pix[order], np.ones(order.size, bool)
        first[1:] = pix[1:] != pix[:-1]
        win = order[first]
        face_ids[pix[first]] = np.concatenate(frag_face)[win]
        bary[pix[first]] = np.concatenate(frag_b)[win]
    return face_ids.reshape(size, size), bary.reshape(size, size, 3)


def shade(
    face_ids: np.ndarray,
    bary: np.ndarray,
    faces: np.ndarray,
    vertex_normals: np.ndarray,
    cam: Camera,
    albedo_fn,
    background=(235, 235, 235),
) -> np.ndarray:
    """Lambert render: headlight plus a key light from above, albedo_fn(face_idx, bary) -> RGB."""
    h, w = face_ids.shape
    out = np.empty((h, w, 3), np.float32)
    out[:] = background
    m = face_ids >= 0
    f = face_ids[m]
    b = bary[m]
    n = (vertex_normals[faces[f]] * b[:, :, None]).sum(axis=1)
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    n = np.where((n @ cam.forward)[:, None] > 0, -n, n)  # face the camera
    key = -cam.forward * 0.6 + np.array([0.0, 0.0, 0.8])
    key /= np.linalg.norm(key)
    light = 0.45 + 0.35 * np.abs(n @ -cam.forward) + 0.3 * np.clip(n @ key, 0, None)
    out[m] = np.clip(albedo_fn(f, b) * light[:, None], 0, 255)
    return out.astype(np.uint8)


def vertex_normals(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    tri = vertices[faces]
    fn = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    vn = np.zeros_like(vertices)
    for i in range(3):
        np.add.at(vn, faces[:, i], fn)
    return vn / np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-12)
