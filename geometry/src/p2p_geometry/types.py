"""Result types for stage 6 geometry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
from p2p_contracts.job import PrintSettings, ProductType

# Matches p2p_contracts.geometry.PartRef.role (minus "figure").
PartRole = Literal["base", "keyring", "hanging_loop", "plate", "text", "back", "cutter"]
Orientation = Literal["upright", "flat"]


@dataclass
class GeometrySettings:
    """Printability limits. Defaults follow the stage 6 rules for a 0.4 mm nozzle, 0.2 mm layers."""

    nozzle_mm: float = 0.4
    layer_mm: float = 0.2
    min_cap_height_mm: float = 5.0
    text_relief_min_mm: float = 0.8
    text_relief_max_mm: float = 1.2
    loop_min_section_mm: float = 2.5
    loop_min_hole_mm: float = 4.0
    loop_min_overlap_mm: float = 1.5
    plate_min_thickness_mm: float = 3.0
    plate_min_overlap_mm: float = 1.0
    keychain_min_thickness_mm: float = 4.0
    voxel_pitch_mm: float | None = None  # None = auto from product size
    structure_filament_id: str | None = None  # None = darkest PLA in the palette

    @property
    def min_stroke_mm(self) -> float:
        return 2 * self.nozzle_mm

    @classmethod
    def coerce(cls, settings: GeometrySettings | PrintSettings | None) -> GeometrySettings:
        if settings is None:
            return cls()
        if isinstance(settings, GeometrySettings):
            return settings
        return cls(nozzle_mm=settings.nozzle_mm, layer_mm=settings.layer_mm)


@dataclass
class Part:
    """One closed manifold mesh the 3MF writer adds as a part of the single print object."""

    name: str
    role: PartRole
    vertices: np.ndarray  # (n, 3) float64, mm, print frame
    faces: np.ndarray  # (m, 3) int64
    filament_id: str | None
    note: str | None = None

    def to_trimesh(self):
        import trimesh

        return trimesh.Trimesh(self.vertices, self.faces, process=False)

    @property
    def watertight(self) -> bool:
        return self.to_trimesh().is_watertight

    @property
    def volume_mm3(self) -> float:
        return float(self.to_trimesh().volume)


@dataclass
class RepairReport:
    source_vertices: int
    source_faces: int
    welded_vertices: int = 0
    removed_faces: dict[str, list[int]] = field(default_factory=dict)
    holes_filled: int = 0
    hole_faces_added: int = 0
    holes_skipped: list[int] = field(default_factory=list)  # edge counts of holes left open
    nonmanifold_edges_before: int = 0
    nonmanifold_edges_after: int = 0
    boundary_edges_before: int = 0
    boundary_edges_after: int = 0
    faces_flipped: int = 0
    inverted: bool = False
    components: int = 1
    small_components: list[int] = field(default_factory=list)  # face counts
    watertight: bool = False
    plinth_cap_faces: int = 0
    plinth_height_frac: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["removed_faces"] = {k: list(map(int, v)) for k, v in self.removed_faces.items()}
        return d


@dataclass
class RepairedFigure:
    """The figure mesh after local repair.

    Face i of `faces` came from source face `face_source[i]` (-1 for faces added to fill holes).
    Source vertex j is now vertex `vertex_source[j]`. When nothing was removed, the first
    `source_faces` entries are the identity, so per-triangle paint and labels carry over as is.
    """

    vertices: np.ndarray
    faces: np.ndarray
    face_source: np.ndarray
    vertex_source: np.ndarray
    report: RepairReport

    @property
    def face_order_preserved(self) -> bool:
        n = self.report.source_faces
        return len(self.face_source) >= n and bool(
            np.array_equal(self.face_source[:n], np.arange(n))
        )

    def remap_face_values(self, values: np.ndarray, fill: Any = 0) -> np.ndarray:
        """Carry a per-source-face array (labels, colors) onto the repaired faces."""
        values = np.asarray(values)
        out = np.empty((len(self.face_source),) + values.shape[1:], dtype=values.dtype)
        ok = self.face_source >= 0
        out[ok] = values[self.face_source[ok]]
        out[~ok] = fill
        return out


@dataclass
class ProductGeometry:
    product: ProductType
    orientation: Orientation
    transform: np.ndarray  # (4, 4): Meshy GLB scene coords -> print frame (mm, Z up, base z=0)
    figure: RepairedFigure  # vertices already in the print frame
    parts: list[Part]
    bbox_min: np.ndarray
    bbox_max: np.ndarray
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # decisions, one line each
    metrics: dict[str, Any] = field(default_factory=dict)
    display: str = "front"  # flat products: which side of the figure faces up

    @property
    def size_tier(self) -> str | None:
        return self.metrics.get("size_tier")

    @property
    def contract_orientation(self) -> str:
        """GeometryResult.orientation: upright; flat_face_up (lying on its back, face up);
        flat_back_down (lying on a flank; its flat back is the bed side)."""
        if self.orientation == "upright":
            return "upright"
        return "flat_face_up" if self.display == "front" else "flat_back_down"

    @property
    def supports(self) -> str:
        """Flat products are height fields: supports none (tree supports would fill a magnet
        pocket). Upright figures need tree supports under belly and chin."""
        return "none" if self.orientation == "flat" else "tree"

    @property
    def size_mm(self) -> np.ndarray:
        return self.bbox_max - self.bbox_min

    def part(self, role: str) -> Part | None:
        return next((p for p in self.parts if p.role == role), None)

    def summary(self) -> dict[str, Any]:
        return {
            "product": self.product.value,
            "orientation": self.contract_orientation,
            "supports": self.supports,
            "transform": self.transform.round(6).tolist(),
            "bbox_min_mm": self.bbox_min.round(3).tolist(),
            "bbox_max_mm": self.bbox_max.round(3).tolist(),
            "size_mm": self.size_mm.round(2).tolist(),
            "figure": {
                "n_vertices": len(self.figure.vertices),
                "n_faces": len(self.figure.faces),
                "face_order_preserved": self.figure.face_order_preserved,
                "repair": self.figure.report.as_dict(),
            },
            "parts": [
                {
                    "name": p.name,
                    "role": p.role,
                    "filament_id": p.filament_id,
                    "n_faces": len(p.faces),
                    "watertight": p.watertight,
                    "volume_mm3": round(p.volume_mm3, 1),
                    "note": p.note,
                }
                for p in self.parts
            ],
            "warnings": self.warnings,
            "notes": self.notes,
            "metrics": _jsonable(self.metrics),
        }


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return x.round(4).tolist()
    if isinstance(x, (np.floating, float)):
        return round(float(x), 4)
    if isinstance(x, np.integer):
        return int(x)
    return x
