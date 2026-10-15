"""Nozzle map, per-layer filament order and change plan against Bambu's calibration slices
(grouping.py, purge.py; spec sections 7.1 to 7.5).

Truth per case: truth.json `sequence` (filament_maps, tools, layer_filament_lists) and `totals`,
plus Bambu's plate_1.gcode read here for the per-change flush blocks, SYNC seconds, nozzle-switch
markers, the sparse infill and support printed around each change, and the fill paths.
"""

from __future__ import annotations

import math
import os
import re
from functools import cache
from pathlib import Path

import numpy as np
import pytest
from p2p_slicer.config import load_project_config, slice_config
from p2p_slicer.grouping import (
    NozzleOrderer,
    fake_trays,
    filament_map,
    fill_seconds_from_paths,
    layer_orders,
    score_maps,
)
from p2p_slicer.moves import Role
from p2p_slicer.purge import change_counts, plan_changes, sync_seconds, tool_sequence
from slicer_testutil import CASE_IDS, CASES, REPO, TRUTH, TRUTH_DIR, needs_truth

MAIN = Path(os.environ.get("P2P_MAIN_CHECKOUT", str(REPO)))
CASE_BY_ID = {c["id"]: c for c in CASES}
FILL_ROLES = {"Sparse infill": Role.SPARSE_INFILL, "Internal solid infill": Role.INTERNAL_SOLID,
              "Top surface": Role.TOP_SURFACE, "Bottom surface": Role.BOTTOM_SURFACE,
              "Bridge": Role.BRIDGE, "Internal Bridge": Role.INTERNAL_BRIDGE}
ABSORB_ROLES = {"Sparse infill": "sparse", "Support": "support", "Support interface": "support",
                "Support transition": "support"}
_T = re.compile(r"^T(\d+)(\s|$)")
FLOW_TOL_MM = 0.5     # per change flush length tolerance, mm of filament (task acceptance)


# ----------------------------------------------------------------------------- truth helpers


def case_input(case: str) -> Path | None:
    rel = CASE_BY_ID[case]["input"]
    for base in (REPO, MAIN):
        p = base / rel
        if p.is_file():
            return p
    return None


def truth_layer_sets(case: str) -> list[list[int]]:
    out: list[list[int]] = []
    for e in TRUTH["cases"][case]["sequence"]["layer_filament_lists"]:
        fil = [int(x) + 1 for x in e["filament_list"].split()]
        for rng in e["layer_ranges"].split(","):
            a, b = (int(x) for x in rng.split())
            out.extend([] for _ in range(b + 1 - len(out)))
            for i in range(a, b + 1):
                out[i] = fil
    return out


def truth_map(case: str) -> list[int]:
    return [int(x) for x in TRUTH["cases"][case]["sequence"]["filament_maps"].split()]


