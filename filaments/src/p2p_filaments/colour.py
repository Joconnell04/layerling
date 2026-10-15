"""Vectorised colour maths: sRGB (IEC 61966-2-1), XYZ, CIELAB, Bradford adaptation, CIEDE2000.

Same constants as p2p_threemf.colour (sRGB matrix and D65 white), so library values computed
there and here agree. Arrays are (..., 3).
"""

from __future__ import annotations

import numpy as np

SRGB_TO_XYZ = np.array([[0.4124564, 0.3575761, 0.1804375],
                        [0.2126729, 0.7151522, 0.0721750],
                        [0.0193339, 0.1191920, 0.9503041]])
XYZ_TO_SRGB = np.linalg.inv(SRGB_TO_XYZ)
D65 = np.array([0.95047, 1.0, 1.08883])


def _xy_to_xyz(x: float, y: float) -> np.ndarray:
    return np.array([x / y, 1.0, (1 - x - y) / y])


# ICC D50, the illuminant of X-Rite's ColorChecker L*a*b* data.
D50 = _xy_to_xyz(0.3457, 0.3585)

BRADFORD = np.array([[0.8951, 0.2664, -0.1614],
                     [-0.7502, 1.7135, 0.0367],
                     [0.0389, -0.0685, 1.0296]])


def srgb_decode(v) -> np.ndarray:
    """sRGB-encoded 0..1 to linear 0..1."""
    v = np.asarray(v, dtype=np.float64)
    return np.where(v <= 0.04045, v / 12.92, ((np.maximum(v, 0) + 0.055) / 1.055) ** 2.4)


def srgb_encode(lin) -> np.ndarray:
    """Linear 0..1 to sRGB-encoded 0..1 (no clipping above 1)."""
    lin = np.asarray(lin, dtype=np.float64)
    lin = np.maximum(lin, 0)
    return np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)


def _f(t: np.ndarray) -> np.ndarray:
    d = 6 / 29
    return np.where(t > d**3, np.cbrt(t), t / (3 * d * d) + 4 / 29)


def _finv(t: np.ndarray) -> np.ndarray:
    d = 6 / 29
    return np.where(t > d, t**3, 3 * d * d * (t - 4 / 29))


def xyz_to_lab(xyz, white=D65) -> np.ndarray:
    xyz = np.asarray(xyz, dtype=np.float64) / white
    fx, fy, fz = _f(xyz[..., 0]), _f(xyz[..., 1]), _f(xyz[..., 2])
    return np.stack([116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)], axis=-1)


def lab_to_xyz(lab, white=D65) -> np.ndarray:
    lab = np.asarray(lab, dtype=np.float64)
    fy = (lab[..., 0] + 16) / 116
    fx = fy + lab[..., 1] / 500
    fz = fy - lab[..., 2] / 200
    return np.stack([_finv(fx), _finv(fy), _finv(fz)], axis=-1) * white


def adapt(xyz, src_white, dst_white) -> np.ndarray:
    """Bradford chromatic adaptation (von Kries in the Bradford cone space)."""
    s = BRADFORD @ np.asarray(src_white)
    d = BRADFORD @ np.asarray(dst_white)
    m = np.linalg.inv(BRADFORD) @ np.diag(d / s) @ BRADFORD
    return np.asarray(xyz, dtype=np.float64) @ m.T


def linear_to_lab(lin) -> np.ndarray:
    """Linear sRGB (D65) to CIELAB D65."""
    return xyz_to_lab(np.asarray(lin, dtype=np.float64) @ SRGB_TO_XYZ.T)


def lab_to_linear(lab) -> np.ndarray:
    return lab_to_xyz(lab) @ XYZ_TO_SRGB.T


def hex_to_lab(colour: str) -> np.ndarray:
    h = colour.strip().lstrip("#")
    rgb = np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)]) / 255.0
    return linear_to_lab(srgb_decode(rgb))


def lab_to_hex(lab) -> str:
    """Nearest 8-bit sRGB (clipped) for display."""
    rgb = np.clip(srgb_encode(lab_to_linear(lab)), 0, 1)
    r, g, b = (round(float(c) * 255) for c in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def ciede2000(lab1, lab2) -> np.ndarray:
    """CIEDE2000 (kL = kC = kH = 1), Sharma, Wu and Dalal 2005. Broadcasts over (..., 3)."""
    lab1 = np.asarray(lab1, dtype=np.float64)
    lab2 = np.asarray(lab2, dtype=np.float64)
    l1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    l2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    c1, c2 = np.hypot(a1, b1), np.hypot(a2, b2)
    cbar7 = ((c1 + c2) / 2) ** 7
    g = 0.5 * (1 - np.sqrt(cbar7 / (cbar7 + 25.0**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.where(c1p == 0, 0.0, np.degrees(np.arctan2(b1, a1p)) % 360)
    h2p = np.where(c2p == 0, 0.0, np.degrees(np.arctan2(b2, a2p)) % 360)
    dlp = l2 - l1
    dcp = c2p - c1p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    zero = (c1p * c2p) == 0
    dh = np.where(zero, 0.0, dh)
    dHp = 2 * np.sqrt(c1p * c2p) * np.sin(np.radians(dh / 2))
    lbp = (l1 + l2) / 2
    cbp = (c1p + c2p) / 2
    hsum = h1p + h2p
    hbp = np.where(np.abs(h1p - h2p) <= 180, hsum / 2,
                   np.where(hsum < 360, (hsum + 360) / 2, (hsum - 360) / 2))
    hbp = np.where(zero, hsum, hbp)
    t = (1 - 0.17 * np.cos(np.radians(hbp - 30)) + 0.24 * np.cos(np.radians(2 * hbp))
         + 0.32 * np.cos(np.radians(3 * hbp + 6)) - 0.20 * np.cos(np.radians(4 * hbp - 63)))
    dtheta = 30 * np.exp(-(((hbp - 275) / 25) ** 2))
    cbp7 = cbp**7
    rc = 2 * np.sqrt(cbp7 / (cbp7 + 25.0**7))
    sl = 1 + 0.015 * (lbp - 50) ** 2 / np.sqrt(20 + (lbp - 50) ** 2)
    sc = 1 + 0.045 * cbp
    sh = 1 + 0.015 * cbp * t
    rt = -np.sin(np.radians(2 * dtheta)) * rc
    return np.sqrt((dlp / sl) ** 2 + (dcp / sc) ** 2 + (dHp / sh) ** 2
                   + rt * (dcp / sc) * (dHp / sh))
