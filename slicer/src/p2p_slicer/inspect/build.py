"""Assemble inspection.json and the renders for one file (section 15 of the spec).

Input: a project 3MF (sliced here with p2p_slicer, else the Bambu CLI, unless slice="none"), a
sliced .gcode.3mf (Bambu's or ours) or a bare .gcode. Every sliced input goes through the same
path: gcode_read -> estimate -> measure, so our slices and Bambu's are read the same way. For
Bambu files, Bambu's own numbers (slice_info, result.json, the G-code header) sit next to ours
with deltas in `parity`."""

from __future__ import annotations

import json
import math
import time as _time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from .package import Package, open_package, package_check

VERSION = 1
FIL_AREA = math.pi * 0.875**2


def _num(v, nd: int = 2):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if math.isfinite(f) else None


def _core():
    """gcode_read, estimate and moves from the slicer core, or None before it lands."""
    try:
        from .. import estimate as est
        from .. import gcode_read as gr
        from .. import moves as mv
    except ImportError:
        return None
    return gr, est, mv


def _native_api():
    try:
        from .. import api  # type: ignore[attr-defined]
    except ImportError:
        return None
    return api if callable(getattr(api, "slice_project", None)) else None


def _sliced_by(pkg: Package) -> dict[str, str | None]:
    gen = pkg.gcode_header.get("generator", "")
    if gen.startswith("BambuStudio"):
        return {"slicer": "bambu_studio", "version": gen.split()[-1]}
    if gen.lower().startswith("p2p_slicer"):
        return {"slicer": "p2p_slicer", "version": gen.split()[-1] if " " in gen else None}
    if pkg.slice_info and pkg.slice_info.client_version:
        return {"slicer": "bambu_studio", "version": pkg.slice_info.client_version}
    return {"slicer": None, "version": None}


def _nozzle_areas(pkg: Package) -> list[dict[str, Any]]:
    raw = pkg.cfg("extruder_printable_area")  # project: one string per nozzle; G-code: "a#b"
    areas = [str(a) for a in raw] if isinstance(raw, list) else (
        str(raw).split("#") if raw else [])
    heights = [float(h) for h in pkg.cfg_list("extruder_printable_height") if h]
    types = pkg.cfg_list("extruder_type")
    diam = pkg.cfg_list("nozzle_diameter")
    vol = pkg.cfg_list("nozzle_volume_type")
    out = []
    for i in range(max(len(areas), 2)):
        xs = []
        if i < len(areas):
            for pt in areas[i].split(","):
                if "x" in pt:
                    xs.append(float(pt.split("x")[0]))
        out.append({"index": i, "side": "left" if i == 0 else "right",
                    "type": types[i] if i < len(types) else None,
                    "diameter_mm": _num(diam[i], 2) if i < len(diam) else None,
                    "volume_type": vol[i] if i < len(vol) else None,
                    "x_range_mm": [min(xs), max(xs)] if xs else None,
                    "max_height_mm": heights[i] if i < len(heights) else None})
    return out


def _filament_maps(pkg: Package, ref: Package | None) -> list[int]:
    for p in (ref, pkg):
        if p is None:
            continue
        if p.slice_info and p.slice_info.meta.get("filament_maps"):
            return [int(x) for x in p.slice_info.meta["filament_maps"].split()]
        if p.plate_settings.get("filament_maps"):
            return [int(x) for x in p.plate_settings["filament_maps"].split()]
    return [int(float(x)) for x in pkg.cfg_list("filament_map") if x]


