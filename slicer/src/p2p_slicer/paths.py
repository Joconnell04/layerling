"""Turn a layer's walls and fills into moves (section 8.1 and 8.3).

Order: islands nearest-first from the current position; per island inner walls, the outer wall,
then fills chained nearest-first. Loops start at the vertex nearest the nozzle (a simple seam
rule; the spec estimates the seam choice at under 1 percent of time) and run clockwise.

Travel: no retraction under `retraction_minimum_travel` (1 mm), or between fill chains of the
same region that stay inside it. Otherwise retract: wipe back along the last extrusion for the
wipe distance spending 95 percent of the retraction, then the last 5 percent in place; lift by
z_hop on a slope while travelling, drop, unretract.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

import numpy as np

from . import geom as G
from .config import SliceConfig
from .moves import PathWriter, Role

FILAMENT_AREA = math.pi / 4 * 1.75 ** 2


@dataclass
class Entity:
    pts: np.ndarray                 # int64 (n, 2)
    closed: bool
    role: int
    width: float
    e_per_mm: float                 # filament mm per mm of path
    speed: float                    # mm/s (before cooling)
    accel: float
    extrude: np.ndarray | None = None   # per segment, for chains with travel links
    region_key: int = 0             # fills of one region share a key (no retract between them)
    skip: bool = False              # printed elsewhere (sparse island taken by a flush)


@dataclass
class Island:
    walls: list[Entity] = field(default_factory=list)   # in print order
    fills: list[Entity] = field(default_factory=list)
    centre: tuple[float, float] = (0.0, 0.0)


@dataclass
class TravelCfg:
    retract_len: float
    retract_speed: float
    deretract_speed: float
    wipe_dist: float
    z_hop: float
    min_travel: float
    travel_speed: float
    travel_accel: float
    short_accel: float


def travel_cfg(cfg: SliceConfig, slot: int, layer0: bool) -> TravelCfg:
    from .speeds import travel_accel
    n = cfg.nozzle_of(slot)
    r = cfg.retraction(slot)
    return TravelCfg(retract_len=r["length"], retract_speed=r["speed"],
                     deretract_speed=r["deretraction_speed"], wipe_dist=r["wipe_distance"],
                     z_hop=r["z_hop"], min_travel=r["minimum_travel"],
                     travel_speed=cfg.nozzle("travel_speed", n, 1000.0),
                     travel_accel=travel_accel(cfg, n, layer0),
                     short_accel=cfg.nozzle("travel_short_distance_acceleration", n, 250.0))


class LayerPrinter:
    """Prints entities through a PathWriter, keeping retraction state and the last path for
    wipes."""

    def __init__(self, pw: PathWriter, tc: TravelCfg, z: float, *, overhang=None,
                 cfg: SliceConfig | None = None, nozzle: int = 0) -> None:
        self.pw = pw
        self.tc = tc
        self.z = z
        self.overhang = overhang      # support.OverhangMap of the layer below, for wall speeds
        self.cfg = cfg
        self.nozzle = nozzle
        self.retracted = 0.0          # mm of filament currently retracted
        self.last_path: list[tuple[float, float]] = []   # recent extrusion points, mm
        self.last_speed = 0.0

    # -- retraction and travel
    def retract(self, wipe: bool = True) -> None:
        tc = self.tc
        if self.retracted > 0 or tc.retract_len <= 0:
            return
        pw = self.pw
        total = tc.retract_len
        done = 0.0
        if wipe and tc.wipe_dist > 0 and len(self.last_path) >= 2:
            pw.accel(tc.travel_accel)
            pw.wipe_start()
            share = 0.95 * total
            pts = list(reversed(self.last_path))
            left = tc.wipe_dist
            wipe_pts = []
            for a, b in itertools.pairwise(pts):
                d = math.hypot(b[0] - a[0], b[1] - a[1])
                if d <= 0:
                    continue
                if d >= left:
                    t = left / d
                    wipe_pts.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), left))
                    left = 0
                    break
                wipe_pts.append((b[0], b[1], d))
                left -= d
            wl = sum(w[2] for w in wipe_pts)
            f = self.last_speed * 60.0 if self.last_speed > 0 else None
            for (x, y, d) in wipe_pts:
                de = -share * d / wl if wl > 0 else 0.0
                pw.wipe(round(x, 3), round(y, 3), de, f)
                done += -de
            pw.wipe_end()
        rest = total - done
        if rest > 1e-9:
            pw.retract(-rest, tc.retract_speed * 60.0)
        self.retracted = total

    def unretract(self) -> None:
        if self.retracted > 0:
            self.pw.retract(self.retracted, self.tc.deretract_speed * 60.0)
            self.retracted = 0.0

    def travel(self, x: float, y: float, *, force_retract: bool = False,
               no_retract: bool = False, into_outer: bool = False) -> None:
        pw = self.pw
        cx, cy, _ = pw.pos
        d = math.hypot(x - cx, y - cy)
        if d < 1e-6:
            return
        tc = self.tc
        if (d < tc.min_travel or no_retract) and not force_retract and self.retracted == 0:
            pw.accel(tc.short_accel if (into_outer and d < 1.0) else tc.travel_accel)
            pw.travel(x, y, f=tc.travel_speed * 60.0)
            return
        self.retract()
        pw.accel(tc.travel_accel)
        if tc.z_hop > 0:
            pw.travel(x, y, self.z + tc.z_hop, f=tc.travel_speed * 60.0)
            pw.travel(x, y, self.z)
        else:
            pw.travel(x, y, f=tc.travel_speed * 60.0)
        self.unretract()

    # -- extrusion
    def extrude_entity(self, ent: Entity, pts_mm: np.ndarray, ext: np.ndarray | None) -> None:
        pw = self.pw
        pw.feature(ent.role, ent.width)
        pw.accel(ent.accel)
        f = ent.speed * 60.0
        self.last_speed = ent.speed
        seg_speed = seg_role = None
        if self.overhang is not None and ent.closed and self.cfg is not None:
            from .support import overhang_speed
            pts_mm, deg = self.overhang.degrees(pts_mm)
            if (deg > 1).any():
                seg_speed = overhang_speed(self.cfg, self.nozzle, deg, ent.speed)
                seg_role = np.where(deg > 5, int(Role.OVERHANG_WALL), ent.role)
        path = [(float(pts_mm[0, 0]), float(pts_mm[0, 1]))]
        for k in range(1, len(pts_mm)):
            x, y = float(pts_mm[k, 0]), float(pts_mm[k, 1])
            px, py = path[-1]
            L = math.hypot(x - px, y - py)
            if ext is not None and not ext[k - 1]:
                pw.accel(self.tc.travel_accel)
                pw.travel(round(x, 3), round(y, 3), f=f)
                pw.accel(ent.accel)
                path = [(x, y)]
                continue
            if L < 1e-4:
                continue
            if seg_speed is not None:
                pw.feature(int(seg_role[k - 1]), ent.width)
                pw.extrude(round(x, 3), round(y, 3), ent.e_per_mm * L,
                           math.floor(seg_speed[k - 1] * 60.0 * 1000 + 0.5) / 1000)
            else:
                pw.extrude(round(x, 3), round(y, 3), ent.e_per_mm * L, f)
            path.append((x, y))
        if seg_speed is not None:
            pw.feature(ent.role, ent.width)
        self.last_path = path[-50:]


def _loop_from_seam(pts: np.ndarray, near: tuple[float, float]) -> np.ndarray:
    """Rotate a closed loop to start at the vertex nearest `near`, clockwise, closed."""
    p = pts.astype(np.float64) * G.UNIT
    if G.area(pts) > 0:
        p = p[::-1]
    d = (p[:, 0] - near[0]) ** 2 + (p[:, 1] - near[1]) ** 2
    k = int(np.argmin(d))
    p = np.roll(p, -k, axis=0)
    return np.vstack([p, p[:1]])


def print_island(lp: LayerPrinter, isl: Island) -> None:
    pw = lp.pw
    for ent in isl.walls:
        cx, cy, _ = pw.pos
        pts = _loop_from_seam(ent.pts, (cx, cy))
        lp.travel(float(pts[0, 0]), float(pts[0, 1]),
                  into_outer=ent.role == Role.OUTER_WALL)
        lp.extrude_entity(ent, pts, None)
    # fills nearest-first
    todo = [e for e in isl.fills if not e.skip]
    last_key = None
    if not todo:
        return
    starts = np.array([e.pts[0] for e in todo], dtype=np.float64) * G.UNIT
    ends = np.array([e.pts[-1] for e in todo], dtype=np.float64) * G.UNIT
    alive = np.ones(len(todo), dtype=bool)
    for _ in range(len(todo)):
        cx, cy, _ = pw.pos
        da = np.where(alive, (starts[:, 0] - cx) ** 2 + (starts[:, 1] - cy) ** 2, np.inf)
        db = np.where(alive, (ends[:, 0] - cx) ** 2 + (ends[:, 1] - cy) ** 2, np.inf)
        ia = int(np.argmin(da))
        ib = int(np.argmin(db))
        if db[ib] < da[ia] or (db[ib] == da[ia] and ib < ia):
            idx, rev = ib, True
        else:
            idx, rev = ia, False
        alive[idx] = False
        ent = todo[idx]
        pts = ent.pts.astype(np.float64) * G.UNIT
        ext = ent.extrude
        if rev:
            pts = pts[::-1]
            ext = ext[::-1] if ext is not None else None
        same_region = last_key is not None and ent.region_key == last_key and \
            ent.role in (Role.SPARSE_INFILL, Role.INTERNAL_SOLID)
        lp.travel(float(pts[0, 0]), float(pts[0, 1]), no_retract=same_region)
        lp.extrude_entity(ent, pts, ext)
        last_key = ent.region_key
