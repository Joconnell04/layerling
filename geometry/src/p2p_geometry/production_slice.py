"""Build the five products from a Meshy job and slice each the way production does.

    uv run python -m p2p_geometry.production_slice --glb model.glb --meshy-3mf multicolor.3mf \
        --out geometry/_out --reports data/slice_reports

For each product: build_product into <out>/<product>/job/s06_printability/a1 (geometry.json and
PLYs), a preview PNG, then the p2p_threemf production writer (Meshy's multicolor paint carried
onto the geometry figure, add-ons as parts, writer defaults: prime_volume_mode Saving,
flush_into_infill 1) and the slicer gate. Supports follow GeometryResult.supports. The
SliceReport goes to <reports>/geometry_<product>__baseline__production.json with a "config" key.

The Meshy 3MF is fitted to the UNCUT figure: the flat products lose Meshy's plinth, and fitting
to the cut figure's bounding box would shift the paint by the plinth height.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
from p2p_contracts.filaments import FilamentLibrary
from p2p_contracts.job import PrintSettings

from .demo import CASES
from .meshio import apply_transform, load_meshy_glb
from .output import write_outputs
from .product import build_product_geometry
from .render import render_product
from .repair import repair_figure

SUPPORT_OVERRIDES = {"none": {"enable_support": "0"}, "tree": {"enable_support": "1"},
                     "normal": {"enable_support": "1", "support_type": "normal(auto)"}}


def production_3mf(meshy_3mf: Path, geo, res, job_dir: Path, raw_v, raw_f, library,
                   settings: PrintSettings, out_path: Path) -> dict:
    """p2p_threemf.retarget with the figure from geometry and a fit to the uncut figure."""
    from p2p_threemf.ply import read_ply
    from p2p_threemf.reader import read_3mf_full
    from p2p_threemf.retarget import candidates, fit_meshy_frame, retarget
    from p2p_threemf.writer import Part

    src = read_3mf_full(meshy_3mf, world=False)
    uncut = apply_transform(repair_figure(raw_v, raw_f).vertices, geo.transform)
    fit = fit_meshy_frame(src.vertices, geo.transform, uncut)
    palette_ids = [f.id for f in candidates(library)]
    parts = []
    for pr in res.parts:
        pv, pf = read_ply(job_dir / pr.mesh.path)
        if pr.role == "cutter":
            parts.append(Part(pv, pf, name=pr.name, subtype="negative_part"))
        else:
            parts.append(Part(pv, pf, slot=palette_ids.index(pr.filament_id) + 1, name=pr.name))
    fig = read_ply(job_dir / res.figure.mesh.path)
    # Faces the geometry stage added (hole fills, the plinth cap) take their neighbours' paint.
    fill = None
    if res.face_index_map is not None:
        fill = np.load(job_dir / res.face_index_map.path) < 0
    # The writer moves the prime tower off wide objects by itself.
    overrides = dict(SUPPORT_OVERRIDES[res.supports])
    out, _ = retarget(meshy_3mf, library, settings, out_path, palette_mode="ams",
                      config_overrides=overrides, transform=fit.matrix,
                      parts=parts, figure=fig, figure_fill=fill,
                      extra_sidecar={"frame_fit": {"axis_ratio_spread": fit.axis_ratio_spread,
                                                   "median_gap_mm": fit.median_gap_mm}})
    out = dict(out)
    out["config_overrides"] = overrides
    tower = (out.get("sidecar") or {}).get("prime_tower")
    if tower is None:
        from p2p_threemf.reader import read_3mf_full

        tower = (read_3mf_full(out_path).sidecar or {}).get("prime_tower")
    if tower and tower.get("moved"):
        out["config_overrides"] = {**overrides, "wipe_tower_x": f"{tower['x']:g}",
                                   "wipe_tower_y": f"{tower['y']:g}"}
    out["frame_fit"] = {"scale": fit.scale, "axis_ratio_spread": fit.axis_ratio_spread,
                        "median_gap_mm": fit.median_gap_mm, "p95_gap_mm": fit.p95_gap_mm}
    return out


def main() -> None:
    from p2p_threemf.slicer import slice_check
    from p2p_threemf.writer import DEFAULT_PROCESS_OVERRIDES

    ap = argparse.ArgumentParser()
    ap.add_argument("--glb", required=True)
    ap.add_argument("--meshy-3mf", required=True)
    ap.add_argument("--library", default="filaments/library.json")
    ap.add_argument("--out", default="geometry/_out")
    ap.add_argument("--reports", default=None)
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    reports = Path(a.reports).resolve() if a.reports else out
    reports.mkdir(parents=True, exist_ok=True)
    lib = FilamentLibrary.model_validate_json(Path(a.library).read_text())
    mesh = load_meshy_glb(a.glb)
    settings = PrintSettings()
    summary_path = out / "production_summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    for name, (product, addons, target) in CASES.items():
        if a.only and name not in a.only:
            continue
        t0 = time.time()
        geo = build_product_geometry(mesh.vertices, mesh.faces, product, addons, target, lib,
                                     settings)
        job = out / name / "job"
        res = write_outputs(geo, job / "s06_printability" / "a1", job_dir=job)
        png = render_product(geo, out / f"{name}.png", lib, mesh.face_colors, title=name)
        tmf = out / name / f"{name}_x2d.3mf"
        wr = production_3mf(Path(a.meshy_3mf), geo, res, job, mesh.vertices, mesh.faces, lib,
                            settings, tmf)
        rep = slice_check(tmf, settings, len(wr["slots_used"]), filament_ids=wr["palette"],
                          out_dir=out / name)
        overrides = {**DEFAULT_PROCESS_OVERRIDES, **settings.process_overrides,
                     **SUPPORT_OVERRIDES[res.supports]}
        data = json.loads(rep.model_dump_json())
        data["config"] = {
            "source": "p2p_geometry round 2, real Meshy beagle (meshy-6-lite GLB + Meshy "
                      "multicolor 3MF), threemf production writer and slicer gate",
            "product": name, "target_size_mm": target, "size_mm": [round(x, 1) for x in geo.size_mm],
            "orientation": res.orientation, "supports": res.supports,
            "process_overrides": overrides, "layer_mm": settings.layer_mm,
            "nozzle_mm": settings.nozzle_mm, "palette": wr["palette"],
            "slots_used": wr["slots_used"], "frame_fit": wr["frame_fit"],
            "parts": [{"name": p.name, "role": p.role, "filament_id": p.filament_id}
                      for p in res.parts],
        }
        rpath = reports / f"geometry_{name}__baseline__production.json"
        rpath.write_text(json.dumps(data, indent=1))
        summary[name] = {
            "ok": rep.ok, "print_time_s": rep.print_time_s, "total_grams": rep.total_grams,
            "purge_grams": rep.purge_grams, "slots_used": rep.slots_used,
            "size_mm": data["config"]["size_mm"], "supports": res.supports,
            "orientation": res.orientation, "warnings": res.warnings, "notes": res.notes,
            "errors": rep.errors, "report": str(rpath), "preview": str(png),
            "seconds": round(time.time() - t0, 1),
        }
        summary_path.write_text(json.dumps(summary, indent=2))
        print(name, json.dumps({k: summary[name][k] for k in
                                ("ok", "print_time_s", "total_grams", "purge_grams",
                                 "slots_used", "errors")}), flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
