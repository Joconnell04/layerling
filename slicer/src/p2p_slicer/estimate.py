"""Print time estimator for Bambu-dialect G-code (section 10 of docs/research/native-slicer.md).

Input is a MoveTable (from gcode_read or our emitter). The model, in our own words:

- Every motion row is a block: length, cruise speed (F), acceleration (the M204 value for its
  move type), safe speed (the speed it may start or stop at: capped by the jerk of any axis whose
  component exceeds its jerk) and a corner limit with the block before (per axis, jerk divided
  by the change of the unit direction on that axis; X, Y and Z only; never above the slower of the
  two cruise speeds).
- A turn of 0.0006 to 29 degrees between two XY moves caps the cruise at sqrt(a r), with r the
  radius implied by the turn; an arc caps it at sqrt(a R). The cap lowers the block's cruise but
  not the speed the corner logic sees.
- Arcs (G2/G3) are single blocks; their start and end directions are the XY tangents, and Z does
  not enter their axis limits.
- Blocks queue. Above 256 queued blocks the queue is planned (a forward pass limits each entry
  by what the block before can reach, a reverse pass by what the block can brake from, a block
  that can always reach its cruise gets its full corner speed back) and all but the last 64 are
  timed; the last block of a plan exits at its safe speed.
- Dwells (G4, M400 with S or P) and G29 inside M622 J1 flush the queue: their seconds go to the
  first block timed; with no block queued they wait for the next plan. G130 (the firmware purge
  line) queues an empty block. G28 is a move to 0 (gcode_read).
- Tool changes add switch, unload and load seconds; SYNC T<s> adds s seconds; both in Flush.

Everything above was checked against Bambu Studio 02.08.02.61 with probe G-code (custom start
G-code, sliced by the CLI) and on the 20 calibration slices (slicer/calibration/truth.json).

Per-row seconds come back in `TimeResult.row_s`, so any grouping (role, layer, filament) is a sum.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

try:  # numba is a dependency; the pure Python fallback keeps the estimator importable anywhere
    from numba import njit
except ImportError:  # pragma: no cover
    def njit(*a, **k):  # type: ignore[misc]
        if a and callable(a[0]):
            return a[0]
        return lambda f: f

from .gcode_read import config_floats
from .moves import ROLE_NAMES, Flag, Kind, MoveTable, Role

QUEUE_REFRESH = 256
QUEUE_KEEP = 64
AXES = ("x", "y", "z", "e")
# limit vector layout per nozzle
L_SPEED, L_ACC, L_JERK = 0, 4, 8
L_PRINT, L_RETRACT, L_TRAVEL = 12, 13, 14
N_LIM = 15


@dataclass
class TimeResult:
    total_s: float
    prepare_s: float
    main_s: float                      # total - prepare - Prime tower - Flush (Bambu main_predication)
    model_s: float                     # total - prepare (G-code header "model printing time")
    per_role_s: dict[str, float]       # time roles, Bambu feature_type_times keys (no Travel)
    travel_s: float                    # Bambu "Travel": travel blocks outside the start G-code
    per_layer_s: np.ndarray            # index 0 = before the first layer (start G-code)
    load_s: dict[int, float] = field(default_factory=dict)    # slot -> load seconds
    unload_s: dict[int, float] = field(default_factory=dict)  # slot -> unload seconds
    row_s: np.ndarray | None = None    # seconds attributed to each MoveTable row
    time_role: np.ndarray | None = None  # time role per row

    def feature_type_times(self) -> dict[str, float]:
        """Bambu result.json shape: roles with time plus Travel."""
        out = {k: v for k, v in self.per_role_s.items() if v > 0}
        out["Travel"] = self.travel_s
        return dict(sorted(out.items()))


# ----------------------------------------------------------------------------- machine limits


def _limit_arrays(cfg: dict[str, str]) -> dict[str, list[float]]:
    defaults = {"machine_max_speed_x": [1000], "machine_max_speed_y": [1000],
                "machine_max_speed_z": [20], "machine_max_speed_e": [120],
                "machine_max_acceleration_x": [20000], "machine_max_acceleration_y": [20000],
                "machine_max_acceleration_z": [500], "machine_max_acceleration_e": [5000],
                "machine_max_jerk_x": [9], "machine_max_jerk_y": [9], "machine_max_jerk_z": [3],
                "machine_max_jerk_e": [2.5], "machine_max_acceleration_extruding": [20000],
                "machine_max_acceleration_retracting": [30000],
                "machine_max_acceleration_travel": [9000]}
    return {k: config_floats(cfg, k, v) or v for k, v in defaults.items()}


def nozzle_limits(cfg: dict[str, str], variants: tuple[int, int] = (0, 3)) -> np.ndarray:
    """(2, 15) normal-mode limits per nozzle: max speed x y z e, max accel x y z e, jerk x y z e,
    max print accel, max retract accel, max travel accel. G-code config blocks hold 4 values per
    key ([nozzle0 normal, nozzle0 silent, nozzle1 normal, nozzle1 silent]); project configs 12
    ([variant x 2 + mode]); a single value applies to both. gcode_flavor marlin (legacy) uses
    the extruding limit for travel."""
    arr = _limit_arrays(cfg)
    legacy = cfg.get("gcode_flavor", "marlin") == "marlin"
    out = np.zeros((2, N_LIM))
    def pick(k: str, n: int) -> float:
        v = arr[k]
        if len(v) >= 12:
            return float(v[variants[n] * 2])
        if len(v) >= 4:
            return float(v[n * 2])
        return float(v[0])

    for n in (0, 1):
        for ai, a in enumerate(AXES):
            out[n, L_SPEED + ai] = pick(f"machine_max_speed_{a}", n)
            out[n, L_ACC + ai] = pick(f"machine_max_acceleration_{a}", n)
            out[n, L_JERK + ai] = pick(f"machine_max_jerk_{a}", n)
        out[n, L_PRINT] = pick("machine_max_acceleration_extruding", n)
        out[n, L_RETRACT] = pick("machine_max_acceleration_retracting", n)
        out[n, L_TRAVEL] = (out[n, L_PRINT] if legacy
                            else pick("machine_max_acceleration_travel", n))
    return out


# ----------------------------------------------------------------------------- geometry per row


def row_geometry(rows: np.ndarray) -> dict[str, np.ndarray]:
    """Per row: length (XYZ, or |E| for E-only moves), XY length, arc radius (0 for lines), and
    unit directions at the start (u0) and end (u1) of the move over (x, y, z, e), each component
    divided by the length. Arcs use their XY tangents and leave z at 0 (see module doc)."""
    n = len(rows)
    kind = rows["kind"]
    x, y, z = rows["x"], rows["y"], rows["z"]
    px = np.concatenate(([0.0], x[:-1])) if n else x.copy()
    py = np.concatenate(([0.0], y[:-1])) if n else y.copy()
    pz = np.concatenate(([0.0], z[:-1])) if n else z.copy()
    motion = kind <= Kind.VIRTUAL
    virt = kind == Kind.VIRTUAL
    still = ~motion | virt
    dx = np.where(still, 0.0, x - px)
    dy = np.where(still, 0.0, y - py)
    dz = np.where(still, 0.0, z - pz)
    de = np.where(motion, rows["e"], 0.0)
    flags = rows["flags"]
    arc = ((flags & (Flag.ARC_CW | Flag.ARC_CCW)) != 0) & motion & ~virt
    lxy = np.hypot(dx, dy)
    with np.errstate(invalid="ignore", divide="ignore"):
        tx0 = np.where(lxy > 0, dx / lxy, 0.0)
        ty0 = np.where(lxy > 0, dy / lxy, 0.0)
    tx1, ty1 = tx0.copy(), ty0.copy()
    radius = np.zeros(n)
    if arc.any():
        idx = np.nonzero(arc)[0]
        ci = rows["i"][idx]
        cj = rows["j"][idx]
        cx = px[idx] + ci
        cy = py[idx] + cj
        r0 = np.hypot(ci, cj)
        a0 = np.arctan2(-cj, -ci)
        a1 = np.arctan2(y[idx] - cy, x[idx] - cx)
        cw = (flags[idx] & Flag.ARC_CW) != 0
        sweep = np.where(cw, a0 - a1, a1 - a0) % (2 * math.pi)
        turns = rows["p"][idx].astype(np.float64)
        # end point on the start point: a full circle (P turns, at least one)
        full = np.hypot(dx[idx], dy[idx]) < 1e-6
        sweep = np.where(full, 2 * math.pi * np.maximum(turns, 1), sweep + 2 * math.pi * turns)
        lxy[idx] = r0 * sweep
        radius[idx] = r0
        rx1, ry1 = x[idx] - cx, y[idx] - cy
        r1 = np.hypot(rx1, ry1)
        sgn = np.where(cw, -1.0, 1.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            tx0[idx] = np.where(r0 > 0, sgn * cj / r0, 0.0)
            ty0[idx] = np.where(r0 > 0, -sgn * ci / r0, 0.0)
            tx1[idx] = np.where(r1 > 0, -sgn * ry1 / r1, 0.0)
            ty1[idx] = np.where(r1 > 0, sgn * rx1 / r1, 0.0)
    length = np.hypot(lxy, dz)
    eonly = (length <= 0.0) & (de != 0.0)
    length = np.where(eonly, np.abs(de), length)
    with np.errstate(invalid="ignore", divide="ignore"):
        inv = np.where(length > 0, 1.0 / length, 0.0)
    fxy = lxy * inv
    uz = np.where(arc, 0.0, dz * inv)
    u0 = np.stack([tx0 * fxy, ty0 * fxy, uz, de * inv], axis=1)
    u1 = np.stack([tx1 * fxy, ty1 * fxy, uz, de * inv], axis=1)
    return {"length": length, "lxy": lxy, "u0": np.ascontiguousarray(u0),
            "u1": np.ascontiguousarray(u1), "radius": radius, "dz": dz, "de": de}


# ----------------------------------------------------------------------------- kernel


@njit(cache=True)
def _trapezoid_time(L, a, entry, cruise, exit_):
    if L <= 0.0:
        return 0.0
    if a <= 0.0:
        return L / cruise if cruise > 0.0 else 0.0
    d_acc = (cruise * cruise - entry * entry) / (2.0 * a)
    d_acc = max(d_acc, 0.0)
    d_dec = (cruise * cruise - exit_ * exit_) / (2.0 * a)
    d_dec = max(d_dec, 0.0)
    d_cruise = L - d_acc - d_dec
    if d_cruise < 0.0:
        # no room to cruise: accelerate until braking reaches the exit speed exactly
        d_acc = (2.0 * a * L - entry * entry + exit_ * exit_) / (4.0 * a)
        d_acc = max(d_acc, 0.0)
        d_acc = min(d_acc, L)
        peak = math.sqrt(max(0.0, entry * entry + 2.0 * a * d_acc))
        d_cruise = 0.0
    else:
        peak = cruise
    t_acc = (math.sqrt(max(0.0, entry * entry + 2.0 * a * d_acc)) - entry) / a
    d_dec = L - d_acc - d_cruise
    t_dec = (peak - math.sqrt(max(0.0, peak * peak - 2.0 * a * d_dec))) / a
    t_cruise = d_cruise / peak if peak > 0.0 else 0.0
    return t_acc + t_cruise + t_dec


@njit(cache=True)
def _plan(qL, qa, qen, qcr, qmx, qsf, qnom, qrow, n, keep, row_s, extra):
    """Plan n queued blocks, time the first n - keep into row_s (the first also gets `extra`),
    shift the kept blocks to the front. Returns the new queue length."""
    for k in range(n - 1):
        if not qnom[k] and qen[k] < qen[k + 1]:
            reach = math.sqrt(qen[k] * qen[k] + 2.0 * qa[k] * qL[k])
            qen[k + 1] = min(qen[k + 1], reach)
    for k in range(n - 1, 0, -1):
        c = k - 1
        if qen[c] != qmx[c]:
            if (not qnom[c]) and qmx[c] > qen[k]:
                v = math.sqrt(qen[k] * qen[k] + 2.0 * qa[c] * qL[c])
                qen[c] = min(v, qmx[c])
            else:
                qen[c] = qmx[c]
    done = n - keep
    for k in range(done):
        ex = qen[k + 1] if k + 1 < n else qsf[k]
        row_s[qrow[k]] += _trapezoid_time(qL[k], qa[k], qen[k], qcr[k], ex)
    if done > 0 and extra != 0.0:
        row_s[qrow[0]] += extra
    for k in range(keep):
        j = done + k
        qL[k] = qL[j]
        qa[k] = qa[j]
        qen[k] = qen[j]
        qcr[k] = qcr[j]
        qmx[k] = qmx[j]
        qsf[k] = qsf[j]
        qnom[k] = qnom[j]
        qrow[k] = qrow[j]
    return keep


@njit(cache=True)
def _simulate(kind, flags, f, length, lxy, radius, u0, u1, args, dwell, nozcol, slotcol, pcol,
              lim, load_t, unload_t, switch_t, row_s, direct_s, load_s, unload_s):
    n = kind.shape[0]
    cap_q = QUEUE_REFRESH + 2
    qL = np.zeros(cap_q)
    qa = np.zeros(cap_q)
    qen = np.zeros(cap_q)
    qcr = np.zeros(cap_q)
    qmx = np.zeros(cap_q)
    qsf = np.zeros(cap_q)
    qnom = np.zeros(cap_q, dtype=np.bool_)
    qrow = np.zeros(cap_q, dtype=np.int64)
    nq = 0
    pending = 0.0
    acc_print = 0.0
    acc_travel = 0.0
    acc_retract = 0.0
    nozzle = 0
    have_prev = False
    pu = np.zeros(4)
    prev_nom = 0.0
    prev_safe = 0.0
    for r in range(n):
        k = kind[r]
        if k == 10:  # M204
            s = args[r, 0]
            if not math.isnan(s):
                acc_print = min(s, lim[nozzle, L_PRINT])
                acc_travel = min(s, lim[nozzle, L_TRAVEL])
            if not math.isnan(args[r, 1]):
                acc_print = min(args[r, 1], lim[nozzle, L_PRINT])
            if not math.isnan(args[r, 2]):
                acc_travel = min(args[r, 2], lim[nozzle, L_TRAVEL])
            if not math.isnan(args[r, 3]):
                acc_retract = min(args[r, 3], lim[nozzle, L_RETRACT])
            continue
        if k == 11:  # M201 / M203 / M205 on the current nozzle
            fl = flags[r]
            base = L_ACC if (fl & 2048) else (L_SPEED if (fl & 4096) else L_JERK)
            for ai in range(4):
                v = args[r, ai]
                if not math.isnan(v):
                    lim[nozzle, base + ai] = v
            continue
        if k == 8:  # tool change
            fl = flags[r]
            extra = 0.0
            if fl & 256:
                extra += switch_t
            if fl & 512:
                extra += unload_t + load_t
                unload_s[pcol[r]] += unload_t
                load_s[slotcol[r]] += load_t
            if fl & 1024:
                extra += load_t
                load_s[slotcol[r]] += load_t
            if nozcol[r] >= 0:
                nozzle = nozcol[r]
            direct_s[r] += extra
            continue
        if k == 7:  # SYNC T<s>
            direct_s[r] += dwell[r]
            continue
        if k == 6 or k == 9:  # dwell, G29: flush the queue, seconds to the first block timed
            if nq >= 1:
                nq = _plan(qL, qa, qen, qcr, qmx, qsf, qnom, qrow, nq, 0, row_s,
                           dwell[r] + pending)
                pending = 0.0
            else:
                pending += dwell[r]
            continue
        if k == 12:  # empty block (G130)
            qL[nq] = 0.0
            qa[nq] = 1.0
            qen[nq] = 0.0
            qcr[nq] = 0.0
            qmx[nq] = 0.0
            qsf[nq] = 0.0
            qnom[nq] = True
            qrow[nq] = r
            nq += 1
            prev_nom = 0.0
            if nq > QUEUE_REFRESH:
                nq = _plan(qL, qa, qen, qcr, qmx, qsf, qnom, qrow, nq, QUEUE_KEEP, row_s,
                           pending)
                pending = 0.0
            continue
        if k > 5:
            continue
        L = length[r]
        if L <= 0.0:
            continue
        v = f[r] / 60.0
        if k == 1:
            a = acc_travel
        elif k == 2 or k == 3 or k == 5:
            a = acc_retract
        else:
            a = acc_print
        rad = radius[r]
        # centripetal cap
        cap = 1e300
        if rad > 0.0:
            cap = math.sqrt(a * rad)
        elif have_prev and (u0[r, 0] != 0.0 or u0[r, 1] != 0.0) and (pu[0] != 0.0 or pu[1] != 0.0):
            n1 = math.hypot(pu[0], pu[1])
            n2 = math.hypot(u0[r, 0], u0[r, 1])
            dd = math.hypot(u0[r, 0] / n2 - pu[0] / n1, u0[r, 1] / n2 - pu[1] / n1)
            if dd > 1e-5 and dd < 0.5:
                cap = math.sqrt(a * lxy[r] / dd)
        # axis limits
        for ai in range(4):
            c = abs(u0[r, ai])
            if c > 0.0:
                if v * c > lim[nozzle, L_SPEED + ai]:
                    v = lim[nozzle, L_SPEED + ai] / c
                if a * c > lim[nozzle, L_ACC + ai]:
                    a = lim[nozzle, L_ACC + ai] / c
        v_nom = v
        v = min(v, cap)
        # safe speed
        safe = v
        for ai in range(4):
            j = lim[nozzle, L_JERK + ai]
            if v * abs(u0[r, ai]) > j and j < safe:
                safe = j
        # corner with the previous block
        if nq > 0 and have_prev and prev_nom > 1e-4:
            vj = min(v_nom, prev_nom)
            for ai in range(3):
                du = abs(u0[r, ai] - pu[ai])
                if du > 1e-9:
                    lj = lim[nozzle, L_JERK + ai] / du
                    vj = min(vj, lj)
            if prev_safe > 0.99 * vj and safe > 0.99 * vj:
                vj = safe
        else:
            vj = safe
        allow = math.sqrt(safe * safe + 2.0 * a * L)
        qL[nq] = L
        qa[nq] = a
        qen[nq] = min(allow, vj)
        qcr[nq] = v
        qmx[nq] = vj
        qsf[nq] = safe
        qnom[nq] = v <= allow
        qrow[nq] = r
        nq += 1
        for ai in range(4):
            pu[ai] = u1[r, ai]
        have_prev = True
        prev_nom = v_nom
        prev_safe = safe
        if nq > QUEUE_REFRESH:
            nq = _plan(qL, qa, qen, qcr, qmx, qsf, qnom, qrow, nq, QUEUE_KEEP, row_s, pending)
            pending = 0.0
    if nq > 0:
        nq = _plan(qL, qa, qen, qcr, qmx, qsf, qnom, qrow, nq, 0, row_s, pending)
    return nq


# ----------------------------------------------------------------------------- estimator


def time_roles(rows: np.ndarray) -> np.ndarray:
    """Bambu's time role per row: the '; FEATURE:' in effect, except that travel outside Custom
    is Undefined, ;VG1 inside VFLUSH, tool changes and SYNC are Flush, and 'Floating vertical
    shell' is counted as 'Internal solid infill' (result.json has no row for it)."""
    kind = rows["kind"]
    flags = rows["flags"]
    trole = rows["role"].astype(np.int64)
    trole[(kind == Kind.TRAVEL) & (trole != Role.CUSTOM)] = Role.UNDEFINED
    trole[(kind == Kind.VIRTUAL) & ((flags & Flag.FLUSH) != 0)] = Role.FLUSH
    trole[(kind == Kind.TOOL_CHANGE) | (kind == Kind.SYNC)] = Role.FLUSH
    trole[trole == Role.FLOATING_SHELL] = Role.INTERNAL_SOLID
    return trole


