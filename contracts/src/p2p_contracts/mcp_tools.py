"""Paint MCP server tool signatures (server: mcp_paint, name "paint").

s09 output layout (jobs/<id>/s09_paint/a<N>/): region_map.json, face_regions.npy, paint_state.json,
checkpoints/<id>/ (paint_state.json + sim_face_filaments.npy + renders), sim_report.json,
checklist.json, renders/ (before_sim.png, after_sim.png, per-iteration sheets), final.3mf,
slice_report.json, manifest.json (data: iterations, confidence, checkpoint ids).

The server is stateful: one open job at a time, holding the mesh, region map, paint state, and a
checkpoint stack under jobs/<job_id>/s09_paint/a<N>/. Launch with `--job <id>` or call open_job.
Every mutating tool returns the new state summary and writes nothing outside the current attempt.
Image-returning tools return MCP ImageContent plus RenderOut paths.

Rules the tools enforce, not the agent:
- Edits address regions by id. Pixel selection (select_by_pixel) is the last resort.
- paint_region only accepts filaments in the palette unless add_to_palette is true and a slot is free.
- Distances are mm on the mesh surface (geodesic), not screen pixels.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .manifest import Strict
from .payloads import PaintOp, PaintPreview
from .regions import ViewName
from .reports import Checklist, SimParams, SimReport, SliceReport

RenderKind = Literal["flat", "shaded", "overlay", "crops", "sim", "texture"]

# ---------------------------------------------------------------- inputs


class OpenJobIn(Strict):
    job_id: str
    attempt: int | None = Field(None, description="Resume this s09 attempt; default new attempt")
    base: str | None = Field(
        None, description="Start the new attempt from a finished paint instead of fresh SAM 3 "
        "labels: 'current' (the paint behind the job's result) or an attempt folder such as "
        "s09_paint/a2. Then edit with apply_paint_ops and read preview_print")


class ListRegionsIn(Strict):
    subject_id: str | None = None
    part: str | None = None
    unassigned_only: bool = False


class LabelRegionsIn(Strict):
    prompts: list[str] | None = Field(None, description="SAM 3 text prompts; default from color spec")
    views: list[ViewName] | None = None
    close_ups: bool = Field(True, description="Also render close-ups around face parts")
    min_vote: float = Field(0.5, ge=0, le=1, description="Multi-view vote share to keep a label")
    replace: bool = Field(False, description="Replace existing regions instead of refining them")
    score_threshold: float = Field(0.5, ge=0, le=1, description="Drop SAM 3 masks below this score")
    markings: bool = Field(True, description="Also split body and head by texture color clusters")
    min_component_frac: float = Field(
        0.02, ge=0, le=1, description="Drop mask components smaller than this share of the mask"
    )


class RenderViewsIn(Strict):
    kinds: list[RenderKind] = Field(default_factory=lambda: ["flat", "overlay"])
    views: list[ViewName] | None = None
    region_ids: list[int] | None = Field(None, description="Highlight these; crops target them")
    size_px: int = 1024


class PaintRegionIn(Strict):
    region_ids: list[int] = Field(min_length=1)
    filament_id: str
    add_to_palette: bool = False


class MergeRegionsIn(Strict):
    region_ids: list[int] = Field(min_length=2)
    label: str | None = None


class SplitRegionIn(Strict):
    region_id: int
    method: Literal["connected_components", "plane", "geodesic_seed", "prompt"]
    plane_point_mm: tuple[float, float, float] | None = None
    plane_normal: tuple[float, float, float] | None = None
    seed_face_ids: list[int] | None = None
    prompt: str | None = None
    labels: list[str] | None = Field(None, description="Labels for the new parts, in order")


class BoundaryEditIn(Strict):
    region_id: int
    mm: float = Field(gt=0, le=10)


class SnapToCreasesIn(Strict):
    region_ids: list[int] | None = Field(None, description="Default: all regions")
    max_shift_mm: float = 1.0
    crease_angle_deg: float = 30.0


class DespeckleIn(Strict):
    min_area_mm2: float = Field(gt=0)
    region_ids: list[int] | None = None


class SelectByPixelIn(Strict):
    view: ViewName
    x: int
    y: int
    radius_px: int = 0
    action: Literal["info", "new_region", "add_to_region"] = "info"
    region_id: int | None = Field(None, description="Target for add_to_region")
    label: str | None = None


class SuggestPaletteIn(Strict):
    k: int = Field(ge=1, le=16)
    fixed: list[str] = Field(default_factory=list, description="Filament ids that must be included")
    owned_only: bool = True
    apply: bool = Field(False, description="Replace palette and re-assign regions to nearest")


class SimulatePrintIn(Strict):
    params: SimParams | None = None
    render: bool = True


class CheckpointIn(Strict):
    note: str


class UndoIn(Strict):
    checkpoint_id: str | None = Field(None, description="Default: previous checkpoint")


class Write3mfIn(Strict):
    name: str = "final"
    use_sim: bool = Field(False, description="Write simulated per-face filaments instead of ideal")


class SliceCheckIn(Strict):
    path: str | None = Field(None, description="Job-relative 3MF; default last write_3mf output")
    keep_gcode: bool = False


class PaintOpsIn(Strict):
    """The studio paint editor's operations (payloads.PaintOp), in order."""

    ops: list[PaintOp] = Field(min_length=1, max_length=500)
    source: Literal["owned", "any"] = Field("owned", description="any: catalog colors allowed")