@cache
def scan(case: str) -> dict:
    """Changes, sparse/support runs and fill paths from Bambu's G-code (one pass)."""
    assert TRUTH_DIR is not None
    text = (TRUTH_DIR / case / "plate_1.gcode").read_text(errors="replace")
    text = text[text.find("; EXECUTABLE_BLOCK_START"):]
    layer = -1
    slot = first = None
    changes: list[dict] = []
    runs: list[dict] = []
    fills: list[tuple[int, int, float, float, float, bool]] = []
    feature = ""
    width = height = 0.0
    x = y = 0.0
    in_flush = in_wipe = False
    own = True
    run = None
    nz_marker = False
    pending_l: float | None = None
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        c0 = s[0]
        if c0 == ";":
            if s.startswith("; CHANGE_LAYER"):
                layer += 1
                own, run = True, None
            elif s.startswith("; FEATURE:"):
                feature = s[10:].strip()
                run = None
            elif s.startswith("; LINE_WIDTH:"):
                width = float(s[13:])
            elif s.startswith("; LAYER_HEIGHT:"):
                height = float(s[15:])
            elif s == "; WIPE_START":
                in_wipe, run = True, None
            elif s == "; WIPE_END":
                in_wipe = False
            elif s.startswith("; NOZZLE_CHANGE_START"):
                nz_marker = True
            elif s == "; VFLUSH_START":
                in_flush = True
            elif s == "; VFLUSH_END":
                in_flush = False
            elif in_flush and s.startswith(";VG1"):
                changes[-1]["vflush"] += float(re.search(r"E(-?[\d.]+)", s).group(1))
            continue
        if c0 == "T":
            m = _T.match(s)
            if not m or int(m.group(1)) >= 255:
                continue
            new = int(m.group(1)) + 1
            if slot is None:
                slot = first = new
            elif new != slot:
                changes.append({"layer": layer, "old": slot, "new": new, "switch": nz_marker,
                                "vflush": 0.0, "sync": None, "L": pending_l})
                slot = new
                own, run = False, None
            nz_marker, pending_l = False, None
            continue
        if c0 == "M":
            if s.startswith("M620.10 A0") and " L" in s:
                pending_l = float(re.search(r" L(-?[\d.]+)", s).group(1))
            continue
        if c0 == "S":
            if s.startswith("SYNC T") and in_flush and changes[-1]["sync"] is None:
                changes[-1]["sync"] = float(s[6:].split()[0])
            continue
        if c0 != "G" or layer < 0 or s[1] not in "0123" or s[2] != " ":
            continue
        nx, ny, e, ci, cj = x, y, 0.0, 0.0, 0.0
        has_xy = False
        for tok in s.split()[1:]:
            k = tok[0]
            if k == "X":
                nx, has_xy = float(tok[1:]), True
            elif k == "Y":
                ny, has_xy = float(tok[1:]), True
            elif k == "E":
                e = float(tok[1:])
            elif k == "I":
                ci = float(tok[1:])
            elif k == "J":
                cj = float(tok[1:])
            elif k == ";":
                break
        if has_xy and e > 0 and not in_wipe:
            if feature in ABSORB_ROLES:
                if run is None:
                    run = {"layer": layer, "slot": slot, "kind": ABSORB_ROLES[feature], "e": 0.0,
                           "after": len(changes), "lead": not own}
                    runs.append(run)
                run["e"] += e
            elif feature != "Prime tower":
                own, run = True, None
            role = FILL_ROLES.get(feature)
            if role is not None:
                if s[1] in "23":
                    cx, cy = x + ci, y + cj
                    a0, a1 = math.atan2(y - cy, x - cx), math.atan2(ny - cy, nx - cx)
                    sweep = ((a0 - a1) if s[1] == "2" else (a1 - a0)) % (2 * math.pi)
                    length = math.hypot(ci, cj) * (sweep if sweep > 1e-9 else 2 * math.pi)
                else:
                    length = math.hypot(nx - x, ny - y)
                fills.append((slot, int(role), length, width, height, layer == 0))
        elif has_xy or e < 0:
            run = None
        x, y = nx, ny
    return {"first": first, "changes": changes, "runs": runs, "fills": fills,
            "layers": layer + 1}


def truth_orders(case: str) -> list[list[int]]:
    """Per layer print order: the slot carried into the layer (when it prints there) plus every
    slot changed to on the layer."""
    sc = scan(case)
    sets = truth_layer_sets(case)
    by_layer: dict[int, list[int]] = {}
    for c in sc["changes"]:
        by_layer.setdefault(c["layer"], []).append(c["new"])
    cur = sc["first"]
    out = []
    for i, ls in enumerate(sets):
        seq = [cur] + by_layer.get(i, [])
        if cur not in ls or len(seq) > len(ls):
            seq = seq[1:]
        out.append(seq)
        if by_layer.get(i):
            cur = by_layer[i][-1]
    return out


