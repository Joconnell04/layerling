"""End to end on synthetic photos: render the card and a checker with a known colour cast and
tone curve, then check calibrate recovers the true CIELAB within dE2000 2 and td_mm."""

import json
import shutil

import cv2
import numpy as np
import pytest
from p2p_filaments.calibrate import CalibrationError, run
from p2p_filaments.card import write_card
from p2p_filaments.colour import ciede2000
from p2p_filaments.library import DEFAULT_LIBRARY, ams_palette, load
from p2p_filaments.render import Camera
from p2p_filaments.synthetic import scene

IDS = ["generic-pla-black", "generic-pla-brown", "bambu-pla-basic-red", "generic-petg-blue",
       "generic-pla-white"]


@pytest.fixture
def lib(tmp_path):
    dst = tmp_path / "library.json"
    shutil.copyfile(DEFAULT_LIBRARY, dst)
    return dst


@pytest.fixture(scope="module")
def card3mf(tmp_path_factory):
    out = tmp_path_factory.mktemp("card") / "swatch_card.3mf"
    write_card(ams_palette(load(DEFAULT_LIBRARY)), out)
    return out


def _photo(tmp_path, sc, name="photo.jpg"):
    p = tmp_path / name
    cv2.imwrite(str(p), sc.photo.bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return p


def _check(rep, sc, max_de=2.0):
    assert [f["filament_id"] for f in rep["filaments"]] == IDS
    for i, f in enumerate(rep["filaments"]):
        de = float(ciede2000(f["lab_measured"], sc.true_lab[i]))
        assert de < max_de, (f["filament_id"], f["lab_measured"], sc.true_lab[i], de)
        td_true = sc.true_td[i]
        assert f["td"]["td_mm"] == pytest.approx(td_true, abs=max(0.08, 0.1 * td_true)), \
            (f["filament_id"], f["td"], td_true)


def test_auto_detection_recovers_lab_and_td_and_writes_library(tmp_path, lib, card3mf):
    sc = scene(seed=3)
    before = json.loads(lib.read_text())
    rep = run(_photo(tmp_path, sc), library=lib, card=card3mf, out_dir=tmp_path / "cal")
    assert rep["checker"]["method"] == "auto" and rep["card"]["markers"] == [0, 1, 2, 3]
    assert rep["checker"]["fit_de2000_mean"] < 1.0
    assert rep["card"]["source"] == str(card3mf)
    _check(rep, sc)
    assert rep["library"]["written"] and not rep["warnings"]
    after = json.loads(lib.read_text())
    for b, a in zip(before["filaments"], after["filaments"], strict=True):
        assert a["lab_source"] == "measured"
        assert {k: v for k, v in a.items() if k not in ("lab", "lab_source", "td_mm")} == \
            {k: v for k, v in b.items() if k not in ("lab", "lab_source", "td_mm")}
    assert (tmp_path / "cal" / "overlay.png").exists()
    saved = json.loads((tmp_path / "cal" / "report.json").read_text())
    assert saved["filaments"][2]["lab_measured"] == rep["filaments"][2]["lab_measured"]


def test_manual_corners_and_dry_run(tmp_path, lib):
    sc = scene(seed=5)
    text = lib.read_text()
    rep = run(_photo(tmp_path, sc), library=lib, card=None, out_dir=tmp_path / "cal",
              checker_points=sc.photo.checker_centres[[0, 5, 23, 18]],
              card_points=sc.photo.card_corners, write=False)
    assert rep["checker"]["method"] == "manual" and rep["card"]["method"] == "manual"
    _check(rep, sc)
    assert not rep["library"]["written"] and lib.read_text() == text


def test_upside_down_checker_and_strong_cast(tmp_path, lib):
    cam = Camera(cast=np.array([[1.0, 0.1, 0.0], [0.05, 0.7, 0.1], [0.0, 0.05, 0.45]]),
                 power=np.array([0.9, 1.1, 1.0]), exposure=0.9, noise=2.0, jpeg_quality=85)
    upside_down = np.array([[2260, 1010], [1370, 1050], [1350, 420], [2230, 380]], float)
    sc = scene(seed=7, camera=cam, checker_quad=upside_down)
    rep = run(_photo(tmp_path, sc), library=lib, card=None, out_dir=tmp_path / "cal",
              write=False)
    _check(rep, sc)
    # manual corners given the other way round are caught by the orientation check too
    rep2 = run(_photo(tmp_path, sc), library=lib, card=None, out_dir=tmp_path / "cal2",
               checker_points=sc.photo.checker_centres[[23, 18, 0, 5]], write=False)
    assert rep2["checker"]["order"] == "reversed"
    _check(rep2, sc)


def test_light_falloff_across_the_card_is_flat_fielded(tmp_path, lib):
    sc = scene(seed=9, card_light=(0.86, 1.14))
    rep = run(_photo(tmp_path, sc), library=lib, card=None, out_dir=tmp_path / "cal",
              write=False)
    _check(rep, sc)
    g = rep["card"]["light_gains_tl_tr_bl_br"]
    assert g[1] > g[0] * 1.08 and g[3] > g[2] * 1.08  # refs sit inside the ramp
    assert any("light varies" in w for w in rep["warnings"])


def test_missing_card_writes_grid_and_explains(tmp_path, lib):
    sc = scene(seed=1)
    img = sc.photo.bgr.copy()
    x0, y0 = sc.photo.card_corners.min(axis=0).astype(int) - 20
    x1, y1 = sc.photo.card_corners.max(axis=0).astype(int) + 20
    img[y0:y1, x0:x1] = 90
    p = tmp_path / "nocard.jpg"
    cv2.imwrite(str(p), img)
    with pytest.raises(CalibrationError, match="card markers"):
        run(p, library=lib, card=None, out_dir=tmp_path / "cal")
    assert (tmp_path / "cal" / "grid.png").exists()


def test_bad_checker_fit_does_not_write(tmp_path, lib):
    """A 2005 chart read as the 2014 one is still close; a wrong chart should not be. Feed
    corner points that land on the card instead of the checker."""
    sc = scene(seed=2)
    text = lib.read_text()
    c = sc.photo.card_corners
    rep = run(_photo(tmp_path, sc), library=lib, card=None, out_dir=tmp_path / "cal",
              checker_points=np.array([c[0], c[1], c[2], c[3]]) * 0.9 + c.mean(axis=0) * 0.1)
    assert not rep["library"]["written"]
    assert lib.read_text() == text
    assert any("dE2000" in w for w in rep["warnings"])
