import numpy as np
from p2p_filaments import checker, correction
from p2p_filaments.colour import (
    ciede2000,
    hex_to_lab,
    lab_to_linear,
    lab_to_xyz,
    linear_to_lab,
    srgb_decode,
    srgb_encode,
    xyz_to_lab,
)
from p2p_threemf import colour as tm_colour

# Sharma, Wu and Dalal (2005), Table 1: pairs 1, 2, 7, 17, 25, 34.
SHARMA = [
    ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
    ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
    ((50.0, 0.0, 0.0), (50.0, -1.0, 2.0), 2.3669),
    ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]


def test_ciede2000_matches_sharma_table():
    a = np.array([p[0] for p in SHARMA])
    b = np.array([p[1] for p in SHARMA])
    np.testing.assert_allclose(ciede2000(a, b), [p[2] for p in SHARMA], atol=1e-4)


def test_ciede2000_matches_threemf_scalar_version():
    rng = np.random.default_rng(1)
    a = rng.uniform([0, -80, -80], [100, 80, 80], (200, 3))
    b = a + rng.normal(0, 8, a.shape)
    ours = ciede2000(a, b)
    theirs = [tm_colour.ciede2000(x, y) for x, y in zip(a, b, strict=True)]
    np.testing.assert_allclose(ours, theirs, atol=1e-9)


def test_hex_and_lab_round_trips():
    for h in ("#161616", "#7C4B00", "#F72323", "#0D6284", "#FFFFFF"):
        np.testing.assert_allclose(hex_to_lab(h), tm_colour.hex_to_lab(h), atol=1e-9)
    lab = np.array([[53.0, 74.0, 55.0], [38.7, -10.5, -25.5]])
    np.testing.assert_allclose(linear_to_lab(lab_to_linear(lab)), lab, atol=1e-9)
    np.testing.assert_allclose(xyz_to_lab(lab_to_xyz(lab)), lab, atol=1e-9)
    v = np.linspace(0, 1, 11)
    np.testing.assert_allclose(srgb_encode(srgb_decode(v)), v, atol=1e-12)


def test_checker_reference_is_sane():
    for name in ("classic24", "classic24-2005"):
        ref = checker.reference_lab_d65(name)
        assert ref.shape == (24, 3)
        greys = ref[list(checker.NEUTRALS)]
        assert np.all(np.diff(greys[:, 0]) < 0)  # white 9.5 down to black 2
        assert np.all(np.hypot(greys[:, 1], greys[:, 2]) < 3.5)
    # The published sRGB values describe the older chart; the Bradford-adapted X-Rite Lab
    # values should agree with them to a few dE except the out-of-gamut cyan.
    srgb_lab = linear_to_lab(srgb_decode(checker.SRGB_PUBLISHED / 255))
    de = ciede2000(srgb_lab, checker.reference_lab_d65("classic24-2005"))
    assert np.median(de) < 3.0


def test_correction_inverts_a_known_camera():
    ref = checker.reference_lab_d65()
    m_cam = np.array([[0.7, 0.2, 0.05], [0.1, 0.8, 0.1], [0.02, 0.15, 0.6]])
    power = np.array([1.1, 0.9, 1.05])
    xyz = lab_to_xyz(ref)
    cam = np.clip(xyz @ m_cam.T, 1e-4, None) ** power  # exactly invertible by the model
    ev = correction.evaluate(cam, ref)
    assert ev["summary"]["fit_de2000_max"] < 0.05
    assert ev["summary"]["loo_de2000_max"] < 0.1


def test_manual_checker_grid_follows_corner_patches():
    centres = np.array([[100, 100], [600, 100], [600, 400], [100, 400]], float)
    found = checker.from_corner_patches(centres)
    got = found.quads.mean(axis=1)
    assert np.allclose(got[0], centres[0]) and np.allclose(got[5], centres[1])
    assert np.allclose(got[23], centres[2]) and np.allclose(got[18], centres[3])
    assert np.allclose(got[7], [200, 200])  # row 1, column 1
