import numpy as np
import pytest
from p2p_filaments.colour import lab_to_linear
from p2p_filaments.layout import STEPS_MM
from p2p_filaments.wedge import estimate, fit_lambda, observed_bound, td_from_model, wedge_colour

T = np.array(STEPS_MM)
BLACK = lab_to_linear([22.0, 0.5, -0.8])
WHITE = lab_to_linear([91.5, -0.6, 2.5])


def _wedges(solid, lam, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    ob = wedge_colour(T[:, None], solid, BLACK, lam) + rng.normal(0, noise, (len(T), 3))
    ow = wedge_colour(T[:, None], solid, WHITE, lam) + rng.normal(0, noise, (len(T), 3))
    return ob, ow


@pytest.mark.parametrize("lab, lam", [
    ([46.0, 58.0, 38.0], [0.80, 0.18, 0.20]),  # red: red light goes deep
    ([39.0, -9.0, -27.0], [0.20, 0.30, 0.45]),  # blue
    ([38.0, 14.0, 30.0], [0.22, 0.16, 0.14]),  # brown
    ([91.5, -0.6, 2.5], [0.55, 0.55, 0.50]),  # white over black is the hard case
])
def test_fit_recovers_decay_lengths_and_td(lab, lam):
    solid = lab_to_linear(lab)
    lam = np.array(lam)
    ob, ow = _wedges(solid, lam, noise=0.002)
    got, rms = fit_lambda(T, ob, ow, solid, BLACK, WHITE)
    np.testing.assert_allclose(got, lam, rtol=0.08)
    assert np.all(rms < 0.005)
    td_true, _ = td_from_model(lam, solid, BLACK, WHITE)
    est = estimate(T, ob, ow, solid, BLACK, WHITE)
    assert est["td_mm"] == pytest.approx(td_true, abs=max(0.05, 0.08 * td_true))
    assert est["fit_de2000_mean"] < 1.0


def test_black_filament_has_tiny_td():
    ob, ow = _wedges(BLACK, np.array([0.04, 0.04, 0.04]))
    est = estimate(T, ob, ow, BLACK, BLACK, WHITE)
    assert est["td_mm"] < 0.25
    assert est["td_over_black_mm"] == 0.0  # black over black never shows its base
    assert est["observed_within_mm"] == 0.2
    assert not est["extrapolated"]


def test_translucent_filament_is_flagged_as_extrapolated():
    solid = lab_to_linear([80.0, -20.0, 60.0])  # a pale translucent yellow-green
    ob, ow = _wedges(solid, np.array([1.5, 1.5, 1.2]))
    est = estimate(T, ob, ow, solid, BLACK, WHITE)
    assert est["td_mm"] > max(STEPS_MM) and est["extrapolated"]
    assert est["observed_within_mm"] is None


def test_td_threshold_moves_td():
    solid = lab_to_linear([46.0, 58.0, 38.0])
    lam = np.array([0.8, 0.18, 0.2])
    loose, _ = td_from_model(lam, solid, BLACK, WHITE, threshold=5.0)
    strict, _ = td_from_model(lam, solid, BLACK, WHITE, threshold=1.0)
    assert loose < strict


def test_observed_bound_needs_all_thicker_steps_within():
    solid = np.array([50.0, 0.0, 0.0])
    b = np.array([[40, 0, 0], [49.5, 0, 0], [45, 0, 0], [49.8, 0, 0], [50, 0, 0]], float)
    assert observed_bound(T, b, b, solid, 2.0) == 0.8
