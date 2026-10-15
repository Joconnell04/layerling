"""Filament library: filaments/library.json (seed and offline copy) and Supabase `filaments`.

Supabase is the live source once configured. Each job snapshots the library it used into its
s08_palette attempt folder and records the hash, so results stay reproducible.

Color: CIELAB, D65 illuminant, 2 degree observer, computed from sRGB (IEC 61966-2-1).
lab_source = "nominal" until a printed swatch is photographed with a color checker.
Color distance everywhere is CIEDE2000.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .manifest import Strict

Lab = tuple[float, float, float]


class Filament(Strict):
    id: str = Field(
        description="Slug rule: lowercase, [a-z0-9-] only, <brand>-<material or line>-<color>, "
        "e.g. bambu-pla-basic-red, generic-petg-blue; unique per owner; never reused for a different spool"
    )
    brand: str
    line: str = Field(description="Basic, Matte, Silk+, ...")
    name: str = Field(description="Color name as sold")
    material: str = "PLA"
    hex: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    lab: Lab
    lab_source: Literal["nominal", "measured"] = "nominal"
    td_mm: float | None = Field(
        None, description="Thickness at which the color over black and over white are both within dE00 2 "
        "of the solid tile (measured from the swatch card wedges); bleed risk"
    )
    owned: bool = True
    spools: int = 1
    ams_slot: int | None = Field(None, description="Currently loaded AMS slot, if any")
    swatch: str | None = Field(None, description="Path under filaments/swatches/ or Storage key")
    bambu_filament_id: str | None = Field(None, description="Bambu preset id, e.g. GFA00")
    slicer_preset: str | None = Field(None, description="e.g. Bambu PLA Basic @BBL X2D")
    density_g_cm3: float = 1.24
    notes: str | None = None


class FilamentLibrary(Strict):
    schema_version: Literal["1"] = "1"
    updated_at: datetime
    illuminant: Literal["D65"] = "D65"
    filaments: list[Filament]
