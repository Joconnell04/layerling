"""Run the paint agent on a real job and collect the evidence in mcp_paint/_out/<job>/.

    uv run python -m p2p_mcp_paint.proof --job <id> [--iterations 8] [--sam3 local]

Copies the before and after previews (Meshy baseline paint vs the agent's paint, each as the
ideal paint and as the simulated print in filament colors), the checklist, and a summary with
iterations, time and Claude usage (list-price equivalent; billed $0 on the subscription).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import shutil
import time
from pathlib import Path

from PIL import Image

from .agent import run_paint
from .job import REPO_ROOT, jobs_root, latest_manifest, output_path


def concept_png(job_dir: Path, max_px: int = 768) -> bytes | None:
    got = latest_manifest(job_dir, "s03_concept")
    rel = output_path(got[1], "approved_concept") if got else None
    if not rel or not (job_dir / rel).is_file():
        return None
    im = Image.open(job_dir / rel).convert("RGB")
    im.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def side_by_side(before: Path, after: Path, out: Path, label_a="before", label_b="after"):
    from PIL import ImageDraw

    a, b = Image.open(before).convert("RGB"), Image.open(after).convert("RGB")
    h = max(a.height, b.height)
    im = Image.new("RGB", (a.width + b.width + 12, h + 30), (24, 25, 28))
    im.paste(a, (0, 30))
    im.paste(b, (a.width + 12, 30))
    d = ImageDraw.Draw(im)
    d.text((8, 8), label_a, fill=(230, 230, 230))
    d.text((a.width + 20, 8), label_b, fill=(230, 230, 230))
    im.save(out)
    im.convert("RGB").save(out.with_suffix(".jpg"), quality=85)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--iterations", type=int, default=8)
    ap.add_argument("--attempt", type=int, default=None)
    ap.add_argument("--jobs-dir", default=None)
    ap.add_argument("--sam3", default="local")
    ap.add_argument("--name", default=None, help="folder name under mcp_paint/_out/")
    a = ap.parse_args(argv)
    root = Path(a.jobs_dir) if a.jobs_dir else jobs_root()
    t0 = time.time()

    def log(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    run = asyncio.run(run_paint(a.job, jobs_dir=root, attempt=a.attempt,
                                max_iterations=a.iterations, sam3_backend=a.sam3,
                                concept_png=concept_png(root / a.job), log=log))
    out = REPO_ROOT / "mcp_paint" / "_out" / (a.name or a.job[:8])
    out.mkdir(parents=True, exist_ok=True)
    pv = run.dir / "previews"
    for p in pv.glob("*.png"):
        shutil.copy(p, out / p.name)
    for b in sorted(pv.glob("before_*.png")):
        a = pv / b.name.replace("before_", "after_", 1)
        if a.is_file():
            side_by_side(b, a, out / b.name.replace("before_", "compare_", 1),
                         "before: Meshy baseline", f"after: paint agent, {run.iterations} "
                         "iterations")
    for name in ("checklist.json", "checklist_baseline.json", "paint_run.json",
                 "slice_report.json", "manifest.json"):
        if (run.dir / name).is_file():
            shutil.copy(run.dir / name, out / name)
    usage = {
        "turns": len(run.model_calls),
        "input_tokens": sum(c.input_tokens for c in run.model_calls),
        "output_tokens": sum(c.output_tokens for c in run.model_calls),
        "cache_read_tokens": sum(c.cache_read_tokens for c in run.model_calls),
        "cache_write_tokens": sum(c.cache_write_tokens for c in run.model_calls),
        "usd_list": round(sum(c.usd_list for c in run.model_calls), 4),
        "usd_billed": 0.0, "billing": "subscription (Claude Code login)",
    }
    summary = {"job_id": run.job_id, "attempt": run.attempt, "iterations": run.iterations,
               "wall_s": round(time.time() - t0, 1), "confidence": run.confidence,
               "review": run.review, "review_reason": run.review_reason,
               "slice_ok": run.slice_ok, "threemf": run.threemf, "usage": usage,
               "baseline_sim": run.baseline, "final_sim": run.final_sim,
               "signoff": run.signoff, "swaps": run.swaps,
               "checklist": [{"id": i.id, "passed": i.passed, "evidence": i.evidence}
                             for i in run.checklist.items]}
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    log(f"evidence in {out}")
    print(json.dumps({k: summary[k] for k in ("iterations", "wall_s", "confidence", "review",
                                              "slice_ok")} | {"usd_list": usage["usd_list"]}))


if __name__ == "__main__":
    main()
