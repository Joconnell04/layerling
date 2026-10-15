"""Filament mass split (model vs purge) from Bambu Studio G-code. An estimate.

Bambu's slice_info.config gives grams per filament but not how much is flush and prime tower.
We sum net extrusion (M83 relative E, retracts included) under model features (walls, infill,
surfaces, bridges, supports) outside the `; VFLUSH_*` and `; WIPE_TOWER_*` blocks, per active
tool, convert with the header's density and diameter, and call the rest of each filament's
header total purge. Tool-change and start G-code moves filament with firmware commands that are
not G1 E moves, so a full per-line purge sum does not add up; the header total is authoritative.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

_E_RE = re.compile(r"\bE(-?\d*\.?\d+)")
_XY_RE = re.compile(r"\b[XYIJ]-?\d*\.?\d")
_TOOL_RE = re.compile(r"^T(\d+)\b")
_LIST_RE = re.compile(r"^; (filament_density|filament_diameter): (.*)$")
_HDR_TIME_RE = re.compile(r"model printing time: ([^;]+); total estimated time: (.+)$")
NON_MODEL_FEATURES = {"Prime tower", "Custom", ""}


def hms_to_s(text: str) -> float:
    """Bambu's header durations ("1h 2m 3s", "25m 55s") in seconds."""
    return float(sum(int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u]
                     for n, u in re.findall(r"(\d+)\s*([dhms])", text)))


@dataclass
class GcodeStats:
    total_g: dict[int, float] = field(default_factory=dict)  # 1-based slot -> grams (header)
    model_g: dict[int, float] = field(default_factory=dict)
    purge_g: dict[int, float] = field(default_factory=dict)
    layers: int = 0
    filament_changes: int = 0  # tool changes after the first tool
    layers_with_change: int = 0
    max_changes_per_layer: int = 0
    nozzle_changes: int = 0  # changes that also switch nozzle (needs the filament map)
    tower_g: float = 0.0  # net extrusion printed as the prime tower
    # Exact buckets, Bambu's rules (docs/research/native-slicer.md section 11): extrude moves
    # (E > 0 with XY) under "; FEATURE: Prime tower", and ";VG1 E" inside "; VFLUSH_START/END"
    # (the firmware purge), both by the tool active at the time. 1-based slot -> grams.
    tower_g_slot: dict[int, float] = field(default_factory=dict)
    flush_g_slot: dict[int, float] = field(default_factory=dict)
    flushes: int = 0  # VFLUSH blocks that purge (equals result.json filament_change_times)
    nozzle_switches: int = 0  # "; NOZZLE_CHANGE_START" markers
    model_time_s: float | None = None  # header "model printing time" (total minus prepare)
    total_time_s: float | None = None  # header "total estimated time"

    @property
    def purge_total(self) -> float:
        return round(sum(self.purge_g.values()), 2)

    @property
    def model_total(self) -> float:
        return round(sum(self.model_g.values()), 2)


