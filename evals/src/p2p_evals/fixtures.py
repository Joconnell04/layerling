"""Fixture store: every paid Meshy output of the stored jobs, kept forever for 0-credit replays.

`build` reads the job folders (read only; nothing in them is ever written) and writes:

- evals/fixtures/store/<job_id>/meshy/<task_id>.json: the full Meshy task object (the terminal
  GET the trace kept, else the manifest's trimmed result; URLs without their signatures).
- evals/fixtures/store/<job_id>/files/<job path>: the files the worker downloaded from each task
  (concept, edit and view images, meshes, thumbnails, Multi-Color 3MFs), plus the job's seed:
  job.json, the photos and request, every file of the accepted s01 to s05 attempts (what a
  rerun from s06 starts from), and the stored run's small s06 to s10 outputs (what a rerun is
  scored against).
- evals/fixtures/manifest.json (committed): per job, each Meshy task with its stage, attempt,
  step key, endpoint, purpose, status, live credits, the request as the manifest kept it, the
  request hash the replay matches on (p2p_worker.fakes.replay.request_hash), and each stored
  file with its sha256 and the task field it came from; then every seed file with its sha256.

`verify` re-hashes the store against the manifest.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from p2p_worker.clients.meshy import ENDPOINTS
from p2p_worker.fakes.replay import (
    MANIFEST_NAME,
    canonical,
    field_hashes,
    get_field,
    request_hash,
)
from p2p_worker.util import sha256_file, utcnow

from .jobs import FIXTURES, StoredJob

VERSION = 1
ENDPOINT_BY_PATH = {e.path: e.name for e in ENDPOINTS.values()}
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "EXPIRED"}
UPSTREAM = ("s01_intake", "s02_color_spec", "s03_concept", "s04_multiview", "s05_mesh")
DOWNSTREAM = ("s06_printability", "s07_baseline", "s08_palette", "s09_paint", "s10_write")
# Downstream files a rerun is scored against (top level of the accepted attempt only).
KEEP_DOWNSTREAM = re.compile(r"^(.*\.json|.*\.ply|.*\.npy|.*\.slice\.log|render_sheet\.png|"
                             r"first_pass\.png)$")
SKIP_DOWNSTREAM = {"preview.glb", "final.3mf", "debug_x2d.3mf", "sim_face_filaments.npy"}
JOB_FILES = ("job.json", "job_row.json", "ledger.jsonl", "messages.jsonl")


# ---------------------------------------------------------------------------- reading a job


def load_manifests(job_dir: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted(job_dir.glob("s*/a*/manifest.json")):
        try:
            out.append(json.loads(p.read_text()))
        except ValueError:
            continue
    return out


def _trace_bodies(job_dir: Path, m: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Meshy trace bodies of one attempt, by task id."""
    out = {}
    for e in (m.get("data") or {}).get("trace") or []:
        if e.get("kind") != "meshy" or not e.get("ref") or not e.get("path"):
            continue
        p = job_dir / e["path"]
        if p.is_file():
            try:
                out[e["ref"]] = json.loads(p.read_text())
            except ValueError:
                continue
    return out


def asset_field(name: str, task: dict[str, Any]) -> str | None:
    """Which field of the task object a downloaded file came from (the stages' own picks:
    s03 image 0, s04 views `<key>_raw_<i>.png`, s05 the GLB and thumbnails, s07 the 3MF)."""
    if name.endswith(".glb"):
        return "model_urls.glb"
    if name.endswith(".3mf"):
        return "model_urls.3mf"
    m = re.fullmatch(r".*_raw_(\d+)\.png", name)
    if m:
        return f"image_urls.{int(m.group(1))}"
    m = re.fullmatch(r"thumb_.+_(front|back|left|right)\.png", name)
    if m:
        f = f"thumbnail_urls.{m.group(1)}"
        return f if get_field(task, f) else "thumbnail_url"
    if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        return "image_urls.0"
    return None


