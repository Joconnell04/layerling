"""The painted model in filament colours: multi-angle set, plate views. p2p_sim rasterizes;
p2p_threemf.read_3mf_full reads the paint (bed frame, world=True)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from p2p_contracts.regions import VIEWS, CameraSpec
from p2p_sim.camera import Camera, _frame, camera_matrices, direction_from_angles
from p2p_sim.mesh import Mesh
from p2p_sim.raster import rasterize
from p2p_sim.render import flat_image, shaded_image
from PIL import Image

BG = (231, 233, 236)  # light neutral, not a stock filament colour
BED = (58, 60, 64)
BED_EDGE = (44, 46, 50)
RIGHT_LIMIT = (92, 72, 72)  # bed strip the right nozzle cannot reach
TOWER = (178, 227, 171)  # Bambu's "Prime tower" line colour
BLACK_AS = (51, 51, 51)  # Bambu draws black filament as #333333
BACK_3Q = CameraSpec(name="back_3q", azimuth_deg=125, elevation_deg=20)
MULTI_VIEWS = ("front", "front_3q", "right", "back_3q", "back", "left", "top")
# Bambu's thumbnail camera: orthographic, eye direction (-0.5, -0.5, 0.707)
ISO_DIR = np.array([-0.5, -0.5, 0.707])


def hex_rgb(hx: str) -> tuple[int, int, int]:
    h = (hx or "#808080").lstrip("#")[:6].ljust(6, "0")
    rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return BLACK_AS if rgb == (0, 0, 0) else rgb  # type: ignore[return-value]


@dataclass(frozen=True, eq=False)
class OrthoCamera(Camera):
    """p2p_sim camera with an orthographic projection (Bambu's thumbnails are orthographic)."""

    half_h: float = 1.0  # half the view height in mm

    @property
    def proj(self) -> np.ndarray:
        hh = self.half_h
        hw = hh * self.width / self.height
        n, f = self.near, self.far
        return np.array([[1 / hw, 0, 0, 0], [0, 1 / hh, 0, 0],
                         [0, 0, -2 / (f - n), -(f + n) / (f - n)], [0, 0, 0, 1]])

    def resized(self, width: int, height: int | None = None) -> OrthoCamera:
        return OrthoCamera(self.name, self.eye, self.target, self.up, self.fov_deg, width,
                           height or width, self.near, self.far, self.half_h)


def ortho_for_box(lo, hi, direction, width: int, height: int, name: str,
                  margin: float = 1.04, up=None) -> OrthoCamera:
    """Orthographic camera looking along -direction that fits the box [lo, hi]."""
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    d = np.asarray(direction, float)
    d = d / np.linalg.norm(d)
    center = (lo + hi) / 2
    radius = float(np.linalg.norm(hi - lo)) / 2 + 1.0
    cam = _frame(center, radius, d, 30.0, 1.0, width, name, up=up)
    corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1])
                         for z in (lo[2], hi[2])])
    v = (np.c_[corners, np.ones(8)] @ cam.view.T)[:, :3]
    half_w = max(abs(v[:, 0]).max(), 1e-3)
    half_h = max(abs(v[:, 1]).max(), 1e-3)
    half_h = max(half_h, half_w * height / width) * margin
    return OrthoCamera(name, cam.eye, cam.target, cam.up, 30.0, width, height,
                       max(cam.near * 0.5, 1e-3), cam.far * 1.5 + radius, half_h)


def _downsample(img: np.ndarray, ss: int) -> np.ndarray:
    if ss == 1:
        return img
    h, w = img.shape[0] // ss, img.shape[1] // ss
    return (img[: h * ss, : w * ss].reshape(h, ss, w, ss, 3).astype(np.float32)
            .mean(axis=(1, 3)).round().astype(np.uint8))


def shade(mesh: Mesh, cam: Camera, face_rgb: np.ndarray, ss: int = 2, bg=BG,
          flat: bool = False) -> np.ndarray:
    """Face-coloured image, per-face (not smoothed) lighting, ss x supersampled."""
    big = cam.resized(cam.width * ss, cam.height * ss)
    r = rasterize(mesh, big)
    if flat:
        return _downsample(flat_image(mesh, r, face_rgb, bg), ss)
    return _downsample(shaded_image(mesh, r, face_rgb, bg, smooth=False), ss)


