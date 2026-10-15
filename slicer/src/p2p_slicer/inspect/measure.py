"""Numbers the inspector reads off a MoveTable and its TimeResult: grams per filament and role,
the change timeline, per-layer rows, travel, checks. Same code for Bambu's G-code and ours."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..moves import ROLE_NAMES, Flag, Kind, MoveTable, Role

FIL_AREA = math.pi * 0.875**2  # mm2, 1.75 mm filament (2.4053)
NOZZLE_VOLUME_MM3 = 92.0  # X2D nozzle_volume: first part of a purge billed to the old filament
MODEL_EXCLUDE = {int(Role.PRIME_TOWER)}
SUPPORT = {int(Role.SUPPORT), int(Role.SUPPORT_INTERFACE), int(Role.SUPPORT_TRANSITION)}
CAT_MODEL, CAT_TOWER, CAT_FLUSH, CAT_OTHER = 0, 1, 2, 3
CATEGORIES = ("Model", "Tower", "Flush", "Other")


@dataclass
class Measure:
    layers: int
    per_slot: dict[int, dict[str, float]]  # slot -> model/support/tower/flush/total mm3
    role_mm3: dict[str, float]
    role_e_mm: dict[str, float]
    role_g: dict[str, float]
    switches: list[dict[str, Any]]
    layer_rows: list[dict[str, Any]]
    travel: dict[str, float]
    filament_changes: int
    ams_swaps: int
    nozzle_switches: int
    first_slot: int | None
    layer_cat_s: np.ndarray | None = None  # (layers + 1, 4) seconds by category
    extruded_sets: list[tuple[int, ...]] = field(default_factory=list)
    bbox_extrude: list[float] | None = None  # x0, y0, x1, y1, zmax of all extrusion


def _time_cat(role: np.ndarray) -> np.ndarray:
    cat = np.full(len(role), CAT_MODEL, np.int64)
    cat[role == Role.PRIME_TOWER] = CAT_TOWER
    cat[role == Role.FLUSH] = CAT_FLUSH
    cat[(role == Role.UNDEFINED) | (role == Role.CUSTOM)] = CAT_OTHER
    return cat


def measure(mt: MoveTable, time=None, nozzle_volume: float = NOZZLE_VOLUME_MM3,
            areas: dict[int, float] | None = None,
            densities: dict[int, float] | None = None) -> Measure:
    rows = mt.rows
    kind = rows["kind"]
    flags = rows["flags"].astype(np.int64)
    role = rows["role"].astype(np.int64)
    slot = rows["slot"].astype(np.int64)
    layer = rows["layer"].astype(np.int64)
    e = rows["e"].astype(np.float64)
    n_layers = int(layer.max()) if len(rows) else 0

    def area(s: int) -> float:
        return (areas or {}).get(s, FIL_AREA)

    ext = kind == Kind.EXTRUDE
    per_slot: dict[int, dict[str, float]] = {}

    def bucket(s: int) -> dict[str, float]:
        return per_slot.setdefault(int(s), {"model": 0.0, "support": 0.0, "tower": 0.0,
                                            "flush": 0.0})

    for s in np.unique(slot[ext]):
        if s <= 0:
            continue
        m = ext & (slot == s)
        b = bucket(s)
        a = area(int(s))
        tower = m & (role == Role.PRIME_TOWER)
        sup = m & np.isin(role, list(SUPPORT))
        b["tower"] += float(e[tower].sum()) * a
        b["support"] += float(e[sup].sum()) * a
        b["model"] += float(e[m & ~tower & ~sup].sum()) * a

    # change timeline and flush billing (first nozzle_volume mm3 to the filament that last left
    # the target nozzle, the rest to the incoming one; section 7.5)
    nozzle = rows["nozzle"].astype(np.int64)
    virt_flush = (kind == Kind.VIRTUAL) & ((flags & Flag.FLUSH) != 0)
    tc = np.nonzero(kind == Kind.TOOL_CHANGE)[0]
    bounds = list(tc) + [len(rows)]
    switches: list[dict[str, Any]] = []
    last_on: dict[int, int] = {}
    prev_slot = 0
    first_slot = None
    changes = ams = nsw = 0
    layer_z = _layer_z(mt, n_layers)
    for k, ri in enumerate(tc):
        new = int(slot[ri])
        noz = int(nozzle[ri])
        seg = slice(ri, bounds[k + 1])
        fl_mm = float(e[seg][virt_flush[seg]].sum())
        fl_mm3 = fl_mm * area(new)
        old_on = last_on.get(noz)
        if first_slot is None and new > 0:
            first_slot = new
        if new > 0 and prev_slot > 0 and new != prev_slot:
            changes += 1
            is_sw = bool(flags[ri] & Flag.SWITCH)
            is_ams = bool(flags[ri] & Flag.AMS_SWAP)
            nsw += is_sw
            ams += is_ams
            to_old = min(fl_mm3, nozzle_volume) if old_on and old_on != new else 0.0
            billed = {}
            if to_old:
                billed[old_on] = to_old
            if fl_mm3 - to_old > 0:
                billed[new] = billed.get(new, 0.0) + fl_mm3 - to_old
            for s_b, v in billed.items():
                bucket(s_b)["flush"] += v
            lay = int(layer[ri])
            switches.append({
                "layer": max(lay - 1, 0), "z": layer_z[lay] if 0 < lay < len(layer_z) else None,
                "from": prev_slot, "to": new, "nozzle": noz, "nozzle_switch": is_sw,
                "ams_swap": is_ams, "flush_mm3": round(fl_mm3, 2),
                "billed_mm3": {str(s_b): round(v, 2) for s_b, v in billed.items()}})
        elif fl_mm3 > 0 and new > 0:
            bucket(new)["flush"] += fl_mm3
        if new > 0:
            last_on[noz] = new
            prev_slot = new
    for b in per_slot.values():
        b["total"] = b["model"] + b["support"] + b["tower"] + b["flush"]

    # roles
    role_mm3: dict[str, float] = {}
    role_e: dict[str, float] = {}
    role_g: dict[str, float] = {}

    def rho(s: int) -> float:
        return (densities or {}).get(s, 1.24)

    for r in np.unique(role[ext]):
        m = ext & (role == r)
        name = ROLE_NAMES[int(r)] if int(r) < len(ROLE_NAMES) else "Undefined"
        role_e[name] = float(e[m].sum())
        per = {int(s): float(e[m & (slot == s)].sum()) * area(int(s)) for s in np.unique(slot[m])}
        role_mm3[name] = sum(per.values())
        role_g[name] = sum(v * rho(s) / 1000 for s, v in per.items())
    fl_all = float(sum(b["flush"] for b in per_slot.values()))
    if fl_all:
        role_mm3["Flush"] = fl_all
        role_e["Flush"] = float(e[virt_flush].sum())
        role_g["Flush"] = sum(b["flush"] * rho(s) / 1000 for s, b in per_slot.items())

    # travel (print only, like Bambu's "Travel" row)
    xs = np.r_[rows["x"][:1], rows["x"][:-1]] if len(rows) else rows["x"]
    ys = np.r_[rows["y"][:1], rows["y"][:-1]] if len(rows) else rows["y"]
    dxy = np.hypot(rows["x"] - xs, rows["y"] - ys)
    printing = (flags & (Flag.START | Flag.END)) == 0
    trav = (kind == Kind.TRAVEL) & printing
    travel = {"mm": round(float(dxy[trav].sum()), 1), "moves": int((trav & (dxy > 1e-6)).sum()),
              "retracts": int(((kind == Kind.RETRACT) & printing).sum()),
              "wipes": int(((kind == Kind.WIPE) & printing).sum())}

    # per layer
    model_ext = ext & ~np.isin(role, list(MODEL_EXCLUDE))
    sets: list[tuple[int, ...]] = []
    per_layer_rows = []
    lay_ext = layer[model_ext]
    slot_ext = slot[model_ext]
    for k in range(1, n_layers + 1):
        sets.append(tuple(sorted({int(s) for s in np.unique(slot_ext[lay_ext == k]) if s > 0})))
    heights = _layer_h(mt, n_layers)
    sw_by_layer = np.zeros(n_layers + 1, np.int64)
    ams_by_layer = np.zeros(n_layers + 1, np.int64)
    nsw_by_layer = np.zeros(n_layers + 1, np.int64)
    fl_by_layer = np.zeros(n_layers + 1)
    for s in switches:
        i = s["layer"] + 1
        sw_by_layer[i] += 1
        ams_by_layer[i] += s["ams_swap"]
        nsw_by_layer[i] += s["nozzle_switch"]
        fl_by_layer[i] += s["flush_mm3"]
    cat_s = None
    per_layer_s = None
    if time is not None:
        per_layer_s = np.asarray(getattr(time, "per_layer_s", []), float)
        row_s = getattr(time, "row_s", None)
        troles = getattr(time, "time_role", None)
        if row_s is not None and troles is not None and len(row_s) == len(rows):
            cat = _time_cat(np.asarray(troles, np.int64))
            cat_s = np.zeros((n_layers + 1, 4))
            for c in range(4):
                m = cat == c
                cat_s[:, c] = np.bincount(np.clip(layer[m], 0, n_layers),
                                          weights=np.asarray(row_s)[m], minlength=n_layers + 1)
    elapsed = float(per_layer_s[0]) if per_layer_s is not None and len(per_layer_s) else 0.0
    for k in range(1, n_layers + 1):
        t = (float(per_layer_s[k]) if per_layer_s is not None and k < len(per_layer_s)
             else None)
        if t is not None:
            elapsed += t
        per_layer_rows.append({
            "index": k - 1, "z": layer_z[k] if k < len(layer_z) else None,
            "height": heights[k] if k < len(heights) else None,
            "time_s": round(t, 2) if t is not None else None,
            "elapsed_s": round(elapsed, 1) if t is not None else None,
            "filaments": list(sets[k - 1]), "changes": int(sw_by_layer[k]),
            "ams_swaps": int(ams_by_layer[k]), "nozzle_switches": int(nsw_by_layer[k]),
            "flush_mm3": round(float(fl_by_layer[k]), 2)})
    bbox = None
    if ext.any():
        bbox = [float(rows["x"][ext].min()), float(rows["y"][ext].min()),
                float(rows["x"][ext].max()), float(rows["y"][ext].max()),
                float(rows["z"][ext].max())]
    return Measure(n_layers, per_slot, role_mm3, role_e, role_g, switches, per_layer_rows, travel,
                   changes, ams, nsw, first_slot, cat_s, sets, bbox)


def _per_layer_meta(mt: MoveTable, key: str, n: int) -> list[float | None] | None:
    """meta[key] (one value per '; CHANGE_LAYER') as a list indexed 0..n (0 = start G-code)."""
    v = mt.meta.get(key)
    if isinstance(v, list | tuple | np.ndarray) and len(v) == n:
        return [None] + [round(float(x), 3) if x is not None and math.isfinite(float(x))
                         else None for x in v]
    return None


def _layer_z(mt: MoveTable, n: int) -> list[float | None]:
    z = _per_layer_meta(mt, "layer_z", n)
    if z is not None:
        return z
    rows = mt.rows
    ext = rows["kind"] == Kind.EXTRUDE
    out: list[float | None] = [None] * (n + 1)
    if ext.any():
        zmax = np.full(n + 1, np.nan)
        np.fmax.at(zmax, np.clip(rows["layer"][ext], 0, n), rows["z"][ext])
        out = [None if np.isnan(v) else round(float(v), 3) for v in zmax]
    return out


def _layer_h(mt: MoveTable, n: int) -> list[float | None]:
    h = _per_layer_meta(mt, "layer_h", n)
    if h is not None:
        return h
    rows = mt.rows
    ext = (rows["kind"] == Kind.EXTRUDE) & (rows["height"] > 0)
    out: list[float | None] = [None] * (n + 1)
    for k in np.unique(rows["layer"][ext]):
        if 0 < k <= n:
            out[int(k)] = round(float(np.median(rows["height"][ext & (rows["layer"] == k)])), 3)
    return out


def checks(mt: MoveTable, meas: Measure, bed=(256.0, 256.0), max_z: float = 261.0,
           right_min_x: float = 20.5, right_max_z: float | None = None,
           tower_bbox: list[float] | None = None,
           object_bbox: list[float] | None = None, types: dict[int, str] | None = None,
           painted_slots: set[int] | None = None) -> list[dict[str, Any]]:
    """The GUI's canvas warnings we can reproduce from moves (section 15)."""
    rows = mt.rows
    ext = rows["kind"] == Kind.EXTRUDE
    out: list[dict[str, Any]] = []

    def warn(code: str, text: str, level: str = "warning") -> None:
        out.append({"code": code, "level": level, "text": text, "source": "inspector"})

    x, y, z = rows["x"][ext], rows["y"][ext], rows["z"][ext]
    tol = 0.05
    if len(x) and ((x < -tol) | (x > bed[0] + tol) | (y < -tol) | (y > bed[1] + tol)).any():
        warn("outside_plate", "Paths outside the plate", "error")
    if len(z) and z.max() > max_z + tol:
        warn("too_high", f"Paths above {max_z:g} mm", "error")
    right = ext & (rows["nozzle"] == 1) & (rows["x"] < right_min_x - tol)
    if right.any():
        warn("right_nozzle_limit",
             f"Right nozzle paths at x under {right_min_x:g} mm ({int(right.sum())} moves)",
             "error")
    if right_max_z is not None:
        high = ext & (rows["nozzle"] == 1) & (rows["z"] > right_max_z + tol)
        if high.any():
            warn("right_nozzle_height", f"Right nozzle paths above {right_max_z:g} mm", "error")
    if tower_bbox is not None:
        x0, y0, x1, y1 = tower_bbox[:4]
        if x0 < -tol or y0 < -tol or x1 > bed[0] + tol or y1 > bed[1] + tol:
            warn("tower_outside", "Prime tower outside the plate", "error")
        if object_bbox is not None:
            ox0, oy0, ox1, oy1 = object_bbox[:4]
            if x0 < ox1 and ox0 < x1 and y0 < oy1 and oy0 < y1:
                warn("tower_conflict", "Prime tower overlaps an object", "error")
    empty = [i for i, s in enumerate(meas.extruded_sets) if not s]
    runs, run = [], []
    last_printed = max((i for i, s in enumerate(meas.extruded_sets) if s), default=-1)
    for i in empty:
        if i > last_printed:
            break
        if run and i != run[-1] + 1:
            runs.append(run)
            run = []
        run.append(i)
    if run:
        runs.append(run)
    for r in runs:
        if len(r) >= 2:
            warn("empty_layers", f"Empty layers {r[0] + 1} to {r[-1] + 1}", "error")
    if types:
        used = {s for s in meas.per_slot if meas.per_slot[s]["total"] > 0}
        kinds = {types.get(s, "").upper() for s in used}
        if any(k.startswith("PLA") for k in kinds) and any(k.startswith("PETG") for k in kinds):
            warn("pla_petg", "PLA and PETG mixed")
    zero = [s for s in meas.switches if s["ams_swap"] and s["flush_mm3"] <= 0]
    if zero:
        warn("flush_zero", f"AMS swaps with no flush ({len(zero)})")
    if painted_slots:
        printed = {s for s in meas.per_slot if meas.per_slot[s]["model"] > 0}
        lost = sorted(painted_slots - printed)
        if lost:
            warn("paint_lost", "Painted but not printed: slot " + ", ".join(map(str, lost)),
                 "error")
    return out
