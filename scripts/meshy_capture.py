"""Record real Meshy responses for fixtures: concept -> lite mesh -> analyze -> multi-color 3MF.

Spends about 28 credits (nano-banana 3, meshy-6-lite textured 15, multi-color 10; analyze free).
Waits on the meshy-webhook edge function (rows in public.meshy_events), never polls Meshy.
One GET per task after its terminal webhook, to record the canonical task object.

Run: uv run --with httpx python scripts/meshy_capture.py data/fixtures/photos/beagle_puppy.jpg
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/fixtures/meshy"
LEDGER = ROOT / "data/ledger.jsonl"
API = "https://api.meshy.ai/openapi"
PLANNED = {"image-to-image": 3, "image-to-3d": 15, "print/analyze": 0, "print/multi-color": 10}


def load_env() -> dict[str, str]:
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.split("#", 1)[0].strip() if k.strip() == "MESHY_MODE" else v.strip()
    return env


ENV = load_env()
MESHY = {"Authorization": f"Bearer {ENV['MESHY_API_KEY']}"}
SB = {"apikey": ENV["SUPABASE_SERVICE_ROLE_KEY"], "Authorization": f"Bearer {ENV['SUPABASE_SERVICE_ROLE_KEY']}"}
SB_URL = ENV["SUPABASE_URL"]


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def spent_credits() -> int:
    if not LEDGER.exists():
        return 0
    return sum(json.loads(l).get("credits", 0) for l in LEDGER.read_text().splitlines() if l.strip())


def record_spend(kind: str, credits: int, task_id: str) -> None:
    ev = {"at": datetime.now(UTC).isoformat(), "vendor": "meshy", "kind": kind, "credits": credits,
          "usd": round(credits * 0.02, 4), "ref": task_id, "test_mode": False}
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps(ev) + "\n")
    httpx.post(f"{SB_URL}/rest/v1/spend", headers={**SB, "Prefer": "return=minimal"},
               json={k: ev[k] for k in ("vendor", "kind", "credits", "usd", "ref", "test_mode")},
               timeout=30).raise_for_status()


def strip(body: dict) -> dict:
    def s(v):
        if isinstance(v, str) and v.startswith("data:"):
            return f"<data-uri sha256={hashlib.sha256(v.encode()).hexdigest()[:16]}>"
        if isinstance(v, list):
            return [s(x) for x in v]
        return v
    return {k: s(v) for k, v in body.items()}


def create(path: str, body: dict, name: str) -> str:
    cap = int(ENV.get("MESHY_BUILD_CREDIT_CAP", "500"))
    if spent_credits() + PLANNED[path] > cap:
        sys.exit(f"credit guard: {spent_credits()} spent + {PLANNED[path]} > cap {cap}")
    r = httpx.post(f"{API}/v1/{path}", headers=MESHY, json=body, timeout=60)
    d = OUT / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "request.json").write_text(json.dumps({"path": f"/openapi/v1/{path}", "body": strip(body)}, indent=2))
    (d / "create_response.json").write_text(json.dumps({"status": r.status_code, "body": r.json()}, indent=2))
    r.raise_for_status()
    task_id = r.json()["result"]
    log(f"{name}: created {task_id} ({r.status_code})")
    return task_id


def wait_webhook(task_id: str, name: str, timeout_s: int = 900) -> list[dict]:
    t0, events = time.time(), []
    while time.time() - t0 < timeout_s:
        r = httpx.get(f"{SB_URL}/rest/v1/meshy_events", headers=SB, timeout=30,
                      params={"task_id": f"eq.{task_id}", "order": "id.asc", "select": "id,received_at,status,event"})
        events = r.json()
        statuses = [e["status"] for e in events]
        if any(s in ("SUCCEEDED", "FAILED", "CANCELED") for s in statuses):
            break
        time.sleep(4)
    (OUT / name / "webhook_events.json").write_text(json.dumps(events, indent=2))
    log(f"{name}: {len(events)} webhook events, statuses {[e['status'] for e in events]}")
    return events


def get_task(path: str, task_id: str, name: str) -> dict:
    r = httpx.get(f"{API}/v1/{path}/{task_id}", headers=MESHY, timeout=60)
    r.raise_for_status()
    task = r.json()
    (OUT / name / "task.json").write_text(json.dumps(task, indent=2))
    return task


def download(url: str, dest: Path) -> None:
    with httpx.stream("GET", url, timeout=300, follow_redirects=True) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    log(f"downloaded {dest.relative_to(ROOT)} ({dest.stat().st_size} bytes)")


def run_step(path: str, body: dict, name: str) -> dict:
    tid = create(path, body, name)
    events = wait_webhook(tid, name)
    task = get_task(path, tid, name)
    credits = int(task.get("consumed_credits") or 0)
    record_spend(path, credits, tid)
    log(f"{name}: status {task.get('status')}, consumed_credits {credits}, webhook_seen {bool(events)}")
    if task.get("status") != "SUCCEEDED":
        sys.exit(f"{name} did not succeed: {task.get('task_error')}")
    return task


def main(photo: Path) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    bal = httpx.get(f"{API}/v1/balance", headers=MESHY, timeout=30).json()
    log(f"balance before: {bal}")
    data_uri = "data:image/jpeg;base64," + base64.b64encode(photo.read_bytes()).decode()

    concept = run_step("image-to-image", {
        "ai_model": "nano-banana",
        "prompt": ("Chunky sculpted collectible figurine of this beagle puppy sitting, one connected solid "
                   "sculpture on a small round base, no loose whiskers, no thin parts, no gaps, flat even "
                   "lighting, neutral background, three-quarter hero view. Clear painted color regions: "
                   "tan brown head and ears, white blaze, muzzle and chest, black saddle on the back, "
                   "black nose, dark brown eyes."),
        "reference_image_urls": [data_uri],
        "remove_background": True,
        "aspect_ratio": "1:1",
    }, "01_image_to_image")
    img_url = concept["image_urls"][0]
    download(img_url, OUT / "01_image_to_image" / "concept.png")

    mesh = run_step("image-to-3d", {
        "image_url": img_url,
        "ai_model": "meshy-6-lite",
        "should_texture": True,
        "multi_view_thumbnails": True,
    }, "02_image_to_3d_lite")
    download(mesh["model_urls"]["glb"], OUT / "02_image_to_3d_lite" / "model.glb")
    for view, u in (mesh.get("thumbnail_urls") or {}).items():
        download(u, OUT / "02_image_to_3d_lite" / f"thumb_{view}.png")

    run_step("print/analyze", {"input_task_id": mesh["id"]}, "03_analyze")

    mc = run_step("print/multi-color", {"input_task_id": mesh["id"], "max_colors": 5, "style": "cartoon"},
                  "04_multi_color")
    download(mc["model_urls"]["3mf"], OUT / "04_multi_color" / "meshy_multicolor.3mf")

    bal = httpx.get(f"{API}/v1/balance", headers=MESHY, timeout=30).json()
    log(f"balance after: {bal}; ledger credits {spent_credits()}")
    log("DONE")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
