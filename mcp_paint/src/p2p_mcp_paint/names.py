"""Short color words for region labels and messages."""

from __future__ import annotations

import re

from p2p_sam3.color import color_name as _sam3_color_name
from p2p_sam3.color import lab_to_hex


def color_name(lab) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(_sam3_color_name(lab)).lower()).strip("_") or "color"


def filament_word(f) -> str:
    """Filament id -> label word from its sold name (Black, Jade White -> jade_white)."""
    return re.sub(r"[^a-z0-9]+", "_", f.name.lower()).strip("_") or color_name(f.lab)


__all__ = ["color_name", "filament_word", "lab_to_hex"]