def estimate(mt: MoveTable, cfg: dict[str, str] | None = None) -> TimeResult:
    """Time a MoveTable the way Bambu Studio 02.08.02.61 does. `cfg` defaults to the table's
    config (the G-code config block for Bambu files)."""
    cfg = cfg if cfg is not None else mt.meta.get("config", {})
    rows = mt.rows
    n = len(rows)
    geo = row_geometry(rows)
    lim = nozzle_limits(cfg)
    load_t = (config_floats(cfg, "machine_load_filament_time", [29.0]) or [29.0])[0]
    unload_t = (config_floats(cfg, "machine_unload_filament_time", [29.0]) or [29.0])[0]
    switch_t = (config_floats(cfg, "machine_switch_extruder_time", [5.0]) or [5.0])[0]
    row_s = np.zeros(n)
    direct_s = np.zeros(n)
    load_s = np.zeros(256)
    unload_s = np.zeros(256)
    _simulate(rows["kind"].astype(np.int64), rows["flags"].astype(np.int64),
              rows["f"].astype(np.float64), geo["length"], geo["lxy"], geo["radius"], geo["u0"],
              geo["u1"], rows["args"].astype(np.float64), rows["dwell_s"].astype(np.float64),
              rows["nozzle"].astype(np.int64), rows["slot"].astype(np.int64) & 255,
              rows["p"].astype(np.int64), lim, float(load_t), float(unload_t), float(switch_t),
              row_s, direct_s, load_s, unload_s)
    row_s += direct_s
    return summarize(mt, row_s, load_s, unload_s)


