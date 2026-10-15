"""sRGB hex to CIELAB (D65, 2 degree) and CIEDE2000, per CLAUDE.md's colour rules."""

from __future__ import annotations

import math

_M = ((0.4124564, 0.3575761, 0.1804375),
      (0.2126729, 0.7151522, 0.0721750),
      (0.0193339, 0.1191920, 0.9503041))
_WHITE = (0.95047, 1.0, 1.08883)


def parse_hex(colour: str) -> tuple[int, int, int]:
    """'#RRGGBB' or '#RRGGBBAA' (Bambu writes alpha) to 0-255 RGB."""
    h = colour.strip().lstrip("#")
    if len(h) not in (6, 8):
        raise ValueError(f"not a hex colour: {colour!r}")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def hex6(colour: str) -> str:
    r, g, b = parse_hex(colour)
    return f"#{r:02X}{g:02X}{b:02X}"


def hex_to_lab(colour: str) -> tuple[float, float, float]:
    rgb = [c / 255.0 for c in parse_hex(colour)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    xyz = [sum(_M[i][j] * lin[j] for j in range(3)) / _WHITE[i] for i in range(3)]
    eps = (6 / 29) ** 3

    def f(t: float) -> float:
        return t ** (1 / 3) if t > eps else t / (3 * (6 / 29) ** 2) + 4 / 29

    fx, fy, fz = (f(t) for t in xyz)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def ciede2000(lab1, lab2) -> float:
    """CIEDE2000 colour difference (kL = kC = kH = 1), Sharma et al. 2005."""
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    cbar = (c1 + c2) / 2
    g = 0.5 * (1 - math.sqrt(cbar**7 / (cbar**7 + 25**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0
    dlp = l2 - l1
    dcp = c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    else:
        dh = h2p - h1p
        if dh > 180:
            dh -= 360
        elif dh < -180:
            dh += 360
        dhp = dh
    dHp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2))
    lbp = (l1 + l2) / 2
    cbp = (c1p + c2p) / 2
    if c1p * c2p == 0:
        hbp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hbp = (h1p + h2p + 360) / 2
    else:
        hbp = (h1p + h2p - 360) / 2
    t = (1 - 0.17 * math.cos(math.radians(hbp - 30)) + 0.24 * math.cos(math.radians(2 * hbp))
         + 0.32 * math.cos(math.radians(3 * hbp + 6)) - 0.20 * math.cos(math.radians(4 * hbp - 63)))
    dtheta = 30 * math.exp(-(((hbp - 275) / 25) ** 2))
    rc = 2 * math.sqrt(cbp**7 / (cbp**7 + 25**7))
    sl = 1 + 0.015 * (lbp - 50) ** 2 / math.sqrt(20 + (lbp - 50) ** 2)
    sc = 1 + 0.045 * cbp
    sh = 1 + 0.015 * cbp * t
    rt = -math.sin(math.radians(2 * dtheta)) * rc
    return math.sqrt((dlp / sl) ** 2 + (dcp / sc) ** 2 + (dHp / sh) ** 2
                     + rt * (dcp / sc) * (dHp / sh))