def observed_islands(case: str, cfg) -> list[dict[int, list[float]]]:
    """Sparse infill and support islands per layer as Bambu's G-code shows them. A run printed
    right after a change, before the new filament prints anything else, was flushed into:
    sparse runs go back to the filament printed before the change, support to key 0. Volumes are
    E x area / flow ratio of the printing filament (Bambu's absorbed volume has no flow ratio)."""
    sc = scan(case)
    out: list[dict[int, list[float]]] = [{} for _ in range(sc["layers"])]
    for r in sc["runs"]:
        vol = r["e"] * cfg.filament_area(r["slot"]) / cfg.fil("filament_flow_ratio", r["slot"], 1.0)
        if r["kind"] == "support":
            owner = 0
        elif r["lead"]:
            owner = sc["changes"][r["after"] - 1]["old"]
        else:
            owner = r["slot"]
        out[r["layer"]].setdefault(owner, []).append(vol)
    return out


@cache
def project(case: str) -> dict | None:
    p = case_input(case)
    return load_project_config(p) if p else None


def need_input(case: str) -> dict:
    proj = project(case)
    if proj is None:
        pytest.skip(f"input 3mf for {case} not on this machine")
    return proj


# ----------------------------------------------------------------------------- acceptance


@needs_truth
@pytest.mark.parametrize("case", CASE_IDS)
def test_filament_map_matches_bambu(case):
    proj = need_input(case)
    cfg = slice_config(proj)
    sets = truth_layer_sets(case)
    fs = fill_seconds_from_paths(scan(case)["fills"], cfg)
    assert filament_map(sets, cfg, fill_seconds=fs) == truth_map(case)
    # Fill time never decides a calibration case: the same map without it.
    assert filament_map(sets, cfg) == truth_map(case)


@needs_truth
@pytest.mark.parametrize("case", CASE_IDS)
def test_layer_orders_and_tool_sequence(case):
    cfg = slice_config(need_input(case), filament_map=truth_map(case))
    want = truth_orders(case)
    got = layer_orders(truth_layer_sets(case), truth_map(case), cfg, first_layer_order=want[0])
    bad = [i for i, (a, b) in enumerate(zip(got, want, strict=True)) if a != b]
    assert not bad, f"layers {bad[:5]}: ours {[got[i] for i in bad[:5]]}, " \
                    f"Bambu {[want[i] for i in bad[:5]]}"
    changes = plan_changes(got, truth_map(case), cfg)
    seq = TRUTH["cases"][case]["sequence"]
    assert tool_sequence(changes, got[0][0]) == seq["tools"]


@needs_truth
@pytest.mark.parametrize("case", CASE_IDS)
def test_changes_match_bambu(case):
    fmap = truth_map(case)
    cfg = slice_config(need_input(case), filament_map=fmap)
    sc = scan(case)
    orders = truth_orders(case)
    changes = plan_changes(orders, fmap, cfg, first_slot=sc["first"],
                           sparse_islands=observed_islands(case, cfg))
    totals = TRUTH["cases"][case]["totals"]
    assert change_counts(changes) == {
        "filament_changes": totals["filament_changes"],
        "ams_swaps": totals["filament_change_times_result_json"],
        "nozzle_switches": totals["nozzle_changes"]}
    assert len(changes) == len(sc["changes"])
    for ours, bb in zip(changes, sc["changes"], strict=True):
        where = f"layer {ours.layer} {ours.old}->{ours.new}"
        assert (ours.layer, ours.old, ours.new) == (bb["layer"], bb["old"], bb["new"]), where
        assert ours.nozzle_switch == bb["switch"], where
        assert ours.flush_length_mm == pytest.approx(bb["vflush"], abs=FLOW_TOL_MM), where
        assert ours.sync_s == bb["sync"], where
        assert bb["L"] == pytest.approx(bb["vflush"], abs=0.002), where
        assert ours.flush_mm3 == 0 or ours.chute_mm3 >= 100.0
    # Without islands every flush goes to the chute: an upper bound, never under Bambu.
    plain = plan_changes(orders, fmap, cfg, first_slot=sc["first"])
    assert all(p.flush_length_mm >= bb["vflush"] - 0.01
               for p, bb in zip(plain, sc["changes"], strict=True))


