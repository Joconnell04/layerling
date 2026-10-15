"""CLI.

  uv run python -m p2p_filaments swatch-card --out filaments/swatch_card.3mf
  uv run python -m p2p_filaments calibrate photo.jpg [--checker classic24]
  uv run python -m p2p_filaments demo-photo /tmp/demo.jpg     # synthetic photo with known answers
  uv run python -m p2p_filaments catalog                      # filaments/catalog.json
  uv run python -m p2p_filaments seed-catalog [--dry-run]     # catalog -> Supabase shared rows
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .library import DEFAULT_LIBRARY, REPO


def cmd_swatch_card(args: argparse.Namespace) -> int:
    import cv2
    import numpy as np

    from .card import card_settings, plan_card, write_card
    from .colour import hex_to_lab, lab_to_linear
    from .library import ams_palette, load
    from .render import CardColours, render_card, to_display

    palette = ams_palette(load(args.library))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    res = write_card(palette, out)
    plan = plan_card(palette)
    lin = lab_to_linear(np.array([hex_to_lab(f.hex) for f in palette]))
    colours = CardColours.from_model(lin, plan.black_slot, plan.white_slot,
                                     [plan.number_slot(i) for i in range(len(palette))],
                                     np.full((len(palette), 3), 0.3))
    preview = out.with_suffix(".png")
    cv2.imwrite(str(preview), to_display(render_card(plan.layout, colours, 12.0)))
    print(f"wrote {out} ({res['size_mm'][0]:g} x {res['size_mm'][1]:g} mm, "
          f"{len(res['parts'])} parts, slots {res['slots_used']}), preview {preview}")
    if args.no_check:
        return 0
    from p2p_threemf import slice_check

    report = slice_check(out, card_settings(len(palette)), len(palette),
                         filament_ids=[f.id for f in palette], out_dir=Path(args.report).parent)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(report.model_dump_json(indent=1) + "\n")
    grams = ", ".join(f"{f.slot}: {f.grams:g} g" for f in report.filaments)
    print(f"slicer gate {'passed' if report.ok else 'FAILED'}: "
          f"{(report.print_time_s or 0) / 60:.0f} min, {report.total_grams} g ({grams}), "
          f"purge {report.purge_grams} g. Report {args.report}")
    for e in report.errors:
        print(f"  error: {e}")
    return 0 if report.ok else 1


def _points(text):
    from .calibrate import parse_points

    return None if text is None else parse_points(text)


def cmd_calibrate(args: argparse.Namespace) -> int:
    from .calibrate import CalibrationError, run

    try:
        rep = run(args.photo, library=args.library, card=args.card, checker=args.checker,
                  checker_points=_points(args.checker_corners),
                  card_points=_points(args.card_corners), out_dir=args.out_dir,
                  write=not args.dry_run, threshold=args.td_threshold)
    except CalibrationError as e:
        print(f"calibrate: {e}", file=sys.stderr)
        return 2
    c = rep["checker"]
    print(f"checker {c['name']} ({c['method']}, {c['order']}): fit dE2000 mean "
          f"{c['fit_de2000_mean']} max {c['fit_de2000_max']}, leave-one-out mean "
          f"{c['loo_de2000_mean']}")
    print(f"card {rep['card']['method']} markers {rep['card']['markers']} "
          f"{rep['card']['px_per_mm']} px/mm")
    print(f"{'slot':>4}  {'filament':<24} {'before':>22} {'measured':>22} {'dE':>5} {'td_mm':>6}")
    for f in rep["filaments"]:
        before = " ".join(f"{x:6.1f}" for x in f["lab_before"]) if f["lab_before"] else "-"
        meas = " ".join(f"{x:6.1f}" for x in f["lab_measured"])
        print(f"{f['ams_slot']:>4}  {f['filament_id']:<24} {before:>22} {meas:>22} "
              f"{f['de2000_vs_before'] or 0:5.1f} {f['td']['td_mm']:6.2f}")
    lib = rep["library"]
    print(f"library {'updated' if lib['written'] else 'not changed'}: {lib['path']}"
          + (f" ({lib['reason']})" if lib["reason"] else ""))
    for w in rep["warnings"]:
        print(f"  warning: {w}")
    print(f"report {rep['report']}\noverlay {rep['overlay']}")
    return 0


def cmd_demo_photo(args: argparse.Namespace) -> int:
    import cv2

    from .synthetic import scene

    sc = scene(seed=args.seed)
    cv2.imwrite(args.out, sc.photo.bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])

    def pts(a):
        return " ".join(f"{x:.0f},{y:.0f}" for x, y in a)

    print(f"wrote {args.out}")
    for i, (lab, td) in enumerate(zip(sc.true_lab, sc.true_td, strict=True)):
        print(f"slot {i + 1}: lab {json.dumps(lab.round(2).tolist())} td_mm {td:.2f}")
    print(f'--card-corners "{pts(sc.photo.card_corners)}"')
    print(f'--checker-corners "{pts(sc.photo.checker_centres[[0, 5, 23, 18]])}"')
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    from .catalog import owned_ids_from, write_catalog

    owned = owned_ids_from(Path(args.owned)) | set(args.exclude_id or [])
    rep = write_catalog(Path(args.out), owned=owned)
    print(f"{'wrote' if rep['changed'] else 'unchanged'} {rep['path']}: {rep['total']} filaments")
    for line, n in rep["per_line"].items():
        print(f"  {line:<22} {n:>3}")
    print("skipped: " + ", ".join(f"{k} {v}" for k, v in rep["skipped"].items()))
    if rep["owned_ids_skipped"]:
        print("owned ids kept out: " + ", ".join(rep["owned_ids_skipped"]))
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from .catalog import load_catalog, owned_ids_from
    from .seed import SeedError, client_for, credentials, seed

    catalog = load_catalog(Path(args.catalog))
    protect = owned_ids_from(Path(args.owned))
    try:
        url, key = credentials()
        with client_for(url, key) as client:
            rep = seed(catalog, client, protect=protect, dry_run=args.dry_run)
    except SeedError as e:
        print(f"seed: {e}", file=sys.stderr)
        return 2
    print(f"catalog {rep['catalog']}, rows before {rep['existing_rows']} "
          f"(owned {rep['owned_rows']}), already present {rep['already_present']}, "
          f"{'would insert' if rep['dry_run'] else 'inserted'} "
          f"{rep['to_insert'] if rep['dry_run'] else rep['inserted']}, "
          f"catalog rows in table {rep['catalog_rows_in_table']}, "
          f"owned rows unchanged {rep['owned_unchanged']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="p2p_filaments")
    sub = p.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("catalog", help="build filaments/catalog.json from the vendored Bambu data")
    k.add_argument("--out", default=str(REPO / "filaments" / "catalog.json"))
    k.add_argument("--owned", default=str(DEFAULT_LIBRARY),
                   help="library whose owned ids the catalog must not reuse")
    k.add_argument("--exclude-id", action="append", help="another id to keep out (repeatable)")
    k.set_defaults(func=cmd_catalog)

    sd = sub.add_parser("seed-catalog",
                        help="insert the catalog into Supabase filaments as shared rows")
    sd.add_argument("--catalog", default=str(REPO / "filaments" / "catalog.json"))
    sd.add_argument("--owned", default=str(DEFAULT_LIBRARY),
                    help="library whose owned ids are snapshotted and checked unchanged")
    sd.add_argument("--dry-run", action="store_true", help="count only, insert nothing")
    sd.set_defaults(func=cmd_seed)

    s = sub.add_parser("swatch-card", help="write the swatch card 3MF and run the slicer gate")
    s.add_argument("--out", default=str(REPO / "filaments" / "swatch_card.3mf"))
    s.add_argument("--library", default=str(DEFAULT_LIBRARY))
    s.add_argument("--report", default=str(REPO / "data" / "slice_reports" / "swatch_card.json"))
    s.add_argument("--no-check", action="store_true", help="skip the slicer gate")
    s.set_defaults(func=cmd_swatch_card)

    c = sub.add_parser("calibrate", help="measure the printed card from a photo")
    c.add_argument("photo")
    c.add_argument("--checker", default="classic24", choices=["classic24", "classic24-2005"])
    c.add_argument("--library", default=str(DEFAULT_LIBRARY))
    c.add_argument("--card", default=str(REPO / "filaments" / "swatch_card.3mf"),
                   help="card 3MF that was printed (column to filament map)")
    c.add_argument("--checker-corners", metavar="'x,y x,y x,y x,y'",
                   help="centres of patches 1 (dark skin), 6 (bluish green), 24 (black), "
                        "19 (white), in photo pixels")
    c.add_argument("--card-corners", metavar="'x,y x,y x,y x,y'",
                   help="card corners top left, top right, bottom right, bottom left "
                        "(tiles at the top), in photo pixels")
    c.add_argument("--out-dir", help="report folder (default filaments/calibration/<photo>)")
    c.add_argument("--dry-run", action="store_true", help="report only, leave library.json")
    c.add_argument("--td-threshold", type=float, default=2.0,
                   help="dE2000 at which a layer counts as hiding its base")
    c.set_defaults(func=cmd_calibrate)

    d = sub.add_parser("demo-photo", help="synthetic photo of the card and checker")
    d.add_argument("out")
    d.add_argument("--seed", type=int, default=0)
    d.set_defaults(func=cmd_demo_photo)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
