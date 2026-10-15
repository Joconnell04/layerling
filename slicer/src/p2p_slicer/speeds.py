"""Feature speeds and accelerations per nozzle (section 8.2), before the cooling slowdown.

speed = the role's configured speed on the nozzle, then capped by the filament's max volumetric
speed divided by the line cross-section (no flow ratio). Layer 0 uses initial_layer_speed (and
initial_layer_infill_speed for its bottom surface) and initial_layer_acceleration.
"""

from __future__ import annotations

from .config import SliceConfig
from .moves import Role
from .walls import cross_section

_SPEED_KEY = {
    Role.OUTER_WALL: "outer_wall_speed",
    Role.INNER_WALL: "inner_wall_speed",
    Role.SPARSE_INFILL: "sparse_infill_speed",
    Role.INTERNAL_SOLID: "internal_solid_infill_speed",
    Role.TOP_SURFACE: "top_surface_speed",
    Role.BOTTOM_SURFACE: "internal_solid_infill_speed",
    Role.GAP_INFILL: "gap_infill_speed",
    Role.BRIDGE: "bridge_speed",
    Role.INTERNAL_BRIDGE: "bridge_speed",
    Role.OVERHANG_WALL: "overhang_totally_speed",
    Role.FLOATING_SHELL: "internal_solid_infill_speed",
}


def _accel_default(cfg: SliceConfig, n: int) -> float:
    return cfg.nozzle("default_acceleration", n, 10000.0)


def feature_accel(cfg: SliceConfig, role: int, n: int, layer0: bool) -> float:
    if layer0:
        return cfg.nozzle("initial_layer_acceleration", n, 500.0)
    d = _accel_default(cfg, n)
    if role in (Role.OUTER_WALL, Role.OVERHANG_WALL):
        return cfg.nozzle("outer_wall_acceleration", n, d)
    if role == Role.INNER_WALL:
        v = cfg.nozzle("inner_wall_acceleration", n, 0.0)
        return v if v > 0 else d
    if role == Role.TOP_SURFACE:
        return cfg.nozzle("top_surface_acceleration", n, d)
    if role == Role.SPARSE_INFILL:
        s = cfg.nozzle_s("sparse_infill_acceleration", n, "100%")
        if s.endswith("%"):
            return d * float(s.rstrip("%")) / 100.0
        return float(s)
    return d


def travel_accel(cfg: SliceConfig, n: int, layer0: bool) -> float:
    if layer0:
        return cfg.nozzle("initial_layer_travel_acceleration", n, 6000.0)
    return cfg.nozzle("travel_acceleration", n, 10000.0)


def feature_speed(cfg: SliceConfig, role: int, n: int, slot: int, width: float, h: float,
                  layer0: bool) -> float:
    """mm/s before cooling."""
    if layer0:
        if role == Role.BOTTOM_SURFACE:
            v = cfg.nozzle("initial_layer_infill_speed", n, 105.0)
        else:
            v = cfg.nozzle("initial_layer_speed", n, 50.0)
    else:
        v = cfg.nozzle(_SPEED_KEY.get(role, "inner_wall_speed"), n, 100.0)
        if role == Role.FLOATING_SHELL:
            s = cfg.nozzle_s("vertical_shell_speed", n, "80%")
            v = v * float(s.rstrip("%")) / 100.0 if s.endswith("%") else float(s)
    if role == Role.INTERNAL_BRIDGE or role == Role.BRIDGE:
        area = 3.141592653589793 / 4 * 0.4 ** 2 if role == Role.INTERNAL_BRIDGE else \
            cross_section(width, h)
    else:
        area = cross_section(width, h)
    mv = cfg.max_vol_speed(slot)
    if area > 0 and mv > 0:
        v = min(v, mv / area)
    return v
