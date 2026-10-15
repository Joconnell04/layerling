"""CLI: demo object, slicer gate, reader summary, snapshot refresh.

  uv run python -m p2p_threemf demo threemf/_out/demo_4color.3mf
  uv run python -m p2p_threemf check threemf/_out/demo_4color.3mf --expected-slots 4
  uv run python -m p2p_threemf read some_project.3mf
  uv run python -m p2p_threemf verify threemf/_out/demo_4color.3mf
  uv run python -m p2p_threemf retarget meshy_multicolor.3mf out_x2d.3mf
  uv run python -m p2p_threemf refresh-profiles --reference bambu_saved_x2d_project.3mf
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from p2p_contracts import PrintSettings


def _settings_from_file(path: Path, slicer: str) -> PrintSettings:
    """PrintSettings matching a project file's embedded presets (for `check`)."""
    import zipfile

    try:
        with zipfile.ZipFile(path) as z:
            cfg = json.loads(z.read("Metadata/project_settings.config"))
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return PrintSettings(slicer=slicer)
    printer = cfg.get("printer_model") or "Bambu Lab X2D"
    nozzle = float((cfg.get("nozzle_diameter") or ["0.4"])[0])
    layer = float(cfg.get("layer_height") or 0.2)
    n = len(cfg.get("filament_colour") or [])
    return PrintSettings(printer=printer, nozzle_mm=nozzle, layer_mm=layer,
                         ams_slots=min(16, max(4, n)), slicer=slicer)


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import demo_mesh, demo_settings, jackson_palette
    from .products import PRODUCTS
    from .writer import write_3mf

    palette = jackson_palette()
    if args.product == "capsule":
        slots = [int(s) for s in args.slots.split(",")]
        v, f, s = demo_mesh(slots)
        parts, name, expected = None, args.name, len(set(slots))
    else:
        prod = PRODUCTS[args.product]()
        v, f, s, parts = prod.vertices, prod.faces, prod.face_slots, prod.parts
        name, expected = prod.name, prod.expected_slots
    res = write_3mf(v, f, s, palette, demo_settings(len(palette)), args.out,
                    object_name=name, parts=parts)
    res["expected_slots"] = expected
    print(json.dumps(res, indent=1))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from .reader import read_3mf_full
    from .slicer import slice_check

    path = Path(args.file)
    settings = _settings_from_file(path, args.slicer)
    fids = None
    try:
        side = read_3mf_full(path).sidecar
        if side:
            fids = [p["filament_id"] for p in side["palette"]]
    except (ValueError, KeyError):
        pass
    report = slice_check(path, settings, args.expected_slots, filament_ids=fids,
                         keep_gcode=args.keep_gcode, timeout_s=args.timeout,
                         out_dir=args.out_dir)
    text = report.model_dump_json(indent=1)
    print(text)
    if args.report:
        Path(args.report).write_text(text + "\n")
    return 0 if report.ok else 1


def cmd_read(args: argparse.Namespace) -> int:
    from .reader import read_3mf_full

    r = read_3mf_full(args.file, world=args.world)
    counts = Counter(r.face_slots.tolist())
    out = {
        "application": r.application,
        "printer": r.printer_settings_id,
        "process": r.print_settings_id,
        "filaments": [{"slot": i + 1, "colour": c,
                       "preset": (r.filament_settings_id[i] if i < len(r.filament_settings_id)
                                  else None)}
                      for i, c in enumerate(r.filament_colours)],
        "n_vertices": len(r.vertices),
        "n_faces": len(r.faces),
        "faces_per_slot": {str(k): v for k, v in sorted(counts.items())},
        "split_faces": int(r.split.sum()),
        "object_extruders": {str(k): v for k, v in r.object_extruders.items()},
        "bbox_mm": [r.vertices.min(axis=0).round(3).tolist(),
                    r.vertices.max(axis=0).round(3).tolist()],
        "warnings": r.warnings,
    }
    print(json.dumps(out, indent=1))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    import dataclasses
    import shutil
    import tempfile

    from .slicer import BambuStudioBackend
    from .verify import readback_check

    backend = BambuStudioBackend()
    if not backend.available():
        print(json.dumps({"ok": False, "errors": ["Bambu Studio is not installed"]}))
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="p2p_verify_"))
    try:
        raw = backend.load_and_save(Path(args.file), tmp)
        if not raw.ok or raw.sliced_3mf is None:
            print(json.dumps({"ok": False, "errors": raw.errors or ["Bambu CLI export failed"]}))
            return 1
        rb = readback_check(args.file, raw.sliced_3mf)
        out = dataclasses.asdict(rb)
        out["bambu_version"] = backend.version()
        out["bambu_log_warnings"] = raw.warnings
        text = json.dumps(out, indent=1)
        print(text)
        if args.report:
            Path(args.report).write_text(text + "\n")
        return 0 if rb.ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_retarget(args: argparse.Namespace) -> int:
    from p2p_contracts.geometry import GeometryResult

    from .retarget import load_library, retarget_3mf, retarget_and_check

    library = load_library(args.library)
    settings = PrintSettings(slicer=args.slicer)
    geometry = None
    job_dir = Path(args.job_dir) if args.job_dir else Path(args.input).parent
    if args.geometry:
        gpath = Path(args.geometry)
        geometry = GeometryResult.model_validate_json(gpath.read_text())
        if not args.job_dir:  # jobs/<id>/s06_printability/a<N>/geometry.json
            job_dir = gpath.resolve().parents[2]
    out: dict = {}
    ok = True
    if args.no_check:
        res = retarget_3mf(args.input, geometry, job_dir, library, settings, args.out,
                           palette_mode=args.palette, material_margin=args.margin,
                           config_overrides={"enable_support": "1"}
                           if args.supports == "on" else None,
                           figure_source=args.figure_source)
    else:
        res, report, enabled = retarget_and_check(
            args.input, library, settings, args.out, geometry=geometry, job_dir=job_dir,
            palette_mode=args.palette, material_margin=args.margin, supports=args.supports,
            keep_gcode=args.keep_gcode, figure_source=args.figure_source)
        out["supports_enabled"] = enabled
        out["slice_report"] = report.model_dump(mode="json")
        ok = report.ok
        if args.report:
            Path(args.report).write_text(report.model_dump_json(indent=1) + "\n")
    print(json.dumps({"write": res, **out}, indent=1))
    return 0 if ok else 1


