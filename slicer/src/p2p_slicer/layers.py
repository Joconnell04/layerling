"""Layer plan (section 3.1 of the spec).

First layer [0, initial_layer_print_height]. Then layers of `layer_height` (or the variable
profile's height at the layer's bottom) are added while the layer's mid plane is below the
object's top. print_z is the layer top, slice_z the mid plane (the first layer slices at half its
height).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class LayerPlan:
    print_z: np.ndarray       # layer tops, mm
    height: np.ndarray        # layer heights, mm
    slice_z: np.ndarray       # mid planes, mm

    def __len__(self) -> int:
        return len(self.print_z)

    @property
    def bottom_z(self) -> np.ndarray:
        return self.print_z - self.height


def parse_profile(text: str) -> list[tuple[float, float]]:
    """Metadata/layer_heights_profile.txt: 'object_id=1|z;h;z;h;...' -> [(z, h)]."""
    body = text.split("|", 1)[1] if "|" in text else text
    vals = [float(v) for v in body.replace("\n", "").split(";") if v.strip()]
    return list(zip(vals[0::2], vals[1::2]))


def _profile_height(profile: list[tuple[float, float]], z: float) -> float:
    h = profile[0][1]
    for pz, ph in profile:
        if pz <= z + 1e-9:
            h = ph
        else:
            break
    return h


def plan_layers(z_max: float, layer_height: float, first_layer_height: float,
                profile: list[tuple[float, float]] | None = None) -> LayerPlan:
    tops = [first_layer_height]
    hs = [first_layer_height]
    z = first_layer_height
    eps = 1e-9
    while True:
        h = _profile_height(profile, z) if profile else layer_height
        if z + h / 2.0 >= z_max - eps:
            break
        z = round(z + h, 9)
        tops.append(z)
        hs.append(h)
    top = np.array(tops)
    height = np.array(hs)
    return LayerPlan(print_z=top, height=height, slice_z=top - height / 2.0)