@dataclass
class Scene:
    """The painted model in bed coordinates (mm, Z up)."""
    vertices: np.ndarray
    faces: np.ndarray
    face_slots: np.ndarray  # 1-based filament per face
    face_part: np.ndarray
    face_object: np.ndarray
    colours: list[str]  # filament colour per slot (slot = index + 1)
    warnings: list[str] = field(default_factory=list)

    @property
    def mesh(self) -> Mesh:
        return Mesh(self.vertices, self.faces, name="model")

    def face_rgb(self) -> np.ndarray:
        lut = np.array([hex_rgb(c) for c in self.colours] or [(128, 128, 128)], np.uint8)
        idx = np.clip(self.face_slots.astype(np.int64) - 1, 0, len(lut) - 1)
        return lut[idx]

    @property
    def bbox(self) -> tuple[np.ndarray, np.ndarray]:
        return self.vertices.min(axis=0), self.vertices.max(axis=0)


def load_scene(path: str | Path) -> Scene:
    from p2p_threemf import read_3mf_full

    m = read_3mf_full(path, world=True)
    return Scene(np.asarray(m.vertices, float), np.asarray(m.faces, np.int64),
                 np.asarray(m.face_slots, np.int32), np.asarray(m.face_part, np.int32),
                 np.asarray(m.face_object, np.int32), list(m.filament_colours),
                 list(m.warnings))


