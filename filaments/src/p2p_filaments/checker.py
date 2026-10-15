"""24-patch colour checker: published reference values and finding the patches in a photo.

Reference values (patch order: row by row from dark skin to black, as printed on the chart):

- classic24: X-Rite ColorChecker Classic, charts made from November 2014 on (new colorants).
  CIELAB D50 / 2 degree, measured with an i1Pro 2 (M0). Source: X-Rite, "New Color
  Specifications for ColorChecker SG and Classic Charts" (2016), CGATS file
  ColorChecker24_After_Nov2014.txt, https://doi.org/10.5281/zenodo.3245895 (the same values
  ship in colour-science as "ColorChecker24 - After November 2014").
- classic24-2005: the same chart made before November 2014. Source: X-Rite (2016) as above,
  ColorChecker24_Before_Nov2014.txt, via colour-science "ColorChecker24 - Before November 2014".
- SRGB_PUBLISHED: the manufacturer's 8-bit sRGB values for the Classic chart, as listed in the
  "ColorChecker" article on Wikipedia (https://en.wikipedia.org/wiki/ColorChecker, table
  column "sRGB"). Used as a cross-check and for display, not for the fit, because they are
  rounded, clipped to the sRGB gamut and describe the pre-2014 colorants.

Calibrite (which now sells the X-Rite charts) uses the post-2014 formulation, so classic24 is
the default. Lab D50 values are adapted to D65 with Bradford before fitting.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .colour import D50, D65, adapt, lab_to_xyz, xyz_to_lab

NAMES = (
    "dark skin", "light skin", "blue sky", "foliage", "blue flower", "bluish green",
    "orange", "purplish blue", "moderate red", "purple", "yellow green", "orange yellow",
    "blue", "green", "red", "yellow", "magenta", "cyan",
    "white 9.5", "neutral 8", "neutral 6.5", "neutral 5", "neutral 3.5", "black 2",
)
ROWS, COLS = 4, 6
NEUTRALS = tuple(range(18, 24))

LAB_D50 = {
    "classic24": np.array([
        [37.54, 14.37, 14.92], [64.66, 19.27, 17.5], [49.32, -3.82, -22.54],
        [43.46, -12.74, 22.72], [54.94, 9.61, -24.79], [70.48, -32.26, -0.37],
        [62.73, 35.83, 56.5], [39.43, 10.75, -45.17], [50.57, 48.64, 16.67],
        [30.1, 22.54, -20.87], [71.77, -24.13, 58.19], [71.51, 18.24, 67.37],
        [28.37, 15.42, -49.8], [54.38, -39.72, 32.27], [42.43, 51.05, 28.62],
        [81.8, 2.67, 80.41], [50.63, 51.28, -14.12], [49.57, -29.71, -28.32],
        [95.19, -1.03, 2.93], [81.29, -0.57, 0.44], [66.89, -0.75, -0.06],
        [50.76, -0.13, 0.14], [35.63, -0.46, -0.48], [20.64, 0.07, -0.46],
    ]),
    "classic24-2005": np.array([
        [37.986, 13.555, 14.059], [65.711, 18.13, 17.81], [49.927, -4.88, -21.905],
        [43.139, -13.095, 21.905], [55.112, 8.844, -25.399], [70.719, -33.397, -0.199],
        [62.661, 36.067, 57.096], [40.02, 10.41, -45.964], [51.124, 48.239, 16.248],
        [30.325, 22.976, -21.587], [72.532, -23.709, 57.255], [71.941, 19.363, 67.857],
        [28.778, 14.179, -50.297], [55.261, -38.342, 31.37], [42.101, 53.378, 28.19],
        [81.733, 4.039, 79.819], [51.935, 49.986, -14.574], [51.038, -28.631, -28.638],
        [96.539, -0.425, 1.186], [81.257, -0.638, -0.335], [66.766, -0.734, -0.504],
        [50.867, -0.153, -0.27], [35.656, -0.421, -1.231], [20.461, -0.079, -0.973],
    ]),
}

SOURCES = {
    "classic24": ("X-Rite (2016), New Color Specifications for ColorChecker SG and Classic "
                  "Charts, ColorChecker24_After_Nov2014.txt, doi:10.5281/zenodo.3245895; "
                  "Lab D50 2 deg, adapted to D65 with Bradford"),
    "classic24-2005": ("X-Rite (2016), New Color Specifications for ColorChecker SG and "
                       "Classic Charts, ColorChecker24_Before_Nov2014.txt, "
                       "doi:10.5281/zenodo.3245895; Lab D50 2 deg, adapted to D65 with Bradford"),
}

SRGB_PUBLISHED = np.array([
    (115, 82, 68), (194, 150, 130), (98, 122, 157), (87, 108, 67), (133, 128, 177),
    (103, 189, 170), (214, 126, 44), (80, 91, 166), (193, 90, 99), (94, 60, 108),
    (157, 188, 64), (224, 163, 46), (56, 61, 150), (70, 148, 73), (175, 54, 60),
    (231, 199, 31), (187, 86, 149), (8, 133, 161), (243, 243, 242), (200, 200, 200),
    (160, 160, 160), (122, 122, 121), (85, 85, 85), (52, 52, 52),
])
SRGB_SOURCE = "manufacturer sRGB values, Wikipedia 'ColorChecker' table"


def reference_lab_d65(checker: str = "classic24") -> np.ndarray:
    """(24, 3) CIELAB D65 of the named checker (Bradford-adapted from X-Rite's D50 values)."""
    if checker not in LAB_D50:
        raise ValueError(f"unknown checker {checker!r}; known: {sorted(LAB_D50)}")
    return xyz_to_lab(adapt(lab_to_xyz(LAB_D50[checker], D50), D50, D65))


# ------------------------------------------------------------------ finding the patches


@dataclass
class CheckerFound:
    """Patch sampling quads in image pixels, (24, 4, 2), in reference patch order."""

    quads: np.ndarray
    method: str  # "auto" (OpenCV mcc) or "manual"
    outline: np.ndarray | None = None  # (4, 2) chart outline, when known


def shrink_quads(quads: np.ndarray, factor: float) -> np.ndarray:
    c = quads.mean(axis=1, keepdims=True)
    return c + (quads - c) * factor


def detect_auto(img_bgr: np.ndarray, sample_fraction: float = 0.8) -> CheckerFound | None:
    """OpenCV contrib `mcc` detector (MCC24). Returns None when nothing is found.
    The patch order it reports is checked against the reference later (see calibrate)."""
    import cv2

    if not hasattr(cv2, "mcc"):
        return None
    det = cv2.mcc.CCheckerDetector_create()
    try:
        ok = det.process(img_bgr, cv2.mcc.MCC24, 1)  # OpenCV 4.x
    except cv2.error:
        det.setColorChartType(cv2.mcc.MCC24)  # OpenCV 5.x
        ok = det.process(img_bgr)
    if not ok:
        return None
    best = det.getBestColorChecker()
    pts = np.asarray(best.getColorCharts(), dtype=np.float64).reshape(-1, 4, 2)
    if pts.shape != (24, 4, 2):
        return None
    return CheckerFound(quads=shrink_quads(pts, sample_fraction), method="auto",
                        outline=np.asarray(best.getBox(), dtype=np.float64).reshape(4, 2))


def from_corner_patches(centres: np.ndarray, sample_fraction: float = 0.35) -> CheckerFound:
    """Manual input: image centres of the four corner patches, in the order dark skin (1),
    bluish green (6), black (24), white (19). The other patch centres follow from the
    homography of the 6 x 4 grid; each patch is sampled over `sample_fraction` of the pitch,
    which stays inside the patch on the Classic, Passport and Mini layouts."""
    import cv2

    centres = np.asarray(centres, dtype=np.float64).reshape(4, 2)
    grid = np.array([[0, 0], [COLS - 1, 0], [COLS - 1, ROWS - 1], [0, ROWS - 1]], np.float64)
    h = cv2.getPerspectiveTransform(grid.astype(np.float32), centres.astype(np.float32))
    half = sample_fraction / 2
    quads = []
    for i in range(24):
        r, c = divmod(i, COLS)
        sq = np.array([[c - half, r - half], [c + half, r - half], [c + half, r + half],
                       [c - half, r + half]])
        quads.append(cv2.perspectiveTransform(sq[None].astype(np.float64), h)[0])
    return CheckerFound(quads=np.array(quads), method="manual")


def reversed_order(found: CheckerFound) -> CheckerFound:
    """The same patches for a chart lying the other way up (patch i <-> patch 23 - i)."""
    return CheckerFound(quads=found.quads[::-1].copy(), method=found.method,
                        outline=found.outline)
