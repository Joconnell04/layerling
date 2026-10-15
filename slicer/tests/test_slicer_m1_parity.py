"""M1: our reader, estimator and grams counter on Bambu's own G-code reproduce Bambu's numbers.

For every calibration case (slicer/calibration/truth.json; Bambu's G-code in _truth/, gitignored,
found through p2p_slicer.compare.find_truth_dir): total, main and per-feature time, grams per
filament (total and model), the three change counts, slots used, filament maps and layers.
"""

import pytest
from p2p_slicer.compare import bambu_reference
from p2p_slicer.report import analyze_gcode
from slicer_testutil import CASE_IDS, TRUTH, TRUTH_DIR, needs_truth

# Tolerances (ASSUMED, from where the estimator lands today; see slicer/README.md)
TOTAL_PCT = 0.05
FEATURE_S = 1.0
FEATURE_PCT = 0.25
GRAMS = 0.001


@needs_truth
@pytest.mark.parametrize("case", CASE_IDS)
def test_bambu_gcode_parity(case):
    folder = TRUTH_DIR / case
    a = analyze_gcode(folder / "plate_1.gcode")
    ref = bambu_reference(folder, analyze=False)
    res = ref["result"]
    t = a.time
    assert abs(t.total_s / res["total_predication"] - 1) * 100 < TOTAL_PCT
    assert abs(t.main_s - res["main_predication"]) < max(1.0, 0.001 * res["main_predication"])
    ours = t.feature_type_times()
    for k, v in res["feature_type_times"].items():
        assert abs(ours.get(k, 0.0) - v) <= max(FEATURE_S, FEATURE_PCT / 100 * v), k
    u = a.usage
    for f in res["filaments"]:
        fu = u.per_slot[f["id"]]
        assert fu.grams() == pytest.approx(f["total_used_g"], abs=GRAMS)
        assert fu.grams("model") + fu.grams("support") == pytest.approx(f["main_used_g"],
                                                                         abs=GRAMS)
    tt = TRUTH["cases"][case]["totals"]
    assert u.filament_changes == tt["filament_changes"]
    assert u.ams_swaps == tt["filament_change_times_result_json"] == res["filament_change_times"]
    assert u.nozzle_switches == tt["nozzle_changes"]
    assert u.slots_used == sorted(f["id"] for f in res["filaments"])
    assert a.layer_count == tt["layer_count"]
    seq = TRUTH["cases"][case]["sequence"]
    assert " ".join(map(str, a.filament_maps)) == seq["filament_maps"]
