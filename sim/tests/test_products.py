"""Hard, realistic products: raised-text nameplate, 45 mm keychain, overlapping multi-part print."""

import numpy as np
import pytest
from p2p_sim.products import keychain, multipart_figure, nameplate
from p2p_sim.simulate import quantize_layers, simulate_print, simulate_print_detailed


def _kept(p, res, label):
    sel = p.face_regions == p.region_id(label)
    a = p.mesh.face_areas
    return a[sel & (res.face_slots == res.face_slots_in)].sum() / a[sel].sum()


@pytest.fixture(scope="module")
def plate_tops():
    return nameplate("tops")


def test_nameplate_painted_tops_1mm_survive_05mm_merge(plate_tops):
    p = plate_tops
    res = simulate_print_detailed(p.mesh, p.face_slots, p.palette_labs, face_regions=p.face_regions)
    assert _kept(p, res, "text_1mm") > 0.97
    assert _kept(p, res, "text_05mm") < 0.1
    m = {x.region_id: x for x in res.report.merged}
    thin = m[p.region_id("text_05mm")]
    assert thin.into_region_id == p.region_id("plate") and thin.reason in ("too_thin", "island")
    assert p.region_id("text_1mm") not in m


@pytest.mark.parametrize("model", ["bambu", "plan"])
@pytest.mark.parametrize("height", [1.0, 0.9])
def test_nameplate_flat_text_tops_are_not_quantized(model, height):
    """Text tops are flat: whatever the layer height alignment, their paint keeps its XY shape."""
    from p2p_contracts.reports import SimParams

    p = nameplate("tops", text_height=height)
    q = quantize_layers(p.mesh, p.face_slots, SimParams(), model)
    tops = p.mesh.face_normals[:, 2] > 0.99
    assert (q[tops] == p.face_slots[tops]).all()


def test_nameplate_painted_tops_penetrate_the_letter_sides(plate_tops):
    """Bambu projects painted tops 5 layers down: a 1 mm letter with a white top prints white
    on its sides too, so its top has 5 white layers and is not a bleed risk."""
    p = plate_tops
    res = simulate_print_detailed(p.mesh, p.face_slots, p.palette_labs, face_regions=p.face_regions)
    c, n = p.mesh.face_centroids, p.mesh.face_normals
    wide_row = c[:, 1] > 0
    sides = wide_row & (np.abs(n[:, 2]) < 0.1) & (c[:, 2] > 3.05) & (c[:, 2] < 3.95)
    assert (p.face_slots[sides] == 1).all()  # painted navy
    assert (res.face_slots[sides] == 2).mean() > 0.95  # printed white
    tops = (p.face_regions == p.region_id("text_1mm"))
    assert not np.isfinite(res.face_light_layers[tops]).any()
    assert not any(b.region_id == p.region_id("text_1mm") and b.area_mm2 > 5
                   for b in res.report.bleed_flags)
    # the plan's quantize model has no penetration: sides stay navy
    plan = simulate_print_detailed(p.mesh, p.face_slots, p.palette_labs,
                                   face_regions=p.face_regions, quantize_model="plan")
    assert (plan.face_slots[sides] == 1).mean() > 0.95


@pytest.mark.parametrize("height,flagged", [(0.6, True), (1.0, False), (2.0, False)])
def test_light_text_part_on_dark_plate_part(height, flagged):
    """White text as its own part on a navy plate part: text height sets the light layers."""
    p = nameplate("parts", text_height=height)
    res = simulate_print_detailed(p.mesh, None, p.palette_labs, face_regions=p.face_regions,
                                  face_parts=p.face_parts, part_slots=p.part_slots)
    tops = (p.face_parts == 1) & (p.mesh.face_normals[:, 2] > 0.99)
    layers = res.face_light_layers[tops]
    if flagged:
        assert np.isfinite(layers).mean() > 0.95
        assert np.floor(np.median(layers) + 1e-6) == round(height / 0.2)
        assert any(b.over_region_id == p.region_id("plate") for b in res.report.bleed_flags)
    else:
        assert not np.isfinite(layers).any()
    # the sunk bottom of the text is hidden inside the plate; the plate outside is not
    c = p.mesh.face_centroids
    assert res.face_hidden[(p.face_parts == 1) & (c[:, 2] < 2.95)].mean() > 0.95
    assert not res.face_hidden[(p.face_parts == 0) & (c[:, 2] < 2.9)].any()
    # separate-part text is a solid white part: thin strokes print as thin walls, colors stay
    assert _kept(p, res, "text_05mm") > 0.99


