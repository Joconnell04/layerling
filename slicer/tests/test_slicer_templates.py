"""Bambu G-code template language (p2p_slicer.templates).

Unit tests for the language, then the acceptance test: for every calibration case, render each
template occurrence in Bambu's G-code (start, filament start/end, change, layer change,
timelapse, end) with values derived from the case, and compare with Bambu's text line by line.
Lines Bambu inserts after placing templates (progress, idle-nozzle temperature) are dropped
first (templates.strip_inserted). Two measures: `lines` ignores blank lines and trailing
spaces (the acceptance measure); `exact` compares every line, blanks included, of the text the
place_* helpers produce.

Where the per-event values come from in this test (the slicer computes them itself):
- config: slice_config(project, filament_map from gcode_config.json);
- walking Bambu's G-code: active filament (T lines), layer index and z (`; CHANGE_LAYER`,
  `; Z_HEIGHT:`), the nozzle z before a change (last Z move), the feed rate in effect, the
  slots used overall and on the first layer, per-layer model nozzles, the first filament on
  each nozzle, max z;
- read from the block itself (decided by planners outside the template module): the next
  filament id (its `T` line), flush_length (M620.10 `L` and the VFLUSH `;VG1` values), the
  prime tower interface flag (`M620.13`), the G29 print area, the timelapse safe position and
  inline-photo flag. The change travel points are change_env's defaults.
"""

from __future__ import annotations

import json
import os
import re
import time
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from p2p_slicer import templates as tp
from p2p_slicer.config import load_project_config, slice_config
from p2p_slicer.templates import TemplateError, format_value, render
from slicer_testutil import CASES, REPO, TRUTH_DIR, needs_truth

# ----------------------------------------------------------------------------- language


def r(text: str, **env: Any) -> str:
    return render(text, env)


def test_substitution_and_expressions():
    env = {"a": 3, "b": 2.5, "s": "PLA", "v": [1, 2, 3], "z": 0.2, "f": 12.0, "n": 299}
    assert r("x[a]y", **env) == "x3y"
    assert r("[v[1]] [v[a]]", **{**env, "a": 2}) == "2 3"
    assert r("{a + 1} {b * 2} {v[a - 1]}", **env) == "4 5 3"
    assert r("Z{z + 3.0} F{f/2.4053*60}", **env) == "Z3.2 F299.339"
    assert r("{n/2} {max(n/2, 100)} {min(n, 100.0)} {ceil(0/125)*5}", **env) == "149 149 100 0"
    assert r("{-7/2} {7 % 3} {-7 % 3} {7.5 % 2}", **env) == "-3 1 -1 1.5"
    assert r("{a > 1 ? 5 : 6} {s == \"PLA\"} {!(a == 3)}", **env) == "5 true false"
    assert r("{a > 1 and not (b > 3) or false}", **env) == "true"
    assert r("{+0.0} {-0.003} {0.002} {40*255.0/100.0} {35*255.0/100.0}") == "0 -0.003 0.002 102 89.25"


def test_number_format():
    assert format_value(2.494491) == "2.49449"
    assert format_value(152.5812) == "152.581"
    assert format_value(1.0) == "1"
    assert format_value(-0.0) == "0"
    assert format_value(7) == "7"


def test_if_blocks_keep_surrounding_text():
    t = "A\n{if x > 1}\nB\n{elsif x > 0}\nC\n{else}\nD\n{endif}\nE"
    assert r(t, x=2) == "A\n\nB\n\nE"
    assert r(t, x=1) == "A\n\nC\n\nE"
    assert r(t, x=0) == "A\n\nD\n\nE"
    nested = "{if a}{if b}ab{else}a{endif}{else}-{endif}"
    assert r(nested, a=True, b=False) == "a"
    assert r(nested, a=False, b=True) == "-"
    multi = '{if (t == "PLA") ||  (t == "PETG")\n ||  (t == "PLA-CF")}\nyes\n{endif}'
    assert r(multi, t="PETG") == "\nyes\n"


def test_enums_and_nil():
    e = tp.Enum("Auto Lift", 0)
    assert r("{z[0] == 0 || z[0] == 3} {z[0] == \"Auto Lift\"}", z=[e]) == "true true"
    assert r("{is_nil(v[0])} {is_nil(v[1])}", v=[tp.NIL, 1.0]) == "true false"


def test_errors():
    with pytest.raises(TemplateError):
        r("{nope}")
    with pytest.raises(TemplateError):
        r("{v[3]}", v=[1])
    with pytest.raises(TemplateError):
        r("{if 1}x")
    with pytest.raises(TemplateError):
        r("{1 +}")