def cmd_lab(args: argparse.Namespace) -> int:
    from .lab import FIXTURES, levers, run_lab

    variants = levers()
    if args.lever:
        variants = [v for v in variants if v.lever in args.lever or v.lever == "baseline"]
    fixtures = tuple(args.fixture) if args.fixture else FIXTURES
    run_lab(Path(args.results), Path(args.reports), variants, fixtures,
            log=lambda m: print(m, flush=True))
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    from .profiles import refresh_snapshot

    print(refresh_snapshot(args.reference))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="p2p_threemf")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="write the procedural multicolor test object")
    d.add_argument("out")
    d.add_argument("--slots", default="5,1,3,2",
                   help="base,band,disc,top slots (1-based, Jackson's palette)")
    d.add_argument("--name", default="p2p_demo_capsule")
    d.add_argument("--product", default="capsule",
                   choices=["capsule", "keychain", "nameplate", "figurine", "keychain_too_thin"],
                   help="capsule = the 4-colour demo; others are the product test fixtures")
    d.set_defaults(fn=cmd_demo)

    c = sub.add_parser("check", help="slice plate 1 and print the SliceReport JSON")
    c.add_argument("file")
    c.add_argument("--expected-slots", type=int, default=None)
    c.add_argument("--slicer", default="bambu_studio", choices=["bambu_studio", "orcaslicer"])
    c.add_argument("--keep-gcode", action="store_true")
    c.add_argument("--timeout", type=float, default=900)
    c.add_argument("--out-dir", default=None)
    c.add_argument("--report", default=None, help="also write the JSON here")
    c.set_defaults(fn=cmd_check)

    r = sub.add_parser("read", help="summarise a 3MF with the independent reader")
    r.add_argument("file")
    r.add_argument("--world", action="store_true")
    r.set_defaults(fn=cmd_read)

    v = sub.add_parser("verify", help="load and re-save with the Bambu CLI, compare what it read")
    v.add_argument("file")
    v.add_argument("--report", default=None)
    v.set_defaults(fn=cmd_verify)

    t = sub.add_parser("retarget", help="map a multicolor 3MF (Meshy) onto the AMS filaments "
                                         "and rewrite it as an X2D project, then slice-check it")
    t.add_argument("input")
    t.add_argument("out")
    t.add_argument("--library", default="filaments/library.json")
    t.add_argument("--palette", default="ams", choices=["ams", "used"])
    t.add_argument("--margin", type=float, default=10.0,
                   help="Delta E 2000 a non-PLA filament must win by")
    t.add_argument("--slicer", default="bambu_studio", choices=["bambu_studio", "orcaslicer"])
    t.add_argument("--supports", default="auto", choices=["auto", "on", "off"],
                   help="auto: enable supports when the slicer reports floating regions")
    t.add_argument("--geometry", default=None, help="s06 geometry.json (GeometryResult)")
    t.add_argument("--job-dir", default=None, help="job folder the geometry paths are relative to")
    t.add_argument("--figure-source", default="meshy", choices=["meshy", "geometry"])
    t.add_argument("--no-check", action="store_true")
    t.add_argument("--keep-gcode", action="store_true")
    t.add_argument("--report", default=None, help="also write the SliceReport JSON here")
    t.set_defaults(fn=cmd_retarget)

    lb = sub.add_parser("lab", help="slice fixtures under purge and time levers")
    lb.add_argument("--results", required=True, help="JSONL, appended; reruns resume")
    lb.add_argument("--reports", required=True, help="folder for the SliceReport JSON files")
    lb.add_argument("--lever", action="append", help="only these levers (plus baseline)")
    lb.add_argument("--fixture", action="append", help="only these fixtures")
    lb.set_defaults(fn=cmd_lab)

    f = sub.add_parser("refresh-profiles", help="rebuild data/bbl_profiles.json")
    f.add_argument("--reference", required=True,
                   help="a Bambu-saved X2D project (.3mf or project_settings.config), 3+ filaments")
    f.set_defaults(fn=cmd_refresh)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
