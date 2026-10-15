"""slice_project: painted project 3MF -> moves, time, grams, SliceReport (section 2 pipeline).

    res = slice_project(Path("in.3mf"), out_dir=Path("out"), gcode=True)
    res.report          # p2p_contracts.SliceReport (slicer "p2p_slicer")
    res.analysis        # report.Analysis: time, usage, per-layer filaments
    res.moves           # MoveTable

Steps: load, layers, section, paint segmentation, walls, surfaces and fills per region, filament
sets per layer, nozzle map and per-layer order (grouping), change plan with flush into infill
(purge), prime tower (tower), moves per layer (paths, templates), cooling slowdown, estimate.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import geom as G
from .config import SliceConfig, slice_config
from .cooling import slow_down_layer
from .infill import Chain, concentric_fill, is_narrow, solid_fill, sparse_grid
from .layers import LayerPlan, plan_layers
from .model import Project, load_project
from .moves import MoveBuilder, MoveTable, PathWriter, Role
from .paths import Entity, Island, LayerPrinter, print_island, travel_cfg
from .report import Analysis, analyze, cli_result_json, slice_report
from .section import section_mesh
from .segment import paint_regions
from .speeds import feature_accel, feature_speed
from .surfaces import classify
from .walls import cross_section, make_walls

SPIRAL_SLOPE = math.atan(math.pi / 60.0)     # 3 degrees in radians, through atan (spec 8.3)


@dataclass
class SliceResult:
    plan: LayerPlan
    moves: MoveTable
    analysis: Analysis
    report: Any
    cfg: SliceConfig
    filament_maps: list[int]
    layer_sets: list[list[int]]
    orders: list[list[int]] = field(default_factory=list)
    changes: list[Any] = field(default_factory=list)
    tower: Any = None
    warnings: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def time(self):
        return self.analysis.time

    @property
    def usage(self):
        return self.analysis.usage


# ----------------------------------------------------------------------------- geometry stage


@dataclass
class SlotWork:
    """One filament's work on one layer."""

    islands: list[Island] = field(default_factory=list)
    sparse_islands: list[list[Entity]] = field(default_factory=list)   # chains per sparse island
    sparse_mm3: list[float] = field(default_factory=list)              # volume per sparse island


def layer_slices(project: Project, plan: LayerPlan) -> tuple[list[G.Paths], list[Any]]:
    loops = section_mesh(project.vertices, project.faces, plan.slice_z)
    slices = [G.union([G.to_int(q) for q in ll.loops]) if ll.loops else [] for ll in loops]
    return slices, loops


def _e_per_mm(cfg: SliceConfig, slot: int, area_mm2: float) -> float:
    return area_mm2 * cfg.flow_ratio(slot) / cfg.filament_area(slot)


