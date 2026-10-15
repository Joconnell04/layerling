from pathlib import Path
"""The real Meshy beagle (meshy-6-lite, 31k faces, 3 bad edges). Skipped when the GLB or the
Bambu Studio CLI is not on this machine."""

import pytest
from geo_fixtures import PRODUCT_CASES
from p2p_geometry import build_product_geometry, repair_figure, slicecheck
from p2p_geometry.export import write_plain_3mf


def test_beagle_repair_drops_only_the_fin(beagle):
    r = repair_figure(beagle.vertices, beagle.faces)
    assert r.report.watertight
    assert r.report.removed_faces == {"fin": [30469]}
    assert r.report.holes_filled == 0
    assert len(r.faces) == len(beagle.faces) - 1


@pytest.fixture(scope="module")
def beagle_products(beagle, library):
    return {
        name: build_product_geometry(beagle.vertices, beagle.faces, p, a, t, library, None)
        for name, (p, a, t) in PRODUCT_CASES.items()
    }


@pytest.mark.parametrize("name", list(PRODUCT_CASES))
def test_beagle_products_build_clean(beagle_products, name):
    geo = beagle_products[name]
    assert all(p.watertight for p in geo.parts)
    assert not geo.warnings, geo.warnings


@pytest.mark.skipif(not slicecheck.available(), reason="Bambu Studio not installed")
@pytest.mark.parametrize("name", list(PRODUCT_CASES))
def test_beagle_products_slice(beagle_products, name, tmp_path):
    geo = beagle_products[name]
    tmf = write_plain_3mf(geo, tmp_path / f"{name}.3mf", slicecheck.bed_center())
    flat = geo.orientation == "flat"
    res = slicecheck.slice_file(tmf, tmp_path / "slice", supports=not flat, timeout_s=300)
    assert res.ok, res.errors
    assert res.grams and res.grams > 1 and res.print_time_s > 60
    if flat:
        assert res.support_lines == 0


def test_build_product_accepts_a_glb_path(beagle, tmp_path):
    BEAGLE = Path(__file__).resolve().parents[2] / ("data/fixtures/meshy/02_image_to_3d_lite/model.glb")
    from p2p_geometry import build_product

    res = build_product(BEAGLE, None, "keychain", [], 45.0, None, None, tmp_path, form="flat")
    assert {p.role for p in res.parts} == {"back", "keyring"}
    assert all(p.watertight for p in res.parts) and res.figure.watertight
    assert res.face_index_map is not None  # the fin face was dropped
