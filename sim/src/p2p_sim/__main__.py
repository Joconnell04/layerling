"""CLI: uv run python -m p2p_sim {render,simulate,regions,fixture,demo} ...

render    <mesh> --out DIR [--kinds flat,shaded,overlay,texture,faceid,sim] [--views ...]
simulate  <mesh> --out DIR [--slots slots.npy --palette palette.json] [--regions regions.npy]
          [--parts face_parts.npy --part-slots 1,2,4] [--render]
regions   <mesh> --regions regions.npy            (prints region geometry JSON)
fixture   --faces 50000 --out DIR                 (procedural figurine + ground-truth paint)
demo      [--out sim/_out]                        (every sample output plus timings.json)

Without --regions/--slots, textured meshes get texture-cluster regions and colors (demo labeler).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from p2p_contracts.regions import VIEWS
from p2p_contracts.reports import SimParams
from PIL import Image

from .color import lab_to_srgb
from .demo import face_id_image, load_palette, palette_json, texture_regions
from .mesh import load_mesh, save_glb
from .regions import region_geometry
from .render import render_crops, render_sheet, render_views
from .simulate import simulate_print_detailed


def _paint(mesh, args):
    """(face_regions, face_slots, palette_labs) from files or texture clusters."""
    regions = np.load(args.regions) if getattr(args, "regions", None) else None
    slots = np.load(args.slots) if getattr(args, "slots", None) else None
    labs = load_palette(args.palette) if getattr(args, "palette", None) else None
    if slots is None or labs is None:
        if mesh.texture is None and mesh.vertex_colors is None:
            if regions is None:
                regions = np.ones(mesh.n_faces, dtype=np.int32)
            return regions, None, None
        auto_regions, cl, centers = texture_regions(mesh, k=args.k)
        slots = cl + 1
        labs = centers
        if regions is None:
            regions = auto_regions
    return regions, np.asarray(slots).astype(np.int64), np.asarray(labs)


def write_renders(mesh, out: Path, kinds, views=None, colors=None, regions=None, sim=None,
                  size: int = 1024, crops=None, sheet: bool = False, name: str | None = None):
    """Render every requested kind and view into out/, with renders.json (cameras, timings)."""
    t0 = time.time()
    out.mkdir(parents=True, exist_ok=True)
    views = views or list(VIEWS)
    std = [k for k in kinds if k in ("flat", "shaded", "overlay", "texture", "sim")]
    if "texture" in std and mesh.texture is None and mesh.vertex_colors is None:
        std.remove("texture")
    if sim is None and "sim" in std:
        std.remove("sim")
    if regions is None and "overlay" in std:
        std.remove("overlay")
    meta, timings = [], {}
    for v in views:
        t = time.time()
        res = render_views(mesh, std, [v], colors, regions, size_px=size, sim=sim)
        timings[v] = {"seconds": round(time.time() - t, 3), "kinds": std}
        for r in res:
            path = out / f"{r.kind}_{v}.png"
            r.save(path)
            meta.append(r.meta(path.name))
        if "faceid" in kinds and res:
            r = res[0]
            Image.fromarray(face_id_image(r.face_id)).save(out / f"faceid_{v}.png")
            np.save(out / f"faceid_{v}.npy", r.face_id)
            np.save(out / f"depth_{v}.npy", r.depth)
            meta.append({"kind": "faceid", "view": v, "path": f"faceid_{v}.png",
                         "npy": f"faceid_{v}.npy", "depth": f"depth_{v}.npy",
                         "camera": r.camera.to_dict()})
    if crops and regions is not None:
        for r in render_crops(mesh, regions, crops, face_colors=colors, size_px=size // 2):
            path = out / f"crops_r{r.region_id}.png"
            r.save(path)
            meta.append(r.meta(path.name))
    if sheet:
        render_sheet(mesh, colors, out / "sheet.png", title=name or mesh.name)
        if sim is not None:
            render_sheet(mesh, sim.face_rgb, out / "sheet_sim.png", kind="sim", sim=sim,
                         title=f"{name or mesh.name} simulated")
    summary = {"n_faces": mesh.n_faces, "size_px": size, "per_view": timings,
               "total_s": round(time.time() - t0, 2), "images": meta}
    (out / "renders.json").write_text(json.dumps(summary, indent=1))
    return summary


def cmd_render(args) -> int:
    mesh = load_mesh(args.mesh, target_size_mm=args.size_mm)
    out = Path(args.out)
    regions, slots, labs = _paint(mesh, args)
    colors = None if slots is None else lab_to_srgb(labs)[slots - 1]
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    views = [v.strip() for v in args.views.split(",")] if args.views else None
    sim = None
    if "sim" in kinds and slots is not None:
        sim = simulate_print_detailed(mesh, slots, labs, face_regions=regions)
    crops = [int(x) for x in args.crops.split(",")] if args.crops else None
    summary = write_renders(mesh, out, kinds, views, colors, regions, sim, args.size, crops,
                            args.sheet)
    if regions is not None:
        np.save(out / "face_regions.npy", np.asarray(regions, dtype=np.int32))
    if slots is not None:
        np.save(out / "face_slots.npy", slots.astype(np.int16))
        (out / "palette.json").write_text(json.dumps(palette_json(labs), indent=1))
    print(json.dumps({k: summary[k] for k in ("n_faces", "total_s")}))
    return 0


def cmd_simulate(args) -> int:
    mesh = load_mesh(args.mesh, target_size_mm=args.size_mm)
    regions, slots, labs = _paint(mesh, args)
    if slots is None:
        print("need --slots and --palette (or a textured mesh)", file=sys.stderr)
        return 2
    params = SimParams(nozzle_mm=args.nozzle, layer_mm=args.layer,
                       min_width_factor=args.min_width_factor, flat_angle_deg=args.flat_angle,
                       min_light_layers=args.min_light_layers,
                       dark_to_light_weight=args.dark_to_light_weight)
    out = Path(args.out)
    t = time.time()
    parts = np.load(args.parts) if args.parts else None
    part_slots = ([int(x) for x in args.part_slots.split(",")] if args.part_slots else None)
    res = simulate_print_detailed(mesh, slots, labs, params, regions, out_dir=out,
                                  job_dir=args.job_dir, face_parts=parts, part_slots=part_slots)
    dt = time.time() - t
    images = []
    if args.render:
        import hashlib

        from p2p_contracts.manifest import FileRef

        p = render_sheet(mesh, res.face_rgb, out / "sim_sheet.png", kind="sim", sim=res,
                         title=f"{mesh.name} simulated")
        base = Path(args.job_dir) if args.job_dir else out
        data = p.read_bytes()
        images.append(FileRef(path=p.relative_to(base).as_posix(), bytes=len(data),
                              sha256=hashlib.sha256(data).hexdigest(), kind="png",
                              role="sim_sheet"))
        for v in ("front", "front_3q"):
            r = render_views(mesh, ["sim"], [v], sim=res, size_px=args.size)[0]
            q = r.save(out / f"sim_{v}.png")
            d = q.read_bytes()
            images.append(FileRef(path=q.relative_to(base).as_posix(), bytes=len(d),
                                  sha256=hashlib.sha256(d).hexdigest(), kind="png",
                                  role=f"sim_{v}"))
    report = res.report.model_copy(update={"images": images})
    (out / "sim_report.json").write_text(report.model_dump_json(indent=1))
    if args.regions is None and regions is not None:
        np.save(out / "face_regions.npy", np.asarray(regions, dtype=np.int32))
    print(json.dumps({"seconds": round(dt, 2), "merged": len(report.merged),
                      "bleed_flags": len(report.bleed_flags), "swaps": report.swaps_total,
                      "purge_g": report.purge_g_est, "model_g": report.model_g_est,
                      "purge_pct": report.purge_pct}))
    return 0


def cmd_regions(args) -> int:
    mesh = load_mesh(args.mesh, target_size_mm=args.size_mm)
    regions, _, _ = _paint(mesh, args)
    geo = region_geometry(mesh, regions)
    print(json.dumps({str(k): g.fields() for k, g in geo.items()}, indent=1))
    return 0


def cmd_fixture(args) -> int:
    from .fixtures import make_figurine

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fx = make_figurine(args.faces)
    save_glb(fx.mesh, out / f"{fx.mesh.name}.glb")
    np.save(out / "face_regions.npy", fx.face_regions)
    np.save(out / "face_slots.npy", fx.face_slots)
    (out / "palette.json").write_text(json.dumps(
        [{"name": n, "hex": h} for n, h in zip(fx.palette_names, fx.palette_hex)], indent=1))
    (out / "labels.json").write_text(json.dumps(fx.labels, indent=1))
    print(json.dumps({"glb": f"{fx.mesh.name}.glb", "n_faces": fx.mesh.n_faces}))
    return 0


MESHY_SAMPLE = Path(__file__).resolve().parents[3] / "data/fixtures/meshy/02_image_to_3d_lite/model.glb"


def cmd_demo(args) -> int:
    """Regenerate every sample output under --out, printing progress as it goes."""
    from .fixtures import make_figurine
    from .products import keychain, multipart_figure, nameplate

    out = Path(args.out)
    kinds = ["flat", "shaded", "overlay", "texture", "faceid", "sim"]
    timings = {}

    def run(name, mesh, colors, regions, slots, labs, crops, parts=None, part_slots=None,
            views=None):
        t = time.time()
        sim = simulate_print_detailed(mesh, slots, labs, face_regions=regions, out_dir=out / name,
                                      face_parts=parts, part_slots=part_slots)
        sim_s = time.time() - t
        (out / name / "sim_report.json").write_text(sim.report.model_dump_json(indent=1))
        summary = write_renders(mesh, out / name, kinds, views, colors, regions, sim, args.size,
                                crops, sheet=True, name=name)
        geo_t = time.time()
        geo = region_geometry(mesh, regions)
        geo_s = time.time() - geo_t
        (out / name / "regions.json").write_text(
            json.dumps({str(k): g.fields() for k, g in geo.items()}, indent=1))
        timings[name] = {"n_faces": mesh.n_faces, "simulate_s": round(sim_s, 2),
                         "region_geometry_s": round(geo_s, 2),
                         "render_s_per_view": {v: t["seconds"]
                                               for v, t in summary["per_view"].items()},
                         "kinds_per_view": kinds}
        (out / "timings.json").write_text(json.dumps(timings, indent=1))
        print(json.dumps({name: timings[name]["simulate_s"], "faces": mesh.n_faces}), flush=True)

    for n in (50_000, 200_000):
        fx = make_figurine(n)
        run(f"figurine_{n // 1000}k", fx.mesh, fx.face_colors, fx.face_regions, fx.face_slots,
            fx.palette_labs, [9, 6, 23])
    for p, views in ((nameplate("tops"), ["top", "front_3q"]),
                     (nameplate("parts", text_height=0.6), ["top", "front_3q"]),
                     (keychain(), ["top", "front_3q"]),
                     (multipart_figure(), None)):
        crops = [p.region_id(x) for x in ("text_05mm", "eye_left", "nose", "loop")
                 if x in p.labels.values()]
        run(p.name, p.mesh, p.face_colors, p.face_regions, p.face_slots, p.palette_labs, crops,
            p.face_parts, p.part_slots, views)
    if MESHY_SAMPLE.is_file():
        m = load_mesh(MESHY_SAMPLE, target_size_mm=60)
        regions, cl, centers = texture_regions(m, k=5)
        run("meshy_beagle", m, lab_to_srgb(centers)[cl], regions, cl + 1, centers, [1, 2])
    print(json.dumps({"done": str(out / "timings.json")}), flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="p2p_sim")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("mesh")
        p.add_argument("--size-mm", type=float, default=None,
                       help="GLB only: scale so the largest extent is this many mm")
        p.add_argument("--regions", default=None, help="face_regions.npy")
        p.add_argument("--slots", default=None, help="face slots .npy (palette index + 1)")
        p.add_argument("--palette", default=None, help="palette JSON")
        p.add_argument("--k", type=int, default=5, help="texture clusters when auto-painting")

    r = sub.add_parser("render")
    common(r)
    r.add_argument("--out", required=True)
    r.add_argument("--kinds", default="flat,shaded,overlay,texture")
    r.add_argument("--views", default=None)
    r.add_argument("--size", type=int, default=1024)
    r.add_argument("--crops", default=None, help="comma-separated region ids")
    r.add_argument("--sheet", action="store_true")
    r.set_defaults(fn=cmd_render)

    s = sub.add_parser("simulate")
    common(s)
    s.add_argument("--out", required=True)
    s.add_argument("--job-dir", default=None)
    s.add_argument("--render", action="store_true")
    s.add_argument("--parts", default=None, help="face_parts.npy (part index per face)")
    s.add_argument("--part-slots", default=None, help="slot per part, e.g. 1,2,4")
    s.add_argument("--size", type=int, default=1024)
    d = SimParams()
    s.add_argument("--nozzle", type=float, default=d.nozzle_mm)
    s.add_argument("--layer", type=float, default=d.layer_mm)
    s.add_argument("--min-width-factor", type=float, default=d.min_width_factor)
    s.add_argument("--flat-angle", type=float, default=d.flat_angle_deg)
    s.add_argument("--min-light-layers", type=int, default=d.min_light_layers)
    s.add_argument("--dark-to-light-weight", type=float, default=d.dark_to_light_weight)
    s.set_defaults(fn=cmd_simulate)

    g = sub.add_parser("regions")
    common(g)
    g.set_defaults(fn=cmd_regions)

    f = sub.add_parser("fixture")
    f.add_argument("--faces", type=int, default=50_000)
    f.add_argument("--out", required=True)
    f.set_defaults(fn=cmd_fixture)

    dm = sub.add_parser("demo")
    dm.add_argument("--out", default="sim/_out")
    dm.add_argument("--size", type=int, default=1024)
    dm.set_defaults(fn=cmd_demo)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
