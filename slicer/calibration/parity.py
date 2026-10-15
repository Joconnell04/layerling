"""Parity of the native slicer against Bambu Studio 02.08.02.61 on the calibration cases.

    uv run python slicer/calibration/parity.py                 # all cases, 4 workers
    uv run python slicer/calibration/parity.py --only keychain_4c_020,figurine_4c_020  # gitleaks:allow (case id, not a secret)
    uv run python slicer/calibration/parity.py --workers 1

For every case in cases.json: slice the input with p2p_slicer.api.slice_project, compare time,
grams per filament, purge, tower, flush, change counts, layer count, slots and maps with Bambu's
result.json, slice_info and G-code (kept in _truth/, see p2p_slicer.compare.find_truth_dir), and
write parity.json (every number) and parity.md (the tables). Prints one line per case.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
MAIN = Path(os.environ.get("P2P_MAIN_CHECKOUT", str(REPO)))


def resolve_input(rel: str) -> Path:
    p = Path(rel)
    for root in (REPO, MAIN):
        if (root / p).exists():
            return root / p
    return REPO / p


def run_case(case: dict) -> dict:
    from p2p_slicer.api import slice_project
    from p2p_slicer.compare import bambu_reference, compare, find_truth_dir
    truth_dir = find_truth_dir(REPO)
    out: dict = {"case": case["id"], "product": case.get("product")}
    try:
        t0 = time.perf_counter()
        res = slice_project(resolve_input(case["input"]),
                            expected_slots=case.get("expected_slots"))
        out["wall_s"] = round(time.perf_counter() - t0, 3)
        out["timings"] = {k: round(v, 3) for k, v in res.timings.items()}
        out["faces"] = len(res.moves.meta.get("faces", [])) or None
        if truth_dir is None or not (truth_dir / case["id"]).exists():
            out["error"] = "no Bambu reference"
            return out
        ref = bambu_reference(truth_dir / case["id"])
        cmp = compare(res.analysis, ref)
        out["rows"] = cmp["rows"]
        out["summary"] = cmp["summary"]
        rep = res.report
        out["report"] = json.loads(rep.model_dump_json())
    except Exception as e:  # noqa: BLE001  (one broken case must not stop the table)
        out["error"] = f"{type(e).__name__}: {e}"
        out["traceback"] = traceback.format_exc()[-2000:]
    return out


def _row(rows: list[dict], q: str) -> dict | None:
    return next((r for r in rows if r["quantity"] == q), None)


def summarize(results: list[dict]) -> tuple[str, dict]:
    lines = []
    head = ("| Case | Time ours min | Time Bambu min | Time err % | Grams ours | Grams Bambu | "
            "Grams err % | Purge err g | Tower err g | Flush err g | Changes | AMS swaps | "
            "Nozzle switches | Layers | Slots | Slice s |")
    lines.append(head)
    lines.append("|" + "---|" * 16)
    t_err, g_err = [], []
    slots_ok = 0
    for r in results:
        if "rows" not in r:
            lines.append(f"| {r['case']} | error: {r.get('error', '')[:80]} |" + " |" * 14)
            continue
        rows = r["rows"]
        t = _row(rows, "time total s")
        g = _row(rows, "grams total")
        pu = _row(rows, "grams purge")
        tw = _row(rows, "grams tower")
        fl = _row(rows, "grams flush")

        def pair(q: str, rows: list[dict] = rows) -> str:
            x = _row(rows, q)
            if not x:
                return ""
            return f"{x['ours']} / {x['bambu']}"
        sl = _row(rows, "slots used")
        ok = sl is not None and sl["ours"] == sl["bambu"]
        slots_ok += int(ok)
        t_err.append(t["pct"])
        g_err.append(g["pct"])
        lines.append(
            f"| {r['case']} | {t['ours'] / 60:.1f} | {t['bambu'] / 60:.1f} | {t['pct']:+.2f} | "
            f"{g['ours']:.2f} | {g['bambu']:.2f} | {g['pct']:+.2f} | {pu['diff']:+.2f} | "
            f"{tw['diff']:+.2f} | {fl['diff']:+.2f} | {pair('filament changes')} | "
            f"{pair('ams swaps')} | {pair('nozzle switches')} | {pair('layers')} | "
            f"{'same' if ok else sl['ours'] + ' vs ' + sl['bambu']} | {r['wall_s']:.1f} |")
    stats = {}
    if t_err:
        at = [abs(x) for x in t_err]
        ag = [abs(x) for x in g_err]
        stats = {"cases": len(t_err), "time_abs_pct_median": statistics.median(at),
                 "time_abs_pct_worst": max(at), "grams_abs_pct_median": statistics.median(ag),
                 "grams_abs_pct_worst": max(ag), "slots_equal": slots_ok,
                 "time_within_5pct": sum(x <= 5 for x in at),
                 "grams_within_3pct": sum(x <= 3 for x in ag)}
    return "\n".join(lines), stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(HERE))
    ap.add_argument("--render", default="", help="rewrite parity.md from this parity.json")
    args = ap.parse_args()
    if args.render:
        data = json.loads(Path(args.render).read_text())
        table, stats = summarize(data["cases"])
        (Path(args.out) / "parity.md").write_text(render_md(table, stats))
        return 0
    cases = json.loads((HERE / "cases.json").read_text())["cases"]
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    results: list[dict] = []
    t0 = time.time()
    with ProcessPoolExecutor(max(1, args.workers)) as ex:
        futs = {ex.submit(run_case, c): c["id"] for c in cases}
        for fu in as_completed(futs):
            r = fu.result()
            results.append(r)
            if "rows" in r:
                t = _row(r["rows"], "time total s")
                g = _row(r["rows"], "grams total")
                print(f"{r['case']:26s} time {t['pct']:+7.2f}%  grams {g['pct']:+7.2f}%  "
                      f"slice {r['wall_s']:.1f}s  ({time.time() - t0:.0f}s)", flush=True)
            else:
                print(f"{r['case']:26s} ERROR {r.get('error')}", flush=True)
    order = {c["id"]: i for i, c in enumerate(cases)}
    results.sort(key=lambda r: order.get(r["case"], 0))
    table, stats = summarize(results)
    out = Path(args.out)
    (out / "parity.json").write_text(json.dumps({"stats": stats, "cases": results}, indent=1,
                                                default=str))
    (out / "parity.md").write_text(render_md(table, stats))
    print(json.dumps(stats, indent=1))
    print("done", flush=True)
    return 0


def render_md(table: str, stats: dict) -> str:
    """parity.md: the headline numbers, then one row per case."""
    s = stats
    head = [
        "# Native slicer parity with Bambu Studio 02.08.02.61 (X2D)",
        "",
        "Generated by `uv run python slicer/calibration/parity.py`. Ours: p2p_slicer sliced from",
        "the case's project 3MF. Bambu: its CLI slice kept in `_truth/` (SOURCE). Errors are",
        "ours minus Bambu; percentages relative to Bambu. Targets (FROM JACKSON): time within",
        "5 percent, total grams within 3 percent, slots used identical.",
        "",
    ]
    if s:
        head += [
            f"- Cases: {s['cases']}",
            (f"- Time error, absolute: median {s['time_abs_pct_median']:.2f} percent, worst "
             f"{s['time_abs_pct_worst']:.2f} percent; {s['time_within_5pct']} of {s['cases']} "
             "within 5 percent"),
            (f"- Grams error, absolute: median {s['grams_abs_pct_median']:.2f} percent, worst "
             f"{s['grams_abs_pct_worst']:.2f} percent; {s['grams_within_3pct']} of {s['cases']} "
             "within 3 percent"),
            f"- Slots used identical: {s['slots_equal']} of {s['cases']}",
            "",
        ]
    head += ["Change columns are ours / Bambu. Slice s is wall time with 4 cases in parallel.",
             "", table, ""]
    return "\n".join(head)


if __name__ == "__main__":
    sys.exit(main())