def collect_tasks(job_dir: Path, manifests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every Meshy task the job paid for (or ran free, like Analyze), once, with the attempt
    that finished it, its step key, the full task object and the files downloaded from it."""
    best: dict[str, dict[str, Any]] = {}
    for m in manifests:
        keys = {tid: k for k, tid in ((m.get("data") or {}).get("meshy_keys") or {}).items()}
        bodies = _trace_bodies(job_dir, m)
        for t in m.get("meshy_tasks") or []:
            tid = t["task_id"]
            body = bodies.get(tid) or {}
            out = body.get("output") or {}
            cand = {
                "task_id": tid, "stage": m["stage"], "attempt": m["attempt"],
                "key": keys.get(tid), "endpoint": ENDPOINT_BY_PATH.get(t["endpoint"]),
                "purpose": t.get("purpose", ""), "status": t.get("status"),
                "credits": int(t.get("credits") or 0), "request": t.get("request") or {},
                "object": out.get("json") or t.get("result") or {},
                "files": list(out.get("files") or []),
                "full": bool(out.get("json")) and not body.get("reconstructed"),
            }
            prev = best.get(tid)
            rank = (cand["status"] in TERMINAL, bool(cand["files"]), cand["full"],
                    cand["attempt"])
            if prev is None or rank > prev["_rank"]:
                cand["_rank"] = rank
                if prev is not None and not cand["key"]:
                    cand["key"] = prev["key"]
                best[tid] = cand
    return sorted(best.values(), key=lambda c: (c["stage"], c["attempt"], c["task_id"]))


def seed_paths(job_dir: Path, job: dict[str, Any]) -> list[str]:
    """Job-relative files a replay starts from and is scored against."""
    out = [p for p in JOB_FILES if (job_dir / p).is_file()]
    out += [p.relative_to(job_dir).as_posix() for p in sorted((job_dir / "input").rglob("*"))
            if p.is_file()]
    attempts = job.get("attempts") or {}
    for stage in UPSTREAM:
        n = attempts.get(stage)
        for d in sorted((job_dir / stage).glob("a*")):
            if d.name == f"a{n}":
                out += [p.relative_to(job_dir).as_posix() for p in sorted(d.rglob("*"))
                        if p.is_file()]
            elif d.is_dir():
                out += [p.relative_to(job_dir).as_posix()
                        for p in [d / "manifest.json", *sorted((d / "trace").glob("*.json"))]
                        if p.is_file()]
    for stage in DOWNSTREAM:
        for d in sorted((job_dir / stage).glob("a*")):
            if not d.is_dir():
                continue
            accepted = d.name == f"a{attempts.get(stage)}"
            for p in sorted(d.iterdir()):
                if not p.is_file() or p.name in SKIP_DOWNSTREAM:
                    continue
                if p.name == "manifest.json" or (accepted and KEEP_DOWNSTREAM.match(p.name)):
                    out.append(p.relative_to(job_dir).as_posix())
            # call bodies, of stale attempts too: a CI replay answers Claude from them, and an
            # accepted attempt may have reused a stale one's cached call (catfish s06 a2 kept
            # a1's concept read)
            out += [p.relative_to(job_dir).as_posix()
                    for p in sorted((d / "trace").glob("*.json"))]
    return out


# ---------------------------------------------------------------------------- build


def _copy(src: Path, dst: Path) -> dict[str, Any]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    sha = sha256_file(src)
    if not dst.is_file() or sha256_file(dst) != sha:
        shutil.copyfile(src, dst)
    return {"sha256": sha, "bytes": src.stat().st_size}


def build_job(sj: StoredJob, job_dir: Path, store: Path) -> dict[str, Any]:
    if not (job_dir / "job.json").is_file():
        raise SystemExit(f"{sj.label}: no job folder at {job_dir}")
    job = json.loads((job_dir / "job.json").read_text())
    manifests = load_manifests(job_dir)
    base = store / sj.job_id
    tasks_out = []
    for t in collect_tasks(job_dir, manifests):
        if not t["endpoint"]:
            raise SystemExit(f"{sj.label}: task {t['task_id']} has an unknown endpoint")
        obj = dict(t["object"])
        obj["id"] = t["task_id"]
        obj.setdefault("type", ENDPOINTS[t["endpoint"]].task_type)
        obj.setdefault("status", t["status"])
        task_rel = f"meshy/{t['task_id']}.json"
        (base / "meshy").mkdir(parents=True, exist_ok=True)
        (base / task_rel).write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n")
        assets = []
        for rel in t["files"]:
            src = job_dir / rel
            fld = asset_field(Path(rel).name, obj)
            if fld is None or not get_field(obj, fld) or not src.is_file():
                raise SystemExit(f"{sj.label}: cannot tell which field of task {t['task_id']} "
                                 f"made {rel}")
            assets.append({"field": fld, "path": rel, **_copy(src, base / "files" / rel)})
        canon = canonical(t["request"], job_dir, sj.job_id)
        tasks_out.append({
            "task_id": t["task_id"], "stage": t["stage"], "attempt": t["attempt"],
            "key": t["key"], "endpoint": t["endpoint"], "purpose": t["purpose"],
            "status": t["status"] if t["status"] in TERMINAL else "SUCCEEDED",
            "credits": t["credits"], "request": t["request"],
            "request_hash": request_hash(t["endpoint"], canon),
            "request_fields": field_hashes(canon),
            "task": task_rel, "task_sha256": sha256_file(base / task_rel),
            "task_full": t["full"], "assets": assets,
        })
    asset_paths = {a["path"] for t in tasks_out for a in t["assets"]}
    files = []
    for rel in seed_paths(job_dir, job):
        if rel in asset_paths:
            continue
        files.append({"path": rel, **_copy(job_dir / rel, base / "files" / rel)})
    result = job.get("result") or {}
    return {
        "job_id": sj.job_id, "label": sj.label, "what": sj.what,
        "product_type": job.get("product_type"), "profile": job.get("profile"),
        "form": job.get("form"), "prompt": job.get("prompt"),
        "attempts": job.get("attempts"),
        "credits_live": sum(t["credits"] for t in tasks_out),  # SOURCE: stage manifests
        "result": {k: result.get(k) for k in ("threemf", "slice_ok", "print_time_s",
                                               "baseline_only")}
        | {"filaments": result.get("filaments")},
        "meshy": tasks_out,
        "files": files,
    }


def build(jobs: list[StoredJob], jobs_dir: Path, out: Path = FIXTURES,
          log=print) -> dict[str, Any]:
    """(Re)build the store for `jobs` and rewrite manifest.json, keeping other jobs' entries."""
    store = out / "store"
    store.mkdir(parents=True, exist_ok=True)
    man_path = out / MANIFEST_NAME
    prev = json.loads(man_path.read_text()) if man_path.is_file() else {"jobs": []}
    entries = {j["job_id"]: j for j in prev.get("jobs", [])}
    for sj in jobs:
        e = build_job(sj, jobs_dir / sj.job_id, store)
        entries[sj.job_id] = e
        size = sum(a["bytes"] for t in e["meshy"] for a in t["assets"])
        log(f"{sj.label:10} {sj.short}  {len(e['meshy']):2} Meshy tasks, "
            f"{sum(len(t['assets']) for t in e['meshy']):2} outputs ({size / 1e6:.1f} MB), "
            f"{e['credits_live']} credits live, {len(e['files'])} seed files")
    order = [j["job_id"] for j in prev.get("jobs", [])]
    order += [sj.job_id for sj in jobs if sj.job_id not in order]
    man = {"version": VERSION, "built_at": utcnow().isoformat(timespec="seconds"),
           "store": "evals/fixtures/store (gitignored)",
           "jobs": [entries[i] for i in order if i in entries]}
    man_path.write_text(json.dumps(man, indent=1, sort_keys=False) + "\n")
    return man


def verify(out: Path = FIXTURES) -> list[str]:
    """Problems: a stored file missing or changed since the manifest was written."""
    man = json.loads((out / MANIFEST_NAME).read_text())
    problems = []
    for job in man["jobs"]:
        base = out / "store" / job["job_id"]
        checks = [(base / t["task"], t["task_sha256"]) for t in job["meshy"]]
        checks += [(base / "files" / a["path"], a["sha256"]) for t in job["meshy"]
                   for a in t["assets"]]
        checks += [(base / "files" / f["path"], f["sha256"]) for f in job["files"]]
        for p, want in checks:
            if not p.is_file():
                problems.append(f"missing {p.relative_to(out)}")
            elif sha256_file(p) != want:
                problems.append(f"changed {p.relative_to(out)}")
    return problems


def job_entry(job_id: str, out: Path = FIXTURES) -> dict[str, Any]:
    man = json.loads((out / MANIFEST_NAME).read_text())
    for j in man["jobs"]:
        if j["job_id"] == job_id:
            return j
    raise SystemExit(f"job {job_id} is not in {out / MANIFEST_NAME}; run fixtures build")