def test_drop_repeated_feedrate_and_strip_inserted():
    text, f = tp.drop_repeated_feedrate("G1 Z3.2 F3000\nM400\nG1 Z3.2 F3000\nG1 Y256 F18000", 5400)
    assert text == "G1 Z3.2 F3000\nM400\nG1 Z3.2\nG1 Y256 F18000" and f == 18000
    lines = ["T4 H-1", "M73 E22", "M400", "M104 T1 S92 N0 ;Multi extruder pre cooling", "M632 S4 W",
             "M104 T0 S210 N0 ;Multi extruder pre heating", "M633", "M73 P18 R25", "M73 L3"]
    assert tp.strip_inserted(lines) == ["T4 H-1", "M73 L3"]


# ----------------------------------------------------------------------------- truth scanner

MAIN_CHECKOUT = Path(os.environ.get("P2P_MAIN_CHECKOUT", str(Path(__file__).resolve().parents[2])))
T_RE = re.compile(r"^\s*T(\d+)(?:\s|$)")
F_RE = re.compile(r"^G[0-3](?:\s.*?)?\sF(\d+(?:\.\d*)?)")
Z_RE = re.compile(r"^G[0-3]\s.*?\bZ(-?[\d.]+)")
MODEL_ROLES = {"Outer wall", "Inner wall", "Overhang wall", "Sparse infill", "Internal solid infill",
               "Top surface", "Bottom surface", "Bridge", "Gap infill", "Floating vertical shell",
               "Brim", "Support", "Support interface", "Support transition", "Ironing"}


def input_path(case: dict[str, Any], truth: Path) -> Path:
    """The case's project 3MF; Bambu's sliced 3MF carries the same settings when it is missing
    (checked: identical results on three cases)."""
    for root in (REPO, MAIN_CHECKOUT):
        p = root / case["input"]
        if p.exists():
            return p
    return truth / case["id"] / "sliced.gcode.3mf"


def _interval(text: str) -> tuple[float, float]:
    """Values that print as `text` with 6 significant digits."""
    v = float(text)
    dec = len(text.split(".")[1]) if "." in text else 0
    h = 0.5 * 10.0 ** -dec
    return v - h, v + h


def flush_length_of(block: list[str]) -> float:
    """flush_length consistent with every place the change block prints it."""
    lo, hi = -1e9, 1e9
    vg: list[str] = []
    inside = False
    for line in block:
        if line.startswith("; VFLUSH_START"):
            inside = True
        elif line.startswith("; VFLUSH_END"):
            break
        elif inside and line.startswith(";VG1 E"):
            vg.append(line.split()[1][1:])
        elif line.startswith("M620.10 A0") and " L" in line:
            a, b = _interval(re.search(r" L(\S+)", line).group(1))
            lo, hi = max(lo, a), min(hi, b)
    offset = 41.5 if len(vg) == 2 else 0.0
    if vg:
        a, b = _interval(vg[-1])
        lo, hi = max(lo, a + offset), min(hi, b + offset)
    if float(vg[-1] if vg else 0) == 0 and offset == 0:
        return 0.0
    return (lo + hi) / 2


def _norm(lines: list[str]) -> list[str]:
    return [x.rstrip() for x in lines if x.strip()]


EVENT_RE = re.compile(
    r"^(?:; CHANGE_LAYER|; Z_HEIGHT:.*|; FEATURE:.*|[ \t]*T\d+(?:[ \t].*)?|======== X2D filament_change.*"
    r"|;======== X2D (?:layer_change|timelapse).*|; filament start gcode.*|; MACHINE_END_GCODE_START)$",
    re.MULTILINE)