def build_layer_work(cfg: SliceConfig, layer: int, h: float, surf: dict[int, Any],
                     walls: dict[int, list[Any]], anchor: tuple[float, float]
                     ) -> dict[int, SlotWork]:
    """Entities per slot and island, with speeds and E per mm, for the current filament map."""
    layer0 = layer == 0
    out: dict[int, SlotWork] = {}
    ang = 45.0 + 90.0 * (layer % 2)
    for slot, wl in walls.items():
        n = cfg.nozzle_of(slot)
        sw = SlotWork()
        areas = []
        for W in wl:
            isl = Island()
            for lp in sorted(W.loops, key=lambda lp: -lp.depth):
                w = lp.width
                isl.walls.append(Entity(
                    pts=lp.path, closed=True, role=lp.role, width=w,
                    e_per_mm=_e_per_mm(cfg, slot, cross_section(w, h)),
                    speed=feature_speed(cfg, lp.role, n, slot, w, h, layer0),
                    accel=feature_accel(cfg, lp.role, n, layer0)))
            sw.islands.append(isl)
            areas.append(W.outer_area)
        rs = surf.get(slot)
        fills: list[Entity] = []
        key = [1]

        def ents(chains: list[Chain], role: int, width: float, area_per_mm: float | None = None,
                 speed_w: float | None = None, *, key: list[int] = key, slot: int = slot,
                 n: int = n) -> list[Entity]:
            key[0] += 1
            out_e = []
            for ch in chains:
                a = area_per_mm if area_per_mm is not None else ch.spacing * h
                out_e.append(Entity(
                    pts=ch.pts, closed=False, role=role, width=width,
                    e_per_mm=_e_per_mm(cfg, slot, a),
                    speed=feature_speed(cfg, role, n, slot, speed_w or width, h, layer0),
                    accel=feature_accel(cfg, role, n, layer0), extrude=ch.extrude,
                    region_key=key[0]))
            return out_e
        if rs is not None:
            ws = 0.5 if layer0 else cfg.f("internal_solid_infill_line_width", 0.42)
            wt = cfg.f("top_surface_line_width", 0.42)
            wsp = 0.5 if layer0 else cfg.f("sparse_infill_line_width", 0.45)
            # layer-0 bottom: lines joined by extrusions along the boundary (only the top's
            # monotonicline pattern changes lines with travel detours)
            fills += ents(solid_fill(rs.bottom, ws, h, ang), Role.BOTTOM_SURFACE, ws)
            fills += ents(solid_fill(rs.bridge, ws, h, ang), Role.BRIDGE, ws)
            wb = 0.4
            fills += ents(solid_fill(rs.internal_bridge, wb + 0.05 + 0.2146 * h, h, ang),
                          Role.BRIDGE, wb, area_per_mm=math.pi / 4 * wb * wb)
            wide, narrow = [], []
            for ex in G.expolygons(rs.solid) if rs.solid else []:
                (narrow if not layer0 and is_narrow(ex) else wide).extend([ex[0], *ex[1]])
            fills += ents(solid_fill(wide, ws, h, ang), Role.INTERNAL_SOLID, ws)
            fills += ents(concentric_fill(narrow, ws, h), Role.INTERNAL_SOLID, ws)
            dens = cfg.pct_or_abs("sparse_infill_density", 1.0)
            dens = dens / 100.0 if dens > 1 else dens
            for outer, holes in G.expolygons(rs.sparse):
                chains = sparse_grid([outer] + holes, wsp, h, dens, anchor, layer)
                if not chains:
                    continue
                es = ents(chains, Role.SPARSE_INFILL, wsp)
                sw.sparse_islands.append(es)
                sw.sparse_mm3.append(sum(c.length_mm * c.spacing * h for c in chains))
            fills += ents(solid_fill(rs.top, wt, h, ang, monotonic=True), Role.TOP_SURFACE, wt)
        for ent in fills + [e for es in sw.sparse_islands for e in es]:
            _assign(sw.islands, areas, ent)
        out[slot] = sw
    return out


def _assign(islands: list[Island], areas: list[G.Paths], ent: Entity) -> None:
    if not islands:
        return
    k = 0
    if len(islands) > 1:
        pt = (int(ent.pts[0, 0]), int(ent.pts[0, 1]))
        for j, a in enumerate(areas):
            if a and G.point_in(pt, G.offset(a, 0.6)):
                k = j
                break
    islands[k].fills.append(ent)


def first_layer_order(regions0: dict[int, G.Paths]) -> list[int]:
    """Spec 7.1: filaments by the contour area of their smallest island that survives a 0.1 mm
    inward offset, largest first; ties by slot."""
    key = []
    for s, ps in regions0.items():
        areas = [G.area(o) for o, _ in G.expolygons(G.offset(ps, -0.1))]
        key.append((-(min(areas) if areas else 0.0), s))
    return [s for _, s in sorted(key)]


# ----------------------------------------------------------------------------- main entry


