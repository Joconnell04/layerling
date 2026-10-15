"""Face-ID rasterization: the only backend-specific part of rendering.

A Raster holds, per pixel: the visible face id (int32, -1 = background), linear view depth (mm,
inf on background), and perspective-correct barycentric coordinates of the pixel center inside
that face. Every render kind is computed from these buffers in numpy, so the GPU and CPU
backends produce the same images wherever they agree on coverage.

Backends
- "gl": moderngl standalone context (CGL on macOS, EGL on Linux). No window, no display.
- "numpy": vectorized CPU z-buffer; slower, used when no GL context can be created.
Select with P2P_SIM_BACKEND=gl|numpy; default tries gl then falls back to numpy.
"""

from __future__ import annotations

import os
import threading
import weakref
from dataclasses import dataclass

import numpy as np

from .camera import Camera
from .mesh import Mesh


@dataclass(eq=False)
class Raster:
    camera: Camera
    face_id: np.ndarray
    """(H, W) int32, -1 background."""
    depth: np.ndarray
    """(H, W) float32 mm along the view axis, inf background."""
    bary: np.ndarray
    """(H, W, 3) float32 barycentrics of the pixel center in face_id's triangle."""

    @property
    def mask(self) -> np.ndarray:
        return self.face_id >= 0

    def points(self, mesh: Mesh) -> np.ndarray:
        """(H, W, 3) world position of each pixel's surface point (NaN on background)."""
        fid = np.where(self.mask, self.face_id, 0)
        tri = mesh.triangles[fid]  # (H, W, 3, 3)
        p = np.einsum("hwk,hwkc->hwc", self.bary.astype(np.float64), tri)
        p[~self.mask] = np.nan
        return p


# ---------------------------------------------------------------------- GL backend

_VS = """
#version 410
uniform mat4 mvp;
uniform mat4 mv;
in vec3 in_pos;
flat out int v_fid;
out vec3 v_bary;
out float v_depth;
void main() {
    int corner = gl_VertexID % 3;
    v_fid = gl_VertexID / 3;
    v_bary = vec3(corner == 0 ? 1.0 : 0.0, corner == 1 ? 1.0 : 0.0, corner == 2 ? 1.0 : 0.0);
    vec4 p = vec4(in_pos, 1.0);
    v_depth = -(mv * p).z;
    gl_Position = mvp * p;
}
"""

_FS = """
#version 410
flat in int v_fid;
in vec3 v_bary;
in float v_depth;
layout(location = 0) out int f_id;
layout(location = 1) out vec4 f_bd;
void main() {
    f_id = v_fid;
    f_bd = vec4(v_bary, v_depth);
}
"""


class GLRasterizer:
    name = "gl"

    def __init__(self) -> None:
        import moderngl

        self._mgl = moderngl
        self.ctx = moderngl.create_standalone_context(require=410)
        self.prog = self.ctx.program(vertex_shader=_VS, fragment_shader=_FS)
        self._fbos: dict[tuple[int, int], tuple] = {}
        self._lock = threading.Lock()
        self.info = dict(self.ctx.info)

    def _fbo(self, w: int, h: int):
        key = (w, h)
        if key not in self._fbos:
            ctx = self.ctx
            tid = ctx.texture((w, h), 1, dtype="i4")
            tbd = ctx.texture((w, h), 4, dtype="f4")
            dep = ctx.depth_renderbuffer((w, h))
            fbo = ctx.framebuffer(color_attachments=[tid, tbd], depth_attachment=dep)
            self._fbos[key] = (fbo, tid, tbd, np.full((h, w), -1, dtype=np.int32).tobytes())
        return self._fbos[key]

    def _vao(self, mesh: Mesh):
        key = ("gl_vao", id(self))
        hit = mesh._cache.get(key)
        if hit is not None:
            return hit
        center = mesh.bounding_sphere[0]
        pos = (mesh.triangles - center).astype(np.float32).reshape(-1, 3)
        vbo = self.ctx.buffer(pos.tobytes())
        vao = self.ctx.vertex_array(self.prog, [(vbo, "3f", "in_pos")])
        entry = (vao, vbo, center)
        mesh._cache[key] = entry
        weakref.finalize(mesh, _release, self.ctx, vao, vbo)
        return entry

    def rasterize(self, mesh: Mesh, camera: Camera) -> Raster:
        w, h = camera.width, camera.height
        with self._lock, self.ctx:  # make this context current (other contexts may exist)
            vao, _vbo, center = self._vao(mesh)
            fbo, tid, tbd, blank = self._fbo(w, h)
            t = np.eye(4)
            t[:3, 3] = center
            mv = camera.view @ t
            mvp = camera.proj @ mv
            self.prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
            self.prog["mv"].write(mv.T.astype(np.float32).tobytes())
            fbo.use()
            fbo.viewport = (0, 0, w, h)
            tid.write(blank)
            fbo.color_mask = [(False, False, False, False)] * 2
            fbo.clear(depth=1.0)
            fbo.color_mask = [(True, True, True, True)] * 2
            self.ctx.enable(self._mgl.DEPTH_TEST)
            self.ctx.disable(self._mgl.CULL_FACE)
            vao.render(self._mgl.TRIANGLES, vertices=mesh.n_faces * 3)
            fid = np.frombuffer(tid.read(), dtype=np.int32).reshape(h, w)[::-1].copy()
            bd = np.frombuffer(tbd.read(), dtype=np.float32).reshape(h, w, 4)[::-1].copy()
        bg = fid < 0
        depth = bd[..., 3]
        depth[bg] = np.inf
        bary = bd[..., :3]
        bary[bg] = 0
        return Raster(camera, fid, depth, bary)


