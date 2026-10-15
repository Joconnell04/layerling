"""MoveTable: one numpy structured array row per G-code event that matters for time and grams.

Both our emitter and the Bambu G-code reader produce a MoveTable, so the estimator, the grams
counter and the inspector read our slices and Bambu's the same way.

A row is a motion (extrude, travel, retract, unretract, wipe), a virtual flush move (`;VG1`), or
a timing or state event (dwell, SYNC, tool change, G29, M204, machine limits). Positions are
absolute mm at the END of the move; `e` is the relative extrusion of the move in mm of filament.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import numpy as np

MOVE_DTYPE = np.dtype([
    ("x", "f8"), ("y", "f8"), ("z", "f8"),   # end position, mm
    ("e", "f8"),                             # relative E, mm of filament
    ("f", "f8"),                             # feedrate, mm/min
    ("kind", "u1"),                          # Kind
    ("role", "u1"),                          # Role: the current '; FEATURE:' (raw, not the time role)
    ("slot", "i1"),                          # 1-based filament after this row, 0 before the first T
    ("nozzle", "i1"),                        # 0 left, 1 right, -1 unknown
    ("layer", "i4"),                         # count of '; CHANGE_LAYER' seen (0 = start G-code)
    ("width", "f4"), ("height", "f4"),       # '; LINE_WIDTH' / '; LAYER_HEIGHT' in effect
    ("accel", "f4"),                         # requested acceleration for this move type (M204)
    ("dwell_s", "f4"),                       # seconds for DWELL, SYNC, G29 rows
    ("flags", "u2"),                         # Flag bits
    ("i", "f8"), ("j", "f8"),                # arc centre offset from the start point
    ("p", "u1"),                             # arc P (full turns)
    ("line", "i4"),                          # 1-based source line (G-code), or 0
    ("args", "f4", (4,)),                    # M204: S P T R; LIMITS: X Y Z E (NaN = absent)
])


class Kind(IntEnum):
    EXTRUDE = 0      # E > 0 with XY motion
    TRAVEL = 1       # no E, or E with motion that is not an extrusion (E < 0 with XY, E > 0 with Z only)
    RETRACT = 2      # E < 0 alone
    UNRETRACT = 3    # E > 0 alone
    WIPE = 4         # any motion inside '; WIPE_START' / '; WIPE_END'
    VIRTUAL = 5      # ';VG1 E.. F..': firmware flush or de-retract, timed as an E-only move
    DWELL = 6        # G4 / M400 with S or P
    SYNC = 7         # 'SYNC T<s>': chute purge seconds
    TOOL_CHANGE = 8  # T<n>; slot and nozzle are the new ones
    G29 = 9          # bed levelling inside 'M622 J1' (machine_prepare_compensation_time)
    M204 = 10        # acceleration change: args = S, P, T, R (NaN when absent)
    LIMITS = 11      # M201 (flags LIM_ACCEL), M203 (LIM_SPEED), M205 (LIM_JERK): args = X Y Z E
    EMPTY = 12       # a planner block with no length (Bambu queues one for G130, the purge line)
    TEXT = 13        # verbatim G-code text (a rendered template); line = index in meta["texts"]


MOTION_KINDS = (Kind.EXTRUDE, Kind.TRAVEL, Kind.RETRACT, Kind.UNRETRACT, Kind.WIPE)


class Flag:
    ARC_CW = 1 << 0      # G2
    ARC_CCW = 1 << 1     # G3
    WIPE = 1 << 2        # inside WIPE_START/END
    START = 1 << 3       # start G-code (prepare time)
    END = 1 << 4         # after '; MACHINE_END_GCODE_START'
    FLUSH = 1 << 5       # inside VFLUSH_START/END
    TOWER = 1 << 6       # inside WIPE_TOWER_START/END
    NOZZLE_CHANGE = 1 << 7   # inside NOZZLE_CHANGE_START/END (ramming)
    SWITCH = 1 << 8      # TOOL_CHANGE row: the new filament is on the other nozzle
    AMS_SWAP = 1 << 9    # TOOL_CHANGE row: the target nozzle held a different filament
    FIRST_LOAD = 1 << 10  # TOOL_CHANGE row: first filament ever loaded on that nozzle
    LIM_ACCEL = 1 << 11  # LIMITS row from M201
    LIM_SPEED = 1 << 12  # LIMITS row from M203
    LIM_JERK = 1 << 13   # LIMITS row from M205
    CHANGE_BLOCK = 1 << 14  # inside the filament change template (between tower and prime)
    TEMPLATE = 1 << 15   # parsed from a TEXT row's template text (the emitter prints the text)


# Role names exactly as Bambu writes them after '; FEATURE: ' and in result.json.
ROLE_NAMES: tuple[str, ...] = (
    "Undefined", "Inner wall", "Outer wall", "Overhang wall", "Sparse infill",
    "Internal solid infill", "Top surface", "Bottom surface", "Bridge", "Gap infill", "Skirt",
    "Brim", "Support", "Support interface", "Support transition", "Prime tower", "Custom",
    "Flush", "Floating vertical shell", "Ironing", "Internal bridge",
)
ROLE_ID: dict[str, int] = {n: i for i, n in enumerate(ROLE_NAMES)}


class Role(IntEnum):
    UNDEFINED = 0
    INNER_WALL = 1
    OUTER_WALL = 2
    OVERHANG_WALL = 3
    SPARSE_INFILL = 4
    INTERNAL_SOLID = 5
    TOP_SURFACE = 6
    BOTTOM_SURFACE = 7
    BRIDGE = 8
    GAP_INFILL = 9
    SKIRT = 10
    BRIM = 11
    SUPPORT = 12
    SUPPORT_INTERFACE = 13
    SUPPORT_TRANSITION = 14
    PRIME_TOWER = 15
    CUSTOM = 16
    FLUSH = 17
    FLOATING_SHELL = 18
    IRONING = 19
    INTERNAL_BRIDGE = 20


SUPPORT_ROLES = frozenset({Role.SUPPORT, Role.SUPPORT_INTERFACE, Role.SUPPORT_TRANSITION})


def role_id(name: str) -> int:
    """Role id for a feature name; unknown names map to Undefined."""
    return ROLE_ID.get(name.strip(), Role.UNDEFINED)


@dataclass
class MoveTable:
    """Rows plus what the estimator needs that is not a move.

    meta keys (all optional): `config` (dict of Bambu config strings), `header` (dict),
    `nozzle_of` (slot -> nozzle), `first_slot`, `layer_z` (list of '; Z_HEIGHT' per layer),
    `layer_h`, `source` ("bambu" or "p2p_slicer"), `filament_count`.
    """

    rows: np.ndarray
    meta: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def kind(self) -> np.ndarray:
        return self.rows["kind"]

    def select(self, *kinds: int) -> np.ndarray:
        return np.isin(self.rows["kind"], np.array(kinds, dtype=np.uint8))

    @property
    def layer_count(self) -> int:
        return int(self.rows["layer"].max()) if len(self.rows) else 0


_NO_ARGS = (0.0, 0.0, 0.0, 0.0)


class MoveBuilder:
    """Append rows cheaply (lists of tuples), then freeze into a MoveTable."""

    __slots__ = ("_rows", "meta")

    def __init__(self, meta: dict[str, Any] | None = None) -> None:
        self._rows: list[tuple] = []
        self.meta: dict[str, Any] = dict(meta or {})

    def add(self, x: float, y: float, z: float, e: float, f: float, kind: int, role: int,
            slot: int, nozzle: int, layer: int, width: float = 0.0, height: float = 0.0,
            accel: float = 0.0, dwell_s: float = 0.0, flags: int = 0, i: float = 0.0,
            j: float = 0.0, p: int = 0, line: int = 0,
            args: tuple[float, float, float, float] = _NO_ARGS) -> None:
        self._rows.append((x, y, z, e, f, kind, role, slot, nozzle, layer, width, height, accel,
                           dwell_s, flags, i, j, p, line, args))

    def __len__(self) -> int:
        return len(self._rows)

    def build(self) -> MoveTable:
        rows = np.array(self._rows, dtype=MOVE_DTYPE) if self._rows else np.zeros(0, MOVE_DTYPE)
        return MoveTable(rows=rows, meta=self.meta)


class PathWriter:
    """Write rows the way our G-code will read: every generated move, plus rendered template
    text (start, change, layer change, end G-code) parsed by the same reader that reads Bambu's
    files, so our estimate treats our output exactly like Bambu's.

    Template text is kept in `meta["texts"]`; a TEXT row points at it (`line` = index) and the
    rows parsed from it carry Flag.TEMPLATE so the emitter prints the text once, verbatim.
    """

    def __init__(self, mb: MoveBuilder, nozzle_of: dict[int, int],
                 prepare_comp: float = 260.0) -> None:
        from .gcode_read import ParseState
        self.mb = mb
        self.add = mb._rows.append
        self.st = ParseState()
        self.nozzle_of = dict(nozzle_of)
        self.prepare_comp = prepare_comp
        self.flags = 0                     # extra flags on generated rows (TOWER, ...)
        self.texts: list[str] = mb.meta.setdefault("texts", [])
        mb.meta.setdefault("layer_z", self.st.layer_z)
        mb.meta.setdefault("layer_h", self.st.layer_h)

    # -- state
    @property
    def pos(self) -> tuple[float, float, float]:
        return self.st.x, self.st.y, self.st.z

    def _row(self, x: float, y: float, z: float, e: float, f: float, kind: int, flags: int = 0,
             i: float = 0.0, j: float = 0.0, p: int = 0, dwell: float = 0.0,
             args: tuple = _NO_ARGS) -> None:
        st = self.st
        acc = st.acc_travel if kind == Kind.TRAVEL else (
            st.acc_retract if kind in (Kind.RETRACT, Kind.UNRETRACT, Kind.VIRTUAL)
            else st.acc_print)
        fl = flags | self.flags
        if st.in_start:
            fl |= Flag.START
        if st.in_end:
            fl |= Flag.END
        self.add((x, y, z, e, f, int(kind), st.role, st.slot, st.nozzle, st.layer, st.width,
                  st.height, acc, dwell, fl, i, j, p, 0, args))

    def feature(self, role: int, width: float | None = None) -> None:
        st = self.st
        st.role = int(role)
        if width is not None:
            st.width = float(width)
        if not st.seen_feature:
            st.seen_feature = True
            st.in_start = role == Role.CUSTOM
        elif st.in_start and not st.start_done:
            st.in_start = False
            st.start_done = True

    def new_layer(self, z: float, height: float) -> None:
        st = self.st
        st.layer += 1
        st.layer_z.append(float(z))
        st.layer_h.append(float(height))
        st.height = float(height)

    def accel(self, s: float) -> None:
        st = self.st
        if st.acc_print == s and st.acc_travel == s:
            return
        st.acc_print = st.acc_travel = float(s)
        self._row(st.x, st.y, st.z, 0.0, st.f, Kind.M204, args=(float(s), NAN, NAN, NAN))

    def travel(self, x: float, y: float, z: float | None = None, f: float | None = None) -> None:
        st = self.st
        nz = st.z if z is None else z
        if f is not None:
            st.f = f
        if x == st.x and y == st.y and nz == st.z:
            return
        self._row(x, y, nz, 0.0, st.f, Kind.TRAVEL)
        st.x, st.y, st.z = x, y, nz

    def extrude(self, x: float, y: float, e: float, f: float | None = None) -> None:
        st = self.st
        if f is not None:
            st.f = f
        if x == st.x and y == st.y:
            return
        self._row(x, y, st.z, e, st.f, Kind.WIPE if st.wiping else Kind.EXTRUDE)
        st.x, st.y = x, y

    def arc(self, x: float, y: float, i: float, j: float, e: float, cw: bool,
            f: float | None = None, z: float | None = None, p: int = 0) -> None:
        st = self.st
        if f is not None:
            st.f = f
        nz = st.z if z is None else z
        if e > 0:
            kind = Kind.EXTRUDE
        elif e < 0:
            kind = Kind.TRAVEL
        else:
            kind = Kind.TRAVEL
        if st.wiping:
            kind = Kind.WIPE
        self._row(x, y, nz, e, st.f, kind, Flag.ARC_CW if cw else Flag.ARC_CCW, i, j, p)
        st.x, st.y, st.z = x, y, nz

    def retract(self, de: float, f: float | None = None) -> None:
        """E-only move (negative de retracts, positive unretracts)."""
        st = self.st
        if f is not None:
            st.f = f
        if de == 0:
            return
        self._row(st.x, st.y, st.z, de, st.f, Kind.RETRACT if de < 0 else Kind.UNRETRACT)

    def wipe_start(self) -> None:
        self.st.wiping = True

    def wipe_end(self) -> None:
        self.st.wiping = False

    def wipe(self, x: float, y: float, e: float, f: float | None = None) -> None:
        st = self.st
        if f is not None:
            st.f = f
        self._row(x, y, st.z, e, st.f, Kind.WIPE, Flag.WIPE)
        st.x, st.y = x, y

    def dwell(self, sec: float) -> None:
        st = self.st
        self._row(st.x, st.y, st.z, 0.0, st.f, Kind.DWELL, dwell=sec)

    def text(self, block: str) -> None:
        """Splice rendered template text: a TEXT row, then its moves parsed with our reader."""
        from .gcode_read import parse_lines
        st = self.st
        self.texts.append(block)
        self.add((st.x, st.y, st.z, 0.0, st.f, int(Kind.TEXT), st.role, st.slot, st.nozzle,
                  st.layer, st.width, st.height, 0.0, 0.0, self.flags, 0.0, 0.0, 0,
                  len(self.texts) - 1, _NO_ARGS))
        parse_lines(block.split("\n"), st, self.add, self.nozzle_of, self.prepare_comp,
                    extra_flags=Flag.TEMPLATE | self.flags)


NAN = float("nan")


def concat(tables: list[MoveTable]) -> MoveTable:
    rows = np.concatenate([t.rows for t in tables]) if tables else np.zeros(0, MOVE_DTYPE)
    meta: dict[str, Any] = {}
    for t in tables:
        meta.update(t.meta)
    return MoveTable(rows=rows, meta=meta)