def slice_project(path: Path, *, out_dir: Path | None = None, gcode: bool = False,
                  overrides: dict[str, Any] | None = None, expected_slots: int | None = None
                  ) -> SliceResult:
    from . import grouping, purge
    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    project = load_project(path, overrides)
    cfg0 = project.cfg
    timings["load"] = time.perf_counter() - t0
    t = time.perf_counter()
    plan = plan_layers(project.z_max, cfg0.layer_height, cfg0.first_layer_height,
                       project.layer_profile)
    slices, loops = layer_slices(project, plan)
    resolution = cfg0.f("resolution", 0.012)
    slices = [G.simplify_paths(s, resolution) for s in slices]
    timings["section"] = time.perf_counter() - t
    t = time.perf_counter()
    if len(np.unique(project.face_slot)) > 1:
        regions = paint_regions(
            project.vertices, project.faces, project.face_slot, project.face_painted,
            project.face_part, loops, slices, plan.slice_z,
            top_layers=cfg0.i("top_color_penetration_layers", 5),
            bottom_layers=cfg0.i("bottom_color_penetration_layers", 3),
            outer_width=cfg0.f("outer_wall_line_width", 0.42), layer_height=cfg0.layer_height)
    else:
        s = int(project.face_slot[0]) if len(project.face_slot) else 1
        regions = [{s: sl} if sl else {} for sl in slices]
    regions = [{s: G.simplify_paths(ps, resolution) for s, ps in r.items()} for r in regions]
    timings["segment"] = time.perf_counter() - t
    t = time.perf_counter()
    n_layers = len(plan)
    wall_loops = cfg0.i("wall_loops", 2)
    walls: list[dict[int, list[Any]]] = []
    fills: list[dict[int, G.Paths]] = []
    envelopes: list[G.Paths] = []
    for i in range(n_layers):
        h = float(plan.height[i])
        layer0 = i == 0
        wo = 0.5 if layer0 else cfg0.f("outer_wall_line_width", 0.42)
        wi = 0.5 if layer0 else cfg0.f("inner_wall_line_width", 0.45)
        above = slices[i + 1] if i + 1 < n_layers else []
        per_slot: dict[int, list[Any]] = {}
        per_fill: dict[int, G.Paths] = {}
        for slot, reg in regions[i].items():
            wl, fa = [], []
            for outer, holes in G.expolygons(reg):
                W = make_walls([outer] + holes, loops=wall_loops, w_outer=wo, w_inner=wi, h=h,
                               one_wall=not above)
                if W.loops:
                    wl.append(W)
                    fa.extend(W.infill_area)
            if wl:
                per_slot[slot] = wl
                per_fill[slot] = fa
        if len(regions[i]) > 1:     # layer-wide infill area for the vertical shell test
            env: G.Paths = []
            for outer, holes in G.expolygons(slices[i]):
                env.extend(make_walls([outer] + holes, loops=wall_loops, w_outer=wo,
                                      w_inner=wi, h=h, one_wall=not above).infill_area)
            envelopes.append(env)
        else:
            envelopes.append([p for ps in per_fill.values() for p in ps])
        walls.append(per_slot)
        fills.append(per_fill)
    layer_sets = [sorted(w.keys()) for w in walls]
    timings["walls"] = time.perf_counter() - t
    t = time.perf_counter()
    surf = classify(slices, fills, list(plan.height),
                    top_layers=cfg0.i("top_shell_layers", 5),
                    top_thickness=cfg0.f("top_shell_thickness", 0.0),
                    bottom_layers=cfg0.i("bottom_shell_layers", 3),
                    bottom_thickness=cfg0.f("bottom_shell_thickness", 0.0),
                    min_sparse_area=cfg0.f("minimum_sparse_infill_area", 15.0),
                    envelopes=envelopes)
    timings["surfaces"] = time.perf_counter() - t
    # nozzle map, then the config resolved for it
    t = time.perf_counter()
    used = sorted({s for ls in layer_sets for s in ls})
    if len(used) > 1:
        fmap = grouping.filament_map(layer_sets, cfg0)
    else:
        fmap = [1] * max(cfg0.n_filaments, 1)
    cfg = slice_config(project.project_config, fmap)
    timings["grouping"] = time.perf_counter() - t
    t = time.perf_counter()
    lo, hi = project.bbox
    anchor = tuple(G.to_int(np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2])).tolist())
    work = [build_layer_work(cfg, i, float(plan.height[i]), surf[i], walls[i], anchor)
            for i in range(n_layers)]
    timings["fills"] = time.perf_counter() - t
    t = time.perf_counter()
    orders = grouping.layer_orders(layer_sets, fmap, cfg,
                                   first_layer_order(regions[0]) if regions else None) \
        if len(used) > 1 else [list(ls) for ls in layer_sets]
    first_slot = next((o[0] for o in orders if o), used[0] if used else 1)
    sparse = [{s: w.sparse_mm3 for s, w in work[i].items()} for i in range(n_layers)]
    changes = purge.plan_changes(orders, fmap, cfg, first_slot=first_slot,
                                 sparse_islands=sparse) if len(used) > 1 else []
    for ch in changes:     # islands a flush goes into print in the new filament (Emitter)
        for owner, idx in getattr(ch, "absorbed_islands", None) or []:
            sw = work[ch.layer].get(owner)
            if sw is not None and idx < len(sw.sparse_islands):
                for ent in sw.sparse_islands[idx]:
                    ent.skip = True
    tower_plan = None
    if changes:
        from .tower import plan_tower
        tower_plan = plan_tower(changes, plan, cfg, layer_slots=[set(ls) for ls in layer_sets])
    timings["changes"] = time.perf_counter() - t
    t = time.perf_counter()
    bb = _bbox_mm(slices[0] if slices else [])
    if tower_plan is not None and tower_plan.enabled:     # the start G-code's G29 area
        tb = tower_plan.bbox
        bb = (min(bb[0], tb[0]), min(bb[1], tb[1]), max(bb[2], tb[2]), max(bb[3], tb[3]))
    em = Emitter(cfg, plan, slices, work, orders, changes, first_slot,
                 TemplateProvider(cfg, used=used, first_layer=layer_sets[0] if layer_sets else used,
                                  orders=orders,
                                  layer_nozzles=[{cfg.nozzle_of(s) for s in ls}
                                                 for ls in layer_sets],
                                  first_layer_bbox=bb,
                                  max_print_z=float(plan.print_z[-1]) if len(plan) else 0.0))
    em.tower_plan = tower_plan
    mt = em.run()
    timings["moves"] = time.perf_counter() - t
    t = time.perf_counter()
    gcfg = cfg.gcode_config()
    mt.meta["config"] = gcfg
    a = analyze(mt, gcfg)
    timings["estimate"] = time.perf_counter() - t
    warns = list(project.warnings)
    if cfg.i("enable_support", 0) == 1:
        warns.append("supports are on in the project but p2p_slicer does not generate them: "
                     "time and grams leave out Bambu's tree supports")
    rep = slice_report(a, expected_slots=expected_slots,
                       printer_profile=cfg.s("printer_settings_id"),
                       process_profile=cfg.s("print_settings_id"), warnings=warns)
    res = SliceResult(plan=plan, moves=mt, analysis=a, report=rep, cfg=cfg, filament_maps=fmap,
                      layer_sets=layer_sets, orders=orders, changes=changes,
                      tower=em.tower_plan, warnings=warns, timings=timings)
    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "slice_report.json").write_text(rep.model_dump_json(indent=1))
        (out_dir / "result.json").write_text(json.dumps(
            cli_result_json(a, objects=project.objects), indent=4, sort_keys=True))
        if gcode:
            from .emit import to_gcode
            (out_dir / "plate_1.gcode").write_bytes(to_gcode(mt, a))
    timings["total"] = time.perf_counter() - t0
    return res


