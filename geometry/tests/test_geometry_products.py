"""The five products on the hard generated figure (thin ears above the head, uneven feet,
Meshy-style defects)."""

import numpy as np
import pytest
from geo_fixtures import PRODUCT_CASES
from p2p_geometry.meshio import apply_transform, try_manifold
from p2p_geometry.repair import repair_figure
from p2p_geometry.solid import voxelize

NAMES = list(PRODUCT_CASES)


@pytest.mark.parametrize("name", NAMES)
def test_every_part_is_closed_and_the_transform_is_exact(products, figure, name):
    geo = products[name]
    assert geo.figure.report.watertight
    for p in geo.parts:
        assert p.watertight, p.name
        assert p.role in {"base", "keyring", "hanging_loop", "plate", "text", "back", "cutter"}
    rep = repair_figure(figure.vertices, figure.faces)
    # source vertex j is repaired vertex rep.vertex_source[j] and placed vertex
    # geo.figure.vertex_source[j] (-1 when a cut dropped it: the relief back, a plinth)
    ok = (geo.figure.vertex_source >= 0) & (rep.vertex_source >= 0)
    np.testing.assert_allclose(apply_transform(rep.vertices[rep.vertex_source[ok]], geo.transform),
                               geo.figure.vertices[geo.figure.vertex_source[ok]], atol=1e-6)
    assert geo.bbox_min[2] == pytest.approx(0.0, abs=1e-6)
    assert not geo.warnings, geo.warnings


@pytest.mark.parametrize("name", NAMES)
def test_size_meets_target(products, name):
    geo = products[name]
    target = PRODUCT_CASES[name][2]
    if name == "nameplate":
        assert geo.size_mm[0] == pytest.approx(target, abs=0.01)
    else:
        assert geo.size_mm.max() == pytest.approx(target, abs=0.5)


def _hole_clear(geo, part, axis):
    """No figure vertex inside the loop's hole cylinder."""
    loop = geo.metrics["loop"]
    r = loop["inner_d_mm"] / 2
    v = geo.figure.vertices
    if axis == "z":
        cx, cy = loop["anchor_x"], None
        hole = next(p for p in geo.parts if p.role == part)
        # hole center y from the part: ring top minus outer radius
        cy = hole.vertices[:, 1].max() - (r + loop["section_mm"])
        d = np.hypot(v[:, 0] - cx, v[:, 1] - cy)
    else:
        hole = next(p for p in geo.parts if p.role == part)
        cz = hole.vertices[:, 2].max() - (r + loop["section_mm"])
        cx = loop["anchor_xy"][0]
        d = np.hypot(v[:, 0] - cx, v[:, 2] - cz)
    return bool((d > r).all())


def test_keychain_lies_flat_with_a_fused_in_plane_ring(products, figure):
    geo = products["keychain"]
    assert geo.orientation == "flat"
    loop = geo.metrics["loop"]
    assert loop["embed_min_mm"] >= 1.5 and loop["embed_mean_mm"] >= 1.5
    assert loop["inner_d_mm"] >= 4.0 and loop["section_mm"] >= 2.5
    assert loop["thickness_mm"] >= 4.0 and loop["load_path_min_thickness_mm"] >= 4.0
    assert _hole_clear(geo, "keyring", "z")
    ring = geo.part("keyring")
    assert ring.vertices[:, 2].min() == pytest.approx(0.0, abs=1e-6)  # printed flat on the bed
    assert geo.size_mm[2] <= 10.0 + 1e-6


