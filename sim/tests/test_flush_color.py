import numpy as np
import pytest
from p2p_sim.color import ciede2000, lab_to_srgb, srgb_to_lab
from p2p_sim.flush import (
    MAX_FLUSH_MM3,
    X2D_MIN_FLUSH_MM3,
    flush_formula_mm3,
    flush_matrix,
    flush_volume_mm3,
    load_flush_table,
)


def test_lab_round_trip_and_known_values():
    rgb = np.array([[255, 255, 255], [0, 0, 0], [255, 0, 0], [18, 120, 200]], np.uint8)
    lab = srgb_to_lab(rgb)
    assert np.allclose(lab[0], [100, 0, 0], atol=0.05)
    assert np.allclose(lab[1], [0, 0, 0], atol=0.05)
    assert np.allclose(lab[2], [53.24, 80.09, 67.20], atol=0.05)
    assert np.array_equal(lab_to_srgb(lab), rgb)


def test_ciede2000_reference_pair():
    # Sharma et al. 2005 test data, pair 1
    assert ciede2000([50, 2.6772, -79.7751], [50, 0, -82.7485]) == pytest.approx(2.0425, abs=1e-4)
    assert ciede2000([50, 0, 0], [50, -1, 2]) == pytest.approx(2.3669, abs=1e-4)


def test_formula_charges_dark_to_light_more():
    black, white = (20, 20, 20), (245, 245, 245)
    up = flush_volume_mm3(black, white, use_table=False)
    down = flush_volume_mm3(white, black, use_table=False)
    assert up > 2 * down
    assert X2D_MIN_FLUSH_MM3 + 60 <= down <= MAX_FLUSH_MM3
    assert flush_formula_mm3(white, white) == 60.0  # floor of the color term
    assert flush_volume_mm3(white, white) == 0.0


def test_installed_table_is_used_when_present():
    tab = load_flush_table()
    if tab is None:
        pytest.skip("Bambu Studio flush data not installed")
    assert flush_volume_mm3((0, 0, 0), (255, 255, 255)) == 900.0
    assert flush_volume_mm3((255, 255, 255), (0, 0, 0)) == 90.0


def test_flush_matrix_shape_and_weight():
    rgb = np.array([[20, 20, 20], [245, 245, 245], [200, 60, 50]], np.uint8)
    m2 = flush_matrix(rgb, dark_to_light_weight=2.0)
    m4 = flush_matrix(rgb, dark_to_light_weight=4.0)
    assert m2.shape == (3, 3) and np.all(np.diag(m2) == 0)
    assert m4[0, 1] == pytest.approx(2 * m2[0, 1])  # black -> white doubled
    assert m4[1, 0] == pytest.approx(m2[1, 0])  # white -> black unchanged