# ----------------------------------------------------------------------------- templates


class TemplateProvider:
    """Rendered start, layer change, timelapse, change and end G-code (templates.py), with the
    lines Bambu writes around them."""

    def __init__(self, cfg: SliceConfig, *, used: list[int], first_layer: list[int],
                 orders: list[list[int]], layer_nozzles: list[set[int]],
                 first_layer_bbox: tuple[float, float, float, float], max_print_z: float
                 ) -> None:
        from . import templates as tp
        self.tp = tp
        self.cfg = cfg
        self.base = tp.base_env(cfg, used_slots=used or [1], first_layer_slots=first_layer or used)
        self.most_used = tp.most_used_physical_extruder(self.base, layer_nozzles)
        first_on: dict[int, int] = {}
        for o in orders:
            for s in o:
                first_on.setdefault(cfg.nozzle_of(s), s - 1)
        self.first_phys = [-1, -1]
        for nz, fid in first_on.items():
            self.first_phys[tp.physical_extruder(self.base, nz)] = fid
        self.bbox = first_layer_bbox
        self.max_print_z = max_print_z
        self.toolchanges = 0
        self.feedrate: float | None = None

    def start(self, first_slot: int) -> str:
        tp = self.tp
        x0, y0, x1, y1 = self.bbox
        env = tp.start_env(self.base, initial_filament_id=first_slot - 1,
                           first_layer_print_min=[x0, y0],
                           first_layer_print_size=[x1 - x0, y1 - y0],
                           max_print_z=self.max_print_z, first_filaments=self.first_phys)
        text = tp.start_block(env)
        # Bambu's own lines after the filament start G-code
        return text + (f";VT{first_slot - 1} H-1\nG90\nG21\nM83 ; use relative distances for "
                       "extrusion\nM981 S1 P20000 ;open spaghetti detector")

    def layer_change(self, *, layer_num: int, layer_z: float, max_layer_z: float,
                     slot: int) -> str:
        tp = self.tp
        env = tp.layer_change_env(self.base, layer_num=layer_num, layer_z=layer_z,
                                  max_layer_z=max_layer_z, current_filament_id=slot - 1)
        return tp.layer_change_block(env).rstrip("\n")

    def timelapse(self, *, layer_num: int, layer_z: float, max_layer_z: float,
                  slot: int) -> str:
        tp = self.tp
        env = tp.timelapse_env(self.base, layer_num=layer_num, layer_z=layer_z,
                               max_layer_z=max_layer_z,
                               curr_physical_extruder_id=tp.physical_extruder(
                                   self.base, self.cfg.nozzle_of(slot)),
                               most_used_physical_extruder_id=self.most_used,
                               has_timelapse_safe_pos=False, timelapse_inline_photo=True)
        return tp.timelapse_block(env).rstrip("\n")

    def change(self, ch: Any, *, layer: int, z: float, max_layer_z: float,
               z_after: float) -> str:
        tp = self.tp
        self.toolchanges += 1
        env = tp.change_env(self.base, current_filament_id=ch.old - 1,
                            next_filament_id=ch.new - 1, layer_num=layer, layer_z=z,
                            max_layer_z=max_layer_z, toolchange_count=self.toolchanges,
                            flush_length=round(ch.flush_length_mm, 3),
                            z_after_toolchange=z_after)
        text, self.feedrate = tp.change_block(env, self.feedrate)
        return text.rstrip("\n")

    def filament_start(self, slot: int, *, layer: int, z: float) -> str:
        tp = self.tp
        env = tp.filament_start_end_env(self.base, filament_id=slot - 1, layer_num=layer,
                                        layer_z=z, max_layer_z=z)
        return tp.filament_start_block(env).rstrip("\n")

    def end(self, *, max_layer_z: float, slot: int, layer_num: int) -> str:
        tp = self.tp
        env = tp.end_env(self.base, max_layer_z=max_layer_z, current_filament_id=slot - 1,
                         layer_num=layer_num)
        return ("M106 S0\nM106 P2 S0\nM981 S0 P20000 ; close spaghetti detector\n"
                + tp.end_block(env))


