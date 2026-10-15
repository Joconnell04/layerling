"""Region map, paint state, and the fixed camera set.

Geometry frame (every mesh the pipeline writes after import):
- Units mm. Z up. The model's front faces -Y. Base sits on z = 0, centered on the XY bbox center.
- Meshy GLB (glTF: Y up, front +Z) converts as (x, y, z)_gltf -> (x, -z, y)_ours, then scaled.

Region map files, both in the same stage attempt folder:
- region_map.json   RegionMap (this model)
- face_regions.npy  int32 array, shape (n_faces,), region id per triangle, 0 = unlabeled
Triangle indices refer to the exact face order of `mesh.path`. Any mesh edit (repair, add-on,
remesh) makes a new mesh file and requires re-projecting labels; never reuse a map across meshes.

Paint state (paint_state.json) is separate from topology: it maps region ids to filament ids.
Multi-part prints add face_parts.npy (int16 per face of the combined mesh: 0 = figure, k = the k-th
GeometryResult part) and PaintState.part_filaments (part name -> filament id).
The 3MF slot of a filament is its 1-based index in `palette`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .manifest import FileRef, Strict

Vec3 = tuple[float, float, float]


class MeshRef(Strict):
    path: str = Field(description="Job-relative path to the mesh (PLY or GLB) the ids index into")
    sha256: str
    n_faces: int
    n_vertices: int
    units: Literal["mm"] = "mm"
    up_axis: Literal["z"] = "z"


RegionSource = Literal[
    "sam3", "meshy_split", "meshy_color", "texture_cluster", "manual", "split", "merge", "pixel",
    "fill",
]


class Region(Strict):
    id: int = Field(ge=1, description="Stable; never reused after a region is deleted")
    label: str = Field(
        description="subject.part[_qualifier], e.g. s1.eye_left, s2.marking_back. left/right are the "
        "SUBJECT's own sides (anatomical): with the front facing -Y, the subject's left is +X."
    )
    subject_id: str | None = None
    part: str = Field(description="One of payloads.PART_VOCAB")
    source: RegionSource
    confidence: float | None = Field(None, ge=0, le=1)
    n_faces: int
    area_mm2: float
    centroid_mm: Vec3
    bbox_min_mm: Vec3
    bbox_max_mm: Vec3
    min_width_mm: float | None = Field(None, description="Thinnest local width; printability")
    views_seen: int = 0
    color_lab: Vec3 | None = Field(None, description="Mean CIELAB of the texture over this region")
    parent_ids: list[int] = Field(default_factory=list)


class RegionMap(Strict):
    schema_version: Literal["1"] = "1"
    mesh: MeshRef
    face_regions: FileRef = Field(description="face_regions.npy, int32 (n_faces,), 0 = unlabeled")
    regions: list[Region]
    next_region_id: int


class PaintState(Strict):
    schema_version: Literal["1"] = "1"
    region_map: FileRef = Field(description="The region_map.json this state applies to")
    palette: list[str] = Field(description="Filament ids; slot = index + 1; len <= AMS slots")
    assignment: dict[str, str] = Field(description="str(region_id) -> filament_id (in palette)")
    default_filament: str = Field(description="Filament for unlabeled faces (usually body color)")
    part_filaments: dict[str, str] = Field(
        default_factory=dict, description="Add-on part name -> filament id (parts are unpainted)"
    )
    iteration: int = 0
    checkpoint_id: str | None = None


# ---------------------------------------------------------------- cameras

ViewName = Literal["front", "back", "left", "right", "top", "front_3q"]


class CameraSpec(Strict):
    name: str
    azimuth_deg: float = Field(description="Around +Z from +X; camera at -Y (front) is -90")
    elevation_deg: float
    fov_deg: float = 30.0
    fit_margin: float = Field(1.15, description="Bounding-sphere radius multiplier for framing")


# Camera names use the VIEWER's sides when facing the model's front (camera `left` sits at -X).
# Region labels use the SUBJECT's own sides (a subject's left eye is at +X). Do not mix them.
VIEWS: dict[str, CameraSpec] = {
    "front": CameraSpec(name="front", azimuth_deg=-90, elevation_deg=10),
    "back": CameraSpec(name="back", azimuth_deg=90, elevation_deg=10),
    "left": CameraSpec(name="left", azimuth_deg=180, elevation_deg=10),
    "right": CameraSpec(name="right", azimuth_deg=0, elevation_deg=10),
    "top": CameraSpec(name="top", azimuth_deg=-90, elevation_deg=89.9),
    "front_3q": CameraSpec(name="front_3q", azimuth_deg=-55, elevation_deg=20),
}
RENDER_SIZE_PX = 1024

# Proof set: every stage that shows the product shows it from these eight sides (the six
# contract views plus the front-left and back-left 3/4 views). Display pose: figure up (+Z),
# face toward the front camera (-Y), flat products turned back upright to face it.
PROOF_VIEWS: dict[str, CameraSpec] = {
    "front": VIEWS["front"],
    "front_3q": VIEWS["front_3q"],
    "left_3q": CameraSpec(name="left_3q", azimuth_deg=-125, elevation_deg=20),
    "left": VIEWS["left"],
    "right": VIEWS["right"],
    "back": VIEWS["back"],
    "back_3q": CameraSpec(name="back_3q", azimuth_deg=125, elevation_deg=20),
    "top": VIEWS["top"],
}
