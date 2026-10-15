"""Procedural test products that are hard on purpose (keychain, nameplate, figurine).

Each builder returns a Product: the painted figure (vertices, faces, face_slots), add-on parts
with a whole-part filament (keyring loop, plate, raised text), and the slots a correct slice
must use. Slots follow Jackson's AMS (demo.jackson_palette): 1 black, 2 brown, 3 red,
4 blue PETG, 5 white. Geometry frame: mm, Z up, base on z = 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .demo import capsule, paint_capsule
from .writer import Part

BLACK, BROWN, RED, BLUE_PETG, WHITE = 1, 2, 3, 4, 5


@dataclass
class Product:
    name: str
    vertices: np.ndarray
    faces: np.ndarray
    face_slots: np.ndarray
    parts: list[Part] = field(default_factory=list)
    expected_slots: int = 0
    notes: str = ""


# ------------------------------------------------------------------ primitives


def box(lo, hi) -> tuple[np.ndarray, np.ndarray]:
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], dtype=float)
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                  [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    return v, f


def merge(meshes: list[tuple[np.ndarray, np.ndarray]]) -> tuple[np.ndarray, np.ndarray]:
    vs, fs, off = [], [], 0
    for v, f in meshes:
        vs.append(v)
        fs.append(f + off)
        off += len(v)
    return np.concatenate(vs), np.concatenate(fs)


def torus(major: float, minor: float, center, n_major: int = 48, n_minor: int = 16
          ) -> tuple[np.ndarray, np.ndarray]:
    """Torus lying flat (axis Z), closed and outward facing."""
    cx, cy, cz = center
    u = np.linspace(0, 2 * np.pi, n_major, endpoint=False)
    w = np.linspace(0, 2 * np.pi, n_minor, endpoint=False)
    uu, ww = np.meshgrid(u, w, indexing="ij")
    x = cx + (major + minor * np.cos(ww)) * np.cos(uu)
    y = cy + (major + minor * np.cos(ww)) * np.sin(uu)
    z = cz + minor * np.sin(ww)
    v = np.stack([x, y, z], axis=-1).reshape(-1, 3)
    faces = []
    for i in range(n_major):
        for j in range(n_minor):
            a = i * n_minor + j
            b = ((i + 1) % n_major) * n_minor + j
            c = ((i + 1) % n_major) * n_minor + (j + 1) % n_minor
            d = i * n_minor + (j + 1) % n_minor
            faces += [(a, b, c), (a, c, d)]
    return v, np.array(faces)


def badge(radius: float, height: float, spacing: float = 0.25, n_side: int = 8
          ) -> tuple[np.ndarray, np.ndarray]:
    """Flat cylinder on z=0 whose top face is finely triangulated (for small painted details)."""
    n_rings = max(2, round(radius / spacing))
    n_seg = max(24, round(2 * np.pi * radius / spacing))
    theta = np.linspace(0, 2 * np.pi, n_seg, endpoint=False)
    verts = [(0.0, 0.0, height)]
    for i in range(1, n_rings + 1):  # top rings, same segment count so the grid is regular
        r = radius * i / n_rings
        verts += [(r * np.cos(t), r * np.sin(t), height) for t in theta]
    for k in range(1, n_side + 1):  # side rings from the top rim down to the bed
        z = height * (1 - k / n_side)
        verts += [(radius * np.cos(t), radius * np.sin(t), z) for t in theta]
    verts.append((0.0, 0.0, 0.0))
    v = np.array(verts)
    faces = []
    for j in range(n_seg):  # top centre fan
        faces.append((0, 1 + j, 1 + (j + 1) % n_seg))
    ring = lambda i: 1 + (i - 1) * n_seg
    total_rings = n_rings + n_side
    for i in range(1, total_rings):
        a0, b0 = ring(i), ring(i + 1)
        for j in range(n_seg):
            j1 = (j + 1) % n_seg
            if i < n_rings:  # top annulus: outward in radius, normals +Z
                faces += [(a0 + j, b0 + j, b0 + j1), (a0 + j, b0 + j1, a0 + j1)]
            else:  # side wall going down, normals outward
                faces += [(a0 + j, b0 + j, b0 + j1), (a0 + j, b0 + j1, a0 + j1)]
    bottom = len(v) - 1
    last = ring(total_rings)
    for j in range(n_seg):  # bottom fan, normals -Z
        faces.append((bottom, last + (j + 1) % n_seg, last + j))
    return v, np.array(faces)


# 5 x 7 pixel glyphs, rows top to bottom.
GLYPHS = {
    "B": ["1111.", "1...1", "1...1", "1111.", "1...1", "1...1", "1111."],
    "I": ["11111", "..1..", "..1..", "..1..", "..1..", "..1..", "11111"],
    "S": [".1111", "1....", "1....", ".111.", "....1", "....1", "1111."],
    "C": [".1111", "1....", "1....", "1....", "1....", "1....", ".1111"],
    "U": ["1...1", "1...1", "1...1", "1...1", "1...1", "1...1", ".111."],
    "T": ["11111", "..1..", "..1..", "..1..", "..1..", "..1..", "..1.."],
}


def pixel_text(text: str, pixel: float, z0: float, z1: float, origin=(0.0, 0.0),
               gap: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Raised block letters: one box per horizontal pixel run, slightly overlapping."""
    boxes = []
    ox, oy = origin
    eps = 0.02
    for k, ch in enumerate(text.upper()):
        rows = GLYPHS[ch]
        x_letter = ox + k * (5 + gap) * pixel
        for r, row in enumerate(rows):
            y = oy + (6 - r) * pixel
            c = 0
            while c < 5:
                if row[c] != "1":
                    c += 1
                    continue
                c0 = c
                while c < 5 and row[c] == "1":
                    c += 1
                boxes.append(box((x_letter + c0 * pixel - eps, y - eps, z0),
                                 (x_letter + c * pixel + eps, y + pixel + eps, z1)))
    return merge(boxes)


