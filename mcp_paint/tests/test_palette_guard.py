"""The achromatic guard: gray never becomes brown."""

from __future__ import annotations

import numpy as np
from p2p_contracts.mcp_tools import SuggestPaletteIn
from p2p_mcp_paint import palette
from p2p_mcp_paint.job import load_library

GRAY = np.array([50.0, 0.0, 0.0])  # the Frenchie's mid gray shading (L* 40 to 55, a*b* ~ 0)


def test_mid_gray_is_closer_to_brown_without_the_guard():
    owned, _ = load_library()
    de = palette.de_matrix([GRAY], palette.filament_labs(owned))[0]
    ids = [f.id for f in owned]
    black, white, brown = (de[ids.index(x)] for x in
                           ("generic-pla-black", "generic-pla-white", "generic-pla-brown"))
    assert brown < min(black, white)  # the bug the guard exists for


def test_guard_sends_gray_to_black_or_white():
    owned, _ = load_library()
    for L in (20, 35, 45, 50, 55, 65, 80):
        j, _ = palette.nearest([[L, 0.5, -0.5]], owned)
        assert owned[int(j[0])].id in ("generic-pla-black", "generic-pla-white"), L


def test_guard_keeps_chromatic_colors_free():
    owned, _ = load_library()
    tan = palette.hex_lab("#8B4513")  # the Frenchie spec's brown eyes
    j, _ = palette.nearest([tan], owned)
    assert owned[int(j[0])].id == "generic-pla-brown"


def test_swap_suggestion_for_gray_when_it_is_required():
    owned, catalog = load_library()
    t = [palette.Target("r1", GRAY, 100.0, True, (1,), "s1.coat_gray")]
    swaps = palette.swap_suggestions(t, owned, owned, catalog, ams_slots=5)
    gray = next(f for f in catalog if f.id == swaps[0]["load"])  # a gray PLA from the catalog
    assert "gray" in gray.name.lower() and palette.chroma(gray.lab)[0] <= 6
    assert swaps[0]["delta_e_now"] > palette.SWAP_DE >= swaps[0]["delta_e_with"]
    assert swaps[0]["replace"] in {f.id for f in owned}


def test_no_swap_when_a_loaded_match_exists():
    owned, catalog = load_library()
    t = [palette.Target("r1", np.array(owned[0].lab), 100.0, True, (1,), "black")]
    assert palette.swap_suggestions(t, owned, owned, catalog, ams_slots=5) == []


def test_choose_palette_prefers_gray_when_allowed():
    owned, catalog = load_library()
    t = [palette.Target("a", GRAY, 50, True), palette.Target("b", np.array([5.0, 0, 0]), 50, True),
         palette.Target("c", np.array([95.0, 0, 0]), 50, True)]
    pal = palette.choose_palette(t, owned + catalog, 3)
    assert any("gray" in f.name.lower() and 30 < f.lab[0] < 70 for f in pal)
    pal = palette.choose_palette(t, owned, 3)
    assert "generic-pla-brown" not in [f.id for f in pal]


def test_session_apply_never_paints_gray_specks_brown(session):
    speck_regions = [r for r in session.regions.values() if r.label.startswith("s2.body_")]
    assert speck_regions and all(session.assignment[r.id] == "generic-pla-brown"
                                 for r in speck_regions)
    out = session.suggest_palette(SuggestPaletteIn(k=5, apply=True)).out
    for r in speck_regions:
        assert session.assignment[r.id] in ("generic-pla-black", "generic-pla-white")
        assert out.delta_e[str(r.id)] > 0
    # s2's brown eyes (chromatic) keep brown
    eyes = [r for r in session.regions.values() if r.label.startswith("s2.eye_")]
    assert all(session.assignment[r.id] == "generic-pla-brown" for r in eyes)
    # the green eyes of s1 have no loaded match: a swap is suggested
    loads = {s["load"] for s in out.swap_suggestions}
    assert any("green" in x for x in loads)
