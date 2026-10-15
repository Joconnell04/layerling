"""p2p_geometry: stage 6. Scale, repair, orient, and exact-geometry add-ons.

Entry point: build_product (pinned in p2p_contracts.geometry). Helpers build single add-ons.
"""

from .addon_color import AddonColorInputs, assign_addon_colors
from .addons import (
    LoopSpec,
    PocketSpec,
    flat_back,
    hanging_loop,
    keyring_loop,
    magnet_pocket,
    oval_base,
    plate,
    round_base,
)
from .meshio import GLTF_TO_PRINT, load_meshy_glb
from .nameplate import nameplate
from .output import build_product, write_outputs
from .plinth import cut_below, detect_plinth
from .product import build_product_geometry, normalize_form, resolve_form
from .repair import repair_figure
from .text import layout_text, text_mesh
from .types import GeometrySettings, Part, ProductGeometry, RepairedFigure

__all__ = [
    "GLTF_TO_PRINT",
    "AddonColorInputs",
    "GeometrySettings",
    "LoopSpec",
    "Part",
    "PocketSpec",
    "ProductGeometry",
    "RepairedFigure",
    "assign_addon_colors",
    "build_product",
    "build_product_geometry",
    "cut_below",
    "detect_plinth",
    "flat_back",
    "hanging_loop",
    "keyring_loop",
    "layout_text",
    "load_meshy_glb",
    "magnet_pocket",
    "nameplate",
    "normalize_form",
    "oval_base",
    "plate",
    "repair_figure",
    "resolve_form",
    "round_base",
    "text_mesh",
    "write_outputs",
]
