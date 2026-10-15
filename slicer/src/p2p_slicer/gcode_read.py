"""Read Bambu-dialect G-code (Bambu Studio's or ours) into a MoveTable.

Classification follows section 10.1 of docs/research/native-slicer.md: E < 0 with motion is a
travel, E < 0 alone a retract, E > 0 with XY an extrusion, E > 0 alone an unretract, E > 0 with Z
only a travel, no E a travel; anything inside '; WIPE_START' / '; WIPE_END' is a wipe. The row's
role is the raw '; FEATURE:' in effect; the estimator decides the time role.

Filaments: Bambu writes `T<n>` with n = 0-based filament index, so slot = n + 1. T65279 and
T65535 in the X2D end G-code (AMS unload) are ignored, as Bambu's processor ignores them.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .moves import Flag, Kind, MoveBuilder, MoveTable, Role, role_id

_HDR_TIME_RE = re.compile(r"model printing time:\s*([^;]+);\s*total estimated time:\s*(.+)$")
NAN = float("nan")
Z4 = (0.0, 0.0, 0.0, 0.0)


def hms_to_s(text: str) -> float:
    s = 0.0
    for num, unit in re.findall(r"(\d+)\s*([dhms])", text):
        s += int(num) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return s


def _params(code: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for tok in code.split()[1:]:
        k = tok[0]
        try:
            out[k] = float(tok[1:]) if len(tok) > 1 else 0.0
        except ValueError:
            continue
    return out


def parse_list(value: str) -> list[str]:
    """Bambu config list: ';' separated when any ';' is present (strings), else ','."""
    v = value.strip()
    if not v:
        return []
    sep = ";" if ";" in v else ","
    return [p.strip().strip('"') for p in v.split(sep)]


def config_floats(cfg: dict[str, str], key: str, default: list[float] | None = None) -> list[float]:
    if key not in cfg:
        return list(default or [])
    out = []
    for p in parse_list(cfg[key]):
        p = p.rstrip("%")
        try:
            out.append(float(p))
        except ValueError:
            out.append(NAN)
    return out


def split_blocks(text: str) -> tuple[dict[str, str], dict[str, str], int]:
    """Header dict, config dict and the line index where executable code starts (0-based)."""
    header: dict[str, str] = {}
    config: dict[str, str] = {}
    in_header = in_config = False
    start = 0
    for idx, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if line == "; HEADER_BLOCK_START":
            in_header = True
            continue
        if line == "; HEADER_BLOCK_END":
            in_header = False
            continue
        if line == "; CONFIG_BLOCK_START":
            in_config = True
            continue
        if line == "; CONFIG_BLOCK_END":
            in_config = False
            start = idx + 1
            break
        if line == "; EXECUTABLE_BLOCK_START":
            start = idx
            break
        if in_config and line.startswith("; "):
            k, sep, v = line[2:].partition(" = ")
            if sep:
                config[k.strip()] = v
        elif in_header and line.startswith("; "):
            body = line[2:]
            m = _HDR_TIME_RE.search(body)
            if m:
                header["model_time_s"] = str(hms_to_s(m.group(1)))
                header["total_time_s"] = str(hms_to_s(m.group(2)))
            elif ":" in body:
                k, _, v = body.partition(":")
                header[k.strip()] = v.strip()
    return header, config, start


def nozzle_map_from_config(cfg: dict[str, str]) -> dict[int, int]:
    """slot (1-based) -> nozzle (0 left, 1 right) from `filament_map` (1-based nozzles)."""
    fm = config_floats(cfg, "filament_map")
    return {k + 1: int(v) - 1 for k, v in enumerate(fm) if not math.isnan(v)}


@dataclass
class ParseState:
    """Everything the reader carries from line to line (also used by moves.PathWriter to splice
    rendered templates into a generated MoveTable)."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    f: float = 0.0
    abs_xyz: bool = True
    rel_e: bool = True
    e_abs_last: float = 0.0
    role: int = int(Role.UNDEFINED)
    slot: int = 0
    nozzle: int = -1
    layer: int = 0
    width: float = 0.0
    height: float = 0.0
    acc_print: float = 0.0
    acc_travel: float = 0.0
    acc_retract: float = 0.0
    wiping: bool = False
    in_start: bool = False
    seen_feature: bool = False
    start_done: bool = False
    in_end: bool = False
    in_flush: bool = False
    in_tower: bool = False
    in_nozzle_change: bool = False
    in_change: bool = False
    j_block: int = -1
    held: dict[int, int] = field(default_factory=dict)      # nozzle -> slot it holds
    layer_z: list[float] = field(default_factory=list)
    layer_h: list[float] = field(default_factory=list)
    tool_seq: list[int] = field(default_factory=list)
    first_seen: int | None = None


