"""Swatch card layout in card millimetres: x right, y up (top view), z up from the bed.

The mesh builder, the preview and synthetic renderer, and the photo sampler all read this, so
they agree on where everything is. One column per AMS filament, 15 mm wide:

    y 40..49  white strip, ArUco markers 0 (left) and 1 (right)
    y 25..40  tile: 2.0 mm of the filament, AMS slot number raised 0.4 mm in the top left
    y 17..25  wedge over black: 0.2 0.4 0.6 0.8 1.2 mm of the filament on a black base
    y  9..17  wedge over white: the same steps on a white base
    y  0..9   white strip, ArUco markers 2 (left) and 3 (right)

The whole top surface is flat at z = 2.0 except the raised numbers: under a step of thickness
t the base fills z = 0 .. 2.0 - t, so every step is exactly t of colour over at least 0.8 mm
of opaque base. Five filaments make a 75 x 49 mm card.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

import numpy as np

LAYOUT_VERSION = 1
TILE_MM = 15.0
TOP_Z = 2.0
STEPS_MM = (0.2, 0.4, 0.6, 0.8, 1.2)
WEDGE_H = 8.0
MARKER_CELL = 1.0
MARKER_CELLS = 6  # 4 x 4 data bits plus a one-cell black border
MARKER_MARGIN = 1.5
STRIP_H = MARKER_CELLS * MARKER_CELL + 2 * MARKER_MARGIN
ARUCO_DICT = "DICT_4X4_50"
MARKER_IDS = (0, 1, 2, 3)  # top left, top right, bottom left, bottom right
DIGIT_PIXEL = 1.2
DIGIT_RAISE = 0.4
DIGIT_INSET = 1.5
TILE_EDGE = 1.5  # sampling stays this far inside a tile and away from the number
STEP_EDGE_X = 0.6
STEP_EDGE_Y = 1.0
BASES = ("black", "white")

# 3 x 5 pixel digits, rows top to bottom.
GLYPHS = {
    "0": ["111", "1.1", "1.1", "1.1", "111"],
    "1": [".1.", "11.", ".1.", ".1.", "111"],
    "2": ["111", "..1", "111", "1..", "111"],
    "3": ["111", "..1", "111", "..1", "111"],
    "4": ["1.1", "1.1", "111", "..1", "..1"],
    "5": ["111", "1..", "111", "..1", "111"],
    "6": ["111", "1..", "111", "1.1", "111"],
    "7": ["111", "..1", "..1", "..1", "..1"],
    "8": ["111", "1.1", "111", "1.1", "111"],
    "9": ["111", "1.1", "111", "..1", "111"],
}


@dataclass(frozen=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    def inset(self, dx: float, dy: float | None = None) -> Rect:
        dy = dx if dy is None else dy
        return Rect(self.x0 + dx, self.y0 + dy, self.x1 - dx, self.y1 - dy)

    @property
    def corners(self) -> np.ndarray:
        """(4, 2) counter-clockwise from bottom left."""
        return np.array([[self.x0, self.y0], [self.x1, self.y0], [self.x1, self.y1],
                         [self.x0, self.y1]], dtype=np.float64)

    @property
    def area(self) -> float:
        return max(0.0, self.x1 - self.x0) * max(0.0, self.y1 - self.y0)

    @property
    def centre(self) -> tuple[float, float]:
        return (self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2


@cache
def marker_bits(marker_id: int) -> np.ndarray:
    """(6, 6) bool, True = black, row 0 at the top of the marker (OpenCV's own pattern)."""
    import cv2

    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, ARUCO_DICT))
    img = d.generateImageMarker(marker_id, MARKER_CELLS)
    return img < 128


