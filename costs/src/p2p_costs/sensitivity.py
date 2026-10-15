"""Which ASSUMED inputs move the own-site price most.

    uv run python -m p2p_costs.sensitivity [--workbook costs/cost_model.xlsx] [--top 10]

Reads the workbook (Inputs, Test data, Market, overrides ignored), then moves each ASSUMED
numeric input down and up by half (flags and counts are skipped) and reports the largest change
in the own-site target-margin price, before rounding, across the web products.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from openpyxl import load_workbook

from .defaults import ASSUMED, PRODUCT_LABELS, WEB_CHANNEL, WEB_PRODUCTS
from .model import compute
from .workbook import INPUTS, MARKET, TEST, read_inputs, read_market, read_test_rows

SKIP_UNITS = {"flag", "choice", "count", "units", "USD"}
# Moves tried per input: half and one and a half times, except the margin (plus or minus 10 points).
MARGIN_STEPS = (-0.1, 0.1)


def load(path: Path) -> tuple[dict[str, dict], list[dict], list[dict]]:
    wbv = load_workbook(path, data_only=True)
    wbf = load_workbook(path)
    inputs = read_inputs(wbf[INPUTS], wbv[INPUTS])
    return inputs, read_test_rows(wbv[TEST]), read_market(wbv[MARKET])


def ranking(inputs: dict[str, dict], tests: list[dict], market: list[dict]) -> list[tuple]:
    values = {k: v["value"] for k, v in inputs.items()}
    base = compute(values, tests, market)

    def targets(res):
        return {p: res.prices[(p, WEB_CHANNEL)]["target"] for p in WEB_PRODUCTS}

    b = targets(base)
    out = []
    for k, meta in inputs.items():
        v = meta["value"]
        if meta.get("provenance") != ASSUMED or not isinstance(v, (int, float)) or v == 0:
            continue
        if (meta.get("unit") or "") in SKIP_UNITS and not k.endswith("price_ending"):
            continue
        if k.endswith("price_ending"):
            continue
        worst = (0.0, "", 0.0, 0.0)
        steps = MARGIN_STEPS if k == "target_margin" else (0.5, 1.5)
        for f in steps:
            vals = dict(values)
            vals[k] = v + f if k == "target_margin" else v * f
            t = targets(compute(vals, tests, market))
            for p in WEB_PRODUCTS:
                d = t[p] - b[p]
                if abs(d) > abs(worst[0]):
                    worst = (d, p, f, t[p])
        if worst[1]:
            out.append((abs(worst[0]), k, meta.get("label"), v, worst[1], worst[2], worst[0]))
    out.sort(reverse=True)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workbook", type=Path,
                    default=Path(__file__).resolve().parents[2] / "cost_model.xlsx")
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args(argv)
    rows = ranking(*load(a.workbook))
    print(f"{'input':<34}{'value':>9}  {'largest move (x0.5 or x1.5)':<40}")
    for _, k, _label, v, p, f, d in rows[: a.top]:
        how = f"{f:+.0%} points" if k == "target_margin" else f"x{f}"
        print(f"{k:<34}{v:>9.4g}  {PRODUCT_LABELS[p]} {d:+.2f} at {how}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
