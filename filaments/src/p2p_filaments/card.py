"""Swatch card mesh and 3MF: one part per role, each with a part-level filament.

Parts (all in one object, touching, no overlaps beyond 0.01 mm):
  swatch_card   white: marker strips (minus black marker cells) and the white wedge base
  base_black    black wedge base
  markers       black ArUco cells
  tile_<slot>   2.0 mm tile of each filament
  wedge_<slot>  the ten wedge steps of each filament (flush with the card top)
  numbers_black / numbers_white   raised AMS slot numbers

The process runs at 100 % infill so the tiles and bases are solid all the way down; the
slicer reports that as a modified process preset.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from p2p_contracts import Filament, PrintSettings
from p2p_threemf import write_3mf
from p2p_threemf.products import box, merge
from p2p_threemf.writer import Part

from .colour import hex_to_lab
from .layout import DIGIT_RAISE, STEPS_MM, TOP_Z, CardLayout, Rect

EPS = 0.01  # boxes of one part overlap by this much so the slicer sees one solid
CONFIG_OVERRIDES = {"sparse_infill_density": "100%", "sparse_infill_pattern": "zig-zag"}
SIDECAR_KEY = "swatch_card"


@dataclass
class CardPlan:
    layout: CardLayout
    palette: list[Filament]
    black_slot: int  # project slots (palette index + 1)
    white_slot: int

    def column_slot(self, i: int) -> int:
        return i + 1

    def number_slot(self, i: int) -> int:
        """White numbers on dark tiles, black on light ones."""
        lab = self.palette[i].lab
        return self.white_slot if lab[0] < 45 else self.black_slot

    def sidecar(self) -> dict:
        return {
            "layout": self.layout.to_dict(),
            "black_slot": self.black_slot,
            "white_slot": self.white_slot,
            "columns": [{"column": i, "slot": self.column_slot(i), "ams_slot": f.ams_slot,
                         "filament_id": f.id, "hex": f.hex.upper()}
                        for i, f in enumerate(self.palette)],
        }


def plan_card(palette: list[Filament]) -> CardPlan:
    """Black base = darkest filament, white base = lightest (by nominal or measured L*)."""
    if len(palette) < 2:
        raise ValueError("the card needs at least a dark and a light filament")
    lightness = [f.lab[0] for f in palette]
    black = int(np.argmin(lightness)) + 1
    white = int(np.argmax(lightness)) + 1
    labels = tuple(str(f.ams_slot if f.ams_slot is not None else i + 1)
                   for i, f in enumerate(palette))
    return CardPlan(CardLayout(labels), palette, black, white)


def _boxes(items: list[tuple[Rect, float, float]]) -> tuple[np.ndarray, np.ndarray]:
    return merge([box((r.x0 - EPS, r.y0 - EPS, z0), (r.x1 + EPS, r.y1 + EPS, z1))
                  for r, z0, z1 in items])


def _strip_white(layout: CardLayout) -> list[Rect]:
    """White cells of both strips on a 0.5 mm grid, as horizontal runs."""
    g = 0.5
    out = []
    for strip in layout.strips():
        nx = round((strip.x1 - strip.x0) / g)
        ny = round((strip.y1 - strip.y0) / g)
        white = np.ones((ny, nx), dtype=bool)  # row 0 at the strip bottom
        for mid in range(4):
            for r, black in layout.marker_cells(mid):
                if not black or not (strip.y0 <= r.y0 < strip.y1):
                    continue
                c0, c1 = round((r.x0 - strip.x0) / g), round((r.x1 - strip.x0) / g)
                r0, r1 = round((r.y0 - strip.y0) / g), round((r.y1 - strip.y0) / g)
                white[r0:r1, c0:c1] = False
        for row in range(ny):
            c = 0
            while c < nx:
                if not white[row, c]:
                    c += 1
                    continue
                c0 = c
                while c < nx and white[row, c]:
                    c += 1
                out.append(Rect(strip.x0 + c0 * g, strip.y0 + row * g,
                                strip.x0 + c * g, strip.y0 + (row + 1) * g))
    return out


def build_parts(plan: CardPlan) -> list[tuple[str, int, np.ndarray, np.ndarray]]:
    """(name, slot, vertices, faces) per part; the first is the white body."""
    lay = plan.layout
    white = [(r, 0.0, TOP_Z) for r in _strip_white(lay)]
    black_base = []
    for i in range(lay.n):
        for j, t in enumerate(STEPS_MM):
            white.append((lay.step(i, "white", j), 0.0, TOP_Z - t))
            black_base.append((lay.step(i, "black", j), 0.0, TOP_Z - t))
    markers = [(r, 0.0, TOP_Z) for mid in range(4) for r, b in lay.marker_cells(mid) if b]
    parts = [("swatch_card", plan.white_slot, *_boxes(white)),
             ("base_black", plan.black_slot, *_boxes(black_base)),
             ("markers", plan.black_slot, *_boxes(markers))]
    for i in range(lay.n):
        slot = plan.column_slot(i)
        parts.append((f"tile_{lay.labels[i]}", slot, *_boxes([(lay.tile(i), 0.0, TOP_Z)])))
        steps = [(lay.step(i, base, j), TOP_Z - t, TOP_Z)
                 for base in ("black", "white") for j, t in enumerate(STEPS_MM)]
        parts.append((f"wedge_{lay.labels[i]}", slot, *_boxes(steps)))
    for slot, name in ((plan.black_slot, "numbers_black"), (plan.white_slot, "numbers_white")):
        runs = [(r, TOP_Z, TOP_Z + DIGIT_RAISE) for i in range(lay.n)
                if plan.number_slot(i) == slot for r in lay.digit_runs(i)]
        if runs:
            parts.append((name, slot, *_boxes(runs)))
    return parts


def card_settings(n_filaments: int) -> PrintSettings:
    return PrintSettings(printer="Bambu Lab X2D", nozzle_mm=0.4, layer_mm=0.2,
                         ams_slots=max(n_filaments, 4), slicer="bambu_studio")


def write_card(palette: list[Filament], out_path: str | Path) -> dict:
    plan = plan_card(palette)
    parts = build_parts(plan)
    _, s0, v0, f0 = parts[0]
    extra = [Part(v, f, slot=s, name=name) for name, s, v, f in parts[1:]]
    res = write_3mf(v0, f0, np.full(len(f0), s0), palette, card_settings(len(palette)),
                    out_path, object_name="swatch_card",
                    config_overrides=dict(CONFIG_OVERRIDES),
                    extra_sidecar={SIDECAR_KEY: plan.sidecar()}, parts=extra)
    res["expected_slots"] = len(palette)
    res["size_mm"] = [plan.layout.width, plan.layout.height, TOP_Z + DIGIT_RAISE]
    return res


def read_card_sidecar(path: str | Path) -> dict | None:
    """The swatch_card section of a card 3MF's p2p sidecar, or None."""
    try:
        with zipfile.ZipFile(path) as z:
            side = json.loads(z.read("Metadata/p2p_threemf.json"))
    except (FileNotFoundError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return None
    return side.get(SIDECAR_KEY)


def nominal_lab(palette: list[Filament]) -> np.ndarray:
    return np.array([f.lab if f.lab is not None else hex_to_lab(f.hex) for f in palette])