def test_per_part_filaments_equal_explicit_face_slots():
    p = nameplate("parts", text_height=0.6)
    a, ra = simulate_print(p.mesh, None, p.palette_labs, face_parts=p.face_parts,
                           part_slots=p.part_slots)
    b, rb = simulate_print(p.mesh, p.part_slots[p.face_parts], p.palette_labs,
                           face_parts=p.face_parts, part_slots=p.part_slots)
    assert np.array_equal(a, b) and ra.swaps_total == rb.swaps_total


@pytest.fixture(scope="module")
def keychain_sim():
    p = keychain()
    res = simulate_print_detailed(p.mesh, p.face_slots, p.palette_labs, face_regions=p.face_regions)
    return p, res


def test_keychain_is_45mm(keychain_sim):
    p, _ = keychain_sim
    assert p.info["size_mm"] == pytest.approx(45.0, abs=1.5)


@pytest.mark.parametrize("label,survives", [
    ("eye_left", True), ("eye_right", True),  # 1.1 mm dots
    ("mouth_thick", True),  # 1.0 mm line
    ("blush_dot_left", True), ("blush_dot_right", True),  # 1.2 mm dots
    ("nose", False),  # 0.7 mm dot
    ("highlight_left", False), ("highlight_right", False),  # 0.4 mm dots inside the eyes
    ("whisker_left", False), ("whisker_right", False),  # 0.5 mm lines
])
def test_keychain_features_near_the_08mm_limit(keychain_sim, label, survives):
    p, res = keychain_sim
    k = _kept(p, res, label)
    assert (k > 0.9) if survives else (k < 0.1), (label, k)


def test_keychain_highlights_merge_into_the_eyes(keychain_sim):
    p, res = keychain_sim
    into = {m.region_id: m.into_region_id for m in res.report.merged}
    assert into[p.region_id("highlight_left")] == p.region_id("eye_left")
    assert into[p.region_id("highlight_right")] == p.region_id("eye_right")


@pytest.fixture(scope="module")
def figure_sim():
    p = multipart_figure()
    res = simulate_print_detailed(p.mesh, p.face_slots, p.palette_labs, face_regions=p.face_regions,
                                  face_parts=p.face_parts, part_slots=p.part_slots)
    return p, res


def test_multipart_hidden_faces(figure_sim):
    p, res = figure_sim
    c = p.mesh.face_centroids
    loop = p.face_parts == 1
    frac = res.face_hidden[loop].mean()
    assert 0.05 < frac < 0.6  # the loop's lower arc sits inside the head
    assert not res.face_hidden[loop & (c[:, 2] > 36.0)].any()
    plate_top = (p.face_parts == 3) & (p.mesh.face_normals[:, 2] > 0.9)
    under_feet = plate_top & (np.hypot(c[:, 0], c[:, 1] + 1.0) < 3.0)
    assert res.face_hidden[under_feet].mean() > 0.9  # covered by the figure's body
    beak = p.face_parts == 2
    assert 0.0 < res.face_hidden[beak].mean() < 0.9


def test_multipart_swaps_count_the_parts_as_one_print(figure_sim):
    p, res = figure_sim
    L = 0.2
    pres = res.layer_slots
    # where the loop leaves the head (about z 32.5 to 34) both filaments print in the same layers
    # (white is also the belly paint lower down, so look above the belly)
    both = np.nonzero(pres[:, 0] & pres[:, 1])[0] * L
    top = both[both > 25.0]
    assert top.size >= 3 and 32.0 < top.min() and top.max() < 34.5, both
    # plate top layers carry the plate and the figure's feet
    assert pres[int(2.7 / L), 3] and pres[int(2.7 / L), 0]
    multi = (pres.sum(axis=1) >= 2).sum()
    assert res.report.swaps_total >= multi
    # separate prints per part would count none of the cross-part swaps
    alone = 0
    for part in range(4):
        sel = p.face_parts == part
        sub = _submesh(p.mesh, sel)
        _, r = simulate_print(sub, p.face_slots[sel], p.palette_labs)
        alone += r.swaps_total
    assert res.report.swaps_total > alone


def test_multipart_light_loop_over_dark_head_bleeds(figure_sim):
    p, res = figure_sim
    flags = res.report.bleed_flags
    assert any(f.region_id == p.region_id("loop") and f.over_region_id == p.region_id("figure")
               for f in flags)


def _submesh(mesh, sel):
    from p2p_sim.mesh import Mesh

    return Mesh(mesh.vertices, mesh.faces[sel])