def _reference(ref: Package) -> dict[str, Any]:
    """Bambu's own numbers from a Bambu-sliced package: slice_info, result.json, header."""
    from p2p_threemf.gcode_stats import hms_to_s, parse_gcode
    from p2p_threemf.slicer import SliceInfo, bambu_numbers

    si = ref.slice_info
    info = SliceInfo()
    if si is not None:
        info.print_time_s = _num(si.meta.get("prediction"), 1)
        info.weight_g = _num(si.meta.get("weight"), 2)
        info.filaments = list(si.filaments)
    totals = {int(f.get("id", 0)): float(f.get("used_g") or 0) for f in info.filaments}
    stats = parse_gcode(ref.gcode, totals) if ref.gcode else None
    nums = bambu_numbers(info, ref.result, stats)
    plate = next(iter(ref.result.get("sliced_plates") or []), {}) if ref.result else {}
    hdr = ref.gcode_header
    out: dict[str, Any] = {
        "time_s": _num(plate.get("total_predication"), 1) or info.print_time_s,
        "prediction_s": info.print_time_s,
        "model_s": hms_to_s(hdr["model printing time"]) if hdr.get("model printing time")
        else None,
        "main_s": _num(plate.get("main_predication"), 1),
        "grams": info.weight_g, "purge_g": nums.purge_grams, "tower_g": nums.tower_grams,
        "ams_swaps": nums.ams_swaps, "filament_changes": nums.filament_changes,
        "nozzle_switches": nums.nozzle_changes,
        "layers": int(hdr["total layer number"]) if hdr.get("total layer number", "").isdigit()
        else None,
        "feature_time_s": {k: _num(v, 2) for k, v in (plate.get("feature_type_times") or {})
                           .items()},
        "filaments": {}, "result_json": str(ref.result_path) if ref.result_path else None,
    }
    if out["time_s"] is not None and out["model_s"] is not None:
        out["prepare_s"] = round(out["time_s"] - out["model_s"], 1)
    for f in nums.filaments:
        raw = next((x for x in info.filaments if int(x.get("id", 0)) == f.slot), {})
        out["filaments"][f.slot] = {
            "total_g": f.grams, "m": f.meters, "model_g": f.model_grams,
            "flush_g": f.flush_grams, "tower_g": f.tower_grams,
            "load_s": _num(raw.get("total_load_time"), 1),
            "unload_s": _num(raw.get("total_unload_time"), 1)}
    out["flush_g"] = (round(sum(f.flush_grams or 0 for f in nums.filaments), 2)
                      if nums.filaments else None)
    return out


def _parity(ours: dict[str, Any], ref: dict[str, Any], roles_ours: dict[str, float],
            fil_ours: dict[int, dict[str, float]]) -> list[dict[str, Any]]:
    rows = []

    def row(label: str, a, b, unit: str):
        if a is None and b is None:
            return
        d = (a - b) if a is not None and b is not None else None
        pct = (100 * d / b) if d is not None and b else None
        ints = isinstance(a, int | None) and isinstance(b, int | None)
        rows.append({"quantity": label, "unit": unit, "ours": a if ints else _num(a, 2),
                     "bambu": b if ints else _num(b, 2), "delta": d if ints else _num(d, 2),
                     "delta_pct": _num(pct, 2)})

    row("Total time", ours.get("time_s"), ref.get("time_s"), "s")
    row("Model time", ours.get("model_s"), ref.get("model_s"), "s")
    row("Prepare time", ours.get("prepare_s"), ref.get("prepare_s"), "s")
    row("Total grams", ours.get("grams"), ref.get("grams"), "g")
    row("Flush grams", ours.get("flush_g"), ref.get("flush_g"), "g")
    row("Tower grams", ours.get("tower_g"), ref.get("tower_g"), "g")
    row("Filament changes", ours.get("filament_changes"), ref.get("filament_changes"), "")
    row("AMS swaps", ours.get("ams_swaps"), ref.get("ams_swaps"), "")
    row("Nozzle switches", ours.get("nozzle_switches"), ref.get("nozzle_switches"), "")
    row("Layers", ours.get("layers"), ref.get("layers"), "")
    for name, t in sorted((ref.get("feature_time_s") or {}).items()):
        if name == "Travel":
            row("Travel time", ours.get("travel_s"), t, "s")
        else:
            row(f"{name} time", roles_ours.get(name), t, "s")
    for slot, f in sorted((ref.get("filaments") or {}).items()):
        o = fil_ours.get(slot) or {}
        row(f"Slot {slot} grams", o.get("total_g"), f.get("total_g"), "g")
    return rows


