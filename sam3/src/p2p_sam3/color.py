"""sRGB <-> CIELAB (D65), CIEDE2000, and plain color names for marking labels."""

from __future__ import annotations

import numpy as np

_M_RGB_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ]
)
_M_XYZ_RGB = np.linalg.inv(_M_RGB_XYZ)
_WHITE_D65 = np.array([0.95047, 1.0, 1.08883])
_EPS, _KAPPA = 216 / 24389, 24389 / 27


def srgb_to_lab(rgb) -> np.ndarray:
    """(..., 3) sRGB, uint8 or float in [0, 255], to CIELAB D65 (L in [0, 100])."""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = lin @ _M_RGB_XYZ.T / _WHITE_D65
    f = np.where(xyz > _EPS, np.cbrt(xyz), (_KAPPA * xyz + 16) / 116)
    return np.stack(
        [116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])],
        axis=-1,
    )


def lab_to_srgb(lab) -> np.ndarray:
    """(..., 3) CIELAB D65 to sRGB float in [0, 255] (clipped)."""
    lab = np.asarray(lab, dtype=np.float64)
    fy = (lab[..., 0] + 16) / 116
    fx = fy + lab[..., 1] / 500
    fz = fy - lab[..., 2] / 200
    f = np.stack([fx, fy, fz], axis=-1)
    xyz = np.where(f**3 > _EPS, f**3, (116 * f - 16) / _KAPPA) * _WHITE_D65
    lin = np.clip(xyz @ _M_XYZ_RGB.T, 0.0, 1.0)
    c = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)
    return np.clip(c * 255.0, 0.0, 255.0)


def lab_to_hex(lab) -> str:
    r, g, b = (round(float(v)) for v in lab_to_srgb(lab))
    return f"#{r:02x}{g:02x}{b:02x}"


def ciede2000(lab1, lab2) -> np.ndarray:
    """CIEDE2000 color difference (kL = kC = kH = 1), broadcasting over leading axes."""
    lab1 = np.asarray(lab1, dtype=np.float64)
    lab2 = np.asarray(lab2, dtype=np.float64)
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    c_bar = (np.hypot(a1, b1) + np.hypot(a2, b2)) / 2
    g = 0.5 * (1 - np.sqrt(c_bar**7 / (c_bar**7 + 25.0**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dLp = L2 - L1
    dCp = c2p - c1p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where(c1p * c2p == 0, 0.0, dh)
    dHp = 2 * np.sqrt(c1p * c2p) * np.sin(np.radians(dh / 2))
    Lp_bar = (L1 + L2) / 2
    Cp_bar = (c1p + c2p) / 2
    hsum = h1p + h2p
    hp_bar = np.where(
        c1p * c2p == 0,
        hsum,
        np.where(
            np.abs(h1p - h2p) <= 180,
            hsum / 2,
            np.where(hsum < 360, (hsum + 360) / 2, (hsum - 360) / 2),
        ),
    )
    t = (
        1
        - 0.17 * np.cos(np.radians(hp_bar - 30))
        + 0.24 * np.cos(np.radians(2 * hp_bar))
        + 0.32 * np.cos(np.radians(3 * hp_bar + 6))
        - 0.20 * np.cos(np.radians(4 * hp_bar - 63))
    )
    d_theta = 30 * np.exp(-(((hp_bar - 275) / 25) ** 2))
    r_c = 2 * np.sqrt(Cp_bar**7 / (Cp_bar**7 + 25.0**7))
    s_l = 1 + 0.015 * (Lp_bar - 50) ** 2 / np.sqrt(20 + (Lp_bar - 50) ** 2)
    s_c = 1 + 0.045 * Cp_bar
    s_h = 1 + 0.015 * Cp_bar * t
    r_t = -np.sin(np.radians(2 * d_theta)) * r_c
    return np.sqrt(
        (dLp / s_l) ** 2 + (dCp / s_c) ** 2 + (dHp / s_h) ** 2 + r_t * (dCp / s_c) * (dHp / s_h)
    )


# One-word names (they go into region labels). Reference colors are typical rendered fur and
# plastic tones, not paint chips; the nearest by CIEDE2000 names a cluster.
NAMED_COLORS: dict[str, tuple[int, int, int]] = {
    "white": (240, 240, 236),
    "cream": (236, 222, 190),
    "silver": (190, 190, 192),
    "gray": (128, 128, 130),
    "charcoal": (64, 64, 66),
    "black": (24, 24, 26),
    "tan": (170, 110, 62),
    "ginger": (215, 120, 50),
    "brown": (105, 66, 40),
    "chocolate": (62, 38, 26),
    "orange": (240, 130, 20),
    "red": (190, 30, 35),
    "pink": (240, 160, 175),
    "yellow": (240, 210, 60),
    "gold": (200, 160, 60),
    "green": (60, 150, 70),
    "blue": (40, 90, 190),
    "purple": (115, 60, 150),
}
_NAMES = list(NAMED_COLORS)
_NAMED_LAB = srgb_to_lab(np.array([NAMED_COLORS[n] for n in _NAMES], float))


def color_name(lab) -> str:
    """Nearest NAMED_COLORS entry to one CIELAB color."""
    d = ciede2000(np.asarray(lab, float)[None, :], _NAMED_LAB)
    return _NAMES[int(np.argmin(d))]


def distinct_color_names(labs) -> list[str]:
    """One name per color, all different: closest (color, name) pairs are taken first, so two
    grays become silver and gray instead of silver twice."""
    labs = np.asarray(labs, float).reshape(-1, 3)
    d = ciede2000(labs[:, None, :], _NAMED_LAB[None, :, :])
    out: list[str | None] = [None] * len(labs)
    used: set[int] = set()
    for flat in np.argsort(d, axis=None, kind="stable"):
        i, j = divmod(int(flat), len(_NAMES))
        if out[i] is None and j not in used:
            out[i] = _NAMES[j]
            used.add(j)
    return [n or color_name(lab) for n, lab in zip(out, labs, strict=True)]
