"""Build and slice every product at every size tier, flag rule breaks, find the smallest safe size.

    uv run python -m p2p_geometry.tiers --glb model.glb --meshy-3mf multicolor.3mf \
        --out geometry/_out --reports data/slice_reports

Per product and tier: job folder under <out>/tiers/<product>_<tier>/, the production 3MF (threemf
writer defaults, supports from GeometryResult), the slicer gate, and a SliceReport at
<reports>/geometry_<product>_<tier>__production.json with a "config" key (tier included).
Hero renders of every product at M go to <out>/hero/. <out>/tiers/summary.json is rewritten
after every build so a killed run keeps what it finished.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
from p2p_contracts.filaments import FilamentLibrary
from p2p_contracts.job import SIZE_TIERS_MM, AddOn, AddOnKind, PrintSettings, SizeTier, TextSpec

from .demo import CASES
from .meshio import load_meshy_glb
from .output import write_outputs
from .product import SIZE_MEASURE, build_product_geometry
from .production_slice import production_3mf
from .render import render_hero_3mf
from .rules import MIN_FIGURE_HEIGHT_MM, printability_flags

LONG_NAMES = ("Bartholomew", "Arthur, Maud & Biscuit")
HERO_VIEW = {"azim": -55.0, "elev": 20.0}  # 3/4 front, the VIEWS front_3q camera
HERO_VIEW_FLAT = {"azim": -70.0, "elev": 55.0}  # flat charms: from above so the face reads


def _build(mesh, product, addons, lib, settings, *, tier=None, size=None):
    return build_product_geometry(mesh.vertices, mesh.faces, product, addons, size, lib, settings,
                                  size_tier=tier)


def smallest_safe(mesh, product, addons, lib, settings) -> dict:
    """Walk the size from the XS tier: up in 1 mm steps while any flag stands, or down in 2 mm
    steps while none does (not below 60 % of XS). Geometry only, no slicing."""
    tiers = SIZE_TIERS_MM[product]
    xs, top = float(tiers[SizeTier.XS]), float(tiers[SizeTier.L])

    def flags_at(mm):
        return [f.as_dict() for f in printability_flags(
            _build(mesh, product, addons, lib, settings, size=mm))]

    f = flags_at(xs)
    if f:
        mm = xs
        while f and mm < top:
            mm += 1.0
            f = flags_at(mm)
        return {"smallest_safe_mm": mm if not f else None, "searched": "up from XS",
                "flags_at_xs": flags_at(xs)}
    mm, last_ok = xs, xs
    while mm - 2.0 >= 0.6 * xs:
        mm -= 2.0
        if flags_at(mm):
            break
        last_ok = mm
    return {"smallest_safe_mm": last_ok, "searched": "down from XS",
            "floor_reached": last_ok <= 0.6 * xs + 2.0, "flags_at_xs": []}


def long_name_fits(mesh, lib, settings) -> dict:
    out = {}
    for tier in SizeTier:
        row = {}
        for name in LONG_NAMES:
            addons = [AddOn(kind=AddOnKind.NAMEPLATE, text=TextSpec(text=name))]
            geo = _build(mesh, "nameplate", addons, lib, settings, tier=tier)
            t = geo.metrics["text"]
            row[name] = {"cap_mm": round(t["cap_height_mm"], 2), "lines": t["lines"],
                         "plate_w_mm": round(geo.metrics["plate"]["w_mm"], 1),
                         "widened": geo.metrics["plate"]["w_mm"] > SIZE_TIERS_MM["nameplate"][tier] + 0.5}
        out[tier.value] = row
    return out


def main() -> None:
    from p2p_threemf.slicer import slice_check
    from p2p_threemf.writer import DEFAULT_PROCESS_OVERRIDES

    ap = argparse.ArgumentParser()
    ap.add_argument("--glb", required=True)
    ap.add_argument("--meshy-3mf", required=True)
    ap.add_argument("--library", default="filaments/library.json")
    ap.add_argument("--out", default="geometry/_out")
    ap.add_argument("--reports", required=True)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--tiers", nargs="*", default=[t.value for t in SizeTier])
    ap.add_argument("--skip-slice", action="store_true")
    ap.add_argument("--skip-search", action="store_true")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    tdir = out / "tiers"
    tdir.mkdir(parents=True, exist_ok=True)
    reports = Path(a.reports).resolve()
    reports.mkdir(parents=True, exist_ok=True)
    lib = FilamentLibrary.model_validate_json(Path(a.library).read_text())
    mesh = load_meshy_glb(a.glb)
    settings = PrintSettings()
    spath = tdir / "summary.json"
    summary = json.loads(spath.read_text()) if spath.exists() else {"builds": {}, "safe": {}}

    def save():
        spath.write_text(json.dumps(summary, indent=1, default=float))

    for name, (product, addons, _) in CASES.items():
        if a.only and name not in a.only:
            continue
        for tv in a.tiers:
            tier = SizeTier(tv)
            t0 = time.time()
            geo = _build(mesh, product, addons, lib, settings, tier=tier)
            flags = [f.as_dict() for f in printability_flags(geo)]
            job = tdir / f"{name}_{tier.value}" / "job"
            res = write_outputs(geo, job / "s06_printability" / "a1", job_dir=job)
            entry = {
                "product": name, "tier": tier.value, "target_mm": SIZE_TIERS_MM[product][tier],
                "measure": SIZE_MEASURE.get(product, "longest side"),
                "size_mm": [round(float(x), 1) for x in geo.size_mm],
                "figure_height_mm": round(float(geo.metrics["figure_height_mm"]), 1),
                "orientation": res.orientation, "supports": res.supports, "flags": flags,
                "warnings": res.warnings, "notes": res.notes,
            }
            if "text" in geo.metrics:
                entry["cap_mm"] = round(geo.metrics["text"]["cap_height_mm"], 2)
            if "loop" in geo.metrics:
                lp = geo.metrics["loop"]
                entry["loop"] = {k: lp.get(k) for k in ("position", "local_thickness_mm",
                                 "outline_width_mm", "load_path_min_thickness_mm", "embed_min_mm")}
            tmf = tdir / f"{name}_{tier.value}" / f"{name}_{tier.value}_x2d.3mf"
            wr = production_3mf(Path(a.meshy_3mf), geo, res, job, mesh.vertices, mesh.faces, lib,
                                settings, tmf)
            if tier == SizeTier.M:
                view = HERO_VIEW_FLAT if res.orientation != "upright" else HERO_VIEW
                entry["hero"] = str(render_hero_3mf(tmf, out / "hero" / f"{name}_m.png", **view))
            if not a.skip_slice:
                rep = slice_check(tmf, settings, len(wr["slots_used"]), filament_ids=wr["palette"],
                                  out_dir=tmf.parent)
                data = json.loads(rep.model_dump_json())
                data["config"] = {
                    "source": "p2p_geometry size tiers, real Meshy beagle (meshy-6-lite GLB + "
                              "Meshy multicolor 3MF), threemf production writer and slicer gate",
                    "product": name, "size_tier": tier.value,
                    "tier_mm": SIZE_TIERS_MM[product][tier], "measure": entry["measure"],
                    "size_mm": entry["size_mm"], "orientation": res.orientation,
                    "supports": res.supports,
                    "process_overrides": {**DEFAULT_PROCESS_OVERRIDES, **settings.process_overrides,
                                          **wr["config_overrides"]},
                    "layer_mm": settings.layer_mm, "nozzle_mm": settings.nozzle_mm,
                    "palette": wr["palette"], "slots_used": wr["slots_used"],
                    "frame_fit": wr["frame_fit"], "printability_flags": flags,
                    "parts": [{"name": p.name, "role": p.role, "filament_id": p.filament_id}
                              for p in res.parts],
                }
                rpath = reports / f"geometry_{name}_{tier.value}__production.json"
                rpath.write_text(json.dumps(data, indent=1))
                entry.update({
                    "ok": rep.ok, "print_time_s": rep.print_time_s,
                    "total_grams": rep.total_grams, "purge_grams": rep.purge_grams,
                    "slots_used": rep.slots_used, "errors": rep.errors,
                    "thin_paint_regions": len(rep.thin_paint),
                    "thin_paint_mm2": round(sum(t["area_mm2"] for t in rep.thin_paint), 2),
                    "report": str(rpath),
                })
            entry["seconds"] = round(time.time() - t0, 1)
            summary["builds"][f"{name}_{tier.value}"] = entry
            save()
            print(name, tier.value, json.dumps({k: entry.get(k) for k in (
                "ok", "print_time_s", "total_grams", "purge_grams", "size_mm")}),
                  [f["rule"] for f in flags], flush=True)
        if not a.skip_search:
            summary["safe"][name] = smallest_safe(mesh, product, addons, lib, settings)
            save()
            print(name, "smallest safe", summary["safe"][name]["smallest_safe_mm"], flush=True)
    if not a.only or "nameplate" in a.only:
        summary["long_names"] = long_name_fits(mesh, lib, settings)
        save()
    summary["rule_figure_height_mm"] = MIN_FIGURE_HEIGHT_MM
    save()
    print("done", flush=True)


if __name__ == "__main__":
    np.set_printoptions(precision=2)
    main()
