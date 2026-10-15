"""Two-nozzle purge model (X2D) and its calibration against Bambu Studio slices."""

import json
from pathlib import Path

import numpy as np
import pytest
from p2p_contracts.reports import SimParams
from p2p_sim.flush import flush_matrix
from p2p_sim.simulate import (
    DENSITY_G_MM3,
    FLUSH_CALIBRATION,
    TOWER_G_PER_CHANGE,
    TOWER_G_PER_LAYER,
    assign_nozzles,
    sequence_nozzles,
    simulate_print_detailed,
)

CAL = json.loads((Path(__file__).parent / "fixtures" / "purge_calibration.json").read_text())


def _presence(layers, k):
    p = np.zeros((len(layers), k), bool)
    for i, row in enumerate(layers):
        p[i, row] = True
    return p


def _estimate(case):
    rgb = np.array(case["palette_rgb"], np.uint8)
    flush = flush_matrix(rgb, np.array(case["palette_lab"]))
    plan = assign_nozzles(_presence(case["layers"], len(rgb)), flush, 2)
    tower = TOWER_G_PER_LAYER * plan.last_change_layer + TOWER_G_PER_CHANGE * plan.changes
    return plan, tower + plan.flush_mm3 * FLUSH_CALIBRATION * DENSITY_G_MM3


def test_two_filaments_on_two_nozzles_never_flush():
    p = np.array([[True, True]] * 10)
    flush = np.array([[0.0, 300.0], [200.0, 0.0]])
    plan = assign_nozzles(p, flush, 2)
    assert sorted(plan.nozzle_of.tolist()) == [0, 1]
    assert plan.flush_mm3 == 0.0 and plan.same_nozzle == 0
    assert plan.changes == 10 and plan.nozzle_changes == 10


def test_the_split_keeps_the_most_swapped_pair_apart():
    # slots 1 and 2 share every layer; slot 3 appears in a few
    p = np.array([[True, True, False]] * 20 + [[True, True, True]] * 3)
    flush = np.full((3, 3), 200.0) - np.diag([200.0] * 3)
    plan = assign_nozzles(p, flush, 2)
    assert plan.nozzle_of[0] != plan.nozzle_of[1]
    one = sequence_nozzles(p, flush, [0, 0, 0])
    assert plan.flush_mm3 < one.flush_mm3


@pytest.mark.parametrize("name", list(CAL["cases"]))
def test_nozzle_split_matches_bambu(name):
    case = CAL["cases"][name]
    plan, _ = _estimate(case)
    bambu = np.array(case["slicer"]["filament_maps"]) - 1
    assert (plan.nozzle_of == bambu).all() or (plan.nozzle_of == 1 - bambu).all()


@pytest.mark.parametrize(("name", "tolerance"),
                         [("bronco_a2", 0.05), ("proto", 0.10), ("frenchie_a3", 0.75)])
def test_purge_estimate_against_the_slicer(name, tolerance):
    """SOURCE: the slicer's purge on our own files; the old one-nozzle model was 54 %, 262 %
    and 316 % over on these three."""
    case = CAL["cases"][name]
    _, est = _estimate(case)
    true = case["slicer"]["purge_g"]
    assert abs(est - true) / true <= tolerance, (est, true)


def test_single_filament_has_no_tower():
    from p2p_sim.fixtures import sphere_mesh

    m = sphere_mesh(8, 0.3)
    res = simulate_print_detailed(m, np.ones(m.n_faces, np.int64), np.array([[50.0, 0, 0]]),
                                  SimParams())
    assert res.report.purge_g_est == 0.0 and res.tower_g == 0.0


def test_purge_estimator_matches_layer_presence_and_the_simulator():
    """The fast estimator (one span table, many palettes) gives the simulator's layer presence
    exactly and its purge within a few percent (it skips erosion and bleed)."""
    from p2p_sim.fixtures import make_figurine
    from p2p_sim.simulate import PurgeEstimator, layer_presence

    fx = make_figurine(20_000, texture=False)
    params = SimParams()
    slots = fx.face_slots.astype(np.int64)
    k = len(fx.palette_hex)
    est = PurgeEstimator(fx.mesh, params)
    from p2p_sim.simulate import presence_from_spans

    assert np.array_equal(presence_from_spans(est.spans, slots, k),
                          layer_presence(fx.mesh, slots, k, params))
    out = est.estimate(slots, fx.palette_rgb, fx.palette_labs)
    sim = simulate_print_detailed(fx.mesh, slots, fx.palette_labs, params)
    assert out["purge_g"] == pytest.approx(sim.report.purge_g_est, rel=0.15)
    # fewer colors, less purge
    two = np.where(slots == 1, 1, 2)
    assert est.estimate(two, fx.palette_rgb[:2], fx.palette_labs[:2])["purge_g"] < out["purge_g"]
