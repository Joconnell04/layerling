"""Product renders for the public site: the real sliced 3MFs, in filament colors, on paper.

Studio lighting built from many shadowed directional lights (a softbox key, a rim, a sky dome and
a paper floor bounce), so shadows, contact shadow and occlusion come out soft and noise free.
PLA material (GGX, roughness 0.45), 0.2 mm layer lines on walls where they are big enough to see.
Output: transparent PNGs, the contact shadow baked into alpha.

    uv run python scripts/render_products.py --out apps/web/public/products \
        --keychain K.3mf --nameplate N.3mf --figurine F.3mf --ornament O.3mf --magnet M.3mf

Views: <product>.png (hero, 3/4, one camera and lens for all), <product>-front.png (front,
orthographic, fixed px per mm, for the size visualizer), lineup.png (all five at true relative
scale), and renders.json (px per mm, bounding boxes, baselines).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import moderngl
import numpy as np
from PIL import Image
from p2p_threemf.reader import read_3mf_full

LAYER_MM = 0.2


# ----------------------------------------------------------------------------- color

def srgb_to_lin(c: np.ndarray) -> np.ndarray:
    c = np.asarray(c, dtype=np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def lin_to_srgb(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055)


def hex_rgb(h: str) -> np.ndarray:
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)])


def filament_albedo(hexcode: str) -> np.ndarray:
    """Linear albedo of a PLA filament. Real white PLA reflects about 85 %, black about 4 %."""
    lin = srgb_to_lin(hex_rgb(hexcode))
    return np.clip(lin * 0.86, 0.016, 0.86)


def aces(x: np.ndarray) -> np.ndarray:
    a, b, c, d, e = 2.51, 0.03, 2.43, 0.59, 0.14
    return np.clip((x * (a * x + b)) / (x * (c * x + d) + e), 0, 1)


# ----------------------------------------------------------------------------- geometry

class Item:
    """One product in display pose: unindexed triangles with per-corner normals and colors."""

    def __init__(self, pos, nrm, alb, pz, up):
        self.pos, self.nrm, self.alb, self.pz, self.up = pos, nrm, alb, pz, up

    @property
    def bmin(self):
        return self.pos.reshape(-1, 3).min(0)

    @property
    def bmax(self):
        return self.pos.reshape(-1, 3).max(0)

    def moved(self, d):
        return Item(self.pos + np.asarray(d), self.nrm, self.alb, self.pz, self.up)


def corner_normals(V: np.ndarray, F: np.ndarray, crease_deg: float = 38.0) -> np.ndarray:
    """Area-weighted vertex normals per face corner, split where faces meet at a crease."""
    e1, e2 = V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]
    fn = np.cross(e1, e2)
    area = np.linalg.norm(fn, axis=1)
    fnu = fn / np.maximum(area, 1e-20)[:, None]
    cv = F.reshape(-1)
    cf = np.repeat(np.arange(len(F)), 3)
    order = np.argsort(cv, kind="stable")
    cv_s, cf_s = cv[order], cf[order]
    starts = np.flatnonzero(np.r_[True, cv_s[1:] != cv_s[:-1]])
    counts = np.diff(np.r_[starts, len(cv_s)])
    grp = np.repeat(np.arange(len(starts)), counts)
    k = counts[grp]
    a = np.repeat(np.arange(len(cv_s)), k)
    offs = np.arange(len(a)) - np.repeat(np.cumsum(k) - k, k)
    b = starts[grp[a]] + offs
    fa, fb = cf_s[a], cf_s[b]
    dot = np.einsum("ij,ij->i", fnu[fa], fnu[fb])
    w = (dot > math.cos(math.radians(crease_deg))) * area[fb]
    acc = np.stack([np.bincount(a, fnu[fb, j] * w, minlength=len(cv_s)) for j in range(3)], 1)
    acc /= np.maximum(np.linalg.norm(acc, axis=1), 1e-20)[:, None]
    out = np.empty_like(acc)
    out[order] = acc
    return out.reshape(-1, 3, 3)



def subdivide(V, F, cn, lab, parts):
    """Midpoint subdivision (geometry unchanged); corner normals interpolated per face."""
    E = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    Es = np.sort(E, axis=1)
    uniq, inv = np.unique(Es, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    mid = len(V) + inv.reshape(3, -1).T          # (F,3): mid of edge01, 12, 20
    V2 = np.concatenate([V, (V[uniq[:, 0]] + V[uniq[:, 1]]) / 2])
    a, b, c = F[:, 0], F[:, 1], F[:, 2]
    ab, bc, ca = mid[:, 0], mid[:, 1], mid[:, 2]
    F2 = np.concatenate([np.stack([a, ab, ca], 1), np.stack([ab, b, bc], 1),
                         np.stack([ca, bc, c], 1), np.stack([ab, bc, ca], 1)])
    na, nb, nc = cn[:, 0], cn[:, 1], cn[:, 2]
    nrm = lambda x: x / np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-12)
    nab, nbc, nca = nrm(na + nb), nrm(nb + nc), nrm(nc + na)
    cn2 = np.concatenate([np.stack([na, nab, nca], 1), np.stack([nab, nb, nbc], 1),
                          np.stack([nca, nbc, nc], 1), np.stack([nab, nbc, nca], 1)])
    return V2, F2, cn2, np.tile(lab, 4), np.tile(parts, 4)


def smooth_labels(V, F, lab, iters=2):
    """Relax jagged paint borders: vertex label weights, smoothed, face label = argmax."""
    k = lab.max() + 1
    e1, e2 = V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]
    area = np.linalg.norm(np.cross(e1, e2), axis=1)
    nv = len(V)
    W = np.zeros((nv, k))
    for j in range(3):
        np.add.at(W, (F[:, j], lab), area)
    W /= np.maximum(W.sum(1, keepdims=True), 1e-20)
    E = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    E = np.unique(np.sort(E, axis=1), axis=0)
    deg = np.bincount(E.ravel(), minlength=nv).astype(float)
    for _ in range(iters):
        acc = np.zeros_like(W)
        np.add.at(acc, E[:, 0], W[E[:, 1]])
        np.add.at(acc, E[:, 1], W[E[:, 0]])
        W = 0.5 * W + 0.5 * acc / np.maximum(deg, 1)[:, None]
    return (W[F[:, 0]] + W[F[:, 1]] + W[F[:, 2]]).argmax(1)

def rot_x(deg: float) -> np.ndarray:
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def rot_z(deg: float) -> np.ndarray:
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def load_item(path: str | Path, pose: str, recolor: dict[int, str] | None = None,
              lean_deg: float = 9.0, turn_deg: float = 0.0, subdiv: int = 2) -> Item:
    """pose: 'upright' (as printed) or 'flat' (printed face up; stood on its bottom edge)."""
    m = read_3mf_full(path, world=True)
    V, F = m.vertices.astype(np.float64), m.faces.astype(np.int64)
    lo, hi = V.min(0), V.max(0)
    V = V - np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])
    cols = list(m.filament_colours)
    alb_slot = np.array([filament_albedo(c) for c in cols])
    slots = np.clip(m.face_slots - 1, 0, len(cols) - 1).astype(np.int64)
    parts = m.face_part.copy()
    cn = corner_normals(V, F)
    # Dense meshes (already painted finely) need fewer or no subdivisions.
    subdiv = max(0, min(subdiv, int(math.floor(math.log(max(1.0, 1_500_000 / len(F)), 4)))))
    for _ in range(subdiv):
        V, F, cn, slots, parts = subdivide(V, F, cn, slots, parts)
        slots = smooth_labels(V, F, slots, iters=2)
    alb = alb_slot[slots]
    if recolor:
        for part_id, hexcode in recolor.items():
            alb[parts == part_id] = filament_albedo(hexcode)
    nrm = cn
    pos = V[F]
    pz = pos[..., 2].copy()
    R = np.eye(3)
    if pose == "flat":
        R = rot_x(90.0)            # face +Z -> -Y, head +Y -> +Z
        R = rot_x(-lean_deg) @ R    # lean back
    R = rot_z(turn_deg) @ R
    pos = pos @ R.T
    nrm = nrm @ R.T
    up = R @ np.array([0.0, 0.0, 1.0])
    pos[..., 2] -= pos[..., 2].min()
    c = (pos.reshape(-1, 3).min(0) + pos.reshape(-1, 3).max(0)) / 2
    pos[..., 0] -= c[0]
    pos[..., 1] -= c[1]
    alb3 = np.repeat(alb[:, None, :], 3, axis=1)
    return Item(pos.astype(np.float32), nrm.astype(np.float32), alb3.astype(np.float32),
                pz.astype(np.float32), up.astype(np.float32))


def merge(items: list[Item]) -> tuple[np.ndarray, ...]:
    pos = np.concatenate([i.pos.reshape(-1, 3) for i in items])
    nrm = np.concatenate([i.nrm.reshape(-1, 3) for i in items])
    alb = np.concatenate([i.alb.reshape(-1, 3) for i in items])
    pz = np.concatenate([i.pz.reshape(-1) for i in items])
    up = np.concatenate([np.repeat(i.up[None], i.pos.shape[0] * 3, 0) for i in items])
    return pos, nrm, alb, pz, up


# ----------------------------------------------------------------------------- cameras

def look_at(eye, target, up=(0, 0, 1)) -> np.ndarray:
    eye, target, up = map(lambda v: np.asarray(v, dtype=np.float64), (eye, target, up))
    f = target - eye
    f /= np.linalg.norm(f)
    s = np.cross(f, up)
    if np.linalg.norm(s) < 1e-6:
        s = np.cross(f, (0, 1, 0))
    s /= np.linalg.norm(s)
    u = np.cross(s, f)
    m = np.eye(4)
    m[0, :3], m[1, :3], m[2, :3] = s, u, -f
    m[:3, 3] = -m[:3, :3] @ eye
    return m


def ortho(l, r, b, t, n, f) -> np.ndarray:
    m = np.eye(4)
    m[0, 0], m[1, 1], m[2, 2] = 2 / (r - l), 2 / (t - b), -2 / (f - n)
    m[0, 3], m[1, 3], m[2, 3] = -(r + l) / (r - l), -(t + b) / (t - b), -(f + n) / (f - n)
    return m


def perspective(fovy_deg, aspect, n, f) -> np.ndarray:
    t = 1 / math.tan(math.radians(fovy_deg) / 2)
    m = np.zeros((4, 4))
    m[0, 0], m[1, 1] = t / aspect, t
    m[2, 2], m[2, 3] = (f + n) / (n - f), 2 * f * n / (n - f)
    m[3, 2] = -1
    return m


def sph(az_deg: float, el_deg: float) -> np.ndarray:
    """Direction from the scene toward a camera or light. az 0 = front (-Y), + = viewer's right."""
    a, e = math.radians(az_deg), math.radians(el_deg)
    return np.array([math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e)])