def inspect_path(path: str | Path, out_dir: str | Path, *, project: str | Path | None = None,
                 result: str | Path | None = None, reference: str | Path | None = None,
                 slice_mode: str = "auto", timeout_s: float = 900,
                 log: Callable[[str], None] = lambda m: None) -> dict[str, Any]:
    """Write inspection.json, renders/ and index.html into out_dir; return the inspection."""
    t_start = _time.monotonic()
    out = Path(out_dir).resolve()
    rdir = out / "renders"
    rdir.mkdir(parents=True, exist_ok=True)
    pkg = open_package(path, result)
    project_path = Path(project).resolve() if project else (pkg.path if pkg.kind != "gcode"
                                                            else None)
    sliced: Package | None = pkg if pkg.sliced else None
    notes: list[str] = []
    native_res = None

    if pkg.kind == "3mf" and slice_mode != "none":
        api = _native_api() if slice_mode in ("auto", "p2p") else None
        if api is not None:
            log("slicing with p2p_slicer")
            try:
                native_res = api.slice_project(pkg.path, out_dir=out / "slice", gcode=True)
            except Exception as e:  # noqa: BLE001 show the model and say why
                notes.append(f"p2p_slicer failed: {type(e).__name__}: {e}")
            got = sorted((out / "slice").glob("*.gcode.3mf"))
            if native_res is not None and got:
                sliced = open_package(got[0])
        elif slice_mode in ("auto", "bambu"):
            from p2p_threemf.slicer import BambuStudioBackend

            be = BambuStudioBackend()
            if be.available():
                log(f"slicing with Bambu Studio {be.version()} (CLI, headless)")
                raw = be.slice(pkg.path, out / "bambu", timeout_s)
                if raw.sliced_3mf is not None:
                    sliced = open_package(raw.sliced_3mf, out / "bambu" / "result.json")
                else:
                    notes.append("Bambu Studio did not slice: " + "; ".join(raw.errors[:3]))
            else:
                notes.append("no slicer here: model panels only")
        else:
            notes.append("no slicer core yet: model panels only")

    ref_pkg = open_package(reference) if reference else None
    if ref_pkg is None and sliced is not None and _sliced_by(sliced)["slicer"] == "bambu_studio":
        ref_pkg = sliced
    ref = _reference(ref_pkg) if ref_pkg is not None and ref_pkg.gcode else None
    cfg_pkg = sliced or pkg

    # ------------------------------------------------------------------ moves and time
    core = _core()
    mt = time_res = meas = None
    if native_res is not None:
        src = getattr(native_res, "analysis", None) or native_res
        mt = getattr(src, "moves", None)
        time_res = getattr(src, "time", None)
    elif sliced is not None and sliced.gcode and core is not None:
        gr, est, _mv = core
        log("reading G-code")
        mt = gr.read_gcode(sliced.gcode)
        log("estimating time")
        time_res = est.estimate(mt)
    elif sliced is not None and core is None:
        notes.append("slicer core not installed: toolpaths, layers and our estimate skipped")

    colours = cfg_pkg.cfg_list("filament_colour") or pkg.cfg_list("filament_colour")
    n_fil = len(colours)
    dens = [float(x) for x in cfg_pkg.cfg_list("filament_density") if x] or [1.24] * n_fil
    diam = [float(x) for x in cfg_pkg.cfg_list("filament_diameter") if x] or [1.75] * n_fil
    densities = {i + 1: dens[i] if i < len(dens) else 1.24 for i in range(max(n_fil, 1))}
    areas = {i + 1: math.pi * (diam[i] / 2) ** 2 if i < len(diam) else FIL_AREA
             for i in range(max(n_fil, 1))}
    if mt is not None:
        from .measure import measure

        nv = cfg_pkg.cfg_float("nozzle_volume", 92.0) or 92.0
        meas = measure(mt, time_res, nozzle_volume=nv, areas=areas, densities=densities)

    # ------------------------------------------------------------------ scene
    scene = None
    if project_path is not None:
        from .scene import load_scene

        log("reading the painted model")
        try:
            scene = load_scene(project_path)
        except (KeyError, ValueError, OSError) as e:
            notes.append(f"model not read: {type(e).__name__}: {e}")

    insp = _assemble(pkg, cfg_pkg, sliced, ref, mt, meas, time_res, scene, project_path,
                     colours, densities, notes, native_res)

    # ------------------------------------------------------------------ renders
    renders: dict[str, Any] = {}
    from .scene import render_multi, render_plate

    tower = insp["plate"].get("tower") or {}
    tbox = tower.get("bbox_mm")
    ttop = tower.get("height_mm") or (insp["totals"].get("max_z_mm") or 0)
    if scene is not None:
        log("rendering the model")
        renders["model"] = {k: f"renders/{v}" for k, v in render_multi(scene, rdir).items()}
    log("rendering the plate")
    obox = None
    if scene is not None:
        obox = scene.bbox
    plate = render_plate(scene, rdir, tuple(insp["printer"]["bed_mm"]),
                         _right_min_x(insp), (tbox, ttop) if tbox else None, obox)
    renders["plate"] = {k: f"renders/{v}" for k, v in plate.items()}
    if sliced is not None:
        for name in ("plate_1.png", "top_1.png", "plate_no_light_1.png"):
            if name in sliced.thumbnails:
                (rdir / f"bambu_{name}").write_bytes(sliced.thumbnails[name])
                renders.setdefault("bambu", {})[name.removesuffix(".png")] = \
                    f"renders/bambu_{name}"
    if mt is not None and meas is not None:
        from .paths import pick_layers, render_layers, render_toolpaths, segments, travel_segments

        log("rendering toolpaths")
        seg = segments(mt)
        renders["toolpaths"] = {k: f"renders/{v}" for k, v in
                                render_toolpaths(seg, colours, rdir).items()}
        picks = pick_layers(meas.extruded_sets, [r["changes"] for r in meas.layer_rows])
        lv = render_layers(seg, travel_segments(mt), picks, colours, rdir)
        renders["layers"] = [{"index": i, "z": meas.layer_rows[i]["z"],
                              "path": f"renders/{p}"} for i, p in lv.items()]
    if meas is not None and meas.layer_rows and meas.layer_rows[0]["time_s"] is not None:
        from .chart import layer_time_svg

        svg = layer_time_svg(insp["layers"], meas.layer_cat_s)
        (rdir / "layer_time.svg").write_text(svg)
        renders["layer_time"] = "renders/layer_time.svg"
    insp["renders"] = renders
    insp["run"] = {"seconds": round(_time.monotonic() - t_start, 2)}

    (out / "inspection.json").write_text(json.dumps(insp, indent=1, default=_json_default))
    from .page import write_page

    write_page(insp, out / "index.html")
    return insp


