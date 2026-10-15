"""Stage 6 output: jobs/<id>/s06_printability/a<N>/geometry.json plus the mesh files it names.

p2p_geometry.build_product writes it; p2p_threemf.retarget_3mf and write_3mf read it.
The figure keeps Meshy's triangles (optionally repaired); labels carry over through face_index_map
instead of being re-projected. Add-ons are separate closed parts that
the 3MF writer places in the SAME object as extra parts, each with its own filament.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .manifest import FileRef, Strict

Matrix4 = tuple[
    tuple[float, float, float, float], tuple[float, float, float, float],
    tuple[float, float, float, float], tuple[float, float, float, float],
]


class PartRef(Strict):
    name: str
    role: Literal["figure", "base", "keyring", "hanging_loop", "plate", "text", "back", "cutter"]
    mesh: FileRef = Field(description="PLY in the print frame (mm, Z up, front -Y)")
    filament_id: str | None = Field(None, description="None for the painted figure")
    watertight: bool


class GeometryResult(Strict):
    schema_version: Literal["1"] = "1"
    source_glb: FileRef | None = Field(None, description="The Meshy glTF the transform applies to")
    transform: Matrix4 = Field(description="Meshy glTF frame -> print frame, row-major")
    figure: PartRef = Field(description="The repaired figure mesh, faces in Meshy order")
    face_index_map: FileRef | None = Field(
        None, description="int32 npy: new face -> original Meshy face, only if repair changed faces"
    )
    parts: list[PartRef] = Field(
        default_factory=list,
        description="Add-ons, not including figure. Written into the 3MF in list order after the figure; "
        "the figure goes first so its paint wins where parts overlap",
    )
    orientation: Literal["upright", "flat_face_up", "flat_back_down"] = "upright"
    supports: Literal["none", "tree", "normal"] = Field("tree", description="Slicer supports to use")
    notes: list[str] = Field(default_factory=list, description="Decisions made, for the manifest")
    bbox_min_mm: tuple[float, float, float]
    bbox_max_mm: tuple[float, float, float]
    warnings: list[str] = Field(default_factory=list)


# Pinned call signatures (implementations live in p2p_geometry and p2p_threemf):
#   p2p_geometry.build_product(figure_vertices, figure_faces, product: ProductType,
#       addons: list[AddOn], target_size_mm: float | None, library: FilamentLibrary,
#       settings: PrintSettings, out_dir: Path) -> GeometryResult
#   p2p_threemf.retarget_3mf(meshy_3mf: Path, geometry: GeometryResult | None, job_dir: Path,
#       library: FilamentLibrary, settings: PrintSettings, out_path: Path) -> dict(path, sha256,
#       n_faces, slots_used, palette)