def parse_lines(lines: list[str], st: ParseState, add: Any, nozzle_of: dict[int, int],
                prepare_comp: float = 260.0, line_offset: int = 0, extra_flags: int = 0) -> None:
    """Parse G-code lines, appending row tuples with `add` and updating `st`."""
    x, y, z, f = st.x, st.y, st.z, st.f
    abs_xyz, rel_e, e_abs_last = st.abs_xyz, st.rel_e, st.e_abs_last
    role, slot, nozzle, layer = st.role, st.slot, st.nozzle, st.layer
    width, height = st.width, st.height
    acc_print, acc_travel, acc_retract = st.acc_print, st.acc_travel, st.acc_retract
    wiping, in_start, seen_feature, start_done = st.wiping, st.in_start, st.seen_feature, \
        st.start_done
    in_end, in_flush, in_tower = st.in_end, st.in_flush, st.in_tower
    in_nozzle_change, in_change, j_block = st.in_nozzle_change, st.in_change, st.j_block
    held, layer_z, layer_h, tool_seq = st.held, st.layer_z, st.layer_h, st.tool_seq
    first_seen = st.first_seen
    xf = extra_flags

    for lineno0, raw in enumerate(lines, line_offset):
        if not raw:
            continue
        c0 = raw[0]
        if c0 == ";":
            # comments and virtual moves
            if raw.startswith("; FEATURE: "):
                name = raw[11:].strip()
                role = role_id(name)
                if not seen_feature:
                    seen_feature = True
                    if name == "Custom":
                        in_start = True
                elif in_start and not start_done:
                    in_start = False
                    start_done = True
                continue
            if raw.startswith(";VG1"):
                p = _params(raw[1:].split(";", 1)[0])
                de = p.get("E", 0.0)
                vf = p.get("F", f)
                flags = (Flag.FLUSH if in_flush else 0) | (Flag.START if in_start else 0) | \
                    (Flag.END if in_end else 0) | (Flag.CHANGE_BLOCK if in_change else 0) | xf
                if de != 0.0:
                    add((x, y, z, de, vf, Kind.VIRTUAL, role, slot, nozzle, layer, width, height,
                         acc_retract, 0.0, flags, 0.0, 0.0, 0, lineno0 + 1, Z4))
                continue
            s = raw.rstrip()
            if s == "; CHANGE_LAYER":
                layer += 1
                layer_z.append(NAN)
                layer_h.append(NAN)
            elif s.startswith("; Z_HEIGHT:"):
                if layer and math.isnan(layer_z[-1]):
                    layer_z[-1] = float(s[11:])
            elif s.startswith("; LAYER_HEIGHT:"):
                try:
                    height = float(s[15:])
                except ValueError:
                    pass
                if layer and math.isnan(layer_h[-1]):
                    layer_h[-1] = height
            elif s.startswith("; LINE_WIDTH:"):
                try:
                    width = float(s[13:])
                except ValueError:
                    pass
            elif s == "; WIPE_START":
                wiping = True
            elif s == "; WIPE_END":
                wiping = False
            elif s == "; VFLUSH_START":
                in_flush = True
            elif s == "; VFLUSH_END":
                in_flush = False
            elif s == "; WIPE_TOWER_START":
                in_tower = True
            elif s == "; WIPE_TOWER_END":
                in_tower = False
            elif s.startswith("; NOZZLE_CHANGE_START"):
                in_nozzle_change = True
            elif s.startswith("; NOZZLE_CHANGE_END"):
                in_nozzle_change = False
            elif s.startswith("; filament end gcode") and layer > 0:
                in_change = True
            elif s == "; MACHINE_END_GCODE_START":
                in_end = True
                in_change = False
            continue

        code = raw.split(";", 1)[0].strip()
        if not code:
            continue
        c0 = code[0]
        if c0 == "G":
            sp = code.find(" ")
            word = code[:sp] if sp > 0 else code
            if word in ("G1", "G0", "G2", "G3"):
                p = _params(code)
                if "F" in p:
                    f = p["F"]
                if abs_xyz:
                    nx = p.get("X", x)
                    ny = p.get("Y", y)
                    nz = p.get("Z", z)
                else:
                    nx = x + p.get("X", 0.0)
                    ny = y + p.get("Y", 0.0)
                    nz = z + p.get("Z", 0.0)
                if "E" in p:
                    if rel_e:
                        de = p["E"]
                    else:
                        de = p["E"] - e_abs_last
                        e_abs_last = p["E"]
                else:
                    de = 0.0
                arc = word in ("G2", "G3") and ("I" in p or "J" in p)
                dx, dy, dz = nx - x, ny - y, nz - z
                if not arc and dx == 0.0 and dy == 0.0 and dz == 0.0 and de == 0.0:
                    continue
                has_xy = dx != 0.0 or dy != 0.0 or arc
                if wiping:
                    kind = Kind.WIPE
                elif de < 0.0:
                    kind = Kind.TRAVEL if (has_xy or dz != 0.0) else Kind.RETRACT
                elif de > 0.0:
                    if has_xy:
                        kind = Kind.EXTRUDE
                    else:
                        kind = Kind.TRAVEL if dz != 0.0 else Kind.UNRETRACT
                else:
                    kind = Kind.TRAVEL
                if kind == Kind.TRAVEL:
                    acc = acc_travel
                elif kind in (Kind.RETRACT, Kind.UNRETRACT):
                    acc = acc_retract
                else:
                    acc = acc_print
                flags = xf
                if arc:
                    flags |= Flag.ARC_CW if word == "G2" else Flag.ARC_CCW
                if wiping:
                    flags |= Flag.WIPE
                if in_start:
                    flags |= Flag.START
                if in_end:
                    flags |= Flag.END
                if in_flush:
                    flags |= Flag.FLUSH
                if in_tower:
                    flags |= Flag.TOWER
                if in_nozzle_change:
                    flags |= Flag.NOZZLE_CHANGE
                if in_change:
                    flags |= Flag.CHANGE_BLOCK
                add((nx, ny, nz, de, f, kind, role, slot, nozzle, layer, width, height, acc, 0.0,
                     flags, p.get("I", 0.0) if arc else 0.0, p.get("J", 0.0) if arc else 0.0,
                     int(p.get("P", 0.0)) if arc else 0, lineno0 + 1, Z4))
                x, y, z = nx, ny, nz
            elif word == "G28":
                # Bambu's processor times a home as a move to 0 on the named axes (all when
                # none is named) at the current feedrate.
                p = _params(code)
                named = [a for a in "XYZ" if a in p]
                nx = 0.0 if (not named or "X" in named) else x
                ny = 0.0 if (not named or "Y" in named) else y
                nz = 0.0 if (not named or "Z" in named) else z
                if (nx, ny, nz) != (x, y, z):
                    add((nx, ny, nz, 0.0, f, Kind.TRAVEL, role, slot, nozzle, layer, width, height,
                         acc_travel, 0.0, (_region(in_start, in_end) | xf), 0.0, 0.0, 0, lineno0 + 1,
                         Z4))
                    x, y, z = nx, ny, nz
            elif word == "G130":
                add((x, y, z, 0.0, f, Kind.EMPTY, role, slot, nozzle, layer, width, height, 0.0,
                     0.0, (_region(in_start, in_end) | xf), 0.0, 0.0, 0, lineno0 + 1, Z4))
            elif word == "G90":
                abs_xyz = True
            elif word == "G91":
                abs_xyz = False
            elif word == "G92":
                p = _params(code)
                if "E" in p:
                    e_abs_last = p["E"]
                if "X" in p:
                    x = p["X"]
                if "Y" in p:
                    y = p["Y"]
                if "Z" in p:
                    z = p["Z"]
            elif word == "G4":
                p = _params(code)
                sec = p.get("S", 0.0) + p.get("P", 0.0) / 1000.0
                if "S" in p or "P" in p:
                    add((x, y, z, 0.0, f, Kind.DWELL, role, slot, nozzle, layer, width, height,
                         0.0, sec, (_region(in_start, in_end) | xf), 0.0, 0.0, 0, lineno0 + 1, Z4))
            elif word == "G29":
                if j_block == 1:
                    add((x, y, z, 0.0, f, Kind.G29, role, slot, nozzle, layer, width, height,
                         0.0, prepare_comp, (_region(in_start, in_end) | xf), 0.0, 0.0, 0, lineno0 + 1, Z4))
            continue
        if c0 == "M":
            sp = code.find(" ")
            word = code[:sp] if sp > 0 else code
            if word == "M400":
                if sp > 0:
                    p = _params(code)
                    if "S" in p or "P" in p:
                        sec = p.get("S", 0.0) + p.get("P", 0.0) / 1000.0
                        add((x, y, z, 0.0, f, Kind.DWELL, role, slot, nozzle, layer, width,
                             height, 0.0, sec, (_region(in_start, in_end) | xf), 0.0, 0.0, 0,
                             lineno0 + 1, Z4))
            elif word == "M204":
                p = _params(code)
                if "S" in p:
                    acc_print = acc_travel = p["S"]
                if "P" in p:
                    acc_print = p["P"]
                if "T" in p:
                    acc_travel = p["T"]
                if "R" in p:
                    acc_retract = p["R"]
                add((x, y, z, 0.0, f, Kind.M204, role, slot, nozzle, layer, width, height, 0.0,
                     0.0, (_region(in_start, in_end) | xf), 0.0, 0.0, 0, lineno0 + 1,
                     (p.get("S", NAN), p.get("P", NAN), p.get("T", NAN), p.get("R", NAN))))
            elif word in ("M201", "M203", "M205"):
                p = _params(code)
                fl = {"M201": Flag.LIM_ACCEL, "M203": Flag.LIM_SPEED, "M205": Flag.LIM_JERK}[word]
                add((x, y, z, 0.0, f, Kind.LIMITS, role, slot, nozzle, layer, width, height, 0.0,
                     0.0, fl | (_region(in_start, in_end) | xf), 0.0, 0.0, 0, lineno0 + 1,
                     (p.get("X", NAN), p.get("Y", NAN), p.get("Z", NAN), p.get("E", NAN))))
            elif word == "M622":
                p = _params(code)
                j_block = int(p.get("J", -1))
            elif word == "M623":
                j_block = -1
            elif word == "M83":
                rel_e = True
            elif word == "M82":
                rel_e = False
            continue
        if c0 == "T":
            m = re.match(r"T(\d+)", code)
            if not m:
                continue
            t = int(m.group(1))
            if t >= 1000:
                continue
            new = t + 1
            if new == slot:
                continue
            new_noz = int(nozzle_of.get(new, 0))
            flags = _region(in_start, in_end) | xf
            if slot and new_noz != nozzle:
                flags |= Flag.SWITCH
            h = held.get(new_noz)
            if h is None:
                flags |= Flag.FIRST_LOAD
            elif h != new:
                flags |= Flag.AMS_SWAP
            held[new_noz] = new
            # TOOL_CHANGE rows: i = previous slot, p = slot the target nozzle held (0 = none)
            add((x, y, z, 0.0, f, Kind.TOOL_CHANGE, role, new, new_noz, layer, width, height, 0.0,
                 0.0, flags, float(slot), 0.0, h if h is not None else 0, lineno0 + 1, Z4))
            slot, nozzle = new, new_noz
            tool_seq.append(new)
            if first_seen is None:
                first_seen = new
            continue
        if code.startswith("SYNC"):
            p = _params(code)
            add((x, y, z, 0.0, f, Kind.SYNC, role, slot, nozzle, layer, width, height, 0.0,
                 p.get("T", 0.0), (_region(in_start, in_end) | xf) | (Flag.FLUSH if in_flush else 0),
                 0.0, 0.0, 0, lineno0 + 1, Z4))
            continue


    st.x, st.y, st.z, st.f = x, y, z, f
    st.abs_xyz, st.rel_e, st.e_abs_last = abs_xyz, rel_e, e_abs_last
    st.role, st.slot, st.nozzle, st.layer = role, slot, nozzle, layer
    st.width, st.height = width, height
    st.acc_print, st.acc_travel, st.acc_retract = acc_print, acc_travel, acc_retract
    st.wiping, st.in_start, st.seen_feature, st.start_done = wiping, in_start, seen_feature, \
        start_done
    st.in_end, st.in_flush, st.in_tower = in_end, in_flush, in_tower
    st.in_nozzle_change, st.in_change, st.j_block = in_nozzle_change, in_change, j_block
    st.first_seen = first_seen


