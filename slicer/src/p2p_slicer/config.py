"""Resolve a project config (Metadata/project_settings.config) into per-nozzle and per-filament
values, the way Bambu Studio resolves it before slicing (what it writes into the G-code config
block).

A project holds three kinds of lists:
- per extruder variant (6 values on the X2D, in `printer_extruder_variant` order): process and
  machine keys such as `outer_wall_speed`. Nozzle n uses its variant (left Direct Drive Standard
  = 0, right Bowden Standard = 3), so the resolved list has 2 values;
- machine limits (12 values, [variant x 2 + mode]): resolved to 4 ([n0 normal, n0 silent, n1
  normal, n1 silent]);
- per filament and variant (N x 6 values, filament-major) such as `nozzle_temperature`: each
  filament takes the value of the variant of the nozzle it is mapped to.

`resolve_config` returns lists of strings keyed like Bambu's G-code config block; `gcode_config`
renders them as that block's strings. `SliceConfig` wraps the result with typed accessors.
Checked key by key against `gcode_config.json` of every calibration case
(tests/test_slicer_config.py).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

MACHINE_LIMIT_PREFIX = "machine_max_"
# Keys whose values Bambu writes as quoted strings joined by ';'.
QUOTED = {"filament_extruder_variant", "print_extruder_variant", "printer_extruder_variant",
          "filament_settings_id", "extruder_variant_list", "filament_notes", "print_compatible_printers",
          "compatible_printers", "filament_vendor"}


@lru_cache(maxsize=1)
def filament_keys() -> frozenset[str]:
    """Keys that filament presets set (per filament lists)."""
    from p2p_threemf.profiles import snapshot_path
    data = json.loads(Path(snapshot_path()).read_text())
    keys: set[str] = set()
    for p in data["presets"].values():
        if p.get("type") == "filament":
            keys.update(p["config"].keys())
    keys.update({"filament_colour", "filament_settings_id", "filament_ids", "filament_map",
                 "filament_nozzle_map", "filament_self_index", "filament_multi_colour",
                 "filament_colour_type"})
    return frozenset(keys)


def as_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if v is None:
        return []
    return [str(v)]


def nozzle_variants(project: dict[str, Any]) -> tuple[int, int]:
    """Index into the variant arrays for nozzle 0 and 1 (Standard volume type)."""
    allv = as_list(project.get("printer_extruder_variant"))
    per = as_list(project.get("extruder_variant_list"))
    vtype = as_list(project.get("nozzle_volume_type")) or ["Standard", "Standard"]
    out = []
    for n in (0, 1):
        names = [s.strip() for s in per[n].split(",")] if n < len(per) else []
        want = next((s for s in names if s.endswith(vtype[min(n, len(vtype) - 1)])),
                    names[0] if names else None)
        out.append(allv.index(want) if want in allv else (0 if n == 0 else 3))
    return out[0], out[1]


def resolve_config(project: dict[str, Any], filament_map: list[int] | None = None) -> dict[str, Any]:
    """Resolved lists of strings (scalars stay strings). `filament_map`: 1-based nozzle per
    filament; defaults to the project's."""
    colours = as_list(project.get("filament_colour"))
    n_fil = len(colours)
    allv = as_list(project.get("printer_extruder_variant"))
    n_var = len(allv) or 6
    v0, v1 = nozzle_variants(project)
    fmap = [int(x) for x in (filament_map or as_list(project.get("filament_map")) or
                             ["1"] * n_fil)]
    fkeys = filament_keys()
    out: dict[str, Any] = {}
    for k, v in project.items():
        if not isinstance(v, list):
            out[k] = v
            continue
        vals = as_list(v)
        if k.startswith(MACHINE_LIMIT_PREFIX) and len(vals) == 2 * n_var:
            out[k] = [vals[v0 * 2], vals[v0 * 2 + 1], vals[v1 * 2], vals[v1 * 2 + 1]]
        elif k in fkeys and n_fil and len(vals) == n_fil * n_var:
            out[k] = [vals[i * n_var + (v0 if fmap[i] == 1 else v1)] for i in range(n_fil)]
        elif k not in fkeys and len(vals) == n_var and n_var > 1:
            out[k] = [vals[v0], vals[v1]]
        else:
            out[k] = vals
    out["filament_map"] = [str(x) for x in fmap]
    out["filament_nozzle_map"] = [str(x - 1) for x in fmap]
    out["filament_map_2"] = [str(x - 1) for x in fmap]
    return out