def _json_default(o):
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(type(o).__name__)


def _right_min_x(insp: dict[str, Any]) -> float | None:
    noz = insp["printer"]["nozzles"]
    if len(noz) > 1 and noz[1].get("x_range_mm"):
        lo = noz[1]["x_range_mm"][0]
        return lo if lo > 0 else None
    return None


def _assemble(pkg: Package, cfg: Package, sliced: Package | None, ref: dict[str, Any] | None,
              mt, meas, time_res, scene, project_path: Path | None, colours: list[str],
              densities: dict[int, float], notes: list[str], native_res=None) -> dict[str, Any]:
    from .scene import faces_per_part, tower_estimate

    by = _sliced_by(sliced) if sliced is not None else {"slicer": None, "version": None}
    if meas is not None and time_res is not None and by["slicer"] is None:
        by = {"slicer": "p2p_slicer", "version": None}
    bed = [256.0, 256.0]
    area = cfg.cfg_list("printable_area")
    pts = [p for p in (area[0].split(",") if len(area) == 1 else area) if "x" in p]
    if pts:
        bed = [max(float(p.split("x")[0]) for p in pts), max(float(p.split("x")[1])
                                                             for p in pts)]
    nozzles = _nozzle_areas(cfg)
    fmaps = _filament_maps(pkg, sliced)
    nsrc = (getattr(native_res, "analysis", None) or native_res) if native_res is not None \
        else None
    if nsrc is not None and getattr(nsrc, "filament_maps", None):
        fmaps = [int(x) for x in nsrc.filament_maps]  # 1-based nozzle per slot
    presets = cfg.cfg_list("filament_settings_id")
    types = cfg.cfg_list("filament_type")
    fids = cfg.cfg_list("filament_ids")
    si = sliced.slice_info if sliced is not None else None

    # objects and parts
    fpp = faces_per_part(scene) if scene is not None else {}
    objects = []
    src_objects = pkg.objects or (sliced.objects if sliced is not None else [])
    for o in src_objects:
        parts = []
        for p in o["parts"]:
            fil = p["filament"] or o["filament"]
            parts.append({"id": p["id"], "name": p["name"], "subtype": p["subtype"],
                          "filament": fil, "faces": p["faces"],
                          "faces_per_filament": {str(k): v for k, v in
                                                 sorted(fpp.get(p["id"], {}).items())}})
        objects.append({"id": o["id"], "name": o["name"], "filament": o["filament"],
                        "faces": o["faces"], "parts": parts})

    painted = ({int(s) for s in np.unique(scene.face_slots)} if scene is not None else set())
    face_counts = ({int(s): int(c) for s, c in zip(*np.unique(scene.face_slots,
                                                                return_counts=True),
                                                    strict=True)} if scene is not None else {})

    # filaments
    filaments = []
    ours_fil: dict[int, dict[str, float]] = {}
    for i, col in enumerate(colours):
        slot = i + 1
        rho = densities.get(slot, 1.24)
        area = FIL_AREA
        u = meas.per_slot.get(slot) if meas is not None else None
        usage = None
        if u is not None:
            usage = {k + "_g": round(u[k] * rho / 1000, 3) for k in
                     ("model", "support", "flush", "tower", "total")}
            usage["total_m"] = round(u["total"] / area / 1000, 3)
            ours_fil[slot] = usage
        load = getattr(time_res, "load_s", {}) or {}
        unload = getattr(time_res, "unload_s", {}) or {}
        filaments.append({
            "slot": slot, "colour": col, "preset": presets[i] if i < len(presets) else None,
            "type": types[i] if i < len(types) else None,
            "filament_id": fids[i] if i < len(fids) else None,
            "nozzle": (fmaps[i] - 1) if i < len(fmaps) else None,
            "used": bool(u and u["total"] > 0) if meas is not None else (
                slot in (ref or {}).get("filaments", {})),
            "faces": face_counts.get(slot, 0),
            "usage": usage,
            "load_s": _num(load.get(slot), 1), "unload_s": _num(unload.get(slot), 1),
            "reference": (ref or {}).get("filaments", {}).get(slot)})

    # totals
    totals: dict[str, Any] = {"layers": meas.layers if meas is not None else None}
    if time_res is not None:
        total = float(time_res.total_s)
        prep = float(time_res.prepare_s)
        totals.update(time_s=round(total, 1), prepare_s=round(prep, 1),
                      model_s=round(float(getattr(time_res, "model_s", total - prep)), 1),
                      travel_s=_num(getattr(time_res, "travel_s", None), 1))
    if meas is not None:
        g = {k: sum(u.get(k + "_g", 0) for u in ours_fil.values())
             for k in ("model", "support", "flush", "tower", "total")}
        totals.update(grams=round(g["total"], 2), model_g=round(g["model"] + g["support"], 2),
                      flush_g=round(g["flush"], 2), tower_g=round(g["tower"], 2),
                      filament_changes=meas.filament_changes, ams_swaps=meas.ams_swaps,
                      nozzle_switches=meas.nozzle_switches,
                      max_z_mm=_num(meas.bbox_extrude[4], 2) if meas.bbox_extrude else None)
    elif scene is not None:
        totals["max_z_mm"] = _num(scene.bbox[1][2], 2)
    totals["reference"] = ref and {k: ref.get(k) for k in (
        "time_s", "prediction_s", "model_s", "prepare_s", "main_s", "grams", "purge_g",
        "flush_g", "tower_g", "ams_swaps", "filament_changes", "nozzle_switches", "layers")}

    # roles
    from .style import ROLE_COLOURS

    roles = []
    per_role = dict(getattr(time_res, "per_role_s", {}) or {}) if time_res is not None else {}
    ref_ft = (ref or {}).get("feature_time_s") or {}
    total_s = totals.get("time_s") or (ref or {}).get("time_s") or 0
    names = list(ROLE_COLOURS) + [n for n in list(per_role) + list(ref_ft)
                                  if n not in ROLE_COLOURS and n != "Travel"]
    for name in names:
        t = per_role.get(name)
        g = meas.role_g.get(name) if meas is not None else None
        e_mm = meas.role_e_mm.get(name) if meas is not None else None
        if not t and not g and not ref_ft.get(name):
            continue
        roles.append({"role": name, "colour": ROLE_COLOURS.get(name, "#BBBBBB"),
                      "time_s": _num(t, 1),
                      "percent": _num(100 * t / total_s, 1) if t and total_s else None,
                      "m": _num(e_mm / 1000, 3) if e_mm else None,
                      "g": _num(g, 3) if g else None,
                      "reference_time_s": ref_ft.get(name)})

    # layers
    layers = []
    planned: dict[int, list[int]] = {}
    for lst in (si.layer_filament_lists if si is not None else []):
        for k in range(lst["first"], lst["last"] + 1):
            planned[k] = lst["slots"]
    if meas is not None:
        for r in meas.layer_rows:
            layers.append({**r, "planned": planned.get(r["index"])})
        cat = meas.layer_cat_s
        if cat is not None:
            for r in layers:
                k = r["index"] + 1
                r["time_split_s"] = {n: round(float(cat[k, c]), 2) for c, n in
                                     enumerate(("model", "tower", "flush", "other"))}
    switches = []
    if meas is not None:
        for s in meas.switches:
            billed = {k: round(v * densities.get(int(k), 1.24) / 1000, 4)
                      for k, v in s["billed_mm3"].items()}
            switches.append({**s, "flush_g": round(sum(billed.values()), 3),
                             "billed_g": billed})

    # plate and tower
    tower = None
    pj = sliced.plate_json if sliced is not None else {}
    tb = next((o["bbox"] for o in pj.get("bbox_objects", []) if o.get("name") == "wipe_tower"),
              None)
    used_slots = [f["slot"] for f in filaments if f["used"]] or sorted(painted)
    height = (meas.bbox_extrude[4] if meas is not None and meas.bbox_extrude
              else (float(scene.bbox[1][2]) if scene is not None else 0.0))
    last_change_z = max((s["z"] or 0 for s in switches), default=None) if switches else None
    if tb is not None:
        tower = {"bbox_mm": [round(v, 3) for v in tb], "basis": "sliced",
                 "height_mm": _num(last_change_z or height, 2)}
    elif (cfg.cfg_list("enable_prime_tower") or ["1"])[0] != "0" and len(used_slots) > 1:
        tx = cfg.cfg_float("wipe_tower_x", 15.0)
        ty = cfg.cfg_float("wipe_tower_y", 220.0)
        w0 = cfg.cfg_float("prime_tower_width", 35.0) or 35.0
        lh = cfg.cfg_float("layer_height", 0.2) or 0.2
        w, d = tower_estimate(len(used_slots), height, lh, w0)
        tower = {"bbox_mm": [tx, ty, round(tx + w, 3), round(ty + d, 3)],
                 "basis": "estimate (ASSUMED: every filament changes on each layer)",
                 "height_mm": _num(height, 2)}
    obox = None
    if scene is not None:
        lo, hi = scene.bbox
        obox = [round(float(v), 3) for v in (*lo, *hi)]
    first = meas.first_slot if meas is not None else None
    if first is None and pj.get("first_extruder") is not None:
        first = int(pj["first_extruder"]) + 1
    plate = {"bed_mm": bed, "objects_bbox_mm": obox, "first_filament": first, "tower": tower,
             "bed_type": cfg.cfg("curr_bed_type")}

    # grouping
    grouping = {"mode": (pkg.plate_settings.get("filament_map_mode")
                         or cfg.cfg("filament_map_mode")),
                "filament_maps": fmaps,
                "left": [s for s in used_slots if s - 1 < len(fmaps) and fmaps[s - 1] == 1],
                "right": [s for s in used_slots if s - 1 < len(fmaps) and fmaps[s - 1] == 2],
                "sequence": (sliced.sequence.get("sequence") if sliced is not None else None),
                "nozzle_sequence": (sliced.sequence.get("nozzle_sequence")
                                    if sliced is not None else None)}

    # warnings
    warnings: list[dict[str, Any]] = []
    for w in (si.warnings if si is not None else []):
        warnings.append({"code": w.get("error_code") or w.get("msg"),
                         "level": "info" if w.get("level") == "1" else "warning",
                         "text": (w.get("msg") or "").replace("_", " "), "source": "slice_info"})
    warnings.extend(_log_warnings(sliced))
    if meas is not None:
        from .measure import checks

        right = _right_min_x({"printer": {"nozzles": nozzles}}) or 20.5
        warnings.extend(checks(
            mt, meas, tuple(bed),
            max_z=cfg.cfg_float("printable_height", 261.0) or 261.0, right_min_x=right,
            right_max_z=nozzles[1].get("max_height_mm") if len(nozzles) > 1 else None,
            tower_bbox=tower["bbox_mm"] if tower else None,
            object_bbox=obox[:2] + obox[3:5] if obox else None,
            types={i + 1: t for i, t in enumerate(types)}, painted_slots=painted or None))
    pc = package_check(sliced or pkg)
    if pc["missing"]:
        warnings.append({"code": "package", "level": "warning", "source": "package",
                         "text": "Missing " + ", ".join(pc["missing"][:4])})
    if pc["gcode_md5_ok"] is False:
        warnings.append({"code": "md5", "level": "error", "source": "package",
                         "text": "G-code checksum mismatch"})
    nrep = getattr(native_res, "report", None) if native_res is not None else None
    nwarn = (getattr(nrep, "warnings", None) if nrep is not None
             else getattr(native_res, "warnings", None)) or []
    for w in nwarn:
        text = getattr(w, "text", None) or getattr(w, "message", None) or str(w)
        warnings.append({"code": getattr(w, "code", None) or "p2p_slicer",
                         "level": getattr(w, "level", None) or "warning", "text": text,
                         "source": "p2p_slicer"})
    for n in notes:
        warnings.append({"code": "note", "level": "info", "text": n, "source": "inspector"})

    ours_totals = {**{k: totals.get(k) for k in (
        "time_s", "model_s", "prepare_s", "travel_s", "grams", "flush_g", "tower_g",
        "filament_changes", "ams_swaps", "nozzle_switches", "layers")}}
    parity = (_parity(ours_totals, ref, dict(per_role), ours_fil)
              if ref is not None and meas is not None else [])

    return {
        "version": VERSION,
        "source": {"path": str(pkg.path), "kind": pkg.kind,
                   "project": str(project_path) if project_path else None,
                   "sliced": str(sliced.path) if sliced is not None else None,
                   "sliced_by": by, "result_json": str(sliced.result_path)
                   if sliced is not None and sliced.result_path else None,
                   "estimate": "p2p_slicer" if time_res is not None else None},
        "printer": {"preset": cfg.cfg("printer_settings_id"), "bed_mm": bed,
                    "max_height_mm": cfg.cfg_float("printable_height", 261.0),
                    "nozzles": nozzles},
        "process": {"preset": cfg.cfg("print_settings_id"),
                    "layer_mm": cfg.cfg_float("layer_height"),
                    "first_layer_mm": cfg.cfg_float("initial_layer_print_height"),
                    "wall_loops": cfg.cfg_float("wall_loops"),
                    "sparse_infill": cfg.cfg("sparse_infill_density"),
                    "prime_tower": cfg.cfg_list("enable_prime_tower")[:1] == ["1"],
                    "prime_volume_mode": cfg.cfg("prime_volume_mode"),
                    "flush_into_infill": cfg.cfg("flush_into_infill"),
                    "supports": cfg.cfg("enable_support")},
        "plate": plate, "objects": objects, "filaments": filaments, "totals": totals,
        "roles": roles,
        "travel": {**(meas.travel if meas is not None else {}),
                   "time_s": totals.get("travel_s"),
                   "reference_time_s": ref_ft.get("Travel")},
        "layers": layers, "switches": switches, "grouping": grouping,
        "warnings": warnings, "package": pc, "parity": parity,
    }


def _log_warnings(sliced: Package | None) -> list[dict[str, Any]]:
    """Slicer log next to a kept file (<stem>.slice.log) or in the Bambu CLI folder."""
    if sliced is None:
        return []
    from p2p_threemf.slicer import _log_lines

    stem = sliced.path.name.removesuffix(".gcode.3mf").removesuffix(".3mf")
    for cand in (sliced.path.parent / f"{stem}.slice.log", sliced.path.parent / "slicer.log"):
        if cand.is_file():
            errors, warnings = _log_lines(cand)
            return ([{"code": "log", "level": "error", "text": e, "source": "slicer log"}
                     for e in errors] +
                    [{"code": "log", "level": "warning", "text": w, "source": "slicer log"}
                     for w in warnings])
    return []