class Scan:
    """One case: Bambu's G-code walked, every template block rendered and compared."""

    def __init__(self, case_id: str, truth: Path) -> None:
        self.case_id = case_id
        folder = truth / case_id
        gcfg = json.loads((folder / "gcode_config.json").read_text())
        fmap = [int(x) for x in gcfg["filament_map"].split(",")]
        case = next(c for c in CASES if c["id"] == case_id)
        self.cfg = slice_config(load_project_config(input_path(case, truth)), filament_map=fmap)
        text = (folder / "plate_1.gcode").read_text()
        s0 = text.index("; EXECUTABLE_BLOCK_START")
        self.offset = text.count("\n", 0, s0)
        text = text[s0:]
        self.lines = text.split("\n")
        # (line index, line) of the lines the walk needs; everything else is motion.
        self.events: list[tuple[int, str]] = []
        pos, line_no = 0, 0
        for m in EVENT_RE.finditer(text):
            line_no += text.count("\n", pos, m.start())
            pos = m.start()
            self.events.append((line_no, m.group(0)))
        self.results: dict[str, list[int]] = {}   # kind -> [blocks, same lines, exact]
        self.diffs: list[tuple[Any, ...]] = []
        self.kinds: dict[str, int] = {}           # change kinds seen

    def record(self, kind: str, ours: str, theirs: list[str], at: int, info: Any = None) -> None:
        o = ours.split("\n")
        if o and o[-1] == "":
            o = o[:-1]
        same = _norm(o) == _norm(theirs)
        exact = [x.rstrip() for x in o] == [x.rstrip() for x in theirs]
        res = self.results.setdefault(kind, [0, 0, 0])
        res[0] += 1
        res[1] += same
        res[2] += exact
        if not same:
            d = [(a, b) for a, b in zip(_norm(o), _norm(theirs)) if a != b][:3]
            self.diffs.append((kind, at + self.offset + 1, d, info))

    def last_before(self, i: int, pat: re.Pattern[str]) -> float | None:
        """Last G0-G3 value matching `pat` in the 400 lines before line i."""
        for k in range(i - 1, max(i - 400, 0), -1):
            x = self.lines[k]
            if x[:1] == "G":
                m = pat.match(x)
                if m:
                    return float(m.group(1))
        return None

    def run(self) -> Scan:
        cfg, L = self.cfg, self.lines
        env0 = tp.config_env(cfg)
        # Pass 1: print-wide facts.
        active = None
        init = None
        used: set[int] = set()
        first_layer: set[int] = set()
        first_on_nozzle: dict[int, int] = {}
        layer_nozzles: list[set[int]] = []
        layer = -1
        zmax = 0.0
        for _, x in self.events:
            if x.startswith("; CHANGE_LAYER"):
                layer += 1
                layer_nozzles.append(set())
            elif x.startswith("; FEATURE:"):
                if layer >= 0 and active is not None and x[10:].strip() in MODEL_ROLES:
                    layer_nozzles[-1].add(tp.nozzle_of(env0, active))
            elif x.startswith("; Z_HEIGHT:"):
                zmax = max(zmax, float(x.split(":")[1]))
            else:
                m = T_RE.match(x)
                if m and int(m.group(1)) < 255:
                    active = int(m.group(1))
                    used.add(active)
                    if init is None:
                        init = active
                    if layer <= 0:
                        first_layer.add(active)
                    first_on_nozzle.setdefault(tp.nozzle_of(env0, active), active)
        assert init is not None
        base = tp.base_env(cfg, used_slots=[u + 1 for u in used],
                           first_layer_slots=[u + 1 for u in first_layer])
        first_phys = [-1, -1]
        for nz, t in first_on_nozzle.items():
            first_phys[tp.physical_extruder(base, nz)] = t
        most_used = tp.most_used_physical_extruder(base, layer_nozzles)

        # The start block (before the first event of the walk).
        i = next(k for k, x in enumerate(L) if x.startswith("; FEATURE: Custom"))
        j = next(k for k in range(i, len(L)) if L[k].startswith("; MACHINE_START_GCODE_END"))
        g29 = next(y for y in L[i:j] if y.strip().startswith("G29 A1"))
        p = {t[0]: float(t[1:]) for t in g29.split()[2:6]}
        env = tp.start_env(base, initial_filament_id=init, first_layer_print_min=[p["X"], p["Y"]],
                           first_layer_print_size=[p["I"], p["J"]], max_print_z=zmax,
                           first_filaments=first_phys)
        ours = tp.start_block(env)
        n = len(ours.split("\n")) - 1
        self.record("start", ours, tp.strip_inserted(L[i:i + n + 10])[:n], i)
        # The initial filament's start text belongs to the start block.
        start_fs = next(k for k, y in self.events if k > j and y.startswith("; filament start gcode"))

        # Pass 2: blocks.
        active = None
        layer, z, zmax_now, tc = -1, 0.0, 0.0, 0
        nozzles_loaded = {tp.nozzle_of(base, init)}
        for i, x in self.events:
            if x.startswith("; CHANGE_LAYER"):
                layer += 1
            elif x.startswith("; Z_HEIGHT:"):
                z = float(x.split(":")[1])
                zmax_now = max(zmax_now, z)
            elif x.startswith("======== X2D filament_change gcode"):
                tc += 1
                k0 = i - 1
                while not L[k0].startswith("; filament end gcode"):
                    k0 -= 1
                blk = L[i:i + 120]
                nxt = next(int(T_RE.match(y).group(1)) for y in blk if T_RE.match(y))
                interface = any(y.startswith("M620.13") for y in blk[:80])
                env = tp.change_env(base, current_filament_id=active, next_filament_id=nxt,
                                    layer_num=layer, layer_z=z, max_layer_z=zmax_now,
                                    toolchange_count=tc, flush_length=flush_length_of(blk),
                                    z_after_toolchange=self.last_before(k0, Z_RE) or z,
                                    is_prime_tower_interface=interface)
                ours, _ = tp.change_block(env, self.last_before(k0, F_RE))
                n = len(ours.split("\n")) - 1
                theirs = tp.strip_inserted(L[k0:k0 + n + 40])[:n]
                kind = ("switch" if tp.nozzle_of(base, active) != tp.nozzle_of(base, nxt) else "ams")
                kind += "+flush" if env["flush_length"] > 0 else ""
                kind += "+first_layer" if layer == 0 else ""
                kind += "+first_load" if tp.nozzle_of(base, nxt) not in nozzles_loaded else ""
                nozzles_loaded.add(tp.nozzle_of(base, nxt))
                kind += "+interface" if interface else ""
                ft = base["filament_type"]
                kind += f"+{ft[active]}>{ft[nxt]}" if ft[active] != ft[nxt] else ""
                self.kinds[kind] = self.kinds.get(kind, 0) + 1
                self.record("change", ours, theirs, i, {"cur": active, "nxt": nxt, "layer": layer})
            elif x.startswith(";======== X2D layer_change gcode"):
                j = next(k for k in range(i, len(L)) if "notify layer change" in L[k])
                env = tp.layer_change_env(base, layer_num=layer, layer_z=z, max_layer_z=zmax_now,
                                          current_filament_id=active)
                ours = tp.layer_change_block(env)
                self.record("layer_change", ours, tp.strip_inserted(L[i:j + 2]), i)
            elif x.startswith(";======== X2D timelapse gcode"):
                j = next(k for k in range(i, len(L)) if L[k].startswith("; SKIPPABLE_END"))
                blk = L[i:j]
                safe = None
                for y in blk:
                    m = re.search(r"M9711 .* U(\S+) V(\S+)", y)
                    if m:
                        safe = (float(m.group(1)), float(m.group(2)))
                env = tp.timelapse_env(
                    base, layer_num=layer, layer_z=z, max_layer_z=zmax_now,
                    curr_physical_extruder_id=tp.physical_extruder(base, tp.nozzle_of(base, active)),
                    most_used_physical_extruder_id=most_used, has_timelapse_safe_pos=safe is not None,
                    timelapse_pos_x=safe[0] if safe else 0, timelapse_pos_y=safe[1] if safe else 0,
                    timelapse_inline_photo=any(y.strip().startswith("M971 ") for y in blk))
                ours = tp.timelapse_block(env)
                self.record("timelapse", ours, tp.strip_inserted(L[i:j + 2]), i)
            elif x.startswith("; filament start gcode") and i > start_fs:
                ours = tp.filament_start_block(tp.filament_start_end_env(base, filament_id=active))
                self.record("filament_start", "\n" + ours, L[i - 1:i + 1], i)  # blank line first
            elif x.startswith("; MACHINE_END_GCODE_START"):
                j = next(k for k in range(i, len(L)) if L[k].startswith("M73 P100"))
                env = tp.end_env(base, max_layer_z=zmax_now, current_filament_id=active,
                                 layer_num=layer, layer_z=z)
                ours = tp.end_block(env)
                self.record("end", ours, tp.strip_inserted(L[i - 1:j]), i)
            else:
                m = T_RE.match(x)
                if m and int(m.group(1)) < 255:
                    active = int(m.group(1))
        return self