def to_gcode_value(key: str, v: Any) -> str:
    if not isinstance(v, list):
        return "" if v is None else str(v)
    if key in QUOTED:
        return ";".join(f'"{s}"' for s in v)
    if any(";" in s for s in v):
        return ";".join(v)
    if key in ("filament_colour", "filament_type", "filament_ids", "filament_vendor",
               "default_filament_colour", "filament_colour_type", "filament_multi_colour"):
        return ";".join(v)
    return ",".join(v)


def gcode_config(resolved: dict[str, Any]) -> dict[str, str]:
    """Strings as in Bambu's G-code config block (the estimator and templates read these)."""
    return {k: to_gcode_value(k, v) for k, v in sorted(resolved.items())}


# ----------------------------------------------------------------------------- typed access


def _f(s: Any, default: float = math.nan) -> float:
    try:
        s = str(s).strip().rstrip("%")
        return float(s)
    except (TypeError, ValueError):
        return default


@dataclass
class SliceConfig:
    """Resolved project config with typed accessors. Nozzles: 0 left (Direct Drive), 1 right
    (Bowden). Slots are 1-based."""

    resolved: dict[str, Any]
    project: dict[str, Any]
    filament_map: list[int]                        # 1-based nozzle per slot
    variants: tuple[int, int] = (0, 3)
    extra: dict[str, Any] = field(default_factory=dict)

    # --- generic
    def raw(self, key: str, default: Any = None) -> Any:
        return self.resolved.get(key, default)

    def s(self, key: str, default: str = "") -> str:
        v = self.resolved.get(key)
        if v is None:
            return default
        if isinstance(v, list):
            return v[0] if v else default
        return str(v)

    def f(self, key: str, default: float = math.nan) -> float:
        return _f(self.s(key), default)

    def i(self, key: str, default: int = 0) -> int:
        v = self.f(key, math.nan)
        return default if math.isnan(v) else int(v)

    def pct_or_abs(self, key: str, base: float) -> float:
        """Values like '15%' of `base`, or absolute."""
        s = self.s(key)
        if s.endswith("%"):
            return _f(s) / 100.0 * base
        return _f(s)

    def nozzle(self, key: str, n: int, default: float = math.nan) -> float:
        """Per-nozzle process or machine value (2 resolved values)."""
        v = self.resolved.get(key)
        if isinstance(v, list):
            if not v:
                return default
            return _f(v[min(n, len(v) - 1)], default)
        return _f(v, default)

    def nozzle_s(self, key: str, n: int, default: str = "") -> str:
        v = self.resolved.get(key)
        if isinstance(v, list):
            return v[min(n, len(v) - 1)] if v else default
        return default if v is None else str(v)

    def fil(self, key: str, slot: int, default: float = math.nan) -> float:
        v = self.resolved.get(key)
        if isinstance(v, list):
            if not v:
                return default
            return _f(v[min(slot - 1, len(v) - 1)], default)
        return _f(v, default)

    def fil_s(self, key: str, slot: int, default: str = "") -> str:
        v = self.resolved.get(key)
        if isinstance(v, list):
            return v[min(slot - 1, len(v) - 1)] if v else default
        return default if v is None else str(v)

    # --- common derived values
    @property
    def n_filaments(self) -> int:
        return len(as_list(self.resolved.get("filament_colour")))

    def nozzle_of(self, slot: int) -> int:
        return self.filament_map[slot - 1] - 1 if 0 < slot <= len(self.filament_map) else 0

    def filament_area(self, slot: int = 1) -> float:
        d = self.fil("filament_diameter", slot, 1.75)
        return math.pi / 4 * d * d

    def flow_ratio(self, slot: int) -> float:
        return self.fil("filament_flow_ratio", slot, 1.0) * self.f("print_flow_ratio", 1.0)

    def max_vol_speed(self, slot: int) -> float:
        return self.fil("filament_max_volumetric_speed", slot, 12.0)

    def retraction(self, slot: int) -> dict[str, float]:
        """Filament value unless nil, else the machine's for the filament's nozzle."""
        n = self.nozzle_of(slot)

        def pick(fkey: str, mkey: str) -> float:
            fv = self.fil_s(fkey, slot, "nil")
            return _f(fv) if fv not in ("nil", "") else self.nozzle(mkey, n)
        return {
            "length": pick("filament_retraction_length", "retraction_length"),
            "speed": pick("filament_retraction_speed", "retraction_speed"),
            "deretraction_speed": pick("filament_deretraction_speed", "deretraction_speed"),
            "wipe_distance": pick("filament_wipe_distance", "wipe_distance"),
            "z_hop": pick("filament_z_hop", "z_hop"),
            "minimum_travel": pick("filament_retraction_minimum_travel",
                                   "retraction_minimum_travel"),
        }

    def z_hop_type(self, slot: int) -> str:
        fv = self.fil_s("filament_z_hop_types", slot, "nil")
        return fv if fv not in ("nil", "") else self.nozzle_s("z_hop_types", self.nozzle_of(slot))

    def flush_matrix(self) -> np.ndarray:
        """(2, N, N) mm3 per nozzle, from filament a (row) to b (column), multiplier applied."""
        vals = [_f(x, 0.0) for x in as_list(self.resolved.get("flush_volumes_matrix"))]
        n = self.n_filaments
        mult = [_f(x, 1.0) for x in as_list(self.resolved.get("flush_multiplier"))] or [1.0]
        per = n * n
        mats = []
        for k in range(2):
            chunk = vals[k * per:(k + 1) * per] if len(vals) >= (k + 1) * per else vals[:per]
            m = np.array(chunk, dtype=float).reshape(n, n) if len(chunk) == per else np.zeros((n, n))
            mats.append(m * mult[min(k, len(mult) - 1)])
        return np.stack(mats)

    def gcode_config(self) -> dict[str, str]:
        return gcode_config(self.resolved)

    @property
    def layer_height(self) -> float:
        return self.f("layer_height", 0.2)

    @property
    def first_layer_height(self) -> float:
        return self.f("initial_layer_print_height", 0.2)


def load_project_config(path: str | Path) -> dict[str, Any]:
    import zipfile
    with zipfile.ZipFile(path) as z:
        return json.loads(z.read("Metadata/project_settings.config"))


# What the Bambu CLI changes before slicing (src/BambuStudio.cpp, as described in section 7.2 of
# the spec): prime volume forced to 45, 4 fake AMS slots on each nozzle.
CLI_OVERRIDES: dict[str, Any] = {
    "extruder_ams_count": ["1#0|4#1", "1#0|4#1"],
}


def slice_config(project: dict[str, Any], filament_map: list[int] | None = None,
                 overrides: dict[str, Any] | None = None, *, cli_parity: bool = True) -> SliceConfig:
    proj = dict(project)
    if cli_parity:
        proj.update(CLI_OVERRIDES)
        n = len(as_list(proj.get("filament_colour")))
        if n:
            proj["filament_prime_volume"] = ["45"] * n
    for k, v in (overrides or {}).items():
        proj[k] = v
    res = resolve_config(proj, filament_map)
    fmap = [int(x) for x in res["filament_map"]]
    return SliceConfig(resolved=res, project=proj, filament_map=fmap,
                       variants=nozzle_variants(proj))
