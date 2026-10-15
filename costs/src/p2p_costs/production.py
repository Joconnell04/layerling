"""What production prints with, read from the packages that decide it.

- Slicer process defaults: p2p_threemf.writer.DEFAULT_PROCESS_OVERRIDES (on top of Bambu's
  profile), and the layer height from p2p_contracts.PrintSettings.
- Orientation: p2p_geometry lays FLAT_PRODUCTS flat, everything else upright.
- Purge-lab variants: p2p_threemf.lab.levers() (layer height, despeckle, adaptive layers) and the
  overrides each run recorded in data/slice_reports/purge_lab.jsonl.

A slice run counts automatically when its settings and orientation match these. Support keys
are not compared: supports are chosen per model in the geometry stage, not by a process default.
Prime tower placement (wipe_tower_x/y) is per-model layout and is not compared either.
Each import falls back to the values below if the package cannot load.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

FALLBACK_OVERRIDES = {"prime_volume_mode": "Saving", "flush_into_infill": "1"}
FALLBACK_BAMBU = {"prime_volume_mode": "Default", "flush_into_infill": "0"}
FALLBACK_FLAT = {"keychain", "magnet"}
SUPPORT_KEYS = {"enable_support", "support_type", "support_on_build_plate_only"}
# Per-model layout, not a purge or time default: where the prime tower sits on the plate.
LAYOUT_KEYS = {"wipe_tower_x", "wipe_tower_y"}
# Lab orientation names -> geometry orientation names.
ORIENT = {"upright": "upright", "back": "flat", "side": "side"}


@dataclass(frozen=True)
class Production:
    overrides: dict[str, str]
    bambu: dict[str, str]
    layer_mm: float
    flat_products: frozenset[str]
    variants: dict[tuple[str, str], dict] = field(default_factory=dict)
    source: str = ""

    def orientation(self, product: str) -> str:
        return "flat" if product in self.flat_products else "upright"

    def describe(self) -> str:
        keys = ", ".join(f"{k}={v}" for k, v in sorted(self.overrides.items()))
        return (f"{keys}, layer {self.layer_mm:g} mm; flat: "
                f"{', '.join(sorted(self.flat_products))}")


@lru_cache(maxsize=1)
def production() -> Production:
    used = []
    try:
        from p2p_threemf.writer import DEFAULT_PROCESS_OVERRIDES as ov
        ov = {str(k): str(v) for k, v in ov.items()}
        used.append("p2p_threemf.writer")
    except Exception:  # noqa: BLE001
        ov = dict(FALLBACK_OVERRIDES)
    bambu, variants = dict(FALLBACK_BAMBU), {}
    try:
        from p2p_threemf import lab

        bambu = {str(k): str(v) for k, v in lab.BAMBU_VALUES.items()}
        for v in lab.levers():
            variants[(v.lever, v.name)] = {
                "layer_mm": v.layer_mm, "despeckle_mm": v.despeckle_mm, "adaptive": v.adaptive,
                "writer_defaults": v.writer_defaults}
        used.append("p2p_threemf.lab")
    except Exception:  # noqa: BLE001
        used.append("lab fallback")
    try:
        from p2p_contracts import PrintSettings

        layer = float(PrintSettings().layer_mm)
    except Exception:  # noqa: BLE001
        layer = 0.2
    try:
        from p2p_geometry.product import FLAT_PRODUCTS

        flat = frozenset(str(getattr(p, "value", p)) for p in FLAT_PRODUCTS)
        used.append("p2p_geometry.product")
    except Exception:  # noqa: BLE001
        flat = frozenset(FALLBACK_FLAT)
    return Production(ov, bambu, layer, flat, variants, source=", ".join(used) or "fallback")


def _cmp(effective: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in effective.items() if k not in SUPPORT_KEYS | LAYOUT_KEYS}


GEOMETRY_ORIENT = {"upright": "upright", "flat_face_up": "flat", "flat_back_down": "flat",
                   "flat": "flat"}


def embedded_config(cfg: dict, product: str, prod: Production | None = None
                    ) -> tuple[bool, str]:
    """(matches production, config text) for a slice report that carries its own "config"
    (geometry production slices: product, orientation, supports, process_overrides, layer_mm)."""
    prod = prod or production()
    eff = dict(prod.overrides)
    eff.update({str(k): str(v) for k, v in (cfg.get("process_overrides") or {}).items()})
    layer = float(cfg.get("layer_mm") or prod.layer_mm)
    orient = GEOMETRY_ORIENT.get(str(cfg.get("orientation") or "upright"), "other")
    want_orient = prod.orientation(product)
    diffs = [f"{k}={eff.get(k, prod.bambu.get(k, '?'))} (production {v})"
             for k, v in sorted(_cmp(prod.overrides).items()) if eff.get(k) != v]
    diffs += [f"{k}={v}" for k, v in sorted(_cmp(eff).items()) if k not in prod.overrides]
    if abs(layer - prod.layer_mm) > 1e-9:
        diffs.append(f"layer {layer:g} mm (production {prod.layer_mm:g})")
    if orient != want_orient:
        diffs.append(f"{orient} (production {want_orient})")
    size = cfg.get("target_size_mm")
    status = "production defaults" if not diffs else "differs: " + ", ".join(diffs)
    text = (f"{status} | geometry production slice"
            f"{f', {size:g} mm' if isinstance(size, (int, float)) else ''} | {orient}; "
            f"supports {cfg.get('supports') or '?'}")
    return not diffs, text


def lab_config(row: dict, product: str, procedural: bool, prod: Production | None = None
               ) -> tuple[bool, str]:
    """(matches production, config text) for one purge-lab row."""
    prod = prod or production()
    eff = dict(prod.overrides)
    eff.update({str(k): str(v) for k, v in (row.get("overrides") or {}).items()})
    lever, name = row.get("lever", ""), row.get("variant", "")
    var = prod.variants.get((lever, name), {})
    layer = float(row.get("layer_mm") or var.get("layer_mm") or prod.layer_mm)
    despeckle = var.get("despeckle_mm", 0.8 if lever == "g_paint" else 0.0)
    adaptive = var.get("adaptive", "adaptive" in name)
    orient = "native" if procedural else ORIENT.get(row.get("orient") or "upright", "other")
    want_orient = prod.orientation(product)
    diffs = [f"{k}={eff.get(k, prod.bambu.get(k, '?'))} (production {v})"
             for k, v in sorted(_cmp(prod.overrides).items()) if eff.get(k) != v]
    diffs += [f"{k}={v}" for k, v in sorted(_cmp(eff).items()) if k not in prod.overrides]
    if abs(layer - prod.layer_mm) > 1e-9:
        diffs.append(f"layer {layer:g} mm (production {prod.layer_mm:g})")
    if despeckle:
        diffs.append(f"despeckle {despeckle:g} mm")
    if adaptive:
        diffs.append("adaptive layers")
    orient_ok = orient == "native" or orient == want_orient
    if not orient_ok:
        diffs.append(f"{orient} (production {want_orient})")
    support = eff.get("enable_support")
    tail = f"; supports {'on' if support == '1' else 'off'}" if support is not None else ""
    status = "production defaults" if not diffs else "differs: " + ", ".join(diffs)
    text = f"{status} | {lever}/{name} | {orient}{tail}"
    return not diffs, text