def parse_gcode(text: str, totals: dict[int, float] | None = None,
                filament_map: list[int] | None = None) -> GcodeStats:
    """totals: grams per slot from slice_info.config, used when the header has none (Orca).
    filament_map: nozzle per slot (slice_info filament_maps), to count nozzle switches."""
    layers = 0
    changes = 0
    layer_changes = 0
    per_layer: list[int] = []
    nozzle_changes = 0
    density: list[float] = []
    diameter: list[float] = []
    header_g: list[float] = []
    used_slots: list[int] = []
    tool = None
    in_flush = in_tower = False
    feature = ""
    relative = True
    e_model: dict[int, float] = {}
    e_tower: dict[int, float] = {}
    e_tower_xy: dict[int, float] = {}
    e_flush: dict[int, float] = {}
    flushes = nozzle_markers = 0
    flush_open = 0.0
    model_time = total_time = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line[0] == ";":
            if line.startswith("; FEATURE: "):
                feature = line[11:]
            elif line == "; CHANGE_LAYER":
                if layers:
                    per_layer.append(layer_changes)
                layers += 1
                layer_changes = 0
            elif line == "; VFLUSH_START":
                in_flush = True
                flush_open = 0.0
            elif line == "; VFLUSH_END":
                in_flush = False
                if flush_open > 0:
                    flushes += 1
            elif line.startswith(";VG1 "):
                em = _E_RE.search(line)
                if em and in_flush and tool is not None:
                    flush_open += float(em.group(1))
                    e_flush[tool] = e_flush.get(tool, 0.0) + float(em.group(1))
            elif line.startswith("; NOZZLE_CHANGE_START"):
                nozzle_markers += 1
            elif line == "; WIPE_TOWER_START":
                in_tower = True
            elif line == "; WIPE_TOWER_END":
                in_tower = False
            elif line.startswith("; total filament weight [g] :"):
                header_g = [float(x) for x in line.split(":", 1)[1].split(",")]
            elif line.startswith("; filament: "):
                used_slots = [int(x) for x in line[12:].split(",") if x.strip().isdigit()]
            elif line.startswith("; model printing time:"):
                m = _HDR_TIME_RE.search(line)
                if m:
                    model_time, total_time = hms_to_s(m.group(1)), hms_to_s(m.group(2))
            else:
                m = _LIST_RE.match(line)
                if m:
                    vals = [float(x) for x in m.group(2).split(",")]
                    if m.group(1) == "filament_density":
                        density = vals
                    else:
                        diameter = vals
            continue
        code = line.split(";", 1)[0].strip()
        if not code:
            continue
        m = _TOOL_RE.match(code)
        if m:
            t = int(m.group(1))
            if t < 1000:  # T65279 / T65535 are AMS retract commands, not tools
                if tool is not None and t != tool:
                    changes += 1
                    layer_changes += 1
                    if (filament_map and t < len(filament_map) and tool < len(filament_map)
                            and filament_map[t] != filament_map[tool]):
                        nozzle_changes += 1
                tool = t
            continue
        if code.startswith("M83"):
            relative = True
            continue
        if code.startswith("M82"):
            relative = False
            continue
        if tool is None or not relative or code[:2] not in ("G0", "G1", "G2", "G3"):
            continue
        em = _E_RE.search(code)
        if not em:
            continue
        if feature == "Prime tower" and not in_flush:
            e_tower[tool] = e_tower.get(tool, 0.0) + float(em.group(1))
            if float(em.group(1)) > 0 and _XY_RE.search(code[2:]):
                e_tower_xy[tool] = e_tower_xy.get(tool, 0.0) + float(em.group(1))
        elif not (in_flush or in_tower or feature in NON_MODEL_FEATURES):
            e_model[tool] = e_model.get(tool, 0.0) + float(em.group(1))

    def grams(slot: int, length_mm: float) -> float:
        i = slot - 1
        d = diameter[i] if i < len(diameter) else 1.75
        rho = density[i] if i < len(density) else 1.24
        return length_mm * math.pi * (d / 2) ** 2 * rho / 1000.0

    per_layer.append(layer_changes)
    stats = GcodeStats(layers=layers, filament_changes=changes,
                       layers_with_change=sum(1 for c in per_layer if c),
                       max_changes_per_layer=max(per_layer, default=0),
                       nozzle_changes=nozzle_changes, flushes=flushes,
                       nozzle_switches=nozzle_markers, model_time_s=model_time,
                       total_time_s=total_time)
    if header_g and used_slots and len(header_g) == len(used_slots):
        stats.total_g = dict(zip(used_slots, header_g, strict=True))
    elif totals:
        stats.total_g = dict(totals)
    for t, length in sorted(e_model.items()):
        stats.model_g[t + 1] = round(max(0.0, grams(t + 1, length)), 3)
    stats.tower_g = round(sum(max(0.0, grams(t + 1, v)) for t, v in e_tower.items()), 3)
    stats.tower_g_slot = {t + 1: round(grams(t + 1, v), 4) for t, v in sorted(e_tower_xy.items())}
    stats.flush_g_slot = {t + 1: round(grams(t + 1, v), 4) for t, v in sorted(e_flush.items())
                          if v > 0}
    for slot, total in stats.total_g.items():
        stats.purge_g[slot] = round(max(0.0, total - stats.model_g.get(slot, 0.0)), 3)
    return stats


# Firmware commands in Bambu's own X2D end G-code ("pull back filament to AMS"): T65279 and
# T65535 unload the left and right nozzles. Bambu's G-code processor logs them as
# "Invalid T command" even in projects Bambu Studio saved itself.
FIRMWARE_TOOL_COMMANDS = {65279, 65535}


def invalid_tool_changes(text: str, n_filaments: int) -> list[tuple[int, int, str]]:
    """T commands that do not select one of the project's filaments, as (line, tool, where).

    where is "end_gcode" for the firmware unload commands inside the machine end G-code
    (expected) and "print" for anything else (a bug in what we wrote). The end G-code starts at
    Bambu's "; MACHINE_END_GCODE_START" marker; OrcaSlicer writes no marker, so without it the
    end is whatever follows the last layer change."""
    lines = text.splitlines()
    end_at = next((i for i, ln in enumerate(lines) if ln.strip() == "; MACHINE_END_GCODE_START"),
                  None)
    if end_at is None:
        end_at = max((i for i, ln in enumerate(lines) if ln.strip() == "; CHANGE_LAYER"),
                     default=len(lines))
    out = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or line[0] == ";":
            continue
        m = _TOOL_RE.match(line.split(";", 1)[0].strip())
        if not m:
            continue
        t = int(m.group(1))
        if t < n_filaments:
            continue
        where = "end_gcode" if i > end_at and t in FIRMWARE_TOOL_COMMANDS else "print"
        out.append((i + 1, t, where))
    return out