class PreviewPrintIn(Strict):
    toolpaths: bool = Field(True, description="Write per-layer toolpaths (PaintPreview format)")
    proof: bool = Field(True, description="Render paint and simulated print from 8 views")


# ---------------------------------------------------------------- outputs


class RegionSummary(Strict):
    id: int
    label: str
    part: str
    subject_id: str | None
    filament_id: str | None
    area_mm2: float
    min_width_mm: float | None
    confidence: float | None


class StateOut(Strict):
    job_id: str
    attempt: int
    iteration: int
    checkpoint_id: str | None
    palette: list[str]
    n_regions: int
    unlabeled_area_pct: float
    changed_region_ids: list[int] = Field(default_factory=list)
    message: str | None = None


class RegionsOut(Strict):
    regions: list[RegionSummary]


class RenderOut(Strict):
    images: list[dict] = Field(description="[{kind, view, path, width, height, region_id?}]")


class PixelOut(Strict):
    face_id: int | None
    region_id: int | None
    point_mm: tuple[float, float, float] | None
    new_region_id: int | None = None


class PaletteOut(Strict):
    palette: list[str]
    delta_e: dict[str, float] = Field(description="str(region_id) -> CIEDE2000 to nearest")
    swap_suggestions: list[dict] = Field(default_factory=list)


class Write3mfOut(Strict):
    path: str
    sha256: str
    n_faces: int
    palette: list[str]
    slots_used: int | None = None


# ---------------------------------------------------------------- registry

TOOLS: dict[str, tuple[type[Strict] | None, type[Strict]]] = {
    "open_job": (OpenJobIn, StateOut),
    "get_state": (None, StateOut),
    "list_regions": (ListRegionsIn, RegionsOut),
    "label_regions": (LabelRegionsIn, RegionsOut),
    "render_views": (RenderViewsIn, RenderOut),
    "paint_region": (PaintRegionIn, StateOut),
    "merge_regions": (MergeRegionsIn, StateOut),
    "split_region": (SplitRegionIn, StateOut),
    "grow": (BoundaryEditIn, StateOut),
    "shrink": (BoundaryEditIn, StateOut),
    "smooth_boundary": (BoundaryEditIn, StateOut),
    "snap_to_creases": (SnapToCreasesIn, StateOut),
    "despeckle": (DespeckleIn, StateOut),
    "select_by_pixel": (SelectByPixelIn, PixelOut),
    "suggest_palette": (SuggestPaletteIn, PaletteOut),
    "simulate_print": (SimulatePrintIn, SimReport),
    "run_checklist": (None, Checklist),
    "checkpoint": (CheckpointIn, StateOut),
    "undo": (UndoIn, StateOut),
    "write_3mf": (Write3mfIn, Write3mfOut),
    "slice_check": (SliceCheckIn, SliceReport),
    "apply_paint_ops": (PaintOpsIn, StateOut),
    "preview_print": (PreviewPrintIn, PaintPreview),
}