# ----------------------------------------------------------------------------- lights

def cone_samples(d: np.ndarray, half_deg: float, n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    d = d / np.linalg.norm(d)
    t = np.cross(d, [0, 0, 1.0])
    if np.linalg.norm(t) < 1e-6:
        t = np.cross(d, [1.0, 0, 0])
    t /= np.linalg.norm(t)
    b = np.cross(d, t)
    out = []
    golden = math.pi * (3 - math.sqrt(5))
    for i in range(n):
        r = math.tan(math.radians(half_deg)) * math.sqrt((i + 0.5) / n)
        a = i * golden + rng.uniform(0, 0.2)
        v = d + r * (math.cos(a) * t + math.sin(a) * b)
        out.append(v / np.linalg.norm(v))
    return np.array(out)


def hemisphere(n: int, lower: bool = False) -> np.ndarray:
    golden = math.pi * (3 - math.sqrt(5))
    out = []
    for i in range(n):
        z = 1 - (i + 0.5) / n          # uniform in solid angle over the hemisphere
        r = math.sqrt(max(0.0, 1 - z * z))
        a = i * golden
        v = np.array([r * math.cos(a), r * math.sin(a), z])
        if lower:
            v[2] = -v[2]
        out.append(v)
    return np.array(out)


def studio_lights() -> tuple[np.ndarray, np.ndarray]:
    """(N,3) directions toward each light and (N,3) linear radiance weights (irradiance units)."""
    dirs, rad = [], []
    key = cone_samples(sph(-42, 54), 14.0, 28, 1)
    for d in key:
        dirs.append(d)
        rad.append(np.array([1.0, 0.965, 0.915]) * 2.35 / len(key))
    rim = cone_samples(sph(150, 26), 7.0, 10, 2)
    for d in rim:
        dirs.append(d)
        rad.append(np.array([0.93, 0.96, 1.0]) * 1.35 / len(rim))
    sky = hemisphere(96)
    for d in sky:
        dirs.append(d)
        lum = 0.62 * (0.55 + 0.45 * d[2])
        rad.append(np.array([0.97, 0.98, 1.0]) * lum * 2 * math.pi / len(sky))
    floor = hemisphere(32, lower=True)
    paper = srgb_to_lin(hex_rgb("#F3EEE4"))
    for d in floor:
        dirs.append(d)
        rad.append(paper * 0.30 * 2 * math.pi / len(floor))
    return np.array(dirs, dtype=np.float32), np.array(rad, dtype=np.float32)


# ----------------------------------------------------------------------------- GL

GBUF_VS = """
#version 330
uniform mat4 mvp;
in vec3 in_pos; in vec3 in_nrm; in vec3 in_alb; in float in_pz; in vec3 in_up;
out vec3 v_pos; out vec3 v_nrm; out vec3 v_alb; out float v_pz; out vec3 v_up;
void main(){ v_pos=in_pos; v_nrm=in_nrm; v_alb=in_alb; v_pz=in_pz; v_up=in_up;
  gl_Position = mvp*vec4(in_pos,1.0); }
"""
GBUF_FS = """
#version 330
in vec3 v_pos; in vec3 v_nrm; in vec3 v_alb; in float v_pz; in vec3 v_up;
uniform float flag; uniform float layer_mm; uniform float layer_amp; uniform vec3 campos;
layout(location=0) out vec4 o_pos; layout(location=1) out vec4 o_nrm; layout(location=2) out vec4 o_alb;
void main(){
  vec3 n = normalize(v_nrm);
  if (dot(n, campos - v_pos) < 0.0) n = -n;
  if (flag > 0.9) {
    float ph = v_pz / layer_mm * 6.2831853;
    float per_px = fwidth(v_pz) / layer_mm;            // layers per pixel
    float aa = clamp(1.0 - (per_px - 0.16) / 0.22, 0.0, 1.0);
    vec3 t = v_up - n * dot(n, v_up);
    float wall = length(t);
    if (wall > 1e-4 && aa > 0.0) {
      t /= wall;
      n = normalize(n + layer_amp * aa * smoothstep(0.15, 0.6, wall) * sin(ph) * t);
    }
  }
  o_pos = vec4(v_pos, flag); o_nrm = vec4(n, 0.0); o_alb = vec4(v_alb, 1.0);
}
"""
DEPTH_VS = """
#version 330
uniform mat4 mvp; in vec3 in_pos;
void main(){ gl_Position = mvp*vec4(in_pos,1.0); }
"""
DEPTH_FS = """
#version 330
out vec4 o; void main(){ o = vec4(gl_FragCoord.z, 0, 0, 1); }
"""
QUAD_VS = """
#version 330
in vec2 in_xy; out vec2 uv;
void main(){ uv = in_xy*0.5+0.5; gl_Position = vec4(in_xy,0,1); }
"""
LIGHT_FS = """
#version 330
#define MAXL 64
in vec2 uv;
uniform sampler2D gpos; uniform sampler2D gnrm; uniform sampler2D atlas;
uniform int nl; uniform mat4 lm[MAXL]; uniform vec3 ld[MAXL]; uniform vec3 lc[MAXL];
uniform vec2 lt[MAXL]; uniform float tile; uniform float atlas_px; uniform float texw[MAXL];
uniform vec3 campos; uniform int ortho_cam; uniform vec3 camdir;
uniform float rough; uniform float f0;
layout(location=0) out vec4 o_diff; layout(location=1) out vec4 o_spec;
float shadow(int i, vec3 P, vec3 N, float ndl){
  float nb = texw[i] * (1.5 + 2.0*(1.0-ndl));
  vec4 q = lm[i] * vec4(P + N*nb, 1.0);
  vec3 s = q.xyz*0.5+0.5;
  if (s.x<=0.0||s.x>=1.0||s.y<=0.0||s.y>=1.0) return 1.0;
  vec2 base = (lt[i] + s.xy*tile) * atlas_px - 0.5;
  vec2 f = fract(base); ivec2 b = ivec2(floor(base));
  float z = s.z - 0.0006;
  float v = 0.0;
  for (int y=-1;y<=2;y++) for (int x=-1;x<=2;x++){
    float d = texelFetch(atlas, b+ivec2(x,y), 0).r;
    float w = (x==-1?1.0-f.x:(x==2?f.x:1.0)) * (y==-1?1.0-f.y:(y==2?f.y:1.0));
    v += w * (z <= d ? 1.0 : 0.0);
  }
  return v/9.0;
}
void main(){
  vec4 P4 = texture(gpos, uv);
  if (P4.w < 0.25) { o_diff = vec4(0); o_spec = vec4(0); return; }
  bool ground = P4.w < 0.75;
  vec3 P = P4.xyz; vec3 N = ground ? vec3(0,0,1) : normalize(texture(gnrm, uv).xyz);
  vec3 V = ortho_cam==1 ? -camdir : normalize(campos - P);
  float a = rough*rough; float a2=a*a; float k=(rough+1.0)*(rough+1.0)/8.0;
  float ndv = max(dot(N,V), 1e-3);
  vec3 dsum = vec3(0); vec3 ssum = vec3(0); float gv=0.0, gt=0.0;
  for (int i=0;i<nl;i++){
    vec3 L = ld[i];
    float ndl = dot(N, L);
    if (ndl <= 0.0) continue;
    if (ground && L.z <= 0.0) continue;
    float vis = shadow(i, P, N, ndl);
    float lum = dot(lc[i], vec3(0.2126,0.7152,0.0722));
    if (ground) { gv += lum*ndl*vis; gt += lum*ndl; continue; }
    dsum += lc[i]*ndl*vis;
    vec3 H = normalize(L+V);
    float ndh = max(dot(N,H),0.0); float vdh = max(dot(V,H),0.0);
    float D = a2/(3.14159265*pow(ndh*ndh*(a2-1.0)+1.0,2.0));
    float G = (ndv/(ndv*(1.0-k)+k))*(ndl/(ndl*(1.0-k)+k));
    float F = f0 + (1.0-f0)*pow(1.0-vdh,5.0);
    ssum += lc[i]*vis*D*G*F/(4.0*ndv);
  }
  o_diff = vec4(dsum, gv); o_spec = vec4(ssum, gt);
}
"""


class Renderer:
    def __init__(self, ctx: moderngl.Context | None = None):
        self.ctx = ctx or moderngl.create_standalone_context(require=330)
        c = self.ctx
        self.p_gbuf = c.program(vertex_shader=GBUF_VS, fragment_shader=GBUF_FS)
        self.p_depth = c.program(vertex_shader=DEPTH_VS, fragment_shader=DEPTH_FS)
        self.p_light = c.program(vertex_shader=QUAD_VS, fragment_shader=LIGHT_FS)
        quad = np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype="f4")
        self.quad = c.vertex_array(self.p_light, [(c.buffer(quad), "2f", "in_xy")])
        self.tile_px = 1024
        self.grid = 8
        self.atlas_px = self.tile_px * self.grid
        self.atlas = c.texture((self.atlas_px, self.atlas_px), 1, dtype="f4")
        self.atlas.filter = (moderngl.NEAREST, moderngl.NEAREST)
        self.atlas_depth = c.depth_renderbuffer((self.atlas_px, self.atlas_px))
        self.atlas_fbo = c.framebuffer([self.atlas], self.atlas_depth)

    def _object_vao(self, prog, pos, nrm=None, alb=None, pz=None, up=None):
        c = self.ctx
        if prog is self.p_depth:
            return c.vertex_array(prog, [(c.buffer(pos.astype("f4").tobytes()), "3f", "in_pos")])
        return c.vertex_array(prog, [
            (c.buffer(pos.astype("f4").tobytes()), "3f", "in_pos"),
            (c.buffer(nrm.astype("f4").tobytes()), "3f", "in_nrm"),
            (c.buffer(alb.astype("f4").tobytes()), "3f", "in_alb"),
            (c.buffer(pz.astype("f4").tobytes()), "1f", "in_pz"),
            (c.buffer(up.astype("f4").tobytes()), "3f", "in_up"),
        ])

    def render(self, items: list[Item], view: np.ndarray, proj: np.ndarray, size: tuple[int, int],
               campos: np.ndarray, camdir: np.ndarray, ortho_cam: bool,
               layer_amp: float = 0.38) -> dict[str, np.ndarray]:
        c = self.ctx
        W, H = size
        pos, nrm, alb, pz, up = merge(items)
        allp = pos.reshape(-1, 3)
        center = (allp.min(0) + allp.max(0)) / 2
        radius = float(np.linalg.norm(allp.max(0) - allp.min(0)) / 2) * 1.02

        # G-buffer
        tpos = c.texture((W, H), 4, dtype="f4")
        tnrm = c.texture((W, H), 4, dtype="f4")
        talb = c.texture((W, H), 4, dtype="f4")
        for t in (tpos, tnrm, talb):
            t.filter = (moderngl.NEAREST, moderngl.NEAREST)
        dep = c.depth_renderbuffer((W, H))
        fbo = c.framebuffer([tpos, tnrm, talb], dep)
        fbo.use()
        c.viewport = (0, 0, W, H)
        fbo.clear(0, 0, 0, 0, depth=1.0)
        c.enable(moderngl.DEPTH_TEST)
        c.disable(moderngl.CULL_FACE)
        mvp = (proj @ view).astype("f4")
        self.p_gbuf["mvp"].write(mvp.T.tobytes())
        self.p_gbuf["layer_mm"] = LAYER_MM
        self.p_gbuf["layer_amp"] = layer_amp
        self.p_gbuf["campos"] = tuple(float(x) for x in campos)
        g = 4000.0
        ground = np.array([[-g, -g, 0], [g, -g, 0], [g, g, 0], [-g, -g, 0], [g, g, 0], [-g, g, 0]],
                          dtype="f4")
        gz = np.zeros(6, dtype="f4")
        gvao = self._object_vao(self.p_gbuf, ground, np.tile([0, 0, 1.0], (6, 1)),
                                np.ones((6, 3)), gz, np.tile([0, 0, 1.0], (6, 1)))
        self.p_gbuf["flag"] = 0.5
        gvao.render()
        ovao = self._object_vao(self.p_gbuf, pos, nrm, alb, pz, up)
        self.p_gbuf["flag"] = 1.0
        ovao.render()

        # lights in batches of 64 shadow maps
        dirs, rads = studio_lights()
        dvao = self._object_vao(self.p_depth, pos)
        out_d = np.zeros((H, W, 4), dtype=np.float64)
        out_s = np.zeros((H, W, 4), dtype=np.float64)
        ltex = c.texture((W, H), 4, dtype="f4")
        stex = c.texture((W, H), 4, dtype="f4")
        lfbo = c.framebuffer([ltex, stex])
        n_batch = self.grid * self.grid
        for b0 in range(0, len(dirs), n_batch):
            bd, br = dirs[b0:b0 + n_batch], rads[b0:b0 + n_batch]
            self.atlas_fbo.use()
            c.viewport = (0, 0, self.atlas_px, self.atlas_px)
            self.atlas_fbo.clear(1.0, 1.0, 1.0, 1.0, depth=1.0)
            mats, tiles, texw = [], [], []
            for i, d in enumerate(bd):
                lv = look_at(center + d * radius * 3, center,
                             up=(0, 0, 1) if abs(d[2]) < 0.95 else (0, 1, 0))
                lp = ortho(-radius, radius, -radius, radius, radius * 1.0, radius * 5.0)
                lmvp = lp @ lv
                tx, ty = i % self.grid, i // self.grid
                c.viewport = (tx * self.tile_px, ty * self.tile_px, self.tile_px, self.tile_px)
                self.p_depth["mvp"].write(lmvp.astype("f4").T.tobytes())
                dvao.render()
                mats.append(lmvp)
                tiles.append((tx / self.grid, ty / self.grid))
                texw.append(2 * radius / self.tile_px)
            lfbo.use()
            c.viewport = (0, 0, W, H)
            c.disable(moderngl.DEPTH_TEST)
            tpos.use(0)
            tnrm.use(1)
            self.atlas.use(2)
            pl = self.p_light
            pl["gpos"], pl["gnrm"], pl["atlas"] = 0, 1, 2
            pl["nl"] = len(bd)
            pad = 64 - len(bd)
            pl["lm"].write(np.concatenate([np.array(mats), np.zeros((pad, 4, 4))]).astype("f4")
                           .transpose(0, 2, 1).tobytes())
            pl["ld"].write(np.concatenate([bd, np.zeros((pad, 3))]).astype("f4").tobytes())
            pl["lc"].write(np.concatenate([br, np.zeros((pad, 3))]).astype("f4").tobytes())
            pl["lt"].write(np.concatenate([np.array(tiles), np.zeros((pad, 2))]).astype("f4").tobytes())
            pl["texw"].write(np.concatenate([np.array(texw), np.zeros(pad)]).astype("f4").tobytes())
            pl["tile"] = 1.0 / self.grid
            pl["atlas_px"] = float(self.atlas_px)
            pl["campos"] = tuple(float(x) for x in campos)
            pl["camdir"] = tuple(float(x) for x in camdir)
            pl["ortho_cam"] = 1 if ortho_cam else 0
            pl["rough"] = 0.40
            pl["f0"] = 0.045
            lfbo.clear(0, 0, 0, 0)
            self.quad.render(moderngl.TRIANGLE_STRIP)
            out_d += np.frombuffer(ltex.read(), dtype="f4").reshape(H, W, 4)
            out_s += np.frombuffer(stex.read(), dtype="f4").reshape(H, W, 4)
            c.enable(moderngl.DEPTH_TEST)

        gp = np.frombuffer(tpos.read(), dtype="f4").reshape(H, W, 4)
        ga = np.frombuffer(talb.read(), dtype="f4").reshape(H, W, 4)
        for o in (tpos, tnrm, talb, dep, fbo, ltex, stex, lfbo, ovao, gvao, dvao):
            o.release()
        return {"flag": gp[..., 3].copy(), "pos": gp[..., :3].copy(), "alb": ga[..., :3].copy(),
                "diff": out_d[..., :3], "spec": out_s[..., :3],
                "gvis": out_d[..., 3], "gtot": out_s[..., 3]}


