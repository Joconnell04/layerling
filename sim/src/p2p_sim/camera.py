"""Cameras: the fixed contract views (p2p_contracts.VIEWS) and region close-ups.

Conventions (shared by every backend and by anyone projecting points):
- World frame is the contract frame (mm, Z up, front faces -Y).
- Camera looks down its -Z axis (OpenGL). `view` maps world -> camera, `proj` camera -> clip.
- Pixel (x, y): x to the right, y down, origin at the top-left corner of the image; the center
  of pixel (i, j) is at (i + 0.5, j + 0.5). `project` returns these continuous coordinates, so
  int(floor(x)), int(floor(y)) indexes the image array as img[y, x].
- depth is the positive distance along the camera's viewing axis, in mm.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from p2p_contracts.regions import PROOF_VIEWS, VIEWS, CameraSpec

ALL_VIEWS: dict[str, CameraSpec] = {**PROOF_VIEWS, **VIEWS}
"""Every named camera: the six contract views plus the two extra proof views."""

from .mesh import Mesh


def _normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def look_at(eye: np.ndarray, target: np.ndarray, up: np.ndarray) -> np.ndarray:
    f = _normalize(target - eye)
    r = np.cross(f, up)
    if np.linalg.norm(r) < 1e-9:  # up parallel to view: pick any perpendicular
        r = np.cross(f, np.array([0.0, 1.0, 0.0]))
        if np.linalg.norm(r) < 1e-9:
            r = np.cross(f, np.array([1.0, 0.0, 0.0]))
    r = _normalize(r)
    u = np.cross(r, f)
    m = np.eye(4)
    m[0, :3], m[1, :3], m[2, :3] = r, u, -f
    m[:3, 3] = -m[:3, :3] @ eye
    return m


def perspective(fov_y_deg: float, aspect: float, near: float, far: float) -> np.ndarray:
    t = 1.0 / np.tan(np.radians(fov_y_deg) / 2)
    m = np.zeros((4, 4))
    m[0, 0] = t / aspect
    m[1, 1] = t
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = 2 * far * near / (near - far)
    m[3, 2] = -1.0
    return m


@dataclass(frozen=True, eq=False)
class Camera:
    name: str
    eye: np.ndarray
    target: np.ndarray
    up: np.ndarray
    fov_deg: float
    width: int
    height: int
    near: float
    far: float

    @property
    def view(self) -> np.ndarray:
        return look_at(self.eye, self.target, self.up)

    @property
    def proj(self) -> np.ndarray:
        return perspective(self.fov_deg, self.width / self.height, self.near, self.far)

    @property
    def view_proj(self) -> np.ndarray:
        return self.proj @ self.view

    @property
    def forward(self) -> np.ndarray:
        return _normalize(self.target - self.eye)

    def project(self, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """World points (N, 3) -> (pixel xy (N, 2) float, depth (N,) mm along the view axis)."""
        p = np.asarray(points, dtype=np.float64).reshape(-1, 3)
        ph = np.concatenate([p, np.ones((len(p), 1))], axis=1)
        cam = ph @ self.view.T
        clip = cam @ self.proj.T
        w = clip[:, 3:4]
        ndc = clip[:, :2] / np.where(np.abs(w) < 1e-12, 1e-12, w)
        x = (ndc[:, 0] + 1) * 0.5 * self.width
        y = (1 - ndc[:, 1]) * 0.5 * self.height
        return np.stack([x, y], axis=1), -cam[:, 2]

    def pixel_rays(self, xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Pixel coords (N, 2) (continuous, as returned by project) -> (origins, unit dirs)."""
        xy = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
        ndc_x = xy[:, 0] / self.width * 2 - 1
        ndc_y = 1 - xy[:, 1] / self.height * 2
        inv = np.linalg.inv(self.view_proj)
        near = np.stack([ndc_x, ndc_y, -np.ones_like(ndc_x), np.ones_like(ndc_x)], 1) @ inv.T
        far = np.stack([ndc_x, ndc_y, np.ones_like(ndc_x), np.ones_like(ndc_x)], 1) @ inv.T
        near = near[:, :3] / near[:, 3:4]
        far = far[:, :3] / far[:, 3:4]
        d = far - near
        d /= np.linalg.norm(d, axis=1, keepdims=True)
        return np.repeat(self.eye[None], len(xy), axis=0), d

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "eye": [float(x) for x in self.eye],
            "target": [float(x) for x in self.target],
            "up": [float(x) for x in self.up],
            "fov_deg": float(self.fov_deg),
            "width": int(self.width),
            "height": int(self.height),
            "near": float(self.near),
            "far": float(self.far),
            "view": self.view.tolist(),
            "proj": self.proj.tolist(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Camera:
        return cls(
            name=d["name"], eye=np.array(d["eye"]), target=np.array(d["target"]),
            up=np.array(d["up"]), fov_deg=d["fov_deg"], width=d["width"], height=d["height"],
            near=d["near"], far=d["far"],
        )

    def resized(self, width: int, height: int | None = None) -> Camera:
        return Camera(self.name, self.eye, self.target, self.up, self.fov_deg, width,
                      height or width, self.near, self.far)


def direction_from_angles(azimuth_deg: float, elevation_deg: float) -> np.ndarray:
    """Unit vector from the target toward the camera. Azimuth around +Z from +X."""
    az, el = np.radians(azimuth_deg), np.radians(elevation_deg)
    return np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])


def _frame(center, radius, direction, fov_deg, margin, size_px, name, up=None) -> Camera:
    direction = _normalize(np.asarray(direction, dtype=np.float64))
    dist = radius * margin / np.sin(np.radians(fov_deg) / 2)
    eye = center + direction * dist
    if up is None:
        up = np.array([0.0, 0.0, 1.0])
        if abs(direction[2]) > 0.999:  # looking straight down/up: back of the model is up
            up = np.array([0.0, 1.0, 0.0]) * np.sign(direction[2])
    near = max(dist - radius * 1.6, dist * 1e-3)
    far = dist + radius * 1.6
    return Camera(name, eye, np.asarray(center, dtype=np.float64), up, fov_deg, size_px, size_px,
                  near, far)


def camera_matrices(spec: CameraSpec | str, mesh: Mesh, size_px: int = 1024) -> Camera:
    """Camera for a contract view, framed on the mesh's bounding sphere (bbox center).

    Distance = radius * fit_margin / sin(fov / 2), so the whole model fits at any azimuth.
    """
    if isinstance(spec, str):
        spec = ALL_VIEWS[spec]
    center, radius = mesh.bounding_sphere
    d = direction_from_angles(spec.azimuth_deg, spec.elevation_deg)
    return _frame(center, radius, d, spec.fov_deg, spec.fit_margin, size_px, spec.name)


def camera_for_points(
    points: np.ndarray,
    direction: np.ndarray,
    size_px: int = 512,
    fov_deg: float = 30.0,
    margin: float = 1.35,
    min_radius_mm: float = 2.0,
    name: str = "crop",
) -> Camera:
    """Tight camera looking at `points` from `direction` (unit vector toward the camera)."""
    p = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    lo, hi = p.min(axis=0), p.max(axis=0)
    center = (lo + hi) / 2
    radius = max(float(np.linalg.norm(p - center, axis=1).max()), min_radius_mm)
    return _frame(center, radius, direction, fov_deg, margin, size_px, name)


def default_cameras(mesh: Mesh, size_px: int = 1024, views: list[str] | None = None) -> dict:
    return {v: camera_matrices(VIEWS[v], mesh, size_px) for v in (views or list(VIEWS))}
