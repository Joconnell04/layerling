"""Small G-code snippets through the reader, estimator and grams counter."""

import math

import pytest
from p2p_slicer.estimate import _trapezoid_time, estimate
from p2p_slicer.gcode_read import read_gcode
from p2p_slicer.moves import Flag, Kind
from p2p_slicer.report import analyze, cli_result_json, slice_report
from p2p_slicer.usage import usage

CFG = """; CONFIG_BLOCK_START
; filament_map = 1,2
; filament_density = 1.24,1.26
; filament_diameter = 1.75,1.75
; nozzle_volume = 92,92
; machine_load_filament_time = 29
; machine_unload_filament_time = 29
; machine_switch_extruder_time = 5
; filament_ids = GFL99;GFA00
; CONFIG_BLOCK_END
"""


def gc(body: str) -> str:
    return CFG + "; EXECUTABLE_BLOCK_START\n" + body


def test_trapezoid_from_rest():
    # 100 mm at 100 mm/s, 1000 mm/s2, from and to rest: two 5 mm ramps of 0.1 s, 90 mm cruise
    t = _trapezoid_time(100.0, 1000.0, 0.0, 100.0, 0.0)
    assert t == pytest.approx(0.1 + 0.9 + 0.1)
    # too short to cruise: triangle
    t = _trapezoid_time(1.0, 1000.0, 0.0, 100.0, 0.0)
    assert t == pytest.approx(2 * math.sqrt(1.0 / 1000.0))


def test_classification():
    mt = read_gcode(gc("""M83
G1 X10 Y0 F6000
G1 X20 E1
G1 E-.8 F1800
G1 E.8
G1 Z1 E.1
; WIPE_START
G1 X21 E-.5
; WIPE_END
G1 X30 Y5 E-.1
G2 X40 Y5 I5 J0 E.5
"""))
    kinds = [Kind(k) for k in mt.rows["kind"]]
    assert kinds == [Kind.TRAVEL, Kind.EXTRUDE, Kind.RETRACT, Kind.UNRETRACT, Kind.TRAVEL,
                     Kind.WIPE, Kind.TRAVEL, Kind.EXTRUDE]
    assert mt.rows["flags"][-1] & Flag.ARC_CW


def test_g91_relative_and_g28_home():
    mt = read_gcode(gc("G90\nG1 X10 Y10 Z5 F6000\nG91\nG1 Z2\nG90\nG28 Z\n"))
    assert list(mt.rows["z"]) == [5.0, 7.0, 0.0]
    assert list(mt.rows["x"]) == [10.0, 10.0, 10.0]


def test_dwell_waits_for_a_block_when_queue_is_empty():
    # a dwell with nothing queued is kept and added to the next block timed; one after the last
    # move is lost (Bambu drops the end G-code's long M400 S180 waits this way)
    mt = read_gcode(gc("M400 S5\nG1 X10 F600\nG4 S0\nM400 S100\n"))
    t = estimate(mt, mt.meta["config"])
    move_only = estimate(read_gcode(gc("G1 X10 F600\nG4 S0\n")), mt.meta["config"])
    assert t.total_s == pytest.approx(move_only.total_s + 5.0)


def test_tool_changes_and_flush_billing():
    body = """M83
T0
G1 X10 Y0 F6000
G1 X20 E1
T1
G1 X30 E1
T0
; VFLUSH_START
;VG1 E41.5 F299
;VG1 E20 F299
SYNC T5
; VFLUSH_END
G1 X40 E1
"""
    mt = read_gcode(gc(body))
    u = usage(mt)
    assert (u.filament_changes, u.nozzle_switches, u.ams_swaps) == (2, 2, 0)
    t = estimate(mt)
    # first load 29, two switches 5 + 5, SYNC 5 and the VG1 moves
    assert t.per_role_s["Flush"] > 29 + 10 + 5
    area = math.pi / 4 * 1.75 ** 2
    # T0 -> T1 -> T0 on two nozzles: nozzle 0 still holds slot 1, so the flush is billed to slot 1
    assert u.per_slot[1].mm3["flush"] == pytest.approx(61.5 * area)


def test_ams_swap_bills_first_92_mm3_to_the_old_filament():
    body = """M83
T0
G1 X10 Y0 F6000
G1 X20 E1
T1
; VFLUSH_START
;VG1 E41.5 F299
;VG1 E40 F299
; VFLUSH_END
G1 X30 E1
"""
    cfg = CFG.replace("filament_map = 1,2", "filament_map = 1,1")
    mt = read_gcode(cfg + body)
    u = usage(mt)
    assert u.ams_swaps == 1 and u.nozzle_switches == 0
    area = math.pi / 4 * 1.75 ** 2
    total = 81.5 * area
    assert u.per_slot[1].mm3["flush"] == pytest.approx(92.0)
    assert u.per_slot[2].mm3["flush"] == pytest.approx(total - 92.0)
    t = estimate(mt)
    assert t.load_s == {2: 29.0, 1: 29.0} or t.load_s[2] == 29.0
    assert t.unload_s[1] == 29.0


def test_reports_have_the_contract_fields():
    mt = read_gcode(gc("M83\nT0\nG1 X10 Y0 F6000\nG1 X20 E1\nT1\nG1 X30 E1\n"))
    a = analyze(mt)
    rep = slice_report(a, expected_slots=2)
    assert rep.slicer == "p2p_slicer" and rep.ok
    assert rep.slots_used == 2 and rep.nozzle_changes == 1 and rep.ams_swaps == 0
    rj = cli_result_json(a)
    plate = rj["sliced_plates"][0]
    assert plate["total_predication"] == pytest.approx(a.time.total_s)
    assert {f["id"] for f in plate["filaments"]} == {1, 2}