def test_ornament_loop_sits_on_the_head_not_an_ear(products, figure):
    geo = products["ornament"]
    loop = geo.metrics["loop"]
    s = geo.metrics["scale_mm_per_unit"]
    ear_inner = (figure.ear_x - 0.035) * s  # ear inner edge, mm from the center line
    ear_tip = geo.figure.vertices[:, 2].max()
    ring = geo.part("hanging_loop")
    half = (loop["inner_d_mm"] / 2 + loop["section_mm"]) * 0.85
    assert abs(loop["anchor_xy"][0]) + half < ear_inner  # stem footprint is between the ears
    assert loop["surface_z"] < ear_tip - 3  # seated on the head, below the ear tips
    assert loop["local_thickness_mm"] >= 4.0 and loop["embed_min_mm"] >= 1.5
    assert _hole_clear(geo, "hanging_loop", "y")
    assert abs(geo.metrics["hang_tilt_deg"]) < 10
    assert np.ptp(ring.vertices[:, 1]) == pytest.approx(loop["thickness_mm"], abs=0.01)


def test_nameplate_text_and_figure(products, library):
    geo = products["nameplate"]
    plate, text = geo.part("plate"), geo.part("text")
    t = geo.metrics["text"]
    assert text.filament_id == "generic-pla-white" and plate.filament_id == "generic-pla-black"
    assert t["cap_height_mm"] >= 5.0 and t["min_stroke_mm"] >= 0.8
    assert 0.8 <= t["relief_mm"] <= 1.2
    top = plate.vertices[:, 2].max()
    assert text.vertices[:, 2].max() == pytest.approx(top + t["relief_mm"], abs=1e-6)
    # text inside the plate, right of the figure
    assert text.vertices[:, 0].min() > geo.figure.vertices[:, 0].max()
    assert text.vertices[:, 0].max() < plate.vertices[:, 0].max()
    # every foot sinks at least 1 mm (feet differ by about 1.6 mm)
    p = geo.metrics["plate"]
    assert p["feet"] == 4 and p["foot_height_spread_mm"] > 1.0
    assert p["figure_sink_mm"] == pytest.approx(1.0 + p["foot_height_spread_mm"])
    assert geo.metrics["com_margin_mm"] > 5


def test_nameplate_feet_all_overlap_the_plate(products):
    geo = products["nameplate"]
    top = geo.metrics["plate"]["t_mm"]
    sol = voxelize(geo.figure.vertices, geo.figure.faces, 0.3)
    # each foot's lowest point is at least 1 mm below the plate top
    from scipy import ndimage

    zb = sol.z_bottom
    mask = np.isfinite(zb) & (zb < top)  # below the plate top: only feet reach there
    lab, n = ndimage.label(mask)
    assert n == 4
    for k in range(1, n + 1):
        assert np.nanmin(zb[lab == k]) <= top - 1.0 + 0.05  # column sampling tolerance


def test_magnet_pocket_back_and_no_supports_needed(products):
    geo = products["magnet"]
    assert geo.orientation == "flat"
    mp = geo.metrics["magnet_pocket"]
    assert mp["d_mm"] == pytest.approx(10.3) and mp["depth_mm"] == pytest.approx(2.2)
    assert not mp["figure_intrudes"] and geo.part("cutter") is None
    back = geo.part("back")
    sol = voxelize(back.vertices, back.faces, 0.2)
    cx, cy = mp["center_xy"]
    pts = np.array([[cx, cy, 0.5], [cx, cy, 2.0], [cx, cy, 3.4]])
    assert list(sol.contains(pts)) == [False, False, True]
    # height field: every layer of the union sits on the layer below (no overhangs)
    fig = try_manifold(geo.figure.vertices, geo.figure.faces)
    bm = try_manifold(back.vertices, back.faces)
    union = fig + bm
    prev = None
    for z in np.arange(4.1, union.bounding_box()[5] - 0.2, 0.4):
        sec = union.slice(z)
        if prev is not None:
            assert (sec - prev.offset(0.05)).area() < 0.5, z
        prev = sec


def test_figurine_base_and_uneven_feet(products):
    geo = products["figurine"]
    base = geo.part("base")
    b = geo.metrics["base"]
    assert b["thickness_mm"] >= 3.0
    assert b["figure_sink_mm"] >= 1.0 + b["foot_height_spread_mm"] - 1e-6
    assert base.vertices[:, 2].min() == pytest.approx(0.0, abs=1e-6)
    assert geo.metrics["com_margin_mm"] > 5
