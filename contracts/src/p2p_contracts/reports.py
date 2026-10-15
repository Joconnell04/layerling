"""Reports produced by simulate_print, the slicer gate, and the paint checklist."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .manifest import FileRef, Strict


class SimParams(Strict):
    nozzle_mm: float = 0.4
    layer_mm: float = 0.2
    min_width_factor: float = Field(2.0, description="Erode regions narrower than factor x nozzle")
    flat_angle_deg: float = Field(
        30.0, description="Surfaces flatter than this count as tops (paint projects down through them)"
    )
    quantize_model: Literal["slicer", "plan"] = Field(
        "slicer", description="slicer: tops keep shape, walls snap to layers (Bambu source); plan: PLAN rule 2"
    )
    paint_penetration_layers: int = Field(5, description="Layers a painted top projects downward")
    nozzles: int = Field(2, description="X2D has 2; filaments on different nozzles swap without purge")
    min_light_layers: int = Field(5, description="Light over dark with fewer layers is flagged")
    dark_to_light_weight: float = Field(
        1.0, description="Multiplier on Bambu's color-based flush volume for dark to light swaps"
    )


class MergedRegion(Strict):
    region_id: int
    into_region_id: int
    area_mm2: float
    reason: Literal["too_thin", "island", "quantized_away"]
    kind: Literal["whole", "sliver"] = Field(
        "whole", description="whole: the region vanished; sliver: only a thin part of it was trimmed"
    )


class BleedFlag(Strict):
    region_id: int
    over_region_id: int
    light_layers: int
    area_mm2: float


class SimReport(Strict):
    params: SimParams
    merged: list[MergedRegion] = Field(default_factory=list)
    bleed_flags: list[BleedFlag] = Field(default_factory=list)
    layers: int
    swaps_total: int
    swaps_max_per_layer: int
    purge_g_est: float
    model_g_est: float
    purge_pct: float
    face_filaments: FileRef = Field(description="sim_face_filaments.npy int16 slot per face")
    images: list[FileRef] = Field(default_factory=list)


class SliceFilament(Strict):
    slot: int
    filament_id: str | None = None
    grams: float
    meters: float | None = None
    model_grams: float | None = Field(None, description="Printed in the model, no flush or tower")
    flush_grams: float | None = None
    tower_grams: float | None = None


class SliceReport(Strict):
    ok: bool
    slicer: Literal["bambu_studio", "orcaslicer", "p2p_slicer"]
    slicer_version: str
    printer_profile: str
    process_profile: str
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    print_time_s: float | None = None
    filaments: list[SliceFilament] = Field(default_factory=list)
    total_grams: float | None = None
    purge_grams: float | None = Field(None, description="Flush and prime tower")
    slots_used: int | None = None
    expected_slots: int | None = None
    output: FileRef | None = Field(None, description="Sliced .gcode.3mf, if kept")
    log: str | None = Field(None, description="Job-relative path to the slicer log")
    filament_changes: int | None = None
    nozzle_changes: int | None = None
    ams_swaps: int | None = Field(None, description="AMS unload and load pairs (Bambu filament_change_times)")
    model_time_s: float | None = Field(None, description="print_time_s minus prepare_time_s")
    prepare_time_s: float | None = Field(None, description="Start and end G-code, heating, homing")
    tower_grams: float | None = Field(None, description="Prime tower share of purge_grams")
    readback_ok: bool | None = Field(None, description="Slicer re-saved file keeps presets and paint")
    readback_slots: int | None = None
    thin_paint: list[dict] = Field(
        default_factory=list, description="[{slot, filament_id, area_mm2, min_width_mm}] under 2 x nozzle"
    )


class ChecklistItem(Strict):
    id: Literal[
        "spec_coverage", "face_parts", "markings", "islands", "creases", "slot_count",
        "sim_matches_intent", "purge_under_40pct",
    ]
    passed: bool | None = None
    by: Literal["code", "model"]
    evidence: str


class Checklist(Strict):
    items: list[ChecklistItem]
    passed_all: bool
    confidence: float = Field(ge=0, le=1)