# ----------------------------------------------------------------------------- moves


class Emitter:
    """Builds the MoveTable layer by layer: start G-code, per layer the filaments in order (each
    after its change and tower block), idle tower blocks, the end G-code, then cooling."""

    def __init__(self, cfg: SliceConfig, plan: LayerPlan, slices: list[G.Paths],
                 work: list[dict[int, SlotWork]], orders: list[list[int]], changes: list[Any],
                 first_slot: int, tp: TemplateProvider) -> None:
        self.cfg = cfg
        self.plan = plan
        self.slices = slices
        self.work = work
        self.orders = orders
        self.changes = changes
        self.first_slot = first_slot
        self.tp = tp
        self.nozzle_of = {s: cfg.nozzle_of(s) for s in range(1, cfg.n_filaments + 1)}
        self.mb = MoveBuilder(meta={"source": "p2p_slicer", "nozzle_of": self.nozzle_of})
        self.pw = PathWriter(self.mb, self.nozzle_of,
                             cfg.f("machine_prepare_compensation_time", 260.0))
        self.tower_plan = None
        self.last_path: list[tuple[float, float]] = []
        self.last_speed = 0.0
        self.retracted = 0.0

    def printer(self, slot: int, layer: int, z: float) -> LayerPrinter:
        lp = LayerPrinter(self.pw, travel_cfg(self.cfg, slot, layer == 0), z,
                          cfg=self.cfg, nozzle=self.cfg.nozzle_of(slot))
        lp.last_path = self.last_path
        lp.last_speed = self.last_speed
        lp.retracted = self.retracted
        if 0 < layer < len(self.slices) and self.slices[layer - 1]:
            lp.overhang = self._overhang(layer)
        return lp

    def _overhang(self, layer: int):
        from .support import OverhangMap
        if not hasattr(self, "_oh_cache"):
            self._oh_cache = {}
        if layer not in self._oh_cache:
            self._oh_cache = {layer: OverhangMap(self.slices[layer - 1],
                                                 self.cfg.f("outer_wall_line_width", 0.42),
                                                 current=self.slices[layer])}
        return self._oh_cache[layer]

    def keep(self, lp: LayerPrinter) -> None:
        self.last_path = lp.last_path
        self.last_speed = lp.last_speed
        self.retracted = lp.retracted

    def run(self) -> MoveTable:
        pw = self.pw
        start = self.tp.start(self.first_slot)
        if start:
            pw.text(start)
        else:
            pw.feature(Role.CUSTOM)
        if not pw.st.slot:
            pw.st.slot = self.first_slot
            pw.st.nozzle = self.nozzle_of.get(self.first_slot, 0)
        layer_starts: list[int] = []
        prev_z = 0.0
        ci = 0
        n = len(self.plan)
        for i in range(n):
            z = float(self.plan.print_z[i])
            h = float(self.plan.height[i])
            pw.new_layer(z, h)
            layer_starts.append(len(self.mb))
            slot = pw.st.slot
            lp = self.printer(slot, i, z)
            if i > 0:
                lp.retract()
            lc = self.tp.layer_change(layer_num=i, layer_z=z, max_layer_z=z, slot=pw.st.slot)
            if lc:
                pw.text(lc)
            tl = self.tp.timelapse(layer_num=i, layer_z=z, max_layer_z=z, slot=pw.st.slot)
            if tl:
                pw.text(tl)
            self.keep(lp)
            tpl = self.tower_plan
            if tpl is not None and tpl.enabled and i <= tpl.last_layer:
                lch = []
                while ci < len(self.changes) and self.changes[ci].layer == i:
                    lch.append(self.changes[ci])
                    ci += 1
                self._tower_layer(i, z, prev_z, lch)
                prev_z = z
                continue
            first_move = True
            for s in self.orders[i] if i < len(self.orders) else []:
                if s != pw.st.slot:
                    ch = self.changes[ci] if ci < len(self.changes) else None
                    ci += 1
                    self.change(ch, s, i, z, prev_z)
                    first_move = False
                sw = self.work[i].get(s)
                if sw is None:
                    continue
                lp = self.printer(s, i, z)
                todo = list(sw.islands)
                if first_move and todo and i > 0:
                    self._spiral_lift(lp, todo, prev_z)
                first_move = False
                while todo:
                    cx, cy, _ = pw.pos
                    k = min(range(len(todo)), key=lambda j: _dist_to_island(todo[j], cx, cy))
                    print_island(lp, todo.pop(k))
                self.keep(lp)
            prev_z = z
        lp = self.printer(pw.st.slot, n, prev_z)
        lp.retract()
        self.keep(lp)
        end = self.tp.end(max_layer_z=prev_z, slot=pw.st.slot, layer_num=max(n - 1, 0))
        if end:
            pw.text(end)
        mt = self.mb.build()
        rows = mt.rows
        bounds = layer_starts + [len(rows)]
        for i in range(n):
            a, b = bounds[i], bounds[i + 1]
            if b <= a:
                continue
            prev = (rows["x"][a - 1], rows["y"][a - 1], rows["z"][a - 1]) if a > 0 else (0, 0, 0)
            seg = rows[a:b]
            slow_down_layer(seg, self.cfg, prev)
            rows[a:b] = seg
        mt.meta["layer_z"] = list(pw.st.layer_z)
        mt.meta["layer_h"] = list(pw.st.layer_h)
        return mt

    def _spiral_lift(self, lp: LayerPrinter, isls: list[Island], prev_z: float) -> None:
        """Layer change: spiral lift tangent to the travel toward the first island."""
        pw = self.pw
        tc = lp.tc
        if tc.z_hop <= 0:
            return
        cx, cy, _ = pw.pos
        k = min(range(len(isls)), key=lambda j: _dist_to_island(isls[j], cx, cy))
        ent = isls[k].walls[0] if isls[k].walls else isls[k].fills[0]
        p = ent.pts.astype(np.float64) * G.UNIT
        d = (p[:, 0] - cx) ** 2 + (p[:, 1] - cy) ** 2
        tx, ty = p[int(np.argmin(d))]
        dx, dy = tx - cx, ty - cy
        dl = math.hypot(dx, dy) or 1.0
        R = tc.z_hop / (2 * math.pi * SPIRAL_SLOPE)
        ux, uy = dx / dl, dy / dl
        pw.accel(tc.travel_accel)
        pw.arc(cx, cy, round(-R * uy, 3), round(R * ux, 3), 0.0, cw=False,
               f=tc.travel_speed * 60.0, z=round(prev_z + tc.z_hop, 3), p=1)

    def _tower_layer(self, i: int, z: float, prev_z: float, lch: list[Any]) -> None:
        """A layer with the prime tower: tower.tower_layer decides the order of tower blocks and
        filament turns and calls back here for the objects, the change G-code and the new
        filament's start G-code."""
        from .tower import tower_layer
        tpl = self.tower_plan
        pw = self.pw
        st = tpl.state
        st["retracted"] = self.retracted
        st["at_tower"] = False
        st["last_path"] = []
        flow: dict[str, Any] = {"absorb": None}
        n0 = len(self.mb)

        def objects(slot: int) -> None:
            lp = self.printer(slot, i, z)
            lp.retracted = st.get("retracted", 0.0)
            lp.last_path = list(st.get("last_path") or [])
            if flow["absorb"] is not None:
                ch, flow["absorb"] = flow["absorb"], None
                self.keep(lp)
                self.print_absorbed(ch, i, z)
                lp = self.printer(slot, i, z)
            sw = self.work[i].get(slot)
            todo = list(sw.islands) if sw is not None else []
            if len(self.mb) == n0 and todo and i > 0:     # objects open the layer
                self._spiral_lift(lp, todo, prev_z)
            while todo:
                cx, cy, _ = pw.pos
                k = min(range(len(todo)), key=lambda j: _dist_to_island(todo[j], cx, cy))
                print_island(lp, todo.pop(k))
            lp.retract()
            self.keep(lp)
            st["retracted"] = lp.retracted
            st["at_tower"] = False
            st["last_path"] = []

        def template(ch: Any) -> None:
            pw.text(self.tp.change(ch, layer=i, z=z, max_layer_z=z, z_after=pw.pos[2]))
            if getattr(ch, "absorbed_islands", None):
                flow["absorb"] = ch

        def filament_start(ch: Any) -> None:
            pw.text(self.tp.filament_start(ch.new, layer=i, z=z))

        tower_layer(tpl, i, lch, pw, self.cfg, slot=pw.st.slot, template=template,
                    filament_start=filament_start, objects=objects)
        self.retracted = st.get("retracted", 0.0)
        self.last_path = []

    def change(self, ch: Any, new: int, layer: int, z: float, prev_z: float) -> None:
        """A filament change: tower block and change template (filled in by tower.py and
        templates.py); without them, a bare T through the template text."""
        pw = self.pw
        lp = self.printer(pw.st.slot, layer, z)
        lp.retract()
        self.keep(lp)
        if ch is None:
            pw.text(f"T{new - 1} H-1")
            return
        txt = self.tp.change(ch, layer=layer, z=z, max_layer_z=z, z_after=pw.pos[2])
        pw.text(txt)
        pw.text(self.tp.filament_start(new, layer=layer, z=z))
        self.print_absorbed(ch, layer, z)

    def print_absorbed(self, ch: Any, layer: int, z: float) -> None:
        """Sparse islands the flush went into, printed in the new filament right after it."""
        taken = getattr(ch, "absorbed_islands", None) or []
        if not taken:
            return
        cfg = self.cfg
        h = float(self.plan.height[layer])
        n = cfg.nozzle_of(ch.new)
        isl = Island()
        for owner, idx in taken:
            sw = self.work[layer].get(owner)
            if sw is None or idx >= len(sw.sparse_islands):
                continue
            for ent in sw.sparse_islands[idx]:
                area = ent.e_per_mm * cfg.filament_area(owner) / cfg.flow_ratio(owner)
                isl.fills.append(Entity(
                    pts=ent.pts, closed=False, role=ent.role, width=ent.width,
                    e_per_mm=_e_per_mm(cfg, ch.new, area),
                    speed=feature_speed(cfg, ent.role, n, ch.new, ent.width, h, layer == 0),
                    accel=feature_accel(cfg, ent.role, n, layer == 0), extrude=ent.extrude,
                    region_key=ent.region_key))
        if isl.fills:
            lp = self.printer(ch.new, layer, z)
            print_island(lp, isl)
            self.keep(lp)


def _bbox_mm(paths: G.Paths) -> tuple[float, float, float, float]:
    b = G.bbox(paths)
    if b is None:
        return (0.0, 0.0, 0.0, 0.0)
    return (round(b[0] * G.UNIT, 4), round(b[1] * G.UNIT, 4), round(b[2] * G.UNIT, 4),
            round(b[3] * G.UNIT, 4))


def _dist_to_island(isl: Island, x: float, y: float) -> float:
    ent = isl.walls[0] if isl.walls else (isl.fills[0] if isl.fills else None)
    if ent is None:
        return 1e18
    p = ent.pts.astype(np.float64) * G.UNIT
    return float(((p[:, 0] - x) ** 2 + (p[:, 1] - y) ** 2).min())


__all__ = ["SliceResult", "slice_project"]
