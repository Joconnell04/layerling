"""Filament use and change counts from a MoveTable (section 11 of docs/research/native-slicer.md).

- Only extrusion moves (E > 0 with XY motion, G1/G2/G3) add material: volume = E x filament
  cross-section. Retracts, unretracts, wipes, E with Z only and the firmware purge line add
  nothing.
- Buckets by the '; FEATURE:' in effect: Support roles to support, Prime tower to tower, all else
  (Custom included) to model; credited to the filament loaded at that row.
- Flush is the ';VG1' E inside '; VFLUSH_START/END'. The first `nozzle_volume` mm3 (92 on the
  X2D) of each purge is billed to the filament that last left that nozzle, the rest to the one
  coming in (this is how Bambu's per-filament totals come out).
- grams = mm3 x density / 1000; metres = mm3 / cross-section / 1000.

Three change counts, kept apart because they mean different things:
- filament_changes: every T to a different filament after the first (GUI "Filament change times");
- ams_swaps: the target nozzle held another filament (result.json filament_change_times);
- nozzle_switches: T to the other nozzle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .gcode_read import config_floats
from .moves import SUPPORT_ROLES, Flag, Kind, MoveTable, Role

BUCKETS = ("model", "support", "tower", "flush")


@dataclass
class FilamentUse:
    slot: int
    mm3: dict[str, float] = field(default_factory=lambda: {b: 0.0 for b in BUCKETS})
    density: float = 1.24
    area_mm2: float = math.pi / 4 * 1.75 ** 2

    @property
    def total_mm3(self) -> float:
        return sum(self.mm3.values())

    def grams(self, bucket: str | None = None) -> float:
        v = self.total_mm3 if bucket is None else self.mm3[bucket]
        return v * self.density / 1000.0

    def metres(self, bucket: str | None = None) -> float:
        v = self.total_mm3 if bucket is None else self.mm3[bucket]
        return v / self.area_mm2 / 1000.0

    def as_dict(self) -> dict[str, float]:
        out: dict[str, float] = {"slot": self.slot}
        for b in BUCKETS:
            out[f"{b}_mm3"] = self.mm3[b]
            out[f"{b}_g"] = self.grams(b)
        out["total_mm3"] = self.total_mm3
        out["total_g"] = self.grams()
        out["total_m"] = self.metres()
        out["main_g"] = self.grams("model") + self.grams("support")
        return out


@dataclass
class Usage:
    per_slot: dict[int, FilamentUse]
    filament_changes: int
    ams_swaps: int
    nozzle_switches: int
    slots_used: list[int]                     # filaments with material (model, tower or flush)
    first_slot: int | None = None
    change_sequence: list[int] = field(default_factory=list)   # slots in load order

    @property
    def total_g(self) -> float:
        return sum(f.grams() for f in self.per_slot.values())

    def bucket_g(self, bucket: str) -> float:
        return sum(f.grams(bucket) for f in self.per_slot.values())

    @property
    def purge_g(self) -> float:
        return self.bucket_g("tower") + self.bucket_g("flush")


def filament_props(cfg: dict[str, str], n: int) -> tuple[list[float], list[float]]:
    """Densities and cross-sections (mm2) per slot index 0..n-1 from a Bambu config."""
    dens = config_floats(cfg, "filament_density") or [1.24]
    diam = config_floats(cfg, "filament_diameter") or [1.75]
    d = [dens[i] if i < len(dens) else dens[-1] for i in range(n)]
    a = [math.pi / 4 * (diam[i] if i < len(diam) else diam[-1]) ** 2 for i in range(n)]
    return d, a


def usage(mt: MoveTable, cfg: dict[str, str] | None = None) -> Usage:
    cfg = cfg if cfg is not None else mt.meta.get("config", {})
    rows = mt.rows
    kind = rows["kind"]
    slots = rows["slot"].astype(np.int64)
    nslots = max(int(slots.max()) if len(rows) else 0, len(config_floats(cfg, "filament_density")))
    dens, area = filament_props(cfg, max(nslots, 1))
    nozzle_vol = config_floats(cfg, "nozzle_volume") or [92.0]

    per: dict[int, FilamentUse] = {}

    def fu(slot: int) -> FilamentUse:
        u = per.get(slot)
        if u is None:
            i = max(slot - 1, 0)
            u = per[slot] = FilamentUse(slot=slot, density=dens[min(i, len(dens) - 1)],
                                        area_mm2=area[min(i, len(area) - 1)])
        return u

    # extrusion by bucket
    ext = (kind == Kind.EXTRUDE) & (rows["e"] > 0) & (slots > 0)
    if ext.any():
        roles = rows["role"][ext].astype(np.int64)
        bucket = np.zeros(len(roles), dtype=np.int64)            # 0 model
        bucket[np.isin(roles, list(SUPPORT_ROLES))] = 1          # support
        bucket[roles == Role.PRIME_TOWER] = 2                    # tower
        es = rows["e"][ext]
        ss = slots[ext]
        for s in np.unique(ss):
            m = ss == s
            u = fu(int(s))
            for b in range(3):
                e_mm = float(es[m & (bucket == b)].sum())
                u.mm3[BUCKETS[b]] += e_mm * u.area_mm2

    # flush billing, and change counts, walking the change and VG1 rows in order
    changes = ams = switches = 0
    seq: list[int] = []
    first: int | None = None
    old_on_target: int = 0
    target_noz = 0
    purge_mm3 = 0.0
    idx = np.nonzero((kind == Kind.TOOL_CHANGE) | (kind == Kind.VIRTUAL))[0]
    for r in idx:
        k = kind[r]
        fl = int(rows["flags"][r])
        if k == Kind.TOOL_CHANGE:
            new = int(rows["slot"][r])
            prev = int(rows["i"][r])
            if first is None:
                first = new
            if prev > 0:
                changes += 1
            if fl & Flag.AMS_SWAP:
                ams += 1
            if fl & Flag.SWITCH:
                switches += 1
            seq.append(new)
            old_on_target = int(rows["p"][r])
            target_noz = int(rows["nozzle"][r])
            purge_mm3 = 0.0
            continue
        if not fl & Flag.FLUSH:
            continue
        s = int(rows["slot"][r])
        if s <= 0:
            continue
        v = float(rows["e"][r]) * fu(s).area_mm2
        if v <= 0:
            continue
        nv = nozzle_vol[min(target_noz, len(nozzle_vol) - 1)] if target_noz >= 0 else nozzle_vol[0]
        to_old = 0.0
        if old_on_target > 0 and old_on_target != s:
            to_old = max(0.0, min(v, nv - purge_mm3))
        purge_mm3 += v
        if to_old > 0:
            fu(old_on_target).mm3["flush"] += to_old
        fu(s).mm3["flush"] += v - to_old

    used = sorted(s for s, u in per.items() if u.total_mm3 > 1e-9)
    if first is None:
        first = mt.meta.get("first_slot")
    return Usage(per_slot=dict(sorted(per.items())), filament_changes=changes, ams_swaps=ams,
                 nozzle_switches=switches, slots_used=used, first_slot=first,
                 change_sequence=seq)