def _release(ctx, *objs) -> None:
    try:
        with ctx:
            for o in objs:
                o.release()
    except Exception:  # noqa: BLE001, S110 - context may already be gone at exit
        pass


# ---------------------------------------------------------------------- numpy backend


class NumpyRasterizer:
    name = "numpy"
    chunk = 1 << 22  # candidate pixels per batch

    def rasterize(self, mesh: Mesh, camera: Camera) -> Raster:
        w, h = camera.width, camera.height
        xy, z = camera.project(mesh.vertices)
        f = mesh.faces
        txy = xy[f]  # (F, 3, 2)
        tz = z[f]
        valid = np.all(tz > camera.near, axis=1) & np.all(np.isfinite(txy), axis=(1, 2))
        mn = txy.min(axis=1)
        mx = txy.max(axis=1)
        i0 = np.clip(np.ceil(mn[:, 0] - 0.5), 0, w - 1).astype(np.int64)
        i1 = np.clip(np.floor(mx[:, 0] - 0.5), -1, w - 1).astype(np.int64)
        j0 = np.clip(np.ceil(mn[:, 1] - 0.5), 0, h - 1).astype(np.int64)
        j1 = np.clip(np.floor(mx[:, 1] - 0.5), -1, h - 1).astype(np.int64)
        bw = i1 - i0 + 1
        bh = j1 - j0 + 1
        x0, y0 = txy[:, 0, 0], txy[:, 0, 1]
        x1, y1 = txy[:, 1, 0], txy[:, 1, 1]
        x2, y2 = txy[:, 2, 0], txy[:, 2, 1]
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        valid &= (bw > 0) & (bh > 0) & (np.abs(area) > 1e-12)
        idx_all = np.nonzero(valid)[0]
        span = np.maximum(bw, bh)[idx_all]
        k_class = np.maximum(1, 2 ** np.ceil(np.log2(np.maximum(span, 1)))).astype(np.int64)

        pix_l, dep_l, fid_l, b1_l, b2_l = [], [], [], [], []
        for k in np.unique(k_class):
            sel = idx_all[k_class == k]
            per = max(1, self.chunk // int(k * k))
            ar = np.arange(k)
            for s in range(0, len(sel), per):
                t = sel[s : s + per]
                ii = i0[t, None, None] + ar[None, None, :]  # (T, 1, K)
                jj = j0[t, None, None] + ar[None, :, None]  # (T, K, 1)
                cx = ii + 0.5
                cy = jj + 0.5
                a = area[t, None, None]
                l1 = ((cx - x0[t, None, None]) * (y2[t, None, None] - y0[t, None, None])
                      - (x2[t, None, None] - x0[t, None, None]) * (cy - y0[t, None, None])) / a
                l2 = ((x1[t, None, None] - x0[t, None, None]) * (cy - y0[t, None, None])
                      - (cx - x0[t, None, None]) * (y1[t, None, None] - y0[t, None, None])) / a
                l0 = 1 - l1 - l2
                inside = (l0 >= 0) & (l1 >= 0) & (l2 >= 0)
                inside &= (ii <= i1[t, None, None]) & (jj <= j1[t, None, None])
                tt, jy, ix = np.nonzero(inside)
                if tt.size == 0:
                    continue
                L0, L1, L2 = l0[tt, jy, ix], l1[tt, jy, ix], l2[tt, jy, ix]
                zt = tz[t[tt]]
                q0, q1, q2 = L0 / zt[:, 0], L1 / zt[:, 1], L2 / zt[:, 2]
                qs = q0 + q1 + q2
                px = i0[t[tt]] + ix
                py = j0[t[tt]] + jy
                pix_l.append(py * w + px)
                dep_l.append(1.0 / qs)
                fid_l.append(t[tt])
                b1_l.append(q1 / qs)
                b2_l.append(q2 / qs)

        fid = np.full(h * w, -1, dtype=np.int32)
        depth = np.full(h * w, np.inf, dtype=np.float32)
        bary = np.zeros((h * w, 3), dtype=np.float32)
        if pix_l:
            pix = np.concatenate(pix_l)
            dep = np.concatenate(dep_l)
            order = np.lexsort((dep, pix))
            ps = pix[order]
            first = np.ones(len(ps), dtype=bool)
            first[1:] = ps[1:] != ps[:-1]
            win = order[first]
            p = pix[win]
            fid[p] = np.concatenate(fid_l)[win]
            depth[p] = dep[win]
            b1 = np.concatenate(b1_l)[win]
            b2 = np.concatenate(b2_l)[win]
            bary[p, 0] = 1 - b1 - b2
            bary[p, 1] = b1
            bary[p, 2] = b2
        return Raster(camera, fid.reshape(h, w), depth.reshape(h, w), bary.reshape(h, w, 3))


# ---------------------------------------------------------------------- selection

_backend = None
_backend_lock = threading.Lock()


def get_backend(name: str | None = None):
    """Return a rasterizer. name: 'gl', 'numpy', or None (env P2P_SIM_BACKEND, else gl->numpy)."""
    global _backend
    want = name or os.environ.get("P2P_SIM_BACKEND") or "auto"
    if want == "numpy":
        return NumpyRasterizer()
    with _backend_lock:
        if _backend is not None and (want == "auto" or _backend.name == want):
            return _backend
        try:
            _backend = GLRasterizer()
        except Exception as e:
            if want == "gl":
                raise RuntimeError(f"GL backend unavailable: {e}") from e
            _backend = NumpyRasterizer()
        return _backend


def rasterize(mesh: Mesh, camera: Camera, backend: str | None = None) -> Raster:
    return get_backend(backend).rasterize(mesh, camera)