def composite(buf: dict[str, np.ndarray], ss: int, exposure: float = 0.34,
              shadow_rgb: str = "#2A2118", shadow_max: float = 0.62,
              footprint: tuple[float, float, float, float] | None = None,
              reach_mm: float | None = None) -> Image.Image:
    flag = buf["flag"]
    obj = flag > 0.75
    gnd = (flag > 0.25) & ~obj
    lin = buf["alb"] * buf["diff"] + buf["spec"]
    rgb = lin_to_srgb(aces(lin * exposure))
    occl = np.where(buf["gtot"] > 1e-6, 1.0 - buf["gvis"] / np.maximum(buf["gtot"], 1e-6), 0.0)
    shade = np.clip(occl, 0, 1) ** 0.9 * shadow_max
    if footprint is not None:
        x0, y0, x1, y1 = footprint
        px, py = buf["pos"][..., 0], buf["pos"][..., 1]
        dx = np.maximum(np.maximum(x0 - px, px - x1), 0)
        dy = np.maximum(np.maximum(y0 - py, py - y1), 0)
        dist = np.hypot(dx, dy)
        reach = reach_mm if reach_mm is not None else 0.22 * max(x1 - x0, y1 - y0) + 8.0
        t = np.clip(dist / reach, 0, 1)
        shade = shade * (1 - t * t * (3 - 2 * t))
    srgb_sh = hex_rgb(shadow_rgb)
    H, W = flag.shape
    out = np.zeros((H, W, 4))
    out[obj, :3] = rgb[obj]
    out[obj, 3] = 1.0
    out[gnd, :3] = srgb_sh * shade[gnd, None]   # premultiplied
    out[gnd, 3] = shade[gnd]
    # GL rows are bottom-up
    out = out[::-1]
    # downsample premultiplied
    h2, w2 = H // ss, W // ss
    out = out[:h2 * ss, :w2 * ss].reshape(h2, ss, w2, ss, 4).mean((1, 3))
    a = out[..., 3:4]
    col = np.where(a > 1e-4, out[..., :3] / np.maximum(a, 1e-4), 0)
    img = np.concatenate([col, a], -1)
    return Image.fromarray((np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA")


# ----------------------------------------------------------------------------- views

HERO_AZ, HERO_EL, HERO_FOV = -26.0, 15.0, 19.0


def hero_camera(items: list[Item], size_px: int, az=HERO_AZ, el=HERO_EL, fov=HERO_FOV):
    pos = np.concatenate([i.pos.reshape(-1, 3) for i in items])
    lo, hi = pos.min(0), pos.max(0)
    center = (lo + hi) / 2
    radius = float(np.linalg.norm(hi - lo) / 2)
    dist = radius / math.sin(math.radians(fov) / 2) * 1.02
    d = sph(az, el)
    eye = center + d * dist
    view = look_at(eye, center)
    proj = perspective(fov, 1.0, dist * 0.2, dist * 4)
    return view, proj, eye, -d


def front_camera(items: list[Item], px_per_mm: float, ss: int, el: float = 4.0, margin_mm=6.0):
    pos = np.concatenate([i.pos.reshape(-1, 3) for i in items])
    lo, hi = pos.min(0), pos.max(0)
    center = (lo + hi) / 2
    d = sph(0, el)
    eye = center + d * 1000
    view = look_at(eye, center)
    vs = pos.astype(np.float64) @ view[:3, :3].T + view[:3, 3]
    x0, x1 = vs[:, 0].min() - margin_mm, vs[:, 0].max() + margin_mm
    y0, y1 = vs[:, 1].min() - margin_mm, vs[:, 1].max() + margin_mm
    w, h = x1 - x0, y1 - y0
    proj = ortho(x0, x1, y0, y1, 1.0, 3000.0)
    W, H = int(round(w * px_per_mm)) * ss, int(round(h * px_per_mm)) * ss
    return view, proj, eye, -d, (W, H)


def fit_canvas(img: Image.Image, size: tuple[int, int], box: tuple[float, float, float, float],
               baseline: float, obj_alpha: float = 0.98) -> Image.Image:
    """Scale the object (opaque pixels) into box (l, t, r, b as fractions) with its lowest opaque
    row on `baseline` (fraction of height); shadow pixels come along."""
    a = np.asarray(img)[..., 3]
    ys, xs = np.nonzero(a >= int(obj_alpha * 255))
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    W, H = size
    l, t, r, b = box
    bw, bh = (r - l) * W, (b - t) * H
    s = min(bw / (x1 - x0), bh / (y1 - y0))
    im2 = img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)
    cx = (x0 + x1) / 2 * s
    oy = y1 * s
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    canvas.alpha_composite(im2, (int(round(W * (l + r) / 2 - cx)), int(round(baseline * H - oy))))
    return canvas


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    for p in ("keychain", "nameplate", "figurine", "ornament", "magnet"):
        ap.add_argument(f"--{p}")
    ap.add_argument("--size", type=int, default=1200)
    ap.add_argument("--ss", type=int, default=2)
    ap.add_argument("--front-px-per-mm", type=float, default=10.0)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--group", action="store_true")
    ap.add_argument("--fronts-only", action="store_true")
    ap.add_argument("--extra", nargs="*", default=[], help="name=path.3mf:pose")
    ap.add_argument("--exposure", type=float, default=0.34)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    poses = {"keychain": "flat", "magnet": "flat", "nameplate": "upright",
             "figurine": "upright", "ornament": "upright"}
    # Part colors for the site set (3MF part ids): blue plate, red loop, white magnet backing.
    recolors = {"nameplate": {2: "#0D6284"}, "ornament": {2: "#F72323"}, "magnet": {2: "#FFFFFF"}}
    rnd = Renderer()
    meta_path = out / "renders.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    items = {}
    for name, pose in poses.items():
        src = getattr(a, name)
        if not src or (a.only and name not in a.only):
            continue
        it = load_item(src, pose, recolor=recolors.get(name))
        items[name] = it
        lo, hi = it.bmin, it.bmax
        # hero
        ss = a.ss
        fp = (float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1]))
        if not a.fronts_only:
            S = a.size * ss * 3 // 2
            view, proj, eye, fwd = hero_camera([it], S)
            buf = rnd.render([it], view, proj, (S, S), eye, fwd, False)
            raw = composite(buf, ss, a.exposure, footprint=fp)
            hero = fit_canvas(raw, (a.size, a.size), (0.1, 0.08, 0.9, 0.86), 0.86)
            hero.save(out / f"{name}.png", optimize=True)
        # front, true scale
        front_el = 32.0 if name == "nameplate" else 4.0
        view, proj, eye, fwd, (W, H) = front_camera([it], a.front_px_per_mm, ss, el=front_el)
        buf = rnd.render([it], view, proj, (W, H), eye, fwd, True)
        # Short shadow so it fades inside the 6 mm margin: no hard edge at the image border.
        front = composite(buf, ss, a.exposure, footprint=fp, reach_mm=4.0)
        front.save(out / f"{name}-front.png", optimize=True)
        fa = np.asarray(front)[..., 3]
        ys, xs = np.nonzero(fa >= 250)
        meta[name] = {
            "hero": f"{name}.png",
            "front": f"{name}-front.png",
            "front_px_per_mm": a.front_px_per_mm,
            "front_size_px": [front.width, front.height],
            "front_object_px": [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1],
            "size_mm": [round(float(x), 1) for x in (hi - lo)],
            "pose": pose,
            "front_elevation_deg": front_el,
            "tier_axis": "width" if name == "nameplate" else "height",
            "source": str(src),
        }
        print(name, meta[name]["size_mm"], flush=True)
    for spec in a.extra:
        name, rest = spec.split("=", 1)
        src, pose = rest.rsplit(":", 1)
        it = load_item(src, pose)
        S = a.size * a.ss * 3 // 2
        view, proj, eye, fwd = hero_camera([it], S)
        buf = rnd.render([it], view, proj, (S, S), eye, fwd, False)
        lo, hi = it.bmin, it.bmax
        raw = composite(buf, a.ss, a.exposure, footprint=(float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])))
        fit_canvas(raw, (a.size, a.size), (0.1, 0.08, 0.9, 0.86), 0.86).save(out / f"{name}.png", optimize=True)
        meta[name] = {"hero": f"{name}.png", "size_mm": [round(float(x), 1) for x in (hi - lo)], "pose": pose, "source": str(src)}
        print(name, meta[name]["size_mm"], flush=True)
    meta_path.write_text(json.dumps(meta, indent=2))
    if a.group and len(items) == 5:
        render_group(rnd, items, out, a)
    print("done", flush=True)


