"""Procedural multicolor test object: a capsule with painted bands and a small painted disc."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
from p2p_contracts import Filament, PrintSettings

# Jackson's current Bambu Studio filament setup (slot order), per the orchestrator.
JACKSON_PALETTE = [
    ("generic-pla-161616", "Generic", "PLA", "Black 161616", "#161616",
     "Generic PLA @BBL X2D 0.4 nozzle", "GFL99"),
    ("generic-pla-7c4b00", "Generic", "PLA", "Brown 7C4B00", "#7C4B00",
     "Generic PLA @BBL X2D 0.4 nozzle", "GFL99"),
    ("bambu-pla-basic-f72323", "Bambu Lab", "PLA Basic", "Red F72323", "#F72323",
     "Bambu PLA Basic @BBL X2D 0.4 nozzle", "GFA00"),
    ("generic-petg-0d6284", "Generic", "PETG", "Blue 0D6284", "#0D6284",
     "Generic PETG @BBL X2D 0.4 nozzle", "GFG99"),
    ("generic-pla-ffffff", "Generic", "PLA", "White FFFFFF", "#FFFFFF",
     "Generic PLA @BBL X2D 0.4 nozzle", "GFL99"),
]


def _lab(hex_colour: str) -> tuple[float, float, float]:
    """sRGB hex to CIELAB D65 (nominal)."""
    rgb = np.array([int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)])
    lin = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = m @ lin / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > (6 / 29) ** 3, np.cbrt(xyz), xyz / (3 * (6 / 29) ** 2) + 4 / 29)
    return (round(float(116 * f[1] - 16), 2), round(float(500 * (f[0] - f[1])), 2),
            round(float(200 * (f[1] - f[2])), 2))


def jackson_palette() -> list[Filament]:
    out = []
    for fid, brand, line, name, hx, preset, bid in JACKSON_PALETTE:
        material = "PETG" if "PETG" in line else "PLA"
        out.append(Filament(id=fid, brand=brand, line=line, name=name, material=material, hex=hx,
                            lab=_lab(hx), bambu_filament_id=bid, slicer_preset=preset,
                            density_g_cm3=1.27 if material == "PETG" else 1.24))
    return out


def demo_settings(n_filaments: int = 5) -> PrintSettings:
    return PrintSettings(printer="Bambu Lab X2D", nozzle_mm=0.4, layer_mm=0.2,
                         ams_slots=max(4, n_filaments), slicer="bambu_studio")


def capsule(radius: float = 12.0, height: float = 40.0, n_seg: int = 64,
            n_ring: int = 24) -> tuple[np.ndarray, np.ndarray]:
    """Closed, manifold capsule with a flat base: a cylinder standing on z=0 with a
    hemispherical top, axis along Z, total height `height` mm. Prints without supports."""
    cyl = height - radius
    rings = []  # (z, r) from the base rim to just below the top pole
    n_cyl = max(2, round(cyl / (np.pi * radius / 2 / n_ring)))
    for i in range(n_cyl + 1):
        rings.append((cyl * i / n_cyl, radius))
    for i in range(1, n_ring):  # top hemisphere
        a = np.pi / 2 * i / n_ring
        rings.append((cyl + radius * np.sin(a), radius * np.cos(a)))
    theta = np.linspace(0, 2 * np.pi, n_seg, endpoint=False)
    verts = [(0.0, 0.0, 0.0)]
    for z, r in rings:
        verts += [(r * np.cos(t), r * np.sin(t), z) for t in theta]
    verts.append((0.0, 0.0, height))
    v = np.array(verts)
    faces = []
    n_r = len(rings)
    for j in range(n_seg):  # bottom cap, normals pointing down (-Z)
        faces.append((0, 1 + (j + 1) % n_seg, 1 + j))
    for i in range(n_r - 1):
        a0, b0 = 1 + i * n_seg, 1 + (i + 1) * n_seg
        for j in range(n_seg):
            j1 = (j + 1) % n_seg
            faces.append((a0 + j, a0 + j1, b0 + j1))
            faces.append((a0 + j, b0 + j1, b0 + j))
    top = len(v) - 1
    last = 1 + (n_r - 1) * n_seg
    for j in range(n_seg):
        faces.append((last + j, last + (j + 1) % n_seg, top))
    return v, np.array(faces, dtype=np.int64)


def paint_capsule(v: np.ndarray, f: np.ndarray, slots: list[int],
                  disc_r: float | None = None) -> np.ndarray:
    """Face slots: base colour, two horizontal bands, a disc on the -Y (front) face, a top cap.

    `slots` = [base, band, disc, top]; repeats are allowed. The disc (default radius 0.1 x
    height) sits between the bands so it never cuts a band into slivers."""
    base, band, disc, top = slots
    c = v[f].mean(axis=1)
    h = v[:, 2].max()
    out = np.full(len(f), base, dtype=np.int64)
    zc = c[:, 2]
    out[((zc > 0.30 * h) & (zc < 0.38 * h)) | ((zc > 0.62 * h) & (zc < 0.70 * h))] = band
    out[zc > 0.88 * h] = top
    # Disc centred on the front (-Y) surface at mid height.
    r = np.hypot(c[:, 0], c[:, 2] - 0.5 * h)
    out[(c[:, 1] < 0) & (r < (0.1 * h if disc_r is None else disc_r))] = disc
    return out


def demo_mesh(slots: list[int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    v, f = capsule()
    return v, f, paint_capsule(v, f, slots)


def now() -> datetime:
    return datetime.now(UTC)