@cache
def scan(case_id: str) -> Scan:
    assert TRUTH_DIR is not None
    return Scan(case_id, TRUTH_DIR).run()


TRUTH_CASES = sorted(c["id"] for c in CASES)


@needs_truth
@pytest.mark.parametrize("case_id", TRUTH_CASES)
def test_templates_match_bambu(case_id: str):
    t0 = time.perf_counter()
    s = scan(case_id)
    dt = time.perf_counter() - t0
    summary = {k: f"{v[1]}/{v[0]} lines, {v[2]} exact" for k, v in s.results.items()}
    print(f"{case_id} ({dt:.2f} s): {summary} kinds {s.kinds}")
    assert s.results.get("start", [0])[0] == 1 and s.results.get("end", [0])[0] == 1
    assert not s.diffs, s.diffs[:5]
    for kind, (n, same, exact) in s.results.items():
        assert same == n == exact, (kind, n, same, exact)


@needs_truth
def test_change_kinds_covered():
    kinds: dict[str, int] = {}
    for cid in ("keychain_4c_020", "swatch_card_5c_020", "nameplate_5c_020", "frenchie_paint_3c_020"):
        for k, v in scan(cid).kinds.items():
            kinds[k] = kinds.get(k, 0) + v
    def seen(*parts: str) -> int:
        return sum(v for k, v in kinds.items() if all(p in k.split("+") for p in parts))

    assert seen("switch") and seen("ams", "flush") and seen("switch", "flush"), kinds
    assert seen("first_load") and seen("first_layer") and seen("interface"), kinds
    assert seen("PETG>PLA") and seen("PLA>PETG"), kinds
