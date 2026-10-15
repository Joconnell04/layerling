"""Export backfilled jobs as trace-viewer fixtures for the web app's mock mode and tests.

    uv run python scripts/trace_fixtures.py [--jobs-dir jobs] --job <id> [--job <id>]

Writes apps/web/lib/traces/fixtures/<job_id>.json (job row fields, stage runs with redacted
manifests, trace bodies by path, spend rows from ledger.jsonl, small JSON and log outputs) and
img/<job8>/<name>.jpg (photos, concept PNGs and thumbnails, at most 512 px, JPEG quality 72).
No GLB, 3MF or PLY. Never under public/ (served on the live site). Everything passes through
p2p_worker.trace.redact; the script fails if a secret pattern survives.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from pathlib import Path

from p2p_worker.trace import redact
from PIL import Image, ImageOps

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "apps" / "web" / "lib" / "traces" / "fixtures"
DEFAULT_JOBS = ["1785a480-84b8-41f9-8f97-1005b6d7d0a6", "819cc491-c341-470a-bb97-63a194fa8dca"]
IMAGE_RE = re.compile(r"^(input/photo_\d+\.(jpe?g|png|webp)|s03_concept/a\d+/[\w-]+\.png|"
                      r"s05_mesh/a\d+/thumb_[\w-]+\.png)$")
DOC_EXT = {".json", ".log"}
DOC_MAX = 40_000
SECRET_RE = re.compile(r"Signature=|Expires=|token=|eyJ[A-Za-z0-9_-]{10,}|msy_|sk-ant|sb_secret")
CANVAS = (239, 232, 218)  # --color-canvas behind transparent renders


def trim(v, n: int = 12):
    """Long lists (thin paint regions, per-face data) cut to n items for the fixture."""
    if isinstance(v, list):
        out = [trim(x, n) for x in v[:n]]
        if len(v) > n:
            out.append(f"... {len(v) - n} more")
        return out
    if isinstance(v, dict):
        return {k: trim(x, n) for k, x in v.items()}
    return v


def jpeg(path: Path) -> bytes:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((512, 512))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, CANVAS)
            bg.paste(im, mask=im.split()[-1])
            im = bg
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=72, optimize=True, progressive=True)
        return buf.getvalue()


def export(jdir: Path) -> dict:
    job_id = jdir.name
    job = json.loads((jdir / "job.json").read_text())
    row = json.loads((jdir / "job_row.json").read_text()) if (jdir / "job_row.json").is_file() \
        else {}
    runs, bodies, docs, images = [], {}, {}, {}
    last = job["created_at"]
    for p in sorted(jdir.glob("s*/a*/manifest.json")):
        m = json.loads(p.read_text())
        last = max(last, m.get("finished_at") or m.get("started_at") or last)
        runs.append({
            "id": f"{job_id[:8]}-{m['stage']}-a{m['attempt']}", "job_id": job_id,
            "owner_id": None, "stage": m["stage"], "attempt": m["attempt"],
            "status": m["status"], "started_at": m.get("started_at"),
            "finished_at": m.get("finished_at"), "usd": m.get("usd", 0),
            "credits": m.get("credits", 0), "confidence": m.get("confidence"),
            "manifest": trim(m), "updated_at": m.get("finished_at") or m.get("started_at"),
        })
        for e in (m.get("data") or {}).get("trace") or []:
            bp = jdir / e["path"]
            if bp.is_file():
                bodies[e["path"]] = trim(json.loads(bp.read_text()), 40)
        for o in m.get("outputs") or []:
            f = jdir / o["path"]
            if f.suffix in DOC_EXT and f.is_file():
                text = f.read_text(errors="replace")
                if f.suffix == ".json":
                    text = json.dumps(trim(json.loads(text)), indent=2)
                if len(text) <= DOC_MAX:
                    docs[o["path"]] = text
    for p in sorted(jdir.rglob("*")):
        rel = p.relative_to(jdir).as_posix()
        if p.is_file() and IMAGE_RE.match(rel):
            name = f"img/{job_id[:8]}/{rel.replace('/', '__').rsplit('.', 1)[0]}.jpg"
            (OUT / name).parent.mkdir(parents=True, exist_ok=True)
            (OUT / name).write_bytes(jpeg(p))
            images[rel] = name
    spend = []
    ledger = jdir / "ledger.jsonl"
    for i, line in enumerate(ledger.read_text().splitlines() if ledger.is_file() else []):
        if line.strip():
            spend.append({"id": i + 1, "owner_id": None, **json.loads(line), "usd_list": 0})
    job_row = {
        "id": job_id, "owner_id": None, "access": job.get("access", "code"),
        "created_at": job["created_at"], "updated_at": last, "title": row.get("title"),
        "prompt": job["prompt"], "product_type": job["product_type"],
        "profile": job.get("profile", "thin_slice"), "status": row.get("status", job["status"]),
        "stage": row.get("stage", job.get("stage")),
        "target_size_mm": row.get("target_size_mm", job.get("target_size_mm")),
        "addons": job.get("addons") or [], "settings": job.get("settings") or {},
        "photos": job.get("photos") or [],
        "budget_usd": job["budget"]["cap_usd"], "budget_credits": job["budget"]["cap_credits"],
        "spent_usd": row.get("spent_usd", job["budget"]["spent_usd"]),
        "spent_credits": row.get("spent_credits", job["budget"]["spent_credits"]),
        "confidence": None, "review_reason": row.get("review_reason"),
        "pause_reason": row.get("pause_reason"), "result": row.get("result", job.get("result")),
    }
    out = redact({"job": job_row, "stage_runs": runs, "bodies": bodies, "docs": docs,
                  "images": images, "spend": spend}, [], cap=None)
    text = json.dumps(out, separators=(",", ":"), sort_keys=True)
    hit = SECRET_RE.search(text)
    if hit:
        raise SystemExit(f"{job_id}: secret pattern survived: {hit.group(0)[:12]}")
    (OUT / f"{job_id}.json").write_text(text + "\n")
    return {"runs": len(runs), "bodies": len(bodies), "docs": len(docs), "images": len(images),
            "bytes": len(text)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs-dir", default=str(REPO / "jobs"))
    ap.add_argument("--job", action="append")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    for job_id in args.job or DEFAULT_JOBS:
        if not re.fullmatch(r"[0-9a-f-]{36}", job_id):
            print(f"not a job id: {job_id}", file=sys.stderr)
            return 2
        print(job_id, export(Path(args.jobs_dir) / job_id))
    total = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"fixtures {total / 1e6:.2f} MB in {OUT.relative_to(REPO)}")
    return 0 if total < 2_000_000 else 1


if __name__ == "__main__":
    sys.exit(main())