def read_gcode(data: bytes | str | Path, *, first_slot: int | None = None,
               nozzle_of: dict[int, int] | None = None) -> MoveTable:
    """Parse G-code into a MoveTable. `nozzle_of` defaults to the config block's filament_map;
    `first_slot` (1-based) seeds the filament before the first T line (Bambu's start G-code
    always has one)."""
    if isinstance(data, Path):
        data = data.read_bytes()
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    header, config, _ = split_blocks(text)
    if nozzle_of is None:
        nozzle_of = nozzle_map_from_config(config)
    prep_s = config_floats(config, "machine_prepare_compensation_time", [260.0])
    prepare_comp = prep_s[0] if prep_s else 260.0
    mb = MoveBuilder(meta={"config": config, "header": header, "nozzle_of": dict(nozzle_of),
                           "source": "gcode"})
    st = ParseState()
    if first_slot:
        st.slot = int(first_slot)
        st.nozzle = int(nozzle_of.get(st.slot, 0))
        st.held[st.nozzle] = st.slot
        st.first_seen = st.slot
    parse_lines(text.splitlines(), st, mb._rows.append, nozzle_of, prepare_comp)
    mt = mb.build()
    mt.meta.update({"layer_z": st.layer_z, "layer_h": st.layer_h, "tool_seq": st.tool_seq,
                    "first_slot": st.first_seen})
    return mt


def _region(in_start: bool, in_end: bool) -> int:
    return (Flag.START if in_start else 0) | (Flag.END if in_end else 0)


def read_gcode_file(path: str | Path, **kw: Any) -> MoveTable:
    return read_gcode(Path(path).read_bytes(), **kw)