# Group shot: all five at true relative scale on one floor, the hero camera. Positions in mm.
GROUP = {
    "figurine": (-62.0, 46.0, 8.0),
    "ornament": (38.0, 62.0, -10.0),
    "nameplate": (-8.0, -52.0, 4.0),
    "magnet": (86.0, -4.0, -14.0),
    "keychain": (112.0, -56.0, -18.0),
}


def render_group(rnd, items, out, a):
    placed = []
    for name, (x, y, turn) in GROUP.items():
        it = items[name]
        R = rot_z(turn)
        pos = it.pos @ R.T.astype(np.float32)
        nrm = it.nrm @ R.T.astype(np.float32)
        up = (R @ it.up).astype(np.float32)
        placed.append(Item(pos + np.array([x, y, 0], dtype=np.float32), nrm, it.alb, it.pz, up))
    W, H = a.size * 2, int(a.size * 1.25)
    ss = a.ss
    allp = np.concatenate([i.pos.reshape(-1, 3) for i in placed])
    lo, hi = allp.min(0), allp.max(0)
    center = (lo + hi) / 2
    radius = float(np.linalg.norm(hi - lo) / 2)
    fov = HERO_FOV
    aspect = W / H
    dist = radius / math.sin(math.radians(fov) / 2) * 0.78
    d = sph(-14.0, 17.0)
    eye = center + d * dist
    view = look_at(eye, center)
    proj = perspective(fov, aspect, dist * 0.2, dist * 4)
    buf = rnd.render(placed, view, proj, (W * ss, H * ss), eye, -d, False)
    img = composite(buf, ss, a.exposure, footprint=(float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])))
    arr = np.asarray(img)[..., 3]
    ys, xs = np.nonzero(arr > 4)
    m = int(0.03 * img.width)
    box = (max(0, xs.min() - m), max(0, ys.min() - m), min(img.width, xs.max() + m), min(img.height, ys.max() + m))
    img = img.crop(box)
    img.save(out / "group.png", optimize=True)
    print("group", img.size, flush=True)


if __name__ == "__main__":
    main()
