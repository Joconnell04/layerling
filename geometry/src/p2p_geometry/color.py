"""Color helpers: sRGB hex to CIELAB (D65) and CIEDE2000, plus add-on filament choice."""

from __future__ import annotations

import math

from p2p_contracts.filaments import Filament, FilamentLibrary

_D65 = (0.95047, 1.0, 1.08883)


def hex_to_rgb01(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def hex_to_lab(hex_color: str) -> tuple[float, float, float]:
    """sRGB (IEC 61966-2-1) hex to CIELAB, D65, 2 degree observer."""

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in hex_to_rgb01(hex_color))
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b

    def f(t: float) -> float:
        return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29

    fx, fy, fz = f(x / _D65[0]), f(y / _D65[1]), f(z / _D65[2])
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def ciede2000(lab1: tuple[float, float, float], lab2: tuple[float, float, float]) -> float:
    """CIEDE2000 color difference (kL = kC = kH = 1)."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    c1 = math.hypot(a1, b1)
    c2 = math.hypot(a2, b2)
    cbar = (c1 + c2) / 2
    g = 0.5 * (1 - math.sqrt(cbar**7 / (cbar**7 + 25**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0
    dLp = L2 - L1
    dCp = c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    else:
        dhp = h2p - h1p
        if dhp > 180:
            dhp -= 360
        elif dhp < -180:
            dhp += 360
    dHp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2))
    Lbp = (L1 + L2) / 2
    Cbp = (c1p + c2p) / 2
    if c1p * c2p == 0:
        hbp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hbp = (h1p + h2p + 360) / 2
    else:
        hbp = (h1p + h2p - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(hbp - 30))
        + 0.24 * math.cos(math.radians(2 * hbp))
        + 0.32 * math.cos(math.radians(3 * hbp + 6))
        - 0.20 * math.cos(math.radians(4 * hbp - 63))
    )
    d_theta = 30 * math.exp(-(((hbp - 275) / 25) ** 2))
    rc = 2 * math.sqrt(Cbp**7 / (Cbp**7 + 25**7))
    sl = 1 + 0.015 * (Lbp - 50) ** 2 / math.sqrt(20 + (Lbp - 50) ** 2)
    sc = 1 + 0.045 * Cbp
    sh = 1 + 0.015 * Cbp * t
    rt = -math.sin(math.radians(2 * d_theta)) * rc
    return math.sqrt(
        (dLp / sl) ** 2 + (dCp / sc) ** 2 + (dHp / sh) ** 2 + rt * (dCp / sc) * (dHp / sh)
    )


def palette(library: FilamentLibrary) -> list[Filament]:
    """Filaments available for add-ons: owned and loaded in the AMS, else owned, else all."""
    loaded = [f for f in library.filaments if f.owned and f.ams_slot is not None]
    if loaded:
        return sorted(loaded, key=lambda f: f.ams_slot or 0)
    owned = [f for f in library.filaments if f.owned]
    return owned or list(library.filaments)


def find(library: FilamentLibrary, filament_id: str | None) -> Filament | None:
    if filament_id is None:
        return None
    return next((f for f in library.filaments if f.id == filament_id), None)


def default_structure_filament(library: FilamentLibrary) -> Filament:
    """Base, plate, backing and loop default: the darkest low-chroma filament in the palette.

    Prefers PLA because PLA and PETG bond poorly to each other and the figure is usually PLA.
    """
    pal = palette(library)
    pla = [f for f in pal if f.material.upper() == "PLA"] or pal
    return min(pla, key=lambda f: (f.lab[0] + 0.5 * math.hypot(f.lab[1], f.lab[2])))


def highest_contrast(
    library: FilamentLibrary, against: Filament, exclude: set[str] | None = None
) -> tuple[Filament, float, bool]:
    """Palette filament with the highest CIEDE2000 against `against`.

    Returns (filament, delta_e, same_material). Filaments of the same material are preferred
    because a PETG layer on a PLA plate delaminates.
    """
    exclude = exclude or set()
    pal = [f for f in palette(library) if f.id != against.id and f.id not in exclude]
    same = [f for f in pal if f.material.upper() == against.material.upper()]
    pool = same or pal
    best = max(pool, key=lambda f: ciede2000(tuple(f.lab), tuple(against.lab)))
    return best, ciede2000(tuple(best.lab), tuple(against.lab)), bool(same)