@needs_truth
@pytest.mark.parametrize("case", CASE_IDS)
def test_purge_billing_matches_result_json(case):
    """Per filament, result.json purge grams minus the G-code's flush and tower grams is the
    92 mm3 nozzle-volume billing of every AMS swap (to the filament that left the nozzle)."""
    fmap = truth_map(case)
    cfg = slice_config(need_input(case), filament_map=fmap)
    sc = scan(case)
    changes = plan_changes(truth_orders(case), fmap, cfg, first_slot=sc["first"],
                           sparse_islands=observed_islands(case, cfg))
    moved: dict[int, float] = {}
    for c in changes:
        for s, v in c.billed_mm3.items():
            moved[s] = moved.get(s, 0.0) + v
        if c.chute_mm3:
            moved[c.new] = moved.get(c.new, 0.0) - c.chute_mm3
    for s, f in TRUTH["cases"][case]["filaments"].items():
        residual = f["purge_g"] - f["gcode_flush_g"] - f["gcode_tower_g"]
        grams = moved.get(int(s), 0.0) * cfg.fil("filament_density", int(s), 1.24) / 1000
        assert grams == pytest.approx(residual, abs=0.003), s


# ----------------------------------------------------------------------------- units


def _cfg(n: int = 3, colours: list[str] | None = None, types: list[str] | None = None,
         matrix: list[list[float]] | None = None, **extra):
    """A minimal SliceConfig from a stand-in project."""
    m = matrix or [[0 if a == b else 100 * (a + 1) + 10 * (b + 1) for b in range(n)]
                   for a in range(n)]
    flat = [str(v) for row in m for v in row] * 2
    proj = {"filament_colour": colours or ["#000000", "#FFFFFF", "#FF0000", "#00FF00", "#0000FF"][:n],
            "filament_type": types or ["PLA"] * n, "flush_volumes_matrix": flat,
            "flush_multiplier": ["1", "1"], "filament_map": ["1"] * n,
            "filament_diameter": ["1.75"] * n, "nozzle_volume": ["92", "92"],
            "filament_change_length": ["10"] * n, "prime_volume_mode": "Saving",
            "flush_into_infill": "1", "flush_into_support": "1",
            "machine_load_filament_time": "29", "machine_unload_filament_time": "29"}
    proj.update(extra)
    return slice_config(proj)


def test_order_permutation_ties_go_to_fewer_changes_then_lexicographic():
    m = np.array([[0, 10, 10], [10, 0, 10], [10, 10, 0]], dtype=float)
    o = NozzleOrderer(m)
    # From 2: start with 2 (no change), then 1 before 3 by id.
    assert o.order(2, [1, 2, 3], []) == (2, 1, 3)
    # Look-ahead: the next layer is [3] only, so finish on 3.
    assert o.order(None, [1, 2, 3], [3]) == (1, 2, 3)
    # Asymmetric flush: 1 -> 2 cheap, 2 -> 1 dear.
    m2 = np.array([[0, 1, 50], [50, 0, 1], [1, 50, 0]], dtype=float)
    assert NozzleOrderer(m2).order(None, [1, 2, 3], []) == (1, 2, 3)
    assert NozzleOrderer(m2).order(3, [1, 2], []) == (1, 2)


def test_layer_order_prints_the_nozzle_that_finished_last_first():
    cfg = _cfg(3)
    orders = layer_orders([[1, 3], [1, 3], [1, 3]], [1, 1, 2], cfg, first_layer_order=[1, 3])
    assert orders == [[1, 3], [3, 1], [1, 3]]


