"""Size tiers: build_product honors SizeTier and says so; tier rules flag small builds."""

import pytest
from geo_fixtures import PRODUCT_CASES
from p2p_contracts.job import SIZE_TIERS_MM, ProductType, SizeTier
from p2p_geometry import build_product, build_product_geometry
from p2p_geometry.product import resolve_size
from p2p_geometry.rules import MIN_FIGURE_HEIGHT_MM, printability_flags


def test_resolve_size():
    kc = ProductType.KEYCHAIN
    assert resolve_size(kc, None, "xs")[:2] == (40.0, SizeTier.XS)
    mm, tier, note = resolve_size(kc, 50.0, SizeTier.L)
    assert (mm, tier) == (60.0, SizeTier.L) and "ignored" in note
    assert resolve_size(kc, 45.0, None)[1] == SizeTier.S
    assert resolve_size(kc, 41.0, None)[1] is None
    assert resolve_size(kc, None, None)[:2] == (52.0, SizeTier.M)


@pytest.mark.parametrize("name", list(PRODUCT_CASES))
def test_tier_sets_the_measured_size(figure, library, tmp_path, name):
    product, addons, _ = PRODUCT_CASES[name]
    res = build_product(figure.vertices, figure.faces, product, addons, None, library, None,
                        tmp_path, size_tier="s")
    want = SIZE_TIERS_MM[product][SizeTier.S]
    size = [hi - lo for lo, hi in zip(res.bbox_min_mm, res.bbox_max_mm, strict=True)]
    if product == ProductType.FIGURINE:
        got = size[2]  # figurine tiers are heights
    elif product == ProductType.NAMEPLATE:
        got = size[0]  # plate width
    else:
        got = max(size)
    assert got == pytest.approx(want, abs=0.5)
    assert any(n.startswith("Size tier S") for n in res.notes)


def test_small_keychain_is_flagged_for_detail(figure, library):
    product, addons, _ = PRODUCT_CASES["keychain"]
    # Below every tier on purpose: a custom 30 mm keychain must still be flagged.
    small = build_product_geometry(figure.vertices, figure.faces, product, addons, 30.0, library,
                                   None)
    rules = {f.rule for f in printability_flags(small)}
    assert small.metrics["figure_height_mm"] < MIN_FIGURE_HEIGHT_MM
    assert "figure_detail" in rules
    big = build_product_geometry(figure.vertices, figure.faces, product, addons, None, library,
                                 None, size_tier="l")
    assert not printability_flags(big)


def test_prime_tower_moves_off_wide_objects_in_the_writer():
    """The writer path moves the tower by itself, for any product and size."""
    from p2p_threemf.writer import place_prime_tower

    def footprint(w, d):
        return (128 - w / 2, 128 - d / 2, 128 + w / 2, 128 + d / 2)

    assert place_prime_tower(footprint(52.0, 30.0))[2] is False  # keychain: default spot is clear
    assert place_prime_tower(footprint(150.0, 67.1))[2] is True  # the L nameplate covered it
    for w, d in ((150.0, 67.1), (98.0, 109.0), (120.0, 30.0), (120.0, 200.0)):
        x, y, _ = place_prime_tower(footprint(w, d))
        x0, y0, x1, y1 = footprint(w, d)
        assert x + 35 < x0 - 7 or x > x1 + 7 or y + 45 < y0 - 7 or y > y1 + 7
        assert 20.5 <= x and x + 35 <= 256 and 0 <= y and y + 45 <= 256  # both X2D nozzles reach it
    assert place_prime_tower(footprint(240.0, 240.0))[2] is False  # no room: keep Bambu's spot
