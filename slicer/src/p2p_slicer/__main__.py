"""Command line: analyze G-code, compare with Bambu, slice a project, inspect a file.

    uv run python -m p2p_slicer analyze plate_1.gcode [--result out/result.json]
    uv run python -m p2p_slicer compare ours.gcode path/to/bambu_case_folder
    uv run python -m p2p_slicer slice in.3mf --out outdir [--gcode] [--compare bambu_folder]
    uv run python -m p2p_slicer inspect <file.3mf | file.gcode.3mf | file.gcode> --out <dir>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _analyze(args: argparse.Namespace) -> int:
    from .report import analyze_gcode, cli_result_json, slice_report
    t0 = time.perf_counter()
    a = analyze_gcode(args.gcode)
    dt = time.perf_counter() - t0
    rep = slice_report(a)
    print(json.dumps(rep.model_dump(exclude_none=True), indent=1))
    print(f"analyzed {len(a.moves)} rows in {dt:.2f} s", file=sys.stderr)
    if args.result:
        Path(args.result).write_text(json.dumps(cli_result_json(a), indent=4, sort_keys=True))
    return 0


def _compare(args: argparse.Namespace) -> int:
    from .compare import bambu_reference, compare, format_rows
    from .report import analyze_gcode
    ours_path = Path(args.ours)
    if ours_path.is_dir():
        cand = [ours_path / "plate_1.gcode", ours_path / "sliced.gcode.3mf"]
        ours_path = next(p for p in cand if p.exists())
    a = analyze_gcode(ours_path)
    ref = bambu_reference(args.bambu)
    out = compare(a, ref)
    print(format_rows(out["rows"]))
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1, default=str))
    return 0


def _slice(args: argparse.Namespace) -> int:
    from .api import slice_project
    t0 = time.perf_counter()
    res = slice_project(Path(args.model), out_dir=Path(args.out), gcode=True,
                        expected_slots=args.expected_slots)
    dt = time.perf_counter() - t0
    rep = res.report
    print(json.dumps(rep.model_dump(exclude_none=True), indent=1))
    print(f"sliced in {dt:.2f} s ({res.timings})", file=sys.stderr)
    if args.compare:
        from .compare import bambu_reference, compare, format_rows
        out = compare(res.analysis, bambu_reference(args.compare))
        print(format_rows(out["rows"]))
    return 0 if rep.ok else 1


def _inspect(args: argparse.Namespace) -> int:
    from .inspect import inspect_path

    t0 = time.monotonic()

    def log(msg: str) -> None:
        print(f"[{time.monotonic() - t0:6.1f}s] {msg}", flush=True)

    insp = inspect_path(args.file, args.out, project=args.project, result=args.result,
                        reference=args.reference, slice_mode=args.slice_mode,
                        timeout_s=args.timeout, log=log)
    t = insp["totals"]
    ref = t.get("reference") or {}
    parts = [f"layers {t.get('layers')}"]
    if t.get("time_s") is not None:
        parts.append(f"time {t['time_s']:.0f} s")
    if ref.get("time_s") is not None:
        parts.append(f"Bambu {ref['time_s']:.0f} s")
    if t.get("grams") is not None:
        parts.append(f"{t['grams']:.2f} g")
    parts.append(f"{sum(1 for w in insp['warnings'] if w['level'] == 'error')} errors")
    log("done: " + ", ".join(parts))
    print(Path(args.out).resolve() / "index.html")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p2p_slicer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="time, grams and counts for a G-code or .gcode.3mf")
    a.add_argument("gcode")
    a.add_argument("--result", help="write a Bambu-shaped result.json here")
    a.set_defaults(fn=_analyze)
    c = sub.add_parser("compare", help="parity table: ours vs a Bambu slice folder")
    c.add_argument("ours", help="our .gcode, .gcode.3mf or a folder with plate_1.gcode")
    c.add_argument("bambu", help="Bambu folder with result.json and plate_1.gcode or sliced.gcode.3mf")
    c.add_argument("--json")
    c.set_defaults(fn=_compare)
    s = sub.add_parser("slice", help="slice a painted project 3MF")
    s.add_argument("model")
    s.add_argument("--out", required=True)
    s.add_argument("--expected-slots", type=int, default=None)
    s.add_argument("--compare", help="Bambu folder to compare with")
    s.set_defaults(fn=_slice)
    i = sub.add_parser("inspect", help="inspection.json, renders and index.html for a file")
    i.add_argument("file", type=Path, help="project .3mf, sliced .gcode.3mf or .gcode")
    i.add_argument("--out", type=Path, required=True, help="output folder")
    i.add_argument("--project", type=Path, help="painted project 3MF for the model views")
    i.add_argument("--result", type=Path, help="Bambu result.json (default: next to the file)")
    i.add_argument("--reference", type=Path, help="Bambu .gcode.3mf to compare ours with")
    i.add_argument("--slice", dest="slice_mode", default="auto",
                   choices=("auto", "p2p", "bambu", "none"),
                   help="how to slice a project 3MF: auto = p2p_slicer, else the Bambu CLI")
    i.add_argument("--timeout", type=float, default=900.0, help="Bambu CLI timeout, seconds")
    i.set_defaults(fn=_inspect)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
