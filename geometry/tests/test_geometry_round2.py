"""Plinth removal, loop positions, and the GeometryResult fields added in round 2."""

import numpy as np
import pytest
from geo_fixtures import PRODUCT_CASES, hard_figure
from p2p_contracts.geometry import GeometryResult
from p2p_contracts.job import AddOn, AddOnKind, ProductType
from p2p_geometry import build_product, build_product_geometry
from p2p_geometry.meshio import GLTF_TO_PRINT, apply_transform
from p2p_geometry.plinth import detect_plinth
from p2p_geometry.repair import edge_stats, repair_figure


@pytest.fixture(scope="module")
def plinth_figure():
    return hard_figure(plinth=True)


def test_plinth_detected_only_when_present(figure, plinth_figure):
    for fig, expect in ((figure, False), (plinth_figure, True)):
        r = repair_figure(fig.vertices, fig.faces)
        p = detect_plinth(apply_transform(r.vertices, GLTF_TO_PRINT), r.faces)
        assert p.found is expect, p.reason
    assert p.z_top == pytest.approx(0.08, abs=0.01)


def test_flat_keychain_drops_the_plinth_and_caps_the_cut(plinth_figure, library):
    geo = build_product_geometry(plinth_figure.vertices, plinth_figure.faces,
                                 ProductType.KEYCHAIN, [], 45.0, library, None, form="flat")
    fig = geo.figure
    rep = fig.report
    assert rep.removed_faces.get("plinth") and rep.plinth_cap_faces > 0
    assert rep.watertight and edge_stats(fig.faces) == (0, 0)
    assert (fig.face_source == -1).sum() >= rep.plinth_cap_faces
    kept = set(fig.face_source[fig.face_source >= 0].tolist())
    assert not kept & set(rep.removed_faces["plinth"])
    # nothing of the figure is left below the cut, in the figure's own upright frame
    raw = apply_transform(fig.vertices, np.linalg.inv(geo.transform))
    up = apply_transform(raw, GLTF_TO_PRINT)
    assert up[:, 2].min() >= 0.08
    assert any("plinth" in n for n in geo.notes) and not geo.warnings


def test_upright_products_keep_the_plinth(plinth_figure, library):
    geo = build_product_geometry(plinth_figure.vertices, plinth_figure.faces,
                                 ProductType.ORNAMENT, [], 70.0, library, None)
    assert "plinth" not in geo.figure.report.removed_faces


def _fig_bounds(geo):
    return geo.figure.vertices.min(axis=0), geo.figure.vertices.max(axis=0)


@pytest.mark.parametrize(("pos", "axis", "sign"), [("left", 0, -1), ("right", 0, 1), ("top", 1, 1)])
def test_keychain_loop_positions(figure, library, pos, axis, sign):
    addons = [AddOn(kind=AddOnKind.KEYRING_LOOP, params={"position": pos})]
    geo = build_product_geometry(figure.vertices, figure.faces, ProductType.KEYCHAIN, addons,
                                 45.0, library, None, form="flat")
    loop = geo.metrics["loop"]
    assert loop["position"] == pos and not loop["auto"]
    lo, hi = _fig_bounds(geo)
    hc = loop["hole_center"][axis]
    assert (hc > hi[axis]) if sign > 0 else (hc < lo[axis])
    assert loop["embed_min_mm"] >= 1.5 and all(p.watertight for p in geo.parts)


@pytest.mark.parametrize(("pos", "axis", "sign"),
                         [("rear", 1, 1), ("left", 0, -1), ("right", 0, 1), ("top", 2, 1),
                          ("back", 2, 1)])
def test_ornament_loop_positions(figure, library, pos, axis, sign):
    """rear sticks out of the figure's rear; back (the customer's "On the back") is the top of
    the back, so it is a top ring."""
    addons = [AddOn(kind=AddOnKind.HANGING_LOOP, params={"position": pos})]
    geo = build_product_geometry(figure.vertices, figure.faces, ProductType.ORNAMENT, addons,
                                 70.0, library, None)
    loop = geo.metrics["loop"]
    assert loop["position"] == ("top" if pos == "back" else pos)
    lo, hi = _fig_bounds(geo)
    center, half = (lo + hi) / 2, (hi - lo) / 2
    hc = np.asarray(loop["hole_center"])
    assert sign * (hc[axis] - center[axis]) > 0.4 * half[axis]  # out on the asked side
    assert loop["embed_min_mm"] >= 1.5 and loop["local_thickness_mm"] >= 4.0
    assert all(p.watertight for p in geo.parts)


def test_auto_loop_picks_the_top_when_it_is_sturdy(products):
    for name in ("keychain", "ornament"):
        loop = products[name].metrics["loop"]
        assert loop["auto"] and loop["position"] == "top"
        assert len(loop["score_by_position"]) >= 3


def test_bad_position_falls_back_to_auto(figure, library):
    addons = [AddOn(kind=AddOnKind.KEYRING_LOOP, params={"position": "back"})]  # into the bed
    geo = build_product_geometry(figure.vertices, figure.faces, ProductType.KEYCHAIN, addons,
                                 45.0, library, None, form="flat")
    assert geo.metrics["loop"]["auto"]
    assert any("faces the bed" in w for w in geo.warnings)


@pytest.mark.parametrize("name", list(PRODUCT_CASES))
def test_geometry_result_orientation_and_supports(tmp_path, figure, library, name):
    product, addons, target = PRODUCT_CASES[name]
    res = build_product(figure.vertices, figure.faces, product, addons, target, library, None,
                        tmp_path)
    assert isinstance(res, GeometryResult)
    flat = name in ("keychain", "magnet")
    assert res.orientation == ("flat_face_up" if flat else "upright")
    assert res.supports == ("none" if flat else "tree")
    assert res.notes and not res.warnings
    assert res.parts and np.all(np.array(res.bbox_max_mm) > np.array(res.bbox_min_mm))
