"""Ground truth from Bambu Studio 2.08 for calibrating the native slicer (p2p_slicer).

    uv run python slicer/calibration/build_truth.py                  # slice what is missing, extract all
    uv run python slicer/calibration/build_truth.py --only keychain_4c_020 --force
    uv run python slicer/calibration/build_truth.py --extract-only   # re-read kept outputs only
    uv run python slicer/calibration/build_truth.py --regen-inputs   # rewrite the generated inputs

Reads cases.json. For each case it:
1. makes sure the input 3MF exists: generated cases are written with p2p_threemf (the writer is
   byte-deterministic, so the sha256 in cases.json should match); copied cases live in inputs/;
   large job files are read from the main checkout (P2P_MAIN_CHECKOUT, default
   the repo root) and never modified.
2. slices it once, headless, through p2p_threemf.slice_check with a Bambu backend that keeps its
   work folder: _truth/<case>/ gets sliced.gcode.3mf, plate_1.gcode, result.json, slicer.log,
   slice_report.json, gcode_config.json and run.json. _truth/ is gitignored (G-code is large).
3. extracts totals, per layer and per feature numbers into truth.json.

Slices run one at a time so wall-clock numbers are comparable. Every Bambu CLI run is a child
process with a timeout; this script prints a line per case so a harness sees output.
SLICER_BIN overrides the Bambu Studio binary path.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
MAIN_CHECKOUT = Path(os.environ.get("P2P_MAIN_CHECKOUT", str(REPO)))
CASES_PATH = HERE / "cases.json"
TRUTH_PATH = HERE / "truth.json"
TRUTH_DIR = HERE / "_truth"
INPUTS_DIR = HERE / "inputs"
SLICE_TIMEOUT_S = 1200

# Config keys copied into truth.json per case (the whole block goes to _truth/<case>/).
CONFIG_KEYS = (
    "printer_settings_id", "print_settings_id", "filament_settings_id", "layer_height",
    "initial_layer_print_height", "nozzle_diameter", "filament_diameter", "filament_density",
    "filament_type", "filament_colour", "filament_map", "filament_map_mode",
    "wall_loops", "top_shell_layers", "bottom_shell_layers", "top_shell_thickness",
    "bottom_shell_thickness", "sparse_infill_density", "sparse_infill_pattern",
    "top_surface_pattern", "bottom_surface_pattern", "internal_solid_infill_pattern",
    "line_width", "outer_wall_line_width", "inner_wall_line_width", "sparse_infill_line_width",
    "internal_solid_infill_line_width", "top_surface_line_width", "initial_layer_line_width",
    "support_line_width", "prime_tower_width", "prime_tower_brim_width", "enable_prime_tower",
    "prime_volume_mode", "prime_tower_rib_wall", "prime_tower_infill_gap", "wipe_tower_x",
    "wipe_tower_y", "flush_into_infill", "flush_into_objects", "flush_into_support",
    "flush_multiplier", "flush_volumes_matrix", "enable_support", "support_type", "support_style",
    "support_threshold_angle", "support_on_build_plate_only", "outer_wall_speed",
    "inner_wall_speed", "sparse_infill_speed", "internal_solid_infill_speed",
    "top_surface_speed", "gap_infill_speed", "bridge_speed", "support_speed",
    "support_interface_speed", "initial_layer_speed", "initial_layer_infill_speed",
    "travel_speed", "default_acceleration", "outer_wall_acceleration",
    "inner_wall_acceleration", "top_surface_acceleration", "sparse_infill_acceleration",
    "initial_layer_acceleration", "travel_acceleration", "filament_max_volumetric_speed",
    "slow_down_layer_time", "slow_down_min_speed", "retraction_length", "retraction_speed",
    "deretraction_speed", "z_hop", "z_hop_types", "retract_when_changing_layer", "wipe",
    "wipe_distance", "seam_position", "ironing_type", "brim_type", "brim_width", "skirt_loops",
    "enable_arc_fitting", "adaptive_layer_height", "independent_support_layer_height",
    "reduce_infill_retraction", "only_one_wall_top", "detect_thin_wall", "enable_overhang_speed",
    "machine_max_acceleration_x", "machine_max_acceleration_y", "machine_max_acceleration_z",
    "machine_max_acceleration_e", "machine_max_speed_x", "machine_max_speed_y",
    "machine_max_speed_z", "machine_max_speed_e", "machine_max_jerk_x", "machine_max_jerk_y",
    "machine_load_filament_time", "machine_unload_filament_time", "machine_switch_extruder_time",
    "machine_prepare_compensation_time", "filament_load_time", "filament_unload_time",
    "timelapse_type", "print_sequence", "curr_bed_type",
)
# Project keys summarised in cases.json "settings".
SETTINGS_KEYS = (
    "printer_settings_id", "print_settings_id", "layer_height", "initial_layer_print_height",
    "filament_settings_id", "filament_colour", "enable_support", "support_type",
    "prime_volume_mode", "flush_into_infill", "enable_prime_tower", "wall_loops",
    "sparse_infill_density", "filament_map_mode", "filament_map",
)


# ----------------------------------------------------------------------------- small helpers


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_input(rel: str) -> Path:
    p = Path(rel)
    if p.is_absolute():
        return p
    for root in (REPO, MAIN_CHECKOUT):
        if (root / p).exists():
            return root / p
    return REPO / p


def project_config(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as z:
            return json.loads(z.read("Metadata/project_settings.config"))
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return {}


def settings_summary(cfg: dict) -> dict:
    out = {k: cfg[k] for k in SETTINGS_KEYS if k in cfg}
    for k in ("filament_settings_id", "filament_colour", "filament_map"):
        if isinstance(out.get(k), list):
            out[k] = ",".join(out[k])
    return out


def print_settings_for(path: Path):
    from p2p_contracts import PrintSettings

    cfg = project_config(path)
    nozzle = float((cfg.get("nozzle_diameter") or ["0.4"])[0])
    layer = float(cfg.get("layer_height") or 0.2)
    n = len(cfg.get("filament_colour") or [])
    return PrintSettings(printer=cfg.get("printer_model") or "Bambu Lab X2D", nozzle_mm=nozzle,
                         layer_mm=layer, ams_slots=min(16, max(4, n)), slicer="bambu_studio")


def log(msg: str) -> None:
    print(msg, flush=True)


# ----------------------------------------------------------------------------- inputs


def generate_input(case: dict, out: Path) -> dict:
    """Write a generated case with p2p_threemf (products, lab variants, single colour)."""
    from p2p_contracts import PrintSettings
    from p2p_threemf.demo import jackson_palette
    from p2p_threemf.lab import Variant, build
    from p2p_threemf.products import PRODUCTS
    from p2p_threemf.writer import write_3mf

    g = case["generator"]
    settings = PrintSettings(layer_mm=g.get("layer_mm", 0.2),
                             process_overrides=dict(g.get("process_overrides") or {}))
    out.parent.mkdir(parents=True, exist_ok=True)
    if g.get("single_color"):
        p = PRODUCTS[g["product"]]()
        slot = int(g["single_color"])
        slots = [slot] * len(p.faces)
        for part in p.parts:
            part.slot = slot if part.slot is not None else None
            if part.face_slots is not None:
                part.face_slots = [slot] * len(part.faces)
        import numpy as np

        res = write_3mf(p.vertices, p.faces, np.asarray(slots), jackson_palette(), settings, out,
                        object_name=f"{p.name}_1c", parts=p.parts)
        return dict(res, expected_slots=1)
    v = Variant("truth", case["id"], layer_mm=settings.layer_mm,
                adaptive=bool(g.get("adaptive")), writer_defaults=True)
    return build(g["product"], v, out, settings)


def ensure_input(case: dict, regen: bool) -> Path:
    path = resolve_input(case["input"])
    if case.get("generator") and (regen or not path.exists()):
        log(f"  writing generated input {path.relative_to(REPO)}")
        generate_input(case, path)
    if not path.exists():
        raise FileNotFoundError(f"{case['id']}: input not found: {case['input']}")
    want = case.get("input_sha256")
    if want and sha256(path) != want:
        log(f"  WARNING {case['id']}: input sha256 differs from cases.json")
    return path


# ----------------------------------------------------------------------------- slicing


def bambu_bin() -> Path:
    from p2p_threemf.slicer import BAMBU_BIN

    return Path(os.environ.get("SLICER_BIN") or BAMBU_BIN)


def make_backend(keep: Path):
    from p2p_threemf.slicer import BambuStudioBackend

    class KeepDirBackend(BambuStudioBackend):
        """Slices in a fixed folder so result.json, plate_1.gcode and the log survive."""

        def __init__(self) -> None:
            super().__init__(bambu_bin())
            self.raw = None

        def slice(self, model, workdir, timeout_s):  # workdir is slice_check's tmp; unused
            self.raw = super().slice(model, keep, timeout_s)
            return self.raw

    return KeepDirBackend()


def slice_case(case: dict, model: Path, force: bool) -> Path:
    from p2p_threemf.slicer import slice_check

    keep = TRUTH_DIR / case["id"]
    run_json = keep / "run.json"
    in_sha = sha256(model)
    if run_json.exists() and not force:
        prev = json.loads(run_json.read_text())
        if prev.get("input_sha256") == in_sha and (keep / "result.json").exists():
            log(f"  kept slice for {case['id']} ({prev.get('slicer_wall_s')} s), skipping")
            return keep
    if keep.exists():
        shutil.rmtree(keep)
    keep.mkdir(parents=True)
    backend = make_backend(keep)
    settings = print_settings_for(model)
    fids = case.get("filament_ids")
    load0 = os.getloadavg()
    t0 = time.monotonic()
    rep = slice_check(model, settings, case.get("expected_slots"), filament_ids=fids,
                      keep_gcode=False, timeout_s=SLICE_TIMEOUT_S, out_dir=keep,
                      backend=backend, verify_readback=True)
    wall = round(time.monotonic() - t0, 2)
    raw = backend.raw
    (keep / "slice_report.json").write_text(rep.model_dump_json(indent=1) + "\n")
    run = {
        "case": case["id"], "input": case["input"], "input_sha256": in_sha,
        "input_bytes": model.stat().st_size, "bambu_bin": str(bambu_bin()),
        "bambu_version": backend.version(),
        "slicer_wall_s": raw.duration_s if raw else None,
        "slice_check_wall_s": wall, "returncode": raw.returncode if raw else None,
        "timed_out": raw.timed_out if raw else None, "loadavg_before": [round(x, 2) for x in load0],
        "ok": rep.ok, "errors": rep.errors[:5],
    }
    run_json.write_text(json.dumps(run, indent=1) + "\n")
    return keep


def retime_case(case: dict, model: Path, repeats: int) -> dict:
    """Slice again `repeats` times in a scratch folder, one at a time, for wall-clock numbers
    less affected by whatever else the machine is doing. Keeps the minimum."""
    import tempfile

    from p2p_threemf.slicer import BambuStudioBackend

    backend = BambuStudioBackend(bambu_bin())
    runs, loads = [], []
    for _ in range(repeats):
        tmp = Path(tempfile.mkdtemp(prefix="p2p_truth_time_"))
        try:
            loads.append(round(os.getloadavg()[0], 2))
            raw = backend.slice(model, tmp, SLICE_TIMEOUT_S)
            if raw.ok:
                runs.append(raw.duration_s)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    out = {"runs_s": runs, "min_s": min(runs) if runs else None, "loadavg": loads}
    (TRUTH_DIR / case["id"] / "timing.json").write_text(json.dumps(out) + "\n")
    return out


# ----------------------------------------------------------------------------- G-code parsing

_PARAM_RE = re.compile(r"([A-Z])([-+]?\d*\.?\d+)")
_HDR_TIME_RE = re.compile(r"model printing time: (.*?); total estimated time: (.*)$")


def hms_to_s(text: str) -> float:
    s = 0.0
    for num, unit in re.findall(r"(\d+)([dhms])", text):
        s += int(num) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return s


def _arc_len(x0, y0, x1, y1, i, j, cw: bool, turns: float) -> float:
    cx, cy = x0 + i, y0 + j
    r = math.hypot(i, j)
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    sweep = (a0 - a1) if cw else (a1 - a0)
    sweep %= 2 * math.pi
    if sweep < 1e-9:
        sweep = 2 * math.pi
        turns = max(turns - 1, 0)
    return r * (sweep + 2 * math.pi * turns)


class Layer:
    __slots__ = (
        "changes",
        "e",
        "f",
        "flush_mm",
        "h",
        "idx",
        "line",
        "m73",
        "retracts",
        "tools",
        "travel_mm",
        "w0",
        "wipes",
        "z",
    )

    def __init__(self, idx: int, w0: float, m73: tuple, line: int) -> None:
        self.idx = idx
        self.z = None
        self.h = None
        self.w0 = w0
        self.m73 = m73
        self.tools: set[int] = set()
        self.changes = 0
        self.e: dict[int, float] = {}
        self.f: dict[str, float] = {}
        self.travel_mm = 0.0
        self.retracts = 0
        self.wipes = 0
        self.flush_mm = 0.0
        self.line = line


def parse_gcode(path: Path) -> dict:
    """One pass over Bambu Studio G-code. Extrusion is M83 relative E (M82 handled too)."""
    header: dict = {}
    config: dict[str, str] = {}
    in_header = in_config = False
    section = "start"  # start | print | end
    feature = ""
    tool: int | None = None
    x = y = z = 0.0
    e_abs_last = 0.0
    e_relative = True
    xy_relative = False
    f_mm_min = 1200.0
    in_flush = in_wipe = False
    weight = 0.0  # naive move time (s), used only to interpolate between M73 anchors
    m73_p = m73_r = None
    anchors: list[tuple[float, float, int, int]] = []  # (weight, elapsed lower bound, P, R)
    layers: list[Layer] = []
    cur: Layer | None = None
    feat: dict[str, dict] = {}
    by_tool_e: dict[int, float] = {}
    flush_by_tool: dict[int, float] = {}
    vderetract_mm = 0.0
    changes = 0
    first_tool = None
    tool_seq: list[int] = []
    nozzle_change_markers = 0
    vflush_blocks = 0
    vflush_nonzero = 0
    flush_open_mm = 0.0
    wipe_blocks = 0
    mv = {"travel_mm": 0.0, "travel_moves": 0, "z_hops": 0, "retracts": 0, "retract_mm": 0.0,
          "deretracts": 0, "deretract_mm": 0.0, "wipe_moves": 0, "wipe_path_mm": 0.0,
          "wipe_retract_mm": 0.0, "arc_moves": 0}
    start_end_e = {"start": 0.0, "end": 0.0}
    end_line = None
    end_weight = 0.0

    def fbucket(name: str) -> dict:
        b = feat.get(name)
        if b is None:
            b = feat[name] = {"e_mm": 0.0, "path_mm": 0.0, "moves": 0, "by_tool_e": {}}
        return b

    with path.open("r", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            c0 = line[0]
            if c0 == ";":
                if line.startswith("; FEATURE: "):
                    feature = line[11:]
                elif line == "; CHANGE_LAYER":
                    section = "print"
                    cur = Layer(len(layers), weight, (m73_p, m73_r), lineno)
                    layers.append(cur)
                    if tool is not None:
                        cur.tools.add(tool)
                elif line.startswith("; Z_HEIGHT: "):
                    if cur is not None and cur.z is None:
                        cur.z = float(line[12:])
                elif line.startswith("; LAYER_HEIGHT: "):
                    if cur is not None and cur.h is None:
                        cur.h = float(line[16:])
                elif line == "; VFLUSH_START":
                    in_flush = True
                    vflush_blocks += 1
                elif line == "; VFLUSH_END":
                    in_flush = False
                    if flush_open_mm > 0:
                        vflush_nonzero += 1
                    flush_open_mm = 0.0
                elif line.startswith(";VG1 "):
                    m = re.search(r"\bE(-?\d*\.?\d+)", line)
                    if m:
                        val = float(m.group(1))
                        if in_flush and tool is not None:
                            flush_open_mm += val
                            flush_by_tool[tool] = flush_by_tool.get(tool, 0.0) + val
                            if cur is not None:
                                cur.flush_mm += val
                        else:
                            vderetract_mm += val
                elif line == "; WIPE_START":
                    in_wipe = True
                    wipe_blocks += 1
                    if cur is not None:
                        cur.wipes += 1
                elif line == "; WIPE_END":
                    in_wipe = False
                elif line == "; NOZZLE_CHANGE_START":
                    nozzle_change_markers += 1
                elif line == "; MACHINE_END_GCODE_START":
                    section = "end"
                    end_line = lineno
                    end_weight = weight
                elif line == "; HEADER_BLOCK_START":
                    in_header = True
                elif line == "; HEADER_BLOCK_END":
                    in_header = False
                elif line == "; CONFIG_BLOCK_START":
                    in_config = True
                elif line == "; CONFIG_BLOCK_END":
                    in_config = False
                elif in_config:
                    k, sep, v = line[2:].partition(" = ")
                    if sep:
                        config[k.strip()] = v
                elif in_header:
                    body = line[2:]
                    m = _HDR_TIME_RE.search(body)
                    if m:
                        header["model_time_s"] = hms_to_s(m.group(1))
                        header["total_time_s"] = hms_to_s(m.group(2))
                    elif ":" in body:
                        k, _, v = body.partition(":")
                        header[k.strip()] = v.strip()
                continue
            code = line.split(";", 1)[0].strip()
            if not code:
                continue
            if c0 == "T":
                m = re.match(r"T(\d+)", code)
                if m:
                    t = int(m.group(1))
                    if t < 1000:  # T65279/T65535 are AMS unload commands in the end G-code
                        if tool is not None and t != tool:
                            changes += 1
                            if cur is not None:
                                cur.changes += 1
                        if first_tool is None:
                            first_tool = t
                        if not tool_seq or tool_seq[-1] != t:
                            tool_seq.append(t)
                        tool = t
                        if cur is not None:
                            cur.tools.add(t)
                continue
            if c0 == "M":
                if code.startswith("M73 ") or code == "M73":
                    ps = {k: float(v) for k, v in _PARAM_RE.findall(code[3:])}
                    if "P" in ps and "R" in ps:
                        p, r = int(ps["P"]), int(ps["R"])
                        if m73_p is None or p != m73_p or r != m73_r:
                            anchors.append((weight, 0.0, p, r))
                        m73_p, m73_r = p, r
                elif code.startswith("M83"):
                    e_relative = True
                elif code.startswith("M82"):
                    e_relative = False
                continue
            if c0 != "G":
                continue
            if len(code) > 1 and code[1] in "0123" and (len(code) == 2 or not code[2].isdigit()
                                                         and code[2] != "."):
                ps = {k: float(v) for k, v in _PARAM_RE.findall(code[2:])}
                if "F" in ps:
                    f_mm_min = ps["F"] or f_mm_min
                nx = ps.get("X", 0.0 if xy_relative else x)
                ny = ps.get("Y", 0.0 if xy_relative else y)
                if xy_relative:
                    nx, ny = x + nx, y + ny
                nz = ps.get("Z", z)
                if "E" in ps:
                    if e_relative:
                        de = ps["E"]
                    else:
                        de = ps["E"] - e_abs_last
                        e_abs_last = ps["E"]
                else:
                    de = 0.0
                g = code[1]
                if g in ("2", "3") and ("I" in ps or "J" in ps):
                    dxy = _arc_len(x, y, nx, ny, ps.get("I", 0.0), ps.get("J", 0.0),
                                   cw=(g == "2"), turns=ps.get("P", 0.0))
                    mv["arc_moves"] += 1
                else:
                    dxy = math.hypot(nx - x, ny - y)
                dz = nz - z
                dist = max(math.hypot(dxy, dz), abs(de))
                if f_mm_min > 0:
                    weight += dist / (f_mm_min / 60.0)
                if section != "print":
                    if de > 0:
                        start_end_e[section] += de
                elif de > 0 and dxy > 1e-9:
                    name = "Flush" if in_flush else (feature or "Undefined")
                    b = fbucket(name)
                    b["e_mm"] += de
                    b["path_mm"] += dxy
                    b["moves"] += 1
                    if tool is not None:
                        b["by_tool_e"][tool] = b["by_tool_e"].get(tool, 0.0) + de
                        by_tool_e[tool] = by_tool_e.get(tool, 0.0) + de
                        if cur is not None:
                            cur.e[tool] = cur.e.get(tool, 0.0) + de
                    if cur is not None:
                        cur.f[name] = cur.f.get(name, 0.0) + de
                elif de < 0 and dxy > 1e-9:
                    mv["wipe_moves"] += 1
                    mv["wipe_path_mm"] += dxy
                    mv["wipe_retract_mm"] += -de
                elif de < 0:
                    mv["retracts"] += 1
                    mv["retract_mm"] += -de
                    if cur is not None:
                        cur.retracts += 1
                elif de > 0:
                    mv["deretracts"] += 1
                    mv["deretract_mm"] += de
                elif dxy > 1e-9:
                    if not in_wipe:
                        mv["travel_mm"] += dxy
                        mv["travel_moves"] += 1
                        if cur is not None:
                            cur.travel_mm += dxy
                elif dz > 1e-9 and section == "print":
                    mv["z_hops"] += 1
                x, y, z = nx, ny, nz
            elif code.startswith("G90"):
                xy_relative = False
            elif code.startswith("G91"):
                xy_relative = True
            elif code.startswith("G92"):
                ps = {k: float(v) for k, v in _PARAM_RE.findall(code[3:])}
                if "E" in ps:
                    e_abs_last = ps["E"]
    return {
        "header": header, "config": config, "layers": layers, "features": feat,
        "by_tool_e": by_tool_e, "flush_by_tool": flush_by_tool, "virtual_deretract_mm":
        vderetract_mm, "tool_changes": changes, "first_tool": first_tool, "tool_seq": tool_seq,
        "nozzle_change_markers": nozzle_change_markers, "vflush_blocks": vflush_blocks,
        "vflush_nonzero": vflush_nonzero,
        "wipe_blocks": wipe_blocks, "moves": mv, "anchors": anchors,
        "end_weight": end_weight if end_line else weight, "total_weight": weight,
        "start_end_e": start_end_e,
    }


def _floats(text: str | None) -> list[float]:
    if not text:
        return []
    return [float(v) for v in re.split(r"[,;]", text) if v.strip()]


def layer_times(parsed: dict, total_s: float) -> list[float]:
    """Elapsed time at each layer start from Bambu's M73 progress (P = floor of percent of
    total, R = floor of minutes remaining). Each change of P or R pins elapsed time to the
    threshold it just crossed; between pins, time follows naive move time (distance / F)."""
    pins: list[tuple[float, float]] = [(0.0, 0.0)]
    last_p = last_r = None
    for w, _, p, r in parsed["anchors"]:
        cands = []
        if last_p is not None and p > last_p:
            cands.append(p / 100.0 * total_s)
        if last_r is not None and r < last_r:
            cands.append(total_s - (r + 1) * 60.0)
        last_p, last_r = p, r
        if cands:
            t = max(cands)
            if t >= pins[-1][1] and w >= pins[-1][0]:
                pins.append((w, t))
    pins.append((parsed["total_weight"], total_s))

    def at(w: float) -> float:
        lo = 0
        for k in range(len(pins)):
            if pins[k][0] <= w:
                lo = k
            else:
                break
        hi = min(lo + 1, len(pins) - 1)
        (w0, t0), (w1, t1) = pins[lo], pins[hi]
        if w1 <= w0:
            return t0
        return t0 + (t1 - t0) * (w - w0) / (w1 - w0)

    return [at(layer.w0) for layer in parsed["layers"]] + [at(parsed["end_weight"])]


# ----------------------------------------------------------------------------- extraction


def read_zip_text(zpath: Path, name: str) -> str | None:
    try:
        with zipfile.ZipFile(zpath) as z:
            return z.read(name).decode("utf-8", "replace") if name in z.namelist() else None
    except (OSError, zipfile.BadZipFile):
        return None


def slice_info(zpath: Path) -> dict:
    text = read_zip_text(zpath, "Metadata/slice_info.config")
    if not text:
        return {}
    root = ET.fromstring(text)
    out: dict = {"filaments": [], "layer_filament_lists": []}
    for p in root.iter("plate"):
        for m in p.findall("metadata"):
            out[m.get("key")] = m.get("value")
        for fil in p.findall("filament"):
            out["filaments"].append(dict(fil.attrib))
        for lf in p.iter("layer_filament_list"):
            out["layer_filament_lists"].append(dict(lf.attrib))
        out["warnings"] = [dict(w.attrib) for w in p.findall("warning")]
    return out


def input_stats(model: Path) -> dict:
    from p2p_threemf.reader import read_3mf_full

    r = read_3mf_full(model)
    slots = {}
    for s in r.face_slots.tolist():
        slots[s] = slots.get(s, 0) + 1
    lo, hi = r.vertices.min(axis=0), r.vertices.max(axis=0)
    return {
        "faces": len(r.faces), "vertices": len(r.vertices),
        "faces_per_slot": {str(k): v for k, v in sorted(slots.items())},
        "painted_slots": len(slots), "split_faces": int(r.split.sum()),
        "parts": len(set(r.face_part.tolist())), "objects": len(set(r.face_object.tolist())),
        "bbox_mm": [round(float(v), 2) for v in (hi - lo)],
        "application": r.application, "filaments_in_project": len(r.filament_colours),
    }


def r3(v: float) -> float:
    return round(float(v), 3)


def extract_case(case: dict, model: Path) -> dict:
    keep = TRUTH_DIR / case["id"]
    run = json.loads((keep / "run.json").read_text())
    result = json.loads((keep / "result.json").read_text())
    rep = json.loads((keep / "slice_report.json").read_text())
    sliced = keep / "sliced.gcode.3mf"
    gcode = keep / "plate_1.gcode"
    if not gcode.exists() and sliced.exists():
        text = read_zip_text(sliced, "Metadata/plate_1.gcode")
        if text:
            gcode.write_text(text)
    plate = (result.get("sliced_plates") or [{}])[0]
    si = slice_info(sliced) if sliced.exists() else {}
    seq_text = read_zip_text(sliced, "Metadata/filament_sequence.json") if sliced.exists() else None
    seq = (json.loads(seq_text) or {}).get("plate_1", {}) if seq_text else {}
    pj_text = read_zip_text(sliced, "Metadata/plate_1.json") if sliced.exists() else None
    pj = json.loads(pj_text) if pj_text else {}

    g = parse_gcode(gcode)
    (keep / "gcode_config.json").write_text(json.dumps(g["config"], indent=0, sort_keys=True))
    cfg = g["config"]
    dens = _floats(cfg.get("filament_density"))
    diam = _floats(cfg.get("filament_diameter"))

    def grams(tool: int, e_mm: float) -> float:
        d = diam[tool] if tool < len(diam) else 1.75
        rho = dens[tool] if tool < len(dens) else 1.24
        return e_mm * math.pi * (d / 2) ** 2 * rho / 1000.0

    total_s = float(plate.get("total_predication") or g["header"].get("total_time_s") or 0)
    starts = layer_times(g, total_s)
    fmap = [int(v) for v in (si.get("filament_maps") or "").split()] if si else []

    # grams per filament: Bambu's own split (result.json) plus our G-code split
    fil = {}
    for f in plate.get("filaments") or []:
        slot = int(f["id"])
        t = slot - 1
        tower_e = g["features"].get("Prime tower", {}).get("by_tool_e", {}).get(t, 0.0)
        fil[str(slot)] = {
            "filament_id": f.get("filament_id"),
            "total_g": r3(f["total_used_g"]), "model_g": r3(f["main_used_g"]),
            "purge_g": r3(f["total_used_g"] - f["main_used_g"]),
            "gcode_flush_g": r3(grams(t, g["flush_by_tool"].get(t, 0.0))),
            "gcode_tower_g": r3(grams(t, tower_e)),
            "gcode_extruded_g": r3(grams(t, g["by_tool_e"].get(t, 0.0))),
            "nozzle": fmap[t] if t < len(fmap) else None,
        }
    for f in si.get("filaments", []):
        d = fil.setdefault(f["id"], {})
        d.update({"used_m": float(f.get("used_m") or 0), "used_g_rounded": float(f.get("used_g") or 0),
                  "load_time_s": float(f.get("total_load_time") or 0),
                  "unload_time_s": float(f.get("total_unload_time") or 0)})
    total_g = sum(v.get("total_g", 0) for v in fil.values())
    model_g = sum(v.get("model_g", 0) for v in fil.values())
    tower_g = sum(v.get("gcode_tower_g", 0) for v in fil.values())
    flush_g = sum(v.get("gcode_flush_g", 0) for v in fil.values())

    ftimes = plate.get("feature_type_times") or {}
    features = {}
    for name in sorted(set(g["features"]) | set(ftimes)):
        b = g["features"].get(name, {"e_mm": 0.0, "path_mm": 0.0, "moves": 0, "by_tool_e": {}})
        features[name] = {
            "time_s": r3(ftimes[name]) if name in ftimes else None,
            "e_mm": r3(b["e_mm"]), "path_mm": r3(b["path_mm"]), "moves": b["moves"],
            "g": r3(sum(grams(t, e) for t, e in b["by_tool_e"].items())),
        }

    layers = g["layers"]
    per_layer = []
    for i, L in enumerate(layers):
        per_layer.append({
            "z": L.z, "h": L.h, "t0_s": round(starts[i], 1),
            "t_s": round(starts[i + 1] - starts[i], 1),
            "m73": list(L.m73) if L.m73[0] is not None else None,
            "tools": sorted(t + 1 for t in L.tools), "changes": L.changes,
            "e_mm": {str(t + 1): round(v, 2) for t, v in sorted(L.e.items()) if v > 0.005},
            "feature_e_mm": {k: round(v, 2) for k, v in sorted(L.f.items()) if v > 0.005},
            "flush_mm": round(L.flush_mm, 2), "travel_mm": round(L.travel_mm, 1),
            "retracts": L.retracts, "wipes": L.wipes,
        })
    first_h = next((L.h for L in layers if L.h is not None), None)
    nozzle_changes_map = None
    if fmap:
        seq_tools = g["tool_seq"]
        nozzle_changes_map = sum(1 for a, b in itertools.pairwise(seq_tools)
                                 if a < len(fmap) and b < len(fmap) and fmap[a] != fmap[b])
    header = g["header"]
    totals = {
        "print_time_s": r3(total_s),
        "print_time_s_slice_info": float(si.get("prediction") or 0) or None,
        "model_time_s_header": header.get("model_time_s"),
        "main_predication_s": r3(plate.get("main_predication") or 0),
        "total_g": r3(total_g), "model_g": r3(model_g), "purge_g": r3(total_g - model_g),
        "tower_g": r3(tower_g), "flush_g": r3(flush_g),
        "purge_residual_g": r3(total_g - model_g - tower_g - flush_g),
        "weight_g_slice_info": float(si.get("weight") or 0) or None,
        "filaments_used": sorted(int(k) for k, v in fil.items() if v.get("total_g", 0) > 0),
        "filament_changes": g["tool_changes"],
        "filament_change_times_result_json": plate.get("filament_change_times"),
        "flushes": g["vflush_nonzero"],
        "flush_blocks": g["vflush_blocks"],
        "flush_mm": r3(sum(g["flush_by_tool"].values())),
        "nozzle_changes": (nozzle_changes_map if nozzle_changes_map is not None
                           else g["nozzle_change_markers"]),
        "nozzle_change_markers": g["nozzle_change_markers"],
        "layer_count": len(layers),
        "layer_count_header": int(header.get("total layer number") or 0) or None,
        "first_layer_height": first_h,
        "initial_layer_print_height": _floats(cfg.get("initial_layer_print_height"))[:1] or None,
        "layer_height": _floats(cfg.get("layer_height"))[:1] or None,
        "max_z": float(header.get("max_z_height") or 0) or None,
        "support_used": si.get("support_used"),
        "feature_time_sum_excl_travel_s": r3(sum(v for k, v in ftimes.items() if k != "Travel")),
    }
    moves = {k: (r3(v) if isinstance(v, float) else v) for k, v in g["moves"].items()}
    moves["wipe_blocks"] = g["wipe_blocks"]
    moves["virtual_deretract_mm"] = r3(g["virtual_deretract_mm"])
    moves["start_gcode_e_mm"] = r3(g["start_end_e"]["start"])
    moves["end_gcode_e_mm"] = r3(g["start_end_e"]["end"])
    bambu_timing_ms = {k: plate.get(k) for k in (
        "sliced_time", "sliced_time_with_cache", "make_perimeters_time", "infill_time",
        "generate_support_material_time")}
    bambu_timing_ms.update({k: result.get(k) for k in ("prepare_time", "export_time")})
    tower_bbox = next((o["bbox"] for o in pj.get("bbox_objects", []) if o.get("name") == "wipe_tower"),
                      None)
    timing_path = keep / "timing.json"
    timing = json.loads(timing_path.read_text()) if timing_path.exists() else {}
    return {
        "input": {"path": case["input"], "sha256": run["input_sha256"],
                  "bytes": run["input_bytes"], **input_stats(model)},
        "run": {"slicer_wall_s": run["slicer_wall_s"], "slice_check_wall_s": run["slice_check_wall_s"],
                "slicer_wall_s_min": timing.get("min_s"), "slicer_wall_s_runs": timing.get("runs_s"),
                "loadavg_retime": timing.get("loadavg"),
                "bambu_internal_ms": bambu_timing_ms, "loadavg_before": run["loadavg_before"],
                "returncode": run["returncode"], "gcode_bytes": gcode.stat().st_size},
        "totals": totals,
        "filaments": fil,
        "features": features,
        "moves": moves,
        "sequence": {"tools": [t + 1 for t in g["tool_seq"]],
                     "bambu_sequence": seq.get("sequence"),
                     "nozzle_sequence": seq.get("nozzle_sequence"),
                     "filament_maps": si.get("filament_maps"),
                     "layer_filament_lists": si.get("layer_filament_lists")},
        "tower_bbox_xy": tower_bbox,
        "config": {k: cfg[k] for k in CONFIG_KEYS if k in cfg},
        "slice_report": {k: rep.get(k) for k in (
            "ok", "print_time_s", "total_grams", "purge_grams", "slots_used", "expected_slots",
            "readback_ok", "errors")} | {"warnings": len(rep.get("warnings") or []),
                                         "thin_paint": len(rep.get("thin_paint") or [])},
        "per_layer": per_layer,
    }


# ----------------------------------------------------------------------------- main


def machine_info() -> dict:
    def sysctl(key: str) -> str | None:
        try:
            return subprocess.run(["sysctl", "-n", key], capture_output=True, text=True,
                                  timeout=5, check=False).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None

    return {"platform": platform.platform(), "cpu": sysctl("machdep.cpu.brand_string"),
            "cores": os.cpu_count(), "memory_bytes": int(sysctl("hw.memsize") or 0) or None}


PROVENANCE = {
    "totals.print_time_s": "SOURCE: Bambu result.json sliced_plates[0].total_predication "
                           "(equals slice_info prediction and the G-code header total)",
    "totals.model_time_s_header": "SOURCE: G-code header 'model printing time'",
    "totals.main_predication_s": "SOURCE: Bambu result.json main_predication",
    "totals.total_g/model_g/purge_g": "SOURCE: result.json filaments total_used_g and "
                                      "main_used_g; purge = total - main",
    "totals.tower_g": "SOURCE (parsed): positive E under '; FEATURE: Prime tower' in the G-code, "
                      "converted with the config's filament_diameter and filament_density",
    "totals.flush_g": "SOURCE (parsed): ';VG1 E' lines inside '; VFLUSH_START/END' (virtual "
                      "moves the firmware executes as M620.10 L<flush_length>), new filament",
    "totals.purge_residual_g": "purge_g - tower_g - flush_g",
    "totals.filament_changes": "SOURCE (parsed): T<n> commands (n < 1000) that change the tool",
    "totals.nozzle_changes": "SOURCE (parsed): tool changes whose filaments sit on different "
                             "nozzles in slice_info filament_maps; nozzle_change_markers counts "
                             "'; NOZZLE_CHANGE_START' (absent in these slices)",
    "totals.flushes": "SOURCE (parsed): '; VFLUSH_START/END' blocks with flush length > 0 "
                      "(flush_blocks counts all, including 0 mm ones on a nozzle switch)",
    "totals.filament_change_times_result_json": "SOURCE: result.json; equals totals.flushes "
                                                "(tool changes with a flush) in all 20 cases",
    "features.*.time_s": "SOURCE: result.json feature_type_times. Everything except Travel sums "
                         "to total_predication; Travel is already inside the other features",
    "features.*.e_mm/path_mm/g": "SOURCE (parsed): extrusion moves (E > 0 with XY motion) by "
                                 "'; FEATURE:' label; 'Flush' is empty (flush is virtual)",
    "per_layer.t0_s/t_s": "SOURCE (parsed, approximate): elapsed time pinned at each M73 P "
                          "(floor of percent) or R (floor of minutes left) change, interpolated "
                          "between pins by distance / feedrate. Error is about 1% of total time "
                          "or 60 s, whichever is smaller, per pin interval",
    "run.slicer_wall_s": "SOURCE: measured wall clock of the Bambu CLI child process on this Mac, "
                         "one slice at a time",
    "run.bambu_internal_ms": "SOURCE: result.json timings; units look like milliseconds "
                             "(ASSUMED from comparison with wall clock)",
    "input.*": "SOURCE: p2p_threemf.reader.read_3mf_full on the input 3MF",
    "slice_report": "p2p_threemf.slice_check SliceReport for the same run (our gate's numbers; "
                    "purge_grams there is an estimate)",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", nargs="*", help="case ids")
    ap.add_argument("--force", action="store_true", help="slice again even if kept")
    ap.add_argument("--extract-only", action="store_true", help="do not slice")
    ap.add_argument("--regen-inputs", action="store_true", help="rewrite generated inputs")
    ap.add_argument("--no-write", action="store_true", help="do not rewrite truth.json")
    ap.add_argument("--retime", type=int, default=0, metavar="N",
                    help="slice each case N more times for wall-clock numbers (minimum kept)")
    ap.add_argument("--pin-inputs", action="store_true",
                    help="write each input's sha256, bytes and settings into cases.json and stop")
    a = ap.parse_args()

    spec = json.loads(CASES_PATH.read_text())
    cases = [c for c in spec["cases"] if not a.only or c["id"] in a.only]
    if a.pin_inputs:
        for case in cases:
            model = ensure_input(case, a.regen_inputs)
            case["input_sha256"] = sha256(model)
            case["input_bytes"] = model.stat().st_size
            case["settings"] = settings_summary(project_config(model))
            log(f"pinned {case['id']}: {case['input_bytes']} bytes")
        CASES_PATH.write_text(json.dumps(spec, indent=1) + "\n")
        return 0
    truth = json.loads(TRUTH_PATH.read_text()) if TRUTH_PATH.exists() else {}
    truth.setdefault("cases", {})
    TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for i, case in enumerate(cases, 1):
        log(f"[{i}/{len(cases)}] {case['id']}")
        try:
            model = ensure_input(case, a.regen_inputs)
            if not a.extract_only:
                slice_case(case, model, a.force)
            if a.retime:
                tm = retime_case(case, model, a.retime)
                log(f"  retimed: {tm['runs_s']} s")
            if not (TRUTH_DIR / case["id"] / "result.json").exists():
                raise RuntimeError("no Bambu result.json (slice failed or not run)")
            t = extract_case(case, model)
        except Exception as e:  # noqa: BLE001  record and keep going
            failures += 1
            log(f"  FAILED {case['id']}: {type(e).__name__}: {e}")
            continue
        truth["cases"][case["id"]] = t
        tt = t["totals"]
        log(f"  ok: {tt['print_time_s'] / 60:.1f} min, {tt['total_g']:.2f} g "
            f"(model {tt['model_g']:.2f}, tower {tt['tower_g']:.2f}, flush {tt['flush_g']:.2f}), "
            f"{tt['layer_count']} layers, {tt['filament_changes']} changes, "
            f"slicer {t['run']['slicer_wall_s']} s")
        if not a.no_write:  # checkpoint after every case
            write_truth(truth, spec)
    log(f"done: {len(cases) - failures} ok, {failures} failed")
    return 1 if failures else 0


def write_truth(truth: dict, spec: dict) -> None:
    order = [c["id"] for c in spec["cases"]]
    cases = {k: truth["cases"][k] for k in order if k in truth["cases"]}
    first = next(iter(cases.values()), None)
    out = {
        "about": "Bambu Studio CLI ground truth for the native slicer. Regenerate with "
                 "slicer/calibration/build_truth.py; inputs and reasons in cases.json.",
        "bambu_version": None,
        "machine": machine_info(),
        "provenance": PROVENANCE,
        "cases": cases,
    }
    if first is not None:
        run = json.loads((TRUTH_DIR / next(iter(cases)) / "run.json").read_text())
        out["bambu_version"] = run.get("bambu_version")
    tmp = TRUTH_PATH.with_suffix(".json.tmp")
    tmp.write_text(dumps_truth(out))
    tmp.replace(TRUTH_PATH)


def dumps_truth(out: dict) -> str:
    """indent=1 JSON, except each per_layer entry sits on one line (keeps the file small)."""
    layers = {}
    for cid, c in out["cases"].items():
        token = f"@@per_layer:{cid}@@"
        layers[token] = c["per_layer"]
        c["per_layer"] = token
    text = json.dumps(out, indent=1)
    for token, rows in layers.items():
        body = ",\n".join("   " + json.dumps(r, separators=(",", ":")) for r in rows)
        text = text.replace(json.dumps(token), "[\n" + body + "\n  ]" if rows else "[]")
        cid = token.split(":", 1)[1].rstrip("@")
        out["cases"][cid]["per_layer"] = rows
    return text + "\n"


if __name__ == "__main__":
    sys.exit(main())
