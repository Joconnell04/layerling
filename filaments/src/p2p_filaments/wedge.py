"""Transmission distance (td_mm) from the step wedges.

Model, per linear sRGB channel c: a layer of thickness t over a base looks like

    R(t) = R_solid + (R_base - R_solid) * exp(-t / lam_c)

R_solid is the filament's own 2.0 mm tile, R_base the black or white tile. The difference
between the two wedges, (R_white - R_black) * exp(-t / lam_c), carries lam_c; fitting both
wedges at once uses all ten steps. lam_c is the decay length (light travels down and back).

td_mm is the thickness at which the colour stops showing its base: the smallest t where the
model puts the colour over black and over white both within `threshold` CIEDE2000 of the solid
colour. That is the stepped-swatch definition HueForge describes ("where the colour stops
changing"), measured in reflection, and it is the number the simulator's light-over-dark bleed
rule compares against the printed thickness.
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import minimize_scalar

from .colour import ciede2000, linear_to_lab

LAM_MIN, LAM_MAX = 0.005, 50.0
T_MAX = 20.0


def wedge_colour(t, solid, base, lam) -> np.ndarray:
    """Linear sRGB of thickness t (broadcasts) over base, with decay lengths lam (3,)."""
    t = np.asarray(t, dtype=np.float64)
    solid, base, lam = (np.asarray(x, dtype=np.float64) for x in (solid, base, lam))
    return solid + (base - solid) * np.exp(-t / lam)


def fit_lambda(t, over_black, over_white, solid, black, white) -> tuple[np.ndarray, np.ndarray]:
    """Per-channel decay length (3,) and RMS residual (3,) in linear units.
    Channels where the bases barely differ from the solid colour carry no information; they
    take the median of the others."""
    t = np.asarray(t, dtype=np.float64)
    ob, ow = np.asarray(over_black, np.float64), np.asarray(over_white, np.float64)
    solid, black, white = (np.asarray(x, np.float64) for x in (solid, black, white))
    lam = np.full(3, np.nan)
    rms = np.full(3, np.nan)
    for c in range(3):
        signal = abs(black[c] - solid[c]) + abs(white[c] - solid[c])
        if signal < 0.01:
            continue

        def cost(u, c=c):
            lc = np.exp(u)
            e = np.exp(-t / lc)
            rb = solid[c] + (black[c] - solid[c]) * e - ob[:, c]
            rw = solid[c] + (white[c] - solid[c]) * e - ow[:, c]
            return float(np.sum(rb * rb) + np.sum(rw * rw))

        grid = np.linspace(np.log(LAM_MIN), np.log(LAM_MAX), 200)
        u0 = grid[int(np.argmin([cost(u) for u in grid]))]
        res = minimize_scalar(cost, bounds=(max(u0 - 0.2, grid[0]), min(u0 + 0.2, grid[-1])),
                              method="bounded")
        lam[c] = float(np.exp(res.x))
        rms[c] = float(np.sqrt(res.fun / (2 * len(t))))
    if np.all(np.isnan(lam)):
        lam[:] = LAM_MIN
    else:
        lam[np.isnan(lam)] = np.nanmedian(lam)
    return lam, rms


def td_from_model(lam, solid, black, white, threshold: float = 2.0
                  ) -> tuple[float, dict[str, float]]:
    """Smallest thickness where the colour over black and over white are both within
    `threshold` CIEDE2000 of the solid colour. Returns (td, {base: td_base})."""
    ts = np.arange(0.0, T_MAX + 1e-9, 0.005)
    lab_solid = linear_to_lab(np.clip(solid, 0, None))
    per_base = {}
    for name, base in (("black", black), ("white", white)):
        rgb = wedge_colour(ts[:, None], solid, base, lam)
        de = ciede2000(linear_to_lab(np.clip(rgb, 0, None)), lab_solid)
        over = np.nonzero(de > threshold)[0]
        per_base[name] = 0.0 if len(over) == 0 else float(ts[min(over[-1] + 1, len(ts) - 1)])
    return max(per_base.values()), per_base


def observed_bound(t, lab_black, lab_white, lab_solid, threshold: float = 2.0) -> float | None:
    """Thinnest measured step from which on both wedges are within threshold of the solid
    colour, straight from the data (no model). None if even the thickest step is not."""
    t = np.asarray(t, dtype=np.float64)
    de = np.maximum(ciede2000(lab_black, lab_solid), ciede2000(lab_white, lab_solid))
    ok = de <= threshold
    for j in range(len(t)):
        if ok[j:].all():
            return float(t[j])
    return None


def estimate(t, over_black, over_white, solid, black, white, threshold: float = 2.0) -> dict:
    """Everything the report needs for one filament; inputs are linear sRGB."""
    lam, rms = fit_lambda(t, over_black, over_white, solid, black, white)
    td, per_base = td_from_model(lam, solid, black, white, threshold)
    lab_solid = linear_to_lab(np.clip(solid, 0, None))
    lab_b = linear_to_lab(np.clip(over_black, 0, None))
    lab_w = linear_to_lab(np.clip(over_white, 0, None))
    model_b = linear_to_lab(np.clip(wedge_colour(np.asarray(t)[:, None], solid, black, lam), 0,
                                    None))
    model_w = linear_to_lab(np.clip(wedge_colour(np.asarray(t)[:, None], solid, white, lam), 0,
                                    None))
    fit_de = np.concatenate([ciede2000(model_b, lab_b), ciede2000(model_w, lab_w)])
    t_max = float(np.max(t))
    return {
        "td_mm": round(td, 3),
        "td_over_black_mm": round(per_base["black"], 3),
        "td_over_white_mm": round(per_base["white"], 3),
        "observed_within_mm": observed_bound(t, lab_b, lab_w, lab_solid, threshold),
        "extrapolated": bool(td > t_max),
        "lambda_mm_rgb": [round(float(x), 4) for x in lam],
        "fit_rms_linear": [None if np.isnan(x) else round(float(x), 5) for x in rms],
        "fit_de2000_mean": round(float(np.mean(fit_de)), 3),
        "fit_de2000_max": round(float(np.max(fit_de)), 3),
        "threshold_de2000": threshold,
        "steps_de2000_over_black": [round(float(x), 2) for x in ciede2000(lab_b, lab_solid)],
        "steps_de2000_over_white": [round(float(x), 2) for x in ciede2000(lab_w, lab_solid)],
    }