def _box(lo, hi) -> tuple[np.ndarray, np.ndarray]:
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], float)
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                  [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    return v, f


def _quad(x0, y0, x1, y1, z) -> tuple[np.ndarray, np.ndarray]:
    v = np.array([[x0, y0, z], [x1, y0, z], [x1, y1, z], [x0, y1, z]], float)
    return v, np.array([[0, 1, 2], [0, 2, 3]])


def plate_mesh(scene: Scene | None, bed: tuple[float, float], right_min_x: float | None,
               tower: tuple[list[float], float] | None) -> tuple[Mesh, np.ndarray]:
    """Model plus bed, the right-nozzle strip and the tower box, with face colours."""
    parts_v, parts_f, parts_c = [], [], []
    off = 0

    def add(v, f, rgb):
        nonlocal off
        parts_v.append(v)
        parts_f.append(f + off)
        parts_c.append(np.tile(np.array(rgb, np.uint8), (len(f), 1)) if len(np.shape(rgb)) == 1
                       else rgb)
        off += len(v)

    add(*_box((-2, -2, -1.6), (bed[0] + 2, bed[1] + 2, -0.6)), BED_EDGE)
    add(*_quad(0, 0, bed[0], bed[1], -0.5), BED)
    if right_min_x:
        add(*_quad(0, 0, right_min_x, bed[1], -0.45), RIGHT_LIMIT)
    if tower is not None:
        (x0, y0, x1, y1), top = tower
        add(*_box((x0, y0, 0.0), (x1, y1, max(top, 0.2))), TOWER)
    if scene is not None:
        add(scene.vertices, scene.faces, scene.face_rgb())
    return Mesh(np.concatenate(parts_v), np.concatenate(parts_f)), np.concatenate(parts_c)


def render_multi(scene: Scene, out_dir: Path, size: int = 560) -> dict[str, str]:
    """The model alone from every contract view plus back_3q, filament colours, shaded."""
    out_dir.mkdir(parents=True, exist_ok=True)
    mesh = scene.mesh
    rgb = scene.face_rgb()
    out = {}
    for name in MULTI_VIEWS:
        spec = BACK_3Q if name == "back_3q" else VIEWS[name]
        cam = camera_matrices(spec, mesh, size)
        img = shade(mesh, cam, rgb)
        Image.fromarray(img).save(out_dir / f"model_{name}.png", optimize=True)
        out[name] = f"model_{name}.png"
    return out


def render_plate(scene: Scene | None, out_dir: Path, bed=(256.0, 256.0),
                 right_min_x: float | None = 20.5,
                 tower: tuple[list[float], float] | None = None,
                 objects_box: tuple[np.ndarray, np.ndarray] | None = None) -> dict[str, str]:
    """plate_iso: the whole plate from Bambu's thumbnail direction. plate_top: straight down at
    2 px/mm, like Bambu's top_1.png. model_iso: Bambu's plate_1.png framing (objects' box grown
    10 percent in x/y and 20 percent in z, tower left out) for a side-by-side check."""
    out_dir.mkdir(parents=True, exist_ok=True)
    mesh, rgb = plate_mesh(scene, bed, right_min_x, tower)
    zmax = float(scene.bbox[1][2]) if scene is not None else 10.0
    if tower is not None:
        zmax = max(zmax, tower[1])
    out = {}
    cam = ortho_for_box((0, 0, -1.6), (bed[0], bed[1], zmax), ISO_DIR, 900, 640, "plate_iso")
    Image.fromarray(shade(mesh, cam, rgb)).save(out_dir / "plate_iso.png", optimize=True)
    out["plate_iso"] = "plate_iso.png"
    w = round(bed[0] * 2)
    cam = ortho_for_box((0, 0, -1.6), (bed[0], bed[1], zmax), (0, 0, 1), w,
                        round(bed[1] * 2), "plate_top", margin=1.0, up=(0, 1, 0))
    Image.fromarray(shade(mesh, cam, rgb, flat=False)).save(out_dir / "plate_top.png",
                                                             optimize=True)
    out["plate_top"] = "plate_top.png"
    if scene is not None:
        lo, hi = objects_box if objects_box is not None else scene.bbox
        lo, hi = np.asarray(lo, float), np.asarray(hi, float)
        c, e = (lo + hi) / 2, (hi - lo) / 2
        grow = np.array([1.1, 1.1, 1.2])
        m_mesh = scene.mesh
        cam = ortho_for_box(c - e * grow, c + e * grow, ISO_DIR, 512, 512, "model_iso",
                            margin=1.0)
        Image.fromarray(shade(m_mesh, cam, scene.face_rgb())).save(out_dir / "model_iso.png",
                                                                    optimize=True)
        out["model_iso"] = "model_iso.png"
    return out


def faces_per_part(scene: Scene) -> dict[int, dict[int, int]]:
    """part id -> {slot: faces}."""
    out: dict[int, dict[int, int]] = {}
    for part in np.unique(scene.face_part):
        sel = scene.face_part == part
        slots, counts = np.unique(scene.face_slots[sel], return_counts=True)
        out[int(part)] = {int(s): int(c) for s, c in zip(slots, counts, strict=True)}
    return out


def tower_estimate(n_used: int, height_mm: float, layer_mm: float, base_width: float,
                   change_len_mm: float = 10.0, prime_mm3: float = 15.0) -> tuple[float, float]:
    """Pre-slice tower footprint (W, D) in mm, ASSUMED worst case: every used filament changes
    once on each layer. Sizing rules from docs/research/native-slicer.md 7.6 (prime lines at
    0.75 mm pitch, ramming lines at 1.25 mm, W = sqrt(D x W0) rounded up to 0.5, minimum depth
    by height)."""
    if n_used <= 1:
        return 0.0, 0.0
    h = layer_mm
    prime_len = prime_mm3 / (h * (0.5 - 0.2146 * h))
    ram_len = change_len_mm * 2.4053 / (h * (1.0 - 0.2146 * h))
    min_d = float(np.interp(height_mm, [0, 5, 100, 250, 350], [5, 5, 20, 40, 60]))
    w = base_width
    d = min_d
    for _ in range(4):
        n_p = math.ceil(prime_len / max(w - 1.0, 1.0))
        n_r = math.ceil(ram_len / max(w - 1.5, 1.0))
        d = max((n_used - 1) * (n_p * 0.75 + n_r * 1.25) + 0.5, min_d)
        w = math.ceil(math.sqrt(d * base_width) * 2) / 2
    return float(w), float(d)


def view_direction(name: str) -> np.ndarray:
    spec = BACK_3Q if name == "back_3q" else VIEWS[name]
    return direction_from_angles(spec.azimuth_deg, spec.elevation_deg)
