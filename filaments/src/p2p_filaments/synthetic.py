"""A known card-and-checker scene for tests and `demo-photo`.

The filament colours and decay lengths below are ASSUMED, plausible values for Jackson's five
AMS filaments. They are not measurements; they exist so the pipeline can be checked against a
known answer.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .checker import reference_lab_d65
from .colour import lab_to_linear
from .layout import CardLayout
from .render import Camera, CardColours, SyntheticPhoto, synth_photo
from .wedge import td_from_model

# slot order 1..5: black, brown, red, blue PETG, white
TRUE_LAB = np.array([
    [22.0, 0.5, -0.8],
    [38.0, 14.0, 30.0],
    [46.0, 58.0, 38.0],
    [39.0, -9.0, -27.0],
    [91.5, -0.6, 2.5],
])
TRUE_LAMBDA = np.array([  # mm, per linear sRGB channel
    [0.04, 0.04, 0.04],
    [0.22, 0.16, 0.14],
    [0.80, 0.18, 0.20],
    [0.20, 0.30, 0.45],
    [0.55, 0.55, 0.50],
])
BLACK_SLOT, WHITE_SLOT = 1, 5


@dataclass
class Scene:
    layout: CardLayout
    colours: CardColours
    photo: SyntheticPhoto
    true_lab: np.ndarray
    true_td: np.ndarray


def number_slots(lab: np.ndarray) -> list[int]:
    return [WHITE_SLOT if L < 45 else BLACK_SLOT for L in lab[:, 0]]


def scene(seed: int = 0, camera: Camera | None = None, checker: str = "classic24",
          threshold: float = 2.0, **photo_kw) -> Scene:
    layout = CardLayout(("1", "2", "3", "4", "5"))
    lin = lab_to_linear(TRUE_LAB)
    colours = CardColours.from_model(lin, BLACK_SLOT, WHITE_SLOT, number_slots(TRUE_LAB),
                                     TRUE_LAMBDA)
    photo = synth_photo(layout, colours, reference_lab_d65(checker), camera=camera, seed=seed,
                        **photo_kw)
    td = np.array([td_from_model(TRUE_LAMBDA[i], lin[i], lin[BLACK_SLOT - 1],
                                 lin[WHITE_SLOT - 1], threshold)[0] for i in range(5)])
    return Scene(layout, colours, photo, TRUE_LAB.copy(), td)
