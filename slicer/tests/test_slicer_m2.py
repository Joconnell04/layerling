"""M2: the planner and emitter.

Unit checks on small synthetic shapes (layers, sections, walls, fills), then end to end on the
calibration keychains against truth.json: time and grams within the M3 targets, the same slots,
filament maps, change counts and layer lists, and our G-code read back gives the same numbers.
"""

import json
import math
import subprocess
import sys

import numpy as np
import pytest
from p2p_slicer import geom as G
from p2p_slicer.infill import hatch, solid_fill, sparse_grid
from p2p_slicer.layers import plan_layers
from p2p_slicer.section import loop_area, section_mesh
from p2p_slicer.walls import make_walls, spacing
from slicer_testutil import CAL, CASES, REPO, TRUTH

INPUTS = {c["id"]: REPO / c["input"] for c in CASES}


def _square(side_mm: float, cx: float = 0.0, cy: float = 0.0) -> list[np.ndarray]:
    h = side_mm / 2
    p = np.array([[cx - h, cy - h], [cx + h, cy - h], [cx + h, cy + h], [cx - h, cy + h]])
    return [G.to_int(p)]


def _cube(side: float) -> tuple[np.ndarray, np.ndarray]:
    v = np.array([[x, y, z] for z in (0, side) for y in (0, side) for x in (0, side)],
                 dtype=np.float64)
    f = np.array([[0, 2, 1], [1, 2, 3], [4, 5, 6], [5, 7, 6], [0, 1, 4], [1, 5, 4],
                  [2, 6, 3], [3, 6, 7], [0, 4, 2], [2, 4, 6], [1, 3, 5], [3, 7, 5]],
                 dtype=np.int64)
    return v, f


# -- units


def test_layer_plan_mid_plane_rule():
    lp = plan_layers(5.0, 0.2, 0.2)
    assert len(lp.print_z) == 25
    assert lp.print_z[-1] == pytest.approx(5.0)
    assert lp.slice_z[0] == pytest.approx(0.1)
    # a top that ends 0.09 mm into a layer drops it; 0.11 mm keeps it
    assert len(plan_layers(5.09, 0.2, 0.2).print_z) == 25
    assert len(plan_layers(5.11, 0.2, 0.2).print_z) == 26


def test_section_of_a_cube_is_its_square():
    v, f = _cube(10.0)
    layers = section_mesh(v, f, np.array([0.1, 5.0, 9.9]))
    for ll in layers:
        assert len(ll.loops) == 1
        assert abs(loop_area(ll.loops[0])) == pytest.approx(100.0, rel=1e-6)


def test_walls_step_inwards_by_spacing():
    w, h = 0.42, 0.2
    walls = make_walls(_square(20.0), loops=2, w_outer=w, w_inner=w, h=h)
    assert len(walls.loops) == 2
    s = spacing(w, h)
    sides = sorted(math.sqrt(abs(G.area(lp.path))) for lp in walls.loops)
    # outer wall centre at w/2 in, the inner one a further spacing in
    assert sides[1] == pytest.approx(20.0 - w, abs=0.01)
    assert sides[0] == pytest.approx(20.0 - w - 2 * s, abs=0.01)
    assert walls.infill_area


def test_solid_fill_volume_matches_area():
    w, h = 0.42, 0.2
    area = _square(10.0)
    # monotonic: links between lines are travels, so the extruded length is the lines alone
    chains = solid_fill(area, w, h, 45.0, monotonic=True)
    length = sum(c.length_mm for c in chains)
    pitch = chains[0].spacing
    assert length * pitch == pytest.approx(100.0, rel=0.03)


def test_sparse_grid_density():
    w, h, d = 0.45, 0.2, 0.15
    area = _square(30.0)
    s = spacing(w, h)
    pitch = s / (d / 2)
    lines = 0.0
    for ang in (45.0, 135.0):
        segs, _, _ = hatch(area, ang, pitch, origin=(0, 0))
        lines += sum(G.perimeter(p, closed=False) for p in segs)
    # two sweeps at pitch s / (d / 2): line length x spacing ~ d x area
    assert lines * s == pytest.approx(d * 900.0, rel=0.05)
    # the chains add the links along the edge on top of the lines
    chains = sparse_grid(area, w, h, d, (0, 0))
    assert sum(c.length_mm for c in chains) >= lines


# -- end to end


def _slice(case, **kw):
    from p2p_slicer.api import slice_project
    return slice_project(INPUTS[case], **kw)


def _check_counts(res, case):
    tr = TRUTH["cases"][case]
    tt = tr["totals"]
    u = res.analysis.usage
    assert u.slots_used == sorted(tt["filaments_used"])
    assert " ".join(map(str, res.analysis.filament_maps)) == tr["sequence"]["filament_maps"]
    assert u.filament_changes == tt["filament_changes"]
    assert u.ams_swaps == tt["filament_change_times_result_json"]
    assert u.nozzle_switches == tt["nozzle_changes"]
    assert res.analysis.layer_count == tt["layer_count"]


@pytest.mark.skipif(not INPUTS["keychain_1c_020"].exists(), reason="calibration input missing")
def test_keychain_single_colour_end_to_end(tmp_path):
    case = "keychain_1c_020"
    res = _slice(case, out_dir=tmp_path, gcode=True)
    tt = TRUTH["cases"][case]["totals"]
    assert res.analysis.time.total_s == pytest.approx(tt["print_time_s"], rel=0.05)
    assert res.analysis.usage.total_g == pytest.approx(tt["total_g"], rel=0.03)
    _check_counts(res, case)
    for name in ("slice_report.json", "result.json", "plate_1.gcode"):
        assert (tmp_path / name).exists()
    rep = json.loads((tmp_path / "slice_report.json").read_text())
    assert rep["slicer"] == "p2p_slicer"
    # the G-code we wrote reads back to the same time and grams
    from p2p_slicer.report import analyze_gcode
    back = analyze_gcode(tmp_path / "plate_1.gcode")
    assert back.time.total_s == pytest.approx(res.analysis.time.total_s, rel=1e-3)
    assert back.usage.total_g == pytest.approx(res.analysis.usage.total_g, rel=1e-3)


@pytest.mark.skipif(not INPUTS["keychain_4c_020"].exists(), reason="calibration input missing")
def test_keychain_four_colour_changes_and_layers():
    case = "keychain_4c_020"
    res = _slice(case)
    _check_counts(res, case)
    from p2p_slicer.report import layer_filament_lists
    want = TRUTH["cases"][case]["sequence"]["layer_filament_lists"]
    key = lambda d: int(d["layer_ranges"].split()[0])
    assert sorted(layer_filament_lists(res.analysis), key=key) == sorted(want, key=key)
    tt = TRUTH["cases"][case]["totals"]
    assert res.analysis.usage.bucket_g("model") == pytest.approx(tt["model_g"], rel=0.04)


def test_cli_slice_writes_report(tmp_path):
    inp = INPUTS["keychain_1c_020"]
    if not inp.exists():
        pytest.skip("calibration input missing")
    r = subprocess.run([sys.executable, "-m", "p2p_slicer", "slice", str(inp), "--out",
                        str(tmp_path)], capture_output=True, text=True, timeout=300, check=False)
    assert r.returncode == 0, r.stderr[-2000:]
    rep = json.loads((tmp_path / "slice_report.json").read_text())
    assert rep["ok"] is True
    assert CAL.exists()
