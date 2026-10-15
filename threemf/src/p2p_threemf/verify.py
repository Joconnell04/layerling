"""Read-back check: what Bambu Studio understood from our file.

Bambu's CLI re-saves the project inside the sliced .gcode.3mf (or a plain --export-3mf). Reading
that copy with our independent reader shows what Bambu parsed: per-triangle paint, the object's
default filament, the preset names, colours and filament ids, and whether Bambu considers any
preset modified (different_settings_to_system). That is the "opens ready to slice" evidence we
can collect without driving the GUI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .reader import read_3mf_full

PRESET_KEYS = ("printer_settings_id", "print_settings_id", "filament_settings_id",
               "filament_colour", "filament_ids", "filament_type")


@dataclass
class Readback:
    ok: bool
    faces_in: int = 0
    faces_bambu: int = 0
    faces_same_slot: int = 0
    slot_counts_in: dict[int, int] = field(default_factory=dict)
    slot_counts_bambu: dict[int, int] = field(default_factory=dict)
    presets_match: bool = False
    modified_settings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _counts(slots: np.ndarray) -> dict[int, int]:
    u, c = np.unique(slots, return_counts=True)
    return {int(a): int(b) for a, b in zip(u, c, strict=True)}


def readback_check(original: str | Path, bambu_copy: str | Path) -> Readback:
    a = read_3mf_full(original)
    b = read_3mf_full(bambu_copy)
    rb = Readback(ok=True, faces_in=len(a.faces), faces_bambu=len(b.faces),
                  slot_counts_in=_counts(a.face_slots), slot_counts_bambu=_counts(b.face_slots))

    if len(a.faces) == len(b.faces) and np.array_equal(a.faces, b.faces):
        rb.faces_same_slot = int((a.face_slots == b.face_slots).sum())
        if rb.faces_same_slot != len(a.faces):
            rb.ok = False
            rb.errors.append(f"Bambu read {len(a.faces) - rb.faces_same_slot} of {len(a.faces)} "
                             "triangles with a different filament")
    elif rb.slot_counts_in != rb.slot_counts_bambu:
        rb.ok = False
        rb.errors.append(f"Bambu changed the mesh and the per-slot face counts differ: "
                         f"{rb.slot_counts_in} vs {rb.slot_counts_bambu}")
    else:
        rb.warnings.append("Bambu changed the triangle order; per-slot face counts match")

    ca, cb = a.project_config or {}, b.project_config or {}
    mismatched = [k for k in PRESET_KEYS if k in ca and ca.get(k) != cb.get(k)]
    rb.presets_match = not mismatched
    if mismatched:
        rb.ok = False
        rb.errors.append("Bambu changed project settings: " + ", ".join(mismatched))
    for i, entry in enumerate(cb.get("different_settings_to_system") or []):
        if entry:
            n_fil = len(cb.get("filament_settings_id") or [])
            where = ("process" if i == 0 else "printer" if i == n_fil + 1 else f"filament {i}")
            rb.modified_settings.append(f"{where}: {entry}")
    if rb.modified_settings:
        rb.warnings.append("presets marked modified: " + "; ".join(rb.modified_settings))
    if a.object_extruders != b.object_extruders:
        rb.ok = False
        rb.errors.append(f"object default filament changed: {a.object_extruders} vs "
                         f"{b.object_extruders}")
    return rb
