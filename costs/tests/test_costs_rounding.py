from __future__ import annotations

import pytest
from p2p_costs.model import median, quartile, round_price, xround


@pytest.mark.parametrize("x,ending,want", [
    (12.01, 0.99, 12.99), (12.99, 0.99, 12.99), (13.0, 0.99, 13.99),
    (12.991, 0.99, 12.99), (12.996, 0.99, 13.99), (0.99 + 11.999999999999998, 0.99, 12.99),
    (24.5, 0.99, 24.99), (12.2, 0.0, 13.0), (13.0, 0.0, 13.0), (13.0000001, 0.0, 13.0), (13.01, 0.0, 14.0),
    (12.0 + 0.1 + 0.2, 0.0, 13.0), (7.49, 0.49, 7.49), (7.5, 0.49, 8.49),
])
def test_round_price(x, ending, want):
    assert round_price(x, ending) == pytest.approx(want, abs=1e-9)


def test_round_price_float_noise():
    # 12.99 computed with float noise must stay 12.99, not jump to 13.99
    assert round_price(12.99 + 1e-12, 0.99) == pytest.approx(12.99)
    assert round_price(13.0 - 1e-12, 0.0) == pytest.approx(13.0)


def test_excel_round_half_away_from_zero():
    assert xround(2.5) == 3.0
    assert xround(-2.5) == -3.0
    assert xround(1.005, 2) == 1.01  # Python's round gives 1.0


def test_quartiles_match_excel_inclusive():
    vals = [10, 20, 30, 40, 50]
    assert quartile(vals, 1) == 20
    assert quartile([1, 2, 3, 4], 1) == pytest.approx(1.75)
    assert median([1, 2, 3, 4]) == 2.5
    assert quartile([], 1) == 0.0 and median([]) == 0.0
