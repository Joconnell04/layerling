"""Printability flags for a built product, used to judge size tiers.

Every stage 6 rule that fails already shows up as a warning; this module sorts them into named
flags and adds one size rule that is not a stage 6 warning:

figure_detail: painted features must stay at least 2 mm across (2.5 x the 0.8 mm paint
minimum). On the Meshy beagle the eyes measure 6.3 % of the figure's height (SOURCE: measured
from the texture colors), so the figure must stand at least 32 mm tall (2.0 / 0.063). The 2 mm
threshold is ASSUMED; the 6.3 % is beagle-specific and other pets will differ.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from p2p_contracts.job import ProductType

EYE_FRACTION = 0.063  # SOURCE: beagle eyes, 6.3 % of figure height (texture colors)
MIN_FEATURE_MM = 2.0  # ASSUMED: smallest painted feature that keeps its shape
MIN_FIGURE_HEIGHT_MM = MIN_FEATURE_MM / EYE_FRACTION  # about 32 mm

_WARNING_RULES = (
    ("loop_seat", ("loop placed on a thinner one", "No wide spot", "No sturdy spot",
                   "No spot for the loop", "No spot on the outline")),
    ("loop_overlap", ("Loop overlap",)),
    ("load_path", ("thick on the load path",)),
    ("text", ("does not fit", "thinner than", "Cap height")),
    ("thickness", ("depth already squeezed",)),
    ("pocket", ("magnet pocket", "pocket needs")),
    ("tip", ("tip over",)),
    ("hanging", ("Hangs tilted",)),
)


@dataclass
class Flag:
    rule: str
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


def printability_flags(geo) -> list[Flag]:
    flags: list[Flag] = []
    for w in geo.warnings:
        rule = next((r for r, keys in _WARNING_RULES if any(k in w for k in keys)), "other")
        flags.append(Flag(rule, w))
    h = geo.metrics.get("figure_height_mm")
    if h is not None and h < MIN_FIGURE_HEIGHT_MM:
        eye = h * EYE_FRACTION
        flags.append(Flag("figure_detail",
                          f"Figure {h:.0f} mm tall: eyes about {eye:.1f} mm, under "
                          f"{MIN_FEATURE_MM:g} mm (needs {MIN_FIGURE_HEIGHT_MM:.0f} mm)"))
    text = geo.metrics.get("text")
    if geo.product == ProductType.NAMEPLATE and text and text.get("cap_height_mm", 99) < 5.0:
        flags.append(Flag("text", f"Name caps {text['cap_height_mm']:.1f} mm, under 5 mm"))
    return flags
