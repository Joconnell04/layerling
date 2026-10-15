"""Color conversions: sRGB <-> CIELAB (D65, 2 degree observer), CIEDE2000.

Matches p2p_contracts.filaments: Lab is computed from sRGB (IEC 61966-2-1), D65 white.
All functions are vectorized over leading axes.
"""

from __future__ import annotations

import numpy as np

# D65 reference white, Y = 1
_WHITE = np.array([0.95047, 1.0, 1.08883])
_RGB2XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ]
)
_XYZ2RGB = np.linalg.inv(_RGB2XYZ)


def _to_linear(c: np.ndarray) -> np.ndarray:
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _to_gamma(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, 12.92 * c, 1.055 * np.power(c, 1 / 2.4) - 0.055)


def srgb_to_lab(rgb) -> np.ndarray:
    """sRGB (uint8 0-255 or float 0-1 if dtype is float and max <= 1) -> Lab, shape (..., 3)."""
    a = np.asarray(rgb)
    if a.dtype.kind in "ui" or a.max(initial=0) > 1.0:
        a = a.astype(np.float64) / 255.0
    lin = _to_linear(a.astype(np.float64))
    xyz = lin @ _RGB2XYZ.T / _WHITE
    eps, kappa = 216 / 24389, 24389 / 27
    f = np.where(xyz > eps, np.cbrt(xyz), (kappa * xyz + 16) / 116)
    lab = np.empty_like(f)
    lab[..., 0] = 116 * f[..., 1] - 16
    lab[..., 1] = 500 * (f[..., 0] - f[..., 1])
    lab[..., 2] = 200 * (f[..., 1] - f[..., 2])
    return lab


def lab_to_srgb(lab) -> np.ndarray:
    """Lab -> sRGB uint8, shape (..., 3). Out-of-gamut values are clipped."""
    lab = np.asarray(lab, dtype=np.float64)
    fy = (lab[..., 0] + 16) / 116
    fx = fy + lab[..., 1] / 500
    fz = fy - lab[..., 2] / 200
    eps, kappa = 216 / 24389, 24389 / 27
    f = np.stack([fx, fy, fz], axis=-1)
    xyz = np.where(f**3 > eps, f**3, (116 * f - 16) / kappa)
    xyz[..., 1] = np.where(lab[..., 0] > kappa * eps, fy**3, lab[..., 0] / kappa)
    lin = (xyz * _WHITE) @ _XYZ2RGB.T
    return np.round(_to_gamma(lin) * 255).astype(np.uint8)


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_to_hex(rgb) -> str:
    r, g, b = (int(x) for x in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def ciede2000(lab1, lab2) -> np.ndarray:
    """CIEDE2000 color difference (kL = kC = kH = 1), broadcasting over leading axes."""
    lab1 = np.asarray(lab1, dtype=np.float64)
    lab2 = np.asarray(lab2, dtype=np.float64)
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    C1 = np.hypot(a1, b1)
    C2 = np.hypot(a2, b2)
    Cm = (C1 + C2) / 2
    G = 0.5 * (1 - np.sqrt(Cm**7 / (Cm**7 + 25.0**7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dLp = L2 - L1
    dCp = C2p - C1p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where(C1p * C2p == 0, 0.0, dh)
    dHp = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dh / 2))
    Lpm = (L1 + L2) / 2
    Cpm = (C1p + C2p) / 2
    hsum = h1p + h2p
    hpm = np.where(
        C1p * C2p == 0,
        hsum,
        np.where(np.abs(h1p - h2p) <= 180, hsum / 2, np.where(hsum < 360, (hsum + 360) / 2, (hsum - 360) / 2)),
    )
    T = (
        1
        - 0.17 * np.cos(np.radians(hpm - 30))
        + 0.24 * np.cos(np.radians(2 * hpm))
        + 0.32 * np.cos(np.radians(3 * hpm + 6))
        - 0.20 * np.cos(np.radians(4 * hpm - 63))
    )
    dtheta = 30 * np.exp(-(((hpm - 275) / 25) ** 2))
    Rc = 2 * np.sqrt(Cpm**7 / (Cpm**7 + 25.0**7))
    Sl = 1 + 0.015 * (Lpm - 50) ** 2 / np.sqrt(20 + (Lpm - 50) ** 2)
    Sc = 1 + 0.045 * Cpm
    Sh = 1 + 0.015 * Cpm * T
    Rt = -np.sin(np.radians(2 * dtheta)) * Rc
    return np.sqrt(
        (dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2 + Rt * (dCp / Sc) * (dHp / Sh)
    )


def palette_rgb(palette_labs) -> np.ndarray:
    """(K, 3) Lab -> (K, 3) uint8 sRGB."""
    return lab_to_srgb(np.asarray(palette_labs, dtype=np.float64).reshape(-1, 3))