@dataclass(frozen=True)
class CardLayout:
    """Geometry for `labels` columns; labels are the printed numbers (AMS slots)."""

    labels: tuple[str, ...]

    @property
    def n(self) -> int:
        return len(self.labels)

    @property
    def width(self) -> float:
        return self.n * TILE_MM

    @property
    def height(self) -> float:
        return 2 * STRIP_H + TILE_MM + 2 * WEDGE_H

    # bands, bottom to top
    @property
    def y_white(self) -> float:
        return STRIP_H

    @property
    def y_black(self) -> float:
        return STRIP_H + WEDGE_H

    @property
    def y_tile(self) -> float:
        return STRIP_H + 2 * WEDGE_H

    def tile(self, i: int) -> Rect:
        x = i * TILE_MM
        return Rect(x, self.y_tile, x + TILE_MM, self.y_tile + TILE_MM)

    def step(self, i: int, base: str, j: int) -> Rect:
        w = TILE_MM / len(STEPS_MM)
        y0 = self.y_black if base == "black" else self.y_white
        x = i * TILE_MM + j * w
        return Rect(x, y0, x + w, y0 + WEDGE_H)

    def step_sample(self, i: int, base: str, j: int) -> Rect:
        return self.step(i, base, j).inset(STEP_EDGE_X, STEP_EDGE_Y)

    def strips(self) -> list[Rect]:
        return [Rect(0, 0, self.width, STRIP_H),
                Rect(0, self.height - STRIP_H, self.width, self.height)]

    # raised numbers
    def digit_box(self, i: int) -> Rect:
        t = self.tile(i)
        k = len(self.labels[i])
        w = (3 * k + (k - 1)) * DIGIT_PIXEL
        x0, y1 = t.x0 + DIGIT_INSET, t.y1 - DIGIT_INSET
        return Rect(x0, y1 - 5 * DIGIT_PIXEL, x0 + w, y1)

    def digit_runs(self, i: int) -> list[Rect]:
        """Horizontal pixel runs of the number, one Rect per run."""
        box = self.digit_box(i)
        out = []
        for k, ch in enumerate(self.labels[i]):
            gx = box.x0 + k * 4 * DIGIT_PIXEL
            for r, row in enumerate(GLYPHS[ch]):
                y1 = box.y1 - r * DIGIT_PIXEL
                c = 0
                while c < 3:
                    if row[c] != "1":
                        c += 1
                        continue
                    c0 = c
                    while c < 3 and row[c] == "1":
                        c += 1
                    out.append(Rect(gx + c0 * DIGIT_PIXEL, y1 - DIGIT_PIXEL,
                                    gx + c * DIGIT_PIXEL, y1))
        return out

    def tile_samples(self, i: int) -> list[Rect]:
        """Areas of tile i that are sampled: away from the edges and the raised number."""
        t = self.tile(i).inset(TILE_EDGE)
        d = self.digit_box(i)
        right = Rect(max(t.x0, d.x1 + 1.0), t.y0, t.x1, t.y1)
        below = Rect(t.x0, t.y0, right.x0, d.y0 - 1.0)
        return [r for r in (right, below) if r.area > 1.0]

    def white_refs(self) -> list[Rect]:
        """Four patches of the white strips between the markers (top left, top right,
        bottom left, bottom right). Same filament everywhere, so they map the light across
        the card."""
        x0 = MARKER_MARGIN + MARKER_CELLS * MARKER_CELL + TILE_EDGE
        x1 = self.width - x0
        xm = self.width / 2
        out = []
        for strip in reversed(self.strips()):
            y0, y1 = strip.y0 + TILE_EDGE, strip.y1 - TILE_EDGE
            out += [Rect(x0, y0, xm - 1.0, y1), Rect(xm + 1.0, y0, x1, y1)]
        return out

    # ArUco markers
    def marker_origin(self, marker_id: int) -> tuple[float, float]:
        """Top-left corner of the marker in card mm."""
        size = MARKER_CELLS * MARKER_CELL
        left = MARKER_MARGIN
        right = self.width - MARKER_MARGIN - size
        top = self.height - MARKER_MARGIN
        bottom = STRIP_H - MARKER_MARGIN
        return {0: (left, top), 1: (right, top), 2: (left, bottom), 3: (right, bottom)}[marker_id]

    def marker_corners(self, marker_id: int) -> np.ndarray:
        """(4, 2) in OpenCV's corner order: top left, top right, bottom right, bottom left."""
        x0, yt = self.marker_origin(marker_id)
        s = MARKER_CELLS * MARKER_CELL
        return np.array([[x0, yt], [x0 + s, yt], [x0 + s, yt - s], [x0, yt - s]], np.float64)

    def marker_cells(self, marker_id: int) -> list[tuple[Rect, bool]]:
        """Every cell of the marker with True for black."""
        x0, yt = self.marker_origin(marker_id)
        bits = marker_bits(marker_id)
        c = MARKER_CELL
        return [(Rect(x0 + col * c, yt - (row + 1) * c, x0 + (col + 1) * c, yt - row * c),
                 bool(bits[row, col]))
                for row in range(MARKER_CELLS) for col in range(MARKER_CELLS)]

    def to_dict(self) -> dict:
        return {"version": LAYOUT_VERSION, "labels": list(self.labels),
                "size_mm": [self.width, self.height], "tile_mm": TILE_MM, "top_z": TOP_Z,
                "steps_mm": list(STEPS_MM), "wedge_h": WEDGE_H, "strip_h": STRIP_H,
                "aruco": ARUCO_DICT, "marker_cell": MARKER_CELL}
