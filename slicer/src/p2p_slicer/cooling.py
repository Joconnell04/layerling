"""Layer-time slowdown (section 8.4) and outer-wall z smoothing (section 8.5).

Slowdown, per layer, on the finished rows of that layer:
1. Layer time = sum of length / F over every G0-G3 move (no acceleration), bucketed by the
   filament loaded at that row. Travel, retracts, wipes, tower, template and change moves count
   but are fixed; extrusions are adjustable except bridges and overhang walls.
2. Filaments in order of `slow_down_layer_time`. For each: if the time of the whole layer still
   open (frozen time plus this and all later filaments) is under its threshold x 1.001, one feed
   ceiling v* is found for this and all later filaments so the layer reaches the threshold;
   every adjustable line runs at min(its speed, max(v*, slow_down_min_speed)). If even the
   minimum is not enough, all adjustable lines go to the minimum. Its time is then frozen.
3. F = floor(60 v + 0.5).
"""

from __future__ import annotations

import numpy as np

from .config import SliceConfig
from .moves import Flag, Kind, Role

FIXED_ROLES = (Role.BRIDGE, Role.OVERHANG_WALL, Role.INTERNAL_BRIDGE)


def _lengths(rows: np.ndarray, prev_xyz: tuple[float, float, float]) -> np.ndarray:
    x = rows["x"]
    y = rows["y"]
    z = rows["z"]
    px = np.concatenate(([prev_xyz[0]], x[:-1]))
    py = np.concatenate(([prev_xyz[1]], y[:-1]))
    pz = np.concatenate(([prev_xyz[2]], z[:-1]))
    L = np.sqrt((x - px) ** 2 + (y - py) ** 2 + (z - pz) ** 2)
    eonly = L <= 0
    return np.where(eonly, np.abs(rows["e"]), L)


def slow_down_layer(rows: np.ndarray, cfg: SliceConfig, prev_xyz: tuple[float, float, float],
                    factor: float = 1.001) -> None:
    """Modify rows['f'] in place (the rows of one layer)."""
    if len(rows) == 0:
        return
    kind = rows["kind"]
    motion = kind <= Kind.WIPE
    if not motion.any():
        return
    L = _lengths(rows, prev_xyz)
    f = rows["f"].astype(np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(motion & (f > 0), L / (f / 60.0), 0.0)
    slots = rows["slot"].astype(np.int64)
    adj = motion & (kind == Kind.EXTRUDE) & ~np.isin(rows["role"], FIXED_ROLES) & \
        ((rows["flags"] & (Flag.TOWER | Flag.TEMPLATE)) == 0) & (f > 0)
    fil = sorted(set(slots[motion].tolist()), key=lambda s: (cfg.fil("slow_down_layer_time", s,
                                                                     8.0), s))
    fil = [s for s in fil if s > 0] or [0]
    v = np.where(f > 0, f / 60.0, 0.0)
    frozen = 0.0
    pending = list(fil)
    while pending:
        s0 = pending[0]
        thr = cfg.fil("slow_down_layer_time", s0, 8.0) if s0 > 0 else 0.0
        enabled = cfg.fil("slow_down_for_layer_cooling", s0, 1.0) if s0 > 0 else 0.0
        grp = np.isin(slots, pending)
        total = frozen + float(t[grp & motion].sum())
        if enabled and thr > 0 and total < thr * factor:
            a = grp & adj
            fixed_t = frozen + float(t[grp & motion & ~adj].sum())
            need = thr - fixed_t
            if a.any() and need > 0:
                vmin = np.array([cfg.fil("slow_down_min_speed", int(s), 20.0) if s > 0 else 20.0
                                 for s in slots[a]])
                va = v[a]
                La = L[a]

                def time_at(cap: float, va: np.ndarray = va, vmin: np.ndarray = vmin,
                            La: np.ndarray = La) -> float:
                    vv = np.minimum(va, np.maximum(cap, vmin))
                    return float((La / vv).sum())
                if time_at(0.0) <= need:
                    newv = np.minimum(va, vmin)
                else:
                    lo, hi = 0.0, float(va.max())
                    for _ in range(60):
                        mid = 0.5 * (lo + hi)
                        if time_at(mid) > need:
                            lo = mid
                        else:
                            hi = mid
                    cap = hi
                    newv = np.minimum(va, np.maximum(cap, vmin))
                changed = newv < va - 1e-9
                idx = np.nonzero(a)[0][changed]
                v[idx] = newv[changed]
                rows["f"][idx] = np.floor(60.0 * newv[changed] + 0.5)
                t[idx] = L[idx] / (rows["f"][idx] / 60.0)
        frozen += float(t[(slots == s0) & motion].sum())
        pending.pop(0)
