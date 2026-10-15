"""Render kinds built on the face-ID raster.

Kinds (p2p_contracts.mcp_tools.RenderKind):
- flat     unlit per-face colors, no antialiasing: interior pixels are the exact filament sRGB
- shaded   per-face colors (or clay) with ambient + two Lambert lights, 2x supersampled
- texture  base-color texture sampled through the UVs, lightly shaded (SAM 3 input, likeness)
- overlay  flat colors + region boundary lines + numbered labels at each region's widest
           visible spot (set-of-marks)
- crops    tight camera aimed at one region, region outlined in magenta; returns its camera
- sim      shaded simulate_print result: bleed tint on flagged faces, faint layer lines

Deterministic for a given backend: fixed lights, fixed cameras, no jitter.
Per-pixel work runs only on covered pixels, in float32.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from p2p_contracts.regions import VIEWS
from PIL import Image, ImageDraw, ImageFont

from .camera import ALL_VIEWS, Camera, camera_for_points, camera_matrices, direction_from_angles
from .mesh import Mesh, sample_texture
from .raster import Raster, rasterize

BG = (104, 110, 120)
"""Default background: a cool mid grey that is not a stock filament color."""
CLAY = (206, 200, 190)
SHEET_ORDER = ["front", "front_3q", "top", "left", "right", "back"]
SUPERSAMPLE = {"flat": 1, "overlay": 1, "shaded": 2, "texture": 2, "sim": 2, "crops": 2}


@dataclass(eq=False)
class RenderResult:
    kind: str
    view: str
    image: np.ndarray
    """(H, W, 3) uint8."""
    camera: Camera
    raster: Raster
    """1x raster: face_id buffer and depth at the image resolution."""
    region_id: int | None = None
    labels: dict[int, tuple[int, int]] = field(default_factory=dict)
    """overlay: region id -> (x, y) pixel of its number."""

    @property
    def face_id(self) -> np.ndarray:
        return self.raster.face_id

    @property
    def depth(self) -> np.ndarray:
        return self.raster.depth

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(self.image).save(path, compress_level=4)
        return path

    def meta(self, path: str | None = None) -> dict:
        d = {
            "kind": self.kind,
            "view": self.view,
            "width": int(self.image.shape[1]),
            "height": int(self.image.shape[0]),
            "camera": self.camera.to_dict(),
        }
        if path is not None:
            d["path"] = path
        if self.region_id is not None:
            d["region_id"] = int(self.region_id)
        if self.labels:
            d["labels"] = {str(k): [int(v[0]), int(v[1])] for k, v in self.labels.items()}
        return d


# ---------------------------------------------------------------------- pixel sets


@dataclass(eq=False)
class Pixels:
    """Covered pixels of a raster, flattened: idx into H*W, face id, barycentrics."""

    r: Raster
    idx: np.ndarray
    fid: np.ndarray
    bary: np.ndarray

    @classmethod
    def of(cls, r: Raster) -> Pixels:
        idx = np.flatnonzero(r.face_id.reshape(-1) >= 0)
        return cls(r, idx, r.face_id.reshape(-1)[idx], r.bary.reshape(-1, 3)[idx])

    def interp(self, corner_vals: np.ndarray) -> np.ndarray:
        """corner_vals (F, 3, C) -> (M, C) barycentric interpolation."""
        cv = corner_vals[self.fid]
        b = self.bary
        return cv[:, 0] * b[:, 0:1] + cv[:, 1] * b[:, 1:2] + cv[:, 2] * b[:, 2:3]

    def points(self, mesh: Mesh) -> np.ndarray:
        return self.interp(_corners32(mesh, "pos"))

    def compose(self, colors: np.ndarray, bg) -> np.ndarray:
        h, w = self.r.face_id.shape
        out = np.empty((h * w, 3), dtype=np.float32)
        out[:] = np.asarray(bg, dtype=np.float32)
        out[self.idx] = colors
        return out.reshape(h, w, 3)


def _corners32(mesh: Mesh, what: str) -> np.ndarray:
    key = ("corners32", what)
    hit = mesh._cache.get(key)
    if hit is None:
        if what == "pos":
            hit = mesh.triangles.astype(np.float32)
        elif what == "normal":
            hit = mesh.vertex_normals[mesh.faces].astype(np.float32)
        elif what == "uv":
            hit = mesh.uv[mesh.faces].astype(np.float32)
        else:
            raise KeyError(what)
        mesh._cache[key] = hit
    return hit


def _downsample(img: np.ndarray, ss: int) -> np.ndarray:
    if ss > 1:
        h, w = img.shape[0] // ss, img.shape[1] // ss
        img = img.reshape(h, ss, w, ss, -1).mean(axis=(1, 3))
    return np.clip(np.rint(img), 0, 255).astype(np.uint8)


def _normals(mesh: Mesh, px: Pixels, smooth: bool = True) -> np.ndarray:
    fn = mesh.face_normals.astype(np.float32)
    if smooth:
        n = px.interp(_corners32(mesh, "normal"))
        ln = np.linalg.norm(n, axis=1, keepdims=True)
        bad = ln[:, 0] < 1e-6
        n = n / np.where(ln > 1e-6, ln, 1.0)
        n[bad] = fn[px.fid[bad]]
    else:
        n = fn[px.fid]
    view = (px.r.camera.eye.astype(np.float32)[None] - px.points(mesh))
    flip = np.einsum("ij,ij->i", n, view) < 0  # back faces seen through holes or clipping
    n[flip] *= -1
    return n


def _lights(camera: Camera) -> tuple[np.ndarray, np.ndarray]:
    """Key light from upper left front, fill from the right; camera space -> world."""
    rot = camera.view[:3, :3]
    key = np.array([-0.45, 0.65, 0.62])
    fill = np.array([0.7, -0.1, 0.7])
    return (rot.T @ (key / np.linalg.norm(key))).astype(np.float32), (
        rot.T @ (fill / np.linalg.norm(fill))
    ).astype(np.float32)


def shade(mesh: Mesh, px: Pixels, smooth: bool = True) -> np.ndarray:
    """(M,) lighting multiplier: 0.34 ambient + 0.62 key + 0.16 fill, clipped at 1.08."""
    n = _normals(mesh, px, smooth)
    key, fill = _lights(px.r.camera)
    s = 0.34 + 0.62 * np.clip(n @ key, 0, None) + 0.16 * np.clip(n @ fill, 0, None)
    return np.clip(s, 0, 1.08)


def _face_rgb(mesh: Mesh, face_colors) -> np.ndarray:
    if face_colors is None:
        return np.tile(np.array(CLAY, dtype=np.uint8), (mesh.n_faces, 1))
    fc = np.asarray(face_colors)
    if fc.shape != (mesh.n_faces, 3):
        raise ValueError(f"face_colors must be ({mesh.n_faces}, 3), got {fc.shape}")
    return fc.astype(np.uint8)


def _font(px: int):
    try:
        return ImageFont.load_default(size=px)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


# ---------------------------------------------------------------------- kinds on a raster


def flat_image(mesh: Mesh, r: Raster, face_colors, bg=BG) -> np.ndarray:
    px = Pixels.of(r)
    return px.compose(_face_rgb(mesh, face_colors)[px.fid].astype(np.float32), bg)


def shaded_image(mesh: Mesh, r: Raster, face_colors=None, bg=BG, smooth=True) -> np.ndarray:
    px = Pixels.of(r)
    col = _face_rgb(mesh, face_colors)[px.fid].astype(np.float32)
    return px.compose(col * shade(mesh, px, smooth)[:, None], bg)


def texture_image(mesh: Mesh, r: Raster, bg=BG, light: float = 0.35, bilinear: bool = True,
                  face_colors=None) -> np.ndarray:
    px = Pixels.of(r)
    if mesh.texture is None or mesh.uv is None:
        base = mesh.face_texture_colors() if face_colors is None else face_colors
        if base is None:
            raise ValueError("mesh has no texture, vertex colors, or face colors")
        col = np.asarray(base, dtype=np.float32)[px.fid]
    else:
        uv = px.interp(_corners32(mesh, "uv"))
        col = sample_texture(mesh.texture, uv, bilinear).astype(np.float32)
    if light > 0:
        col = col * ((1 - light) + light * shade(mesh, px))[:, None]
    return px.compose(col, bg)


def sim_image(mesh: Mesh, r: Raster, face_colors, bleed_mix=None, bleed_rgb=None,
              layer_mm: float | None = 0.2, bg=BG) -> np.ndarray:
    """Shaded print preview: bleed-flagged faces pulled toward the darker color behind them."""
    px = Pixels.of(r)
    col = _face_rgb(mesh, face_colors)[px.fid].astype(np.float32)
    if bleed_mix is not None and bleed_rgb is not None:
        m = np.asarray(bleed_mix, dtype=np.float32)[px.fid][:, None]
        col = col * (1 - m) + np.asarray(bleed_rgb, dtype=np.float32)[px.fid] * m
    s = shade(mesh, px)
    if layer_mm:
        ph = (px.points(mesh)[:, 2] / layer_mm) % 1.0
        s = s * (1 - 0.07 * (np.abs(ph - 0.5) * 2) ** 4)
    return px.compose(col * s[:, None], bg)


def region_image(r: Raster, face_regions: np.ndarray) -> np.ndarray:
    """(H, W) region id per pixel, -1 background."""
    fr = np.asarray(face_regions)
    out = np.full(r.face_id.shape, -1, dtype=np.int64)
    m = r.mask
    out[m] = fr[r.face_id[m]]
    return out


def boundary_mask(reg: np.ndarray, thickness: int = 2) -> np.ndarray:
    """Pixels where the region id changes (including the silhouette)."""
    b = np.zeros(reg.shape, dtype=bool)
    dx = reg[:, 1:] != reg[:, :-1]
    dy = reg[1:, :] != reg[:-1, :]
    b[:, 1:] |= dx
    b[1:, :] |= dy
    if thickness >= 2:
        b[:, :-1] |= dx
        b[:-1, :] |= dy
    if thickness >= 3:
        from scipy.ndimage import binary_dilation

        b = binary_dilation(b, iterations=thickness - 2)
    return b


def label_positions(reg: np.ndarray, min_px: int = 40) -> dict[int, tuple[int, int, float]]:
    """Region id -> (x, y, clearance_px) at the visible pixel farthest from the region's edge."""
    from scipy.ndimage import distance_transform_edt, find_objects

    out: dict[int, tuple[int, int, float]] = {}
    lab = np.where(reg > 0, reg, 0)
    if lab.max() <= 0:
        return out
    for rid0, sl in enumerate(find_objects(lab)):
        if sl is None:
            continue
        rid = rid0 + 1
        sub = lab[sl] == rid
        if sub.sum() < min_px:
            continue
        d = distance_transform_edt(np.pad(sub, 1))[1:-1, 1:-1]
        j, i = np.unravel_index(int(np.argmax(d)), d.shape)
        out[rid] = (int(sl[1].start + i), int(sl[0].start + j), float(d[j, i]))
    return out