def summarize(mt: MoveTable, row_s: np.ndarray, load_s: np.ndarray | None = None,
              unload_s: np.ndarray | None = None) -> TimeResult:
    rows = mt.rows
    n = len(rows)
    kind = rows["kind"]
    flags = rows["flags"]
    trole = time_roles(rows)
    total = float(row_s.sum())
    per_role = np.bincount(trole, weights=row_s, minlength=len(ROLE_NAMES))
    per_role_s = {ROLE_NAMES[i]: float(per_role[i]) for i in range(len(ROLE_NAMES))
                  if per_role[i] > 0}
    start = (flags & Flag.START) != 0
    prepare = float(row_s[start].sum())
    travel = float(row_s[(kind == Kind.TRAVEL) & ~start].sum())
    layers = rows["layer"].astype(np.int64)
    per_layer = np.bincount(layers, weights=row_s) if n else np.zeros(1)
    main = total - prepare - per_role_s.get("Prime tower", 0.0) - per_role_s.get("Flush", 0.0)
    loads = {i: float(v) for i, v in enumerate(load_s) if v} if load_s is not None else {}
    unloads = {i: float(v) for i, v in enumerate(unload_s) if v} if unload_s is not None else {}
    return TimeResult(total_s=total, prepare_s=prepare, main_s=main, model_s=total - prepare,
                      per_role_s=per_role_s, travel_s=travel, per_layer_s=per_layer,
                      load_s=loads, unload_s=unloads, row_s=row_s, time_role=trole)