def test_plan_changes_kinds_flush_and_billing():
    m = [[0, 300, 200], [150, 0, 250], [120, 130, 0]]
    cfg = _cfg(3, matrix=m)
    fmap = [1, 1, 2]
    orders = [[1, 3], [3, 1, 2], [2]]
    ch = plan_changes(orders, fmap, cfg)
    assert [(c.old, c.new) for c in ch] == [(1, 3), (3, 1), (1, 2)]
    first, back, swap = ch
    assert first.nozzle_switch and first.first_load and not first.ams_swap
    assert first.flush_mm3 == 0 and first.flush_length_mm == 0 and first.sync_s == 0
    assert first.ramming_mm == 10 and first.prime_mm3 == 15
    assert back.nozzle_switch and not back.ams_swap and back.flush_mm3 == 0
    assert swap.ams_swap and not swap.nozzle_switch and swap.held_old == 1
    assert swap.flush_mm3 == 300 and swap.chute_mm3 == 300
    assert swap.flush_length_mm == pytest.approx(300 / (math.pi / 4 * 1.75**2))
    assert swap.sync_s == 5 * math.ceil(swap.flush_length_mm / 125)
    assert swap.billed_mm3 == {1: 92.0, 2: pytest.approx(208.0)}
    assert tool_sequence(ch, 1) == [1, 3, 1, 2]
    assert change_counts(ch) == {"filament_changes": 3, "ams_swaps": 1, "nozzle_switches": 2}


def test_flush_into_infill_takes_whole_islands_of_earlier_filaments():
    cfg = _cfg(3, matrix=[[0, 300, 300], [300, 0, 300], [300, 300, 0]])
    fmap = [1, 1, 1]
    orders = [[1], [1, 2, 3]]
    islands = [{}, {1: [120.0, 150.0, 40.0], 3: [500.0], 0: [30.0]}]
    ch = plan_changes(orders, fmap, cfg, sparse_islands=islands)
    a, b = ch
    # 1 -> 2: slot 1's islands until 300 is used (the second one overshoots), then stop.
    assert a.absorbed_islands == [(1, 0), (1, 1), (1, 2)] and a.absorbed_mm3 == 310
    assert a.chute_mm3 == 100  # never under the chute minimum
    # 2 -> 3: slot 3's own infill is not eligible (3 prints after); support is.
    assert b.absorbed_islands == [(0, 0)] and b.absorbed_mm3 == 30 and b.chute_mm3 == 270
    no_infill = _cfg(3, matrix=[[0, 300, 300], [300, 0, 300], [300, 300, 0]],
                     flush_into_infill="0", flush_into_support="0")
    assert [c.chute_mm3 for c in plan_changes(orders, fmap, no_infill,
                                              sparse_islands=islands)] == [300, 300]


def test_sync_seconds_rounds_like_the_gcode():
    assert sync_seconds(0.0) == 0
    assert sync_seconds(41.575) == 5
    assert sync_seconds(125.0004) == 5
    assert sync_seconds(125.01) == 10


def test_fake_trays_follow_the_cli():
    assert fake_trays(_cfg(5)) == [[1, 2, 3, 4], [5, 1, 2, 3]]
    assert fake_trays(_cfg(3)) == [[1, 2, 3, 1], [2, 3, 1, 2]]


def test_colour_match_breaks_a_mirror_tie():
    # Two filaments on every layer: one per nozzle scores best, and both ways score the same.
    # The lowest mask puts slot 1 right; with 5 slots, slot 5's colour only sits in the right
    # nozzle's fake trays, so the CLI's colour match puts slot 5 right instead.
    cfg = _cfg(5, colours=["#101010", "#7C4B00", "#F72323", "#0D6284", "#FFFFFF"],
               types=["PLA", "PLA", "PLA", "PETG", "PLA"])
    sets = [[1, 5]] * 4
    scores, used = score_maps(sets, cfg)
    assert used == [1, 5]
    assert scores[1].score == scores[2].score < scores[0].score
    assert filament_map(sets, cfg, cli_fake_ams=False) == [2, 1, 1, 1, 1]
    assert filament_map(sets, cfg) == [1, 1, 1, 1, 2]


def test_unused_filaments_map_left_and_single_filament():
    cfg = _cfg(3)
    assert filament_map([[2]] * 3, cfg) == [1, 1, 1]
    assert filament_map([], cfg) == [1, 1, 1]