def draw_labels(img: np.ndarray, pos: dict[int, tuple[int, int, float]], size_px: int) -> np.ndarray:
    im = Image.fromarray(img)
    dr = ImageDraw.Draw(im)
    base = max(11, round(size_px / 46))
    for rid, (x, y, clear) in sorted(pos.items()):
        fs = int(np.clip(clear * 1.1, 10, base))
        dr.text((x, y), str(rid), fill=(255, 255, 255), font=_font(fs), anchor="mm",
                stroke_width=max(2, fs // 7), stroke_fill=(0, 0, 0))
    return np.asarray(im)


def overlay_image(mesh: Mesh, r: Raster, face_colors, face_regions: np.ndarray, bg=BG,
                  line_rgb=(12, 12, 12), min_label_px: int = 40):
    img = flat_image(mesh, r, face_colors, bg)
    reg = region_image(r, face_regions)
    img[boundary_mask(reg, 2)] = line_rgb
    pos = label_positions(reg, min_label_px)
    out = draw_labels(_downsample(img, 1), pos, r.face_id.shape[1])
    return out, {k: (v[0], v[1]) for k, v in pos.items()}


def outline(img: np.ndarray, hit: np.ndarray, rgb=(255, 0, 200), width: int = 2) -> np.ndarray:
    from scipy.ndimage import binary_dilation

    edge = binary_dilation(hit, iterations=width) & ~hit
    out = img.copy()
    out[edge] = rgb
    return out


# ---------------------------------------------------------------------- high level


def _rasters(mesh: Mesh, cam: Camera, levels, backend) -> dict[int, Raster]:
    out = {1: rasterize(mesh, cam, backend)}
    for ss in set(levels) - {1}:
        out[ss] = rasterize(mesh, cam.resized(cam.width * ss, cam.height * ss), backend)
    return out


def _render_kind(mesh, kind, rasters, ss, face_colors, face_regions, bg, sim,
                 region_id) -> RenderResult:
    r1 = rasters[1]
    rh = rasters[ss]
    labels: dict[int, tuple[int, int]] = {}
    if kind == "flat":
        img = _downsample(flat_image(mesh, rh, face_colors, bg), ss)
    elif kind == "shaded":
        img = _downsample(shaded_image(mesh, rh, face_colors, bg), ss)
    elif kind == "texture":
        img = _downsample(texture_image(mesh, rh, bg), ss)
    elif kind == "overlay":
        if face_regions is None:
            raise ValueError("overlay needs face_regions")
        img, labels = overlay_image(mesh, r1, face_colors, face_regions, bg)
    elif kind == "sim":
        if sim is None:
            raise ValueError("sim render needs a SimResult")
        img = _downsample(sim_image(mesh, rh, sim.face_rgb, sim.face_bleed_mix,
                                    sim.face_bleed_rgb, sim.params.layer_mm, bg), ss)
    elif kind == "crops":
        img = _downsample(shaded_image(mesh, rh, face_colors, bg), ss)
        if face_regions is not None and region_id is not None:
            img = outline(img, region_image(r1, face_regions) == region_id)
    else:
        raise ValueError(f"unknown render kind: {kind}")
    return RenderResult(kind, r1.camera.name, img, r1.camera, r1, region_id=region_id,
                        labels=labels)


def render(mesh: Mesh, view: str | Camera, kind: str = "shaded", face_colors=None,
           face_regions=None, size_px: int = 1024, bg=BG, ss: int | None = None,
           backend: str | None = None, sim=None, region_id: int | None = None) -> RenderResult:
    """Render one kind for one view (a contract view name or a Camera)."""
    cam = view if isinstance(view, Camera) else camera_matrices(ALL_VIEWS[view], mesh, size_px)
    level = SUPERSAMPLE.get(kind, 1) if ss is None else ss
    rasters = _rasters(mesh, cam, [level], backend)
    return _render_kind(mesh, kind, rasters, level, face_colors, face_regions, bg, sim, region_id)


def render_views(mesh: Mesh, kinds=("flat", "shaded", "overlay"), views=None, face_colors=None,
                 face_regions=None, size_px: int = 1024, bg=BG, backend: str | None = None,
                 sim=None) -> list[RenderResult]:
    """All kinds for all views, one raster pass per view and supersample level."""
    out = []
    kinds = [k for k in kinds if k != "crops"]
    for v in views or list(VIEWS):
        cam = camera_matrices(ALL_VIEWS[v], mesh, size_px)
        rasters = _rasters(mesh, cam, [SUPERSAMPLE.get(k, 1) for k in kinds], backend)
        for k in kinds:
            out.append(_render_kind(mesh, k, rasters, SUPERSAMPLE.get(k, 1), face_colors,
                                    face_regions, bg, sim, None))
    return out


def best_region_camera(mesh: Mesh, face_regions: np.ndarray, region_id: int, size_px: int = 512,
                       probe_px: int = 128, backend: str | None = None) -> Camera:
    """Tight camera on one region, from the candidate direction that shows most of it.

    Candidates: the region's area-weighted mean normal and the six contract view directions.
    Each is probed at low resolution; the most visible region pixels wins (the mean normal gets
    a 10% bonus so a head-on view wins ties).
    """
    sel = np.nonzero(np.asarray(face_regions) == region_id)[0]
    if sel.size == 0:
        raise ValueError(f"region {region_id} has no faces")
    pts = mesh.triangles[sel].reshape(-1, 3)
    n = (mesh.face_normals[sel] * mesh.face_areas[sel, None]).sum(axis=0)
    cands = []
    if np.linalg.norm(n) > 1e-9:
        cands.append((n / np.linalg.norm(n), 1.1))
    for spec in VIEWS.values():
        cands.append((direction_from_angles(spec.azimuth_deg, spec.elevation_deg), 1.0))
    in_sel = np.zeros(mesh.n_faces + 1, dtype=bool)
    in_sel[sel] = True
    best, best_score = cands[0][0], -1.0
    for d, bonus in cands:
        cam = camera_for_points(pts, d, size_px=probe_px, name=f"crop_r{region_id}")
        r = rasterize(mesh, cam, backend)
        vis = in_sel[r.face_id[r.mask]].sum() * bonus
        if vis > best_score:
            best, best_score = d, vis
    return camera_for_points(pts, best, size_px=size_px, name=f"crop_r{region_id}")


def render_crops(mesh: Mesh, face_regions: np.ndarray, region_ids, face_colors=None,
                 size_px: int = 512, bg=BG, backend: str | None = None) -> list[RenderResult]:
    """One tight shaded close-up per region, the target region outlined in magenta."""
    out = []
    for rid in region_ids:
        cam = best_region_camera(mesh, face_regions, int(rid), size_px, backend=backend)
        rasters = _rasters(mesh, cam, [SUPERSAMPLE["crops"]], backend)
        out.append(_render_kind(mesh, "crops", rasters, SUPERSAMPLE["crops"], face_colors,
                                face_regions, bg, None, int(rid)))
    return out


def render_sheet(mesh: Mesh, face_colors, out_png: str | Path, kind: str = "shaded",
                 cell_px: int = 512, title: str | None = None, bg=(40, 41, 46),
                 backend: str | None = None, sim=None) -> Path:
    """3 x 2 grid of the six contract views (front, 3/4, top / left, right, back)."""
    pad, label_h = 10, 26
    head = 34 if title else 0
    cols, rows = 3, 2
    w = cols * cell_px + (cols + 1) * pad
    h = head + rows * (cell_px + label_h) + (rows + 1) * pad
    sheet = Image.new("RGB", (w, h), (22, 22, 25))
    dr = ImageDraw.Draw(sheet)
    font = _font(15)
    if title:
        dr.text((pad + 2, pad + 4), title, fill=(230, 230, 235), font=_font(18))
    for i, v in enumerate(SHEET_ORDER):
        res = render(mesh, v, kind, face_colors=face_colors, size_px=cell_px, bg=bg,
                     backend=backend, sim=sim)
        c, rr = i % cols, i // cols
        x = pad + c * (cell_px + pad)
        y = head + pad + rr * (cell_px + label_h + pad)
        sheet.paste(Image.fromarray(res.image), (x, y))
        dr.text((x + 2, y + cell_px + 5), v.replace("_", " "), fill=(150, 152, 160), font=font)
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_png)
    return out_png
