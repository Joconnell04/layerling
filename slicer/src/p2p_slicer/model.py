"""Painted project 3MF -> one bed-frame mesh with a filament per triangle (section 2 step 1).

Uses p2p_threemf.reader.read_3mf_full(world=True): build transforms applied, so coordinates are
the plate's (what Bambu's G-code uses). Each triangle carries its effective slot: the painted
filament where painted, else the part's filament, else the object's. `face_painted` keeps the
difference (segmentation treats unpainted area as losing to paint on tops and bottoms).
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .config import SliceConfig, load_project_config, slice_config
from .layers import parse_profile


@dataclass
class Project:
    path: Path
    vertices: np.ndarray          # (V, 3) float64, bed frame, mm
    faces: np.ndarray             # (F, 3) int64
    face_slot: np.ndarray         # (F,) int16, effective 1-based slot
    face_painted: np.ndarray      # (F,) bool, paint (not the part filament) decides the slot
    face_part: np.ndarray         # (F,) int32
    project_config: dict[str, Any]
    layer_profile: list[tuple[float, float]] | None
    objects: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cfg: SliceConfig | None = None

    @property
    def z_max(self) -> float:
        return float(self.vertices[:, 2].max()) if len(self.vertices) else 0.0

    @property
    def bbox(self) -> tuple[np.ndarray, np.ndarray]:
        return self.vertices.min(0), self.vertices.max(0)

    @property
    def n_filaments(self) -> int:
        return len(self.project_config.get("filament_colour") or [])


def load_project(path: str | Path, overrides: dict[str, Any] | None = None) -> Project:
    from p2p_threemf.reader import read_3mf_full
    path = Path(path)
    r = read_3mf_full(path, world=True)
    proj = load_project_config(path)
    for k, v in (overrides or {}).items():
        proj[k] = v
    profile = None
    with zipfile.ZipFile(path) as z:
        if "Metadata/layer_heights_profile.txt" in z.namelist():
            profile = parse_profile(z.read("Metadata/layer_heights_profile.txt").decode())
    painted = np.array([c != "" for c in r.codes], dtype=bool)
    v = np.asarray(r.vertices, dtype=np.float64)
    # Bambu drops an object so its lowest point sits on the bed
    zmin = v[:, 2].min() if len(v) else 0.0
    if abs(zmin) > 1e-9:
        v = v.copy()
        v[:, 2] -= zmin
    lo, hi = v.min(0), v.max(0)
    objects = [{"name": path.stem, "bbox": {"x": float(lo[0]), "y": float(lo[1]),
                                             "z": float(lo[2]), "width": float(hi[0] - lo[0]),
                                             "depth": float(hi[1] - lo[1]),
                                             "height": float(hi[2] - lo[2])},
                "triangle_count": len(r.faces)}]
    p = Project(path=path, vertices=v, faces=np.asarray(r.faces, dtype=np.int64),
                face_slot=np.asarray(r.face_slots, dtype=np.int16), face_painted=painted,
                face_part=np.asarray(r.face_part, dtype=np.int32), project_config=proj,
                layer_profile=profile, objects=objects, warnings=list(r.warnings))
    p.cfg = slice_config(proj)
    return p