def text_width(text: str, pixel: float, gap: float = 1.0) -> float:
    n = len(text)
    return (n * 5 + (n - 1) * gap) * pixel


# ------------------------------------------------------------------ products


def _disc_mask(c: np.ndarray, center, diameter: float, top_z: float) -> np.ndarray:
    on_top = c[:, 2] > top_z - 1e-6
    return on_top & (np.hypot(c[:, 0] - center[0], c[:, 1] - center[1]) <= diameter / 2)


def keychain(eye_d: float = 1.2, nose_d: float = 1.5, spacing: float = 0.25) -> Product:
    """45 mm keychain: a white badge face with small black eyes and a red nose painted on top,
    and a separate brown 2.5 mm keyring loop overlapping the rim."""
    radius, height = 18.0, 5.0
    v, f = badge(radius, height, spacing=spacing)
    c = v[f].mean(axis=1)
    s = np.full(len(f), WHITE)
    for ex in (-5.0, 5.0):
        s[_disc_mask(c, (ex, 3.0), eye_d, height)] = BLACK
    s[_disc_mask(c, (0.0, -2.5), nose_d, height)] = RED
    loop_minor = 1.25  # 2.5 mm thick ring
    loop_major = 4.0
    lv, lf = torus(loop_major, loop_minor, (0.0, radius + loop_major - 1.0, loop_minor))
    total = radius + (radius + 2 * loop_major - 1.0 + loop_minor)
    return Product(
        name="keychain", vertices=v, faces=f, face_slots=s,
        parts=[Part(lv, lf, slot=BROWN, name="keyring_loop")],
        expected_slots=len({WHITE, BLACK, RED, BROWN}),
        notes=f"{total:.1f} mm long, eyes {eye_d} mm, nose {nose_d} mm, loop 2.5 mm thick")


def nameplate(text: str = "BISCUIT") -> Product:
    """120 mm plate (brown) with raised white text and a painted figurine standing on it."""
    plate_v, plate_f = box((-60.0, -15.0, 0.0), (60.0, 15.0, 3.0))
    pixel = 1.6
    w = text_width(text, pixel)
    tv, tf = pixel_text(text, pixel, z0=2.8, z1=4.0, origin=(-56.0, -7 * pixel / 2))
    assert w < 80, "text must leave room for the figurine"
    fv, ff = capsule(radius=8.0, height=26.0, n_seg=48, n_ring=12)
    fs = paint_capsule(fv, ff, [WHITE, BLACK, BLUE_PETG, RED], disc_r=2.0)
    fv = fv + np.array([44.0, 0.0, 2.5])  # stands on the plate, sunk 0.5 mm into it
    return Product(
        name="nameplate", vertices=fv, faces=ff, face_slots=fs,
        parts=[Part(plate_v, plate_f, slot=BROWN, name="plate"),
               Part(tv, tf, slot=WHITE, name=f"text_{text}")],
        expected_slots=len({WHITE, BLACK, BLUE_PETG, RED, BROWN}),
        notes=f"text {w:.1f} mm wide, {7 * pixel:.1f} mm tall, 1.0 mm relief")


def figurine() -> Product:
    v, f = capsule()
    s = paint_capsule(v, f, [WHITE, BLACK, RED, BROWN])
    return Product(name="figurine", vertices=v, faces=f, face_slots=s,
                   expected_slots=len({WHITE, BLACK, RED, BROWN}), notes="40 mm capsule")


def keychain_too_thin() -> Product:
    """Same keychain with 0.5 mm eyes and nose: below the 0.8 mm minimum on a 0.4 nozzle."""
    p = keychain(eye_d=0.5, nose_d=0.6, spacing=0.12)
    p.name = "keychain_too_thin"
    return p


PRODUCTS = {"keychain": keychain, "nameplate": nameplate, "figurine": figurine,
            "keychain_too_thin": keychain_too_thin}
