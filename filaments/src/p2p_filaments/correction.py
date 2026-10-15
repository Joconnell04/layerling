"""Camera colour correction fitted on the 24 checker patches.

Model: linearise the photo with the sRGB curve, apply a power per channel (absorbs the camera's
tone curve and white balance gain differences), then a 3 x 3 matrix to CIE XYZ D65:

    XYZ = M @ (rgb_lin ** gamma)

12 parameters, fitted by least squares on the CIELAB differences of all 24 patches, started from
the linear least squares matrix. A 3 x 3 matrix in linear light also covers photos saved in
Display P3 instead of sRGB (same transfer curve, different primaries).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares

from .colour import XYZ_TO_SRGB, ciede2000, lab_to_xyz, xyz_to_lab

FLOOR = 1e-5


@dataclass
class Correction:
    gamma: np.ndarray  # (3,)
    matrix: np.ndarray  # (3, 3) tone-corrected camera rgb -> XYZ D65

    def xyz(self, rgb_lin) -> np.ndarray:
        x = np.maximum(np.asarray(rgb_lin, dtype=np.float64), FLOOR) ** self.gamma
        return x @ self.matrix.T

    def lab(self, rgb_lin) -> np.ndarray:
        return xyz_to_lab(self.xyz(rgb_lin))

    def linear_srgb(self, rgb_lin) -> np.ndarray:
        return self.xyz(rgb_lin) @ XYZ_TO_SRGB.T

    def to_dict(self) -> dict:
        return {"model": "XYZ = M @ rgb_lin ** gamma",
                "gamma": [round(float(g), 5) for g in self.gamma],
                "matrix": [[round(float(v), 6) for v in row] for row in self.matrix]}


def _unpack(p: np.ndarray) -> Correction:
    return Correction(np.exp(p[:3]), p[3:].reshape(3, 3))


def fit(rgb_lin, lab_ref, weights=None) -> Correction:
    """rgb_lin (k, 3) linearised camera values, lab_ref (k, 3) CIELAB D65 targets."""
    rgb = np.maximum(np.asarray(rgb_lin, dtype=np.float64), FLOOR)
    lab_ref = np.asarray(lab_ref, dtype=np.float64)
    w = np.ones(len(rgb)) if weights is None else np.asarray(weights, dtype=np.float64)
    xyz_ref = lab_to_xyz(lab_ref)
    m0, *_ = np.linalg.lstsq(rgb * w[:, None], xyz_ref * w[:, None], rcond=None)
    p0 = np.concatenate([np.zeros(3), m0.T.ravel()])

    def resid(p):
        c = _unpack(p)
        return ((c.lab(rgb) - lab_ref) * w[:, None]).ravel()

    res = least_squares(resid, p0, method="lm", max_nfev=4000)
    return _unpack(res.x)


def evaluate(rgb_lin, lab_ref, names=None) -> dict:
    """Fit on all patches, plus leave-one-out: each patch predicted by a fit without it."""
    rgb_lin = np.asarray(rgb_lin, dtype=np.float64)
    lab_ref = np.asarray(lab_ref, dtype=np.float64)
    corr = fit(rgb_lin, lab_ref)
    de = ciede2000(corr.lab(rgb_lin), lab_ref)
    loo = np.empty(len(rgb_lin))
    for i in range(len(rgb_lin)):
        keep = np.arange(len(rgb_lin)) != i
        c = fit(rgb_lin[keep], lab_ref[keep])
        loo[i] = ciede2000(c.lab(rgb_lin[i]), lab_ref[i])
    names = names or [str(i + 1) for i in range(len(rgb_lin))]
    worst = int(np.argmax(de))
    return {
        "correction": corr,
        "de2000": de,
        "de2000_loo": loo,
        "summary": {
            "fit_de2000_mean": round(float(de.mean()), 3),
            "fit_de2000_max": round(float(de.max()), 3),
            "fit_worst_patch": names[worst],
            "loo_de2000_mean": round(float(loo.mean()), 3),
            "loo_de2000_max": round(float(loo.max()), 3),
        },
    }
