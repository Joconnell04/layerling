"""Supabase smoke: one TEST job through `serve` against the real project, then clean up.

    uv run python -m p2p_worker smoke-supabase          (or the @pytest.mark.supabase test)

What it does, in order:
1. Uploads a generated photo to inputs/<job_id>/input/photo_1.jpg and creates the jobs row the
   way the web app does (draft, prompt message, then queued), with access='code', owner_id null
   and a title starting "TEST".
2. Starts `python -m p2p_worker serve` as a child process with Meshy on the fake server
   (MESHY_MODE=test, FAKE_MESHY_WEBHOOKS=0), MockClaude, and P2P_ONLY_JOBS=<job_id> so no other
   job in the project is touched.
3. Plays both outsiders: the web app (inserts answers, concept_pick, mesh_decision messages when
   a gate opens) and the meshy-webhook edge function (inserts a meshy_events row for each task
   the worker records). The worker must hear both over Realtime, confirm each event with one GET
   to the fake Meshy, and set processed_at.
4. Checks stage_runs, jobs.stage and status, assistant messages, Storage artifacts, spend rows,
   meshy_events.processed_at, the workers heartbeat, and the JSON log file.
5. Deletes every row and object it created (only those: by this job id, its task ids, and this
   worker id), unless --keep.

No Meshy or Claude spend: the fake Meshy server and MockClaude run inside the child.
"""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import Settings

THIN = ("s01_intake", "s03_concept", "s05_mesh", "s06_printability", "s07_baseline")


def _photo_jpeg() -> bytes:
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (480, 360), (225, 225, 225))
    ImageDraw.Draw(im).ellipse((140, 80, 340, 280), fill=(190, 140, 80))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Admin:
    """Service-role PostgREST and Storage calls for the smoke run. Never prints the key."""

    url: str
    key: str = field(repr=False)

    def __post_init__(self) -> None:
        self.http = httpx.Client(base_url=self.url.rstrip("/"), timeout=30.0, headers={
            "apikey": self.key, "Authorization": f"Bearer {self.key}"})

    def rest(self, method: str, table: str, *, params: dict | None = None, body: Any = None,
             prefer: str | None = None) -> Any:
        r = self.http.request(method, f"/rest/v1/{table}", params=params, json=body,
                              headers={"Prefer": prefer} if prefer else None)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {table}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else None

    def upload(self, bucket: str, key: str, data: bytes, mime: str) -> None:
        r = self.http.post(f"/storage/v1/object/{bucket}/{key}", content=data,
                           headers={"Content-Type": mime, "x-upsert": "true"})
        if r.status_code >= 400:
            raise RuntimeError(f"upload {bucket}/{key}: {r.status_code} {r.text[:200]}")

    def list_all(self, bucket: str, prefix: str) -> list[str]:
        """Every object name under prefix (Storage lists one level at a time)."""
        out: list[str] = []
        todo = [prefix.rstrip("/")]
        while todo:
            p = todo.pop()
            r = self.http.post(f"/storage/v1/object/list/{bucket}",
                               json={"prefix": p + "/", "limit": 1000, "offset": 0})
            if r.status_code >= 400:
                raise RuntimeError(f"list {bucket}/{p}: {r.status_code} {r.text[:200]}")
            for item in r.json():
                name = f"{p}/{item['name']}"
                (out.append(name) if item.get("id") else todo.append(name))
        return sorted(out)

    def remove(self, bucket: str, names: list[str]) -> None:
        for i in range(0, len(names), 100):
            r = self.http.request("DELETE", f"/storage/v1/object/{bucket}",
                                  json={"prefixes": names[i:i + 100]})
            if r.status_code >= 400:
                raise RuntimeError(f"remove {bucket}: {r.status_code} {r.text[:200]}")


@dataclass
class Report:
    job_id: str
    worker_id: str
    checks: dict[str, bool] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    cleanup: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(self.checks.values())

    def check(self, name: str, passed: bool, detail: Any = None) -> None:
        self.checks[name] = bool(passed)
        if detail is not None:
            self.details[name] = detail


class WebApp:
    """Answers the open gate the way apps/web does: one user message, reply_to the prompt."""

    def __init__(self, admin: Admin, job_id: str):
        self.admin = admin
        self.job_id = job_id
        self.answered: set[str] = set()

    def step(self) -> str | None:
        msgs = self.admin.rest("GET", "messages", params={
            "job_id": f"eq.{self.job_id}", "role": "eq.assistant", "order": "created_at",
            "select": "id,kind,stage,payload"})
        for m in reversed(msgs):
            if m["id"] in self.answered or m["kind"] not in ("questions", "concepts",
                                                              "mesh_review"):
                continue
            if m["kind"] == "questions":
                kind, payload = "answers", {"answers": [
                    {"question_id": q["id"], "option_ids": [q["options"][0]["id"]]}
                    for q in m["payload"]["questions"]]}
            elif m["kind"] == "concepts":
                kind, payload = "concept_pick", {
                    "candidate_id": m["payload"]["candidates"][-1]["id"], "approve": True}
            else:
                kind, payload = "mesh_decision", {"action": "approve"}
            self.admin.rest("POST", "messages", prefer="return=minimal", body={
                "job_id": self.job_id, "owner_id": None, "role": "user", "kind": kind,
                "stage": m["stage"], "payload": payload, "reply_to": m["id"]})
            self.answered.add(m["id"])
            return kind
        return None


class EdgeFunction:
    """Plays supabase/functions/meshy-webhook: a meshy_events row per task the worker made."""

    def __init__(self, admin: Admin, job_id: str, delay_s: float = 1.0):
        self.admin = admin
        self.job_id = job_id
        self.delay_s = delay_s
        self.seen: dict[str, float] = {}
        self.sent: dict[str, dict] = {}

    def step(self) -> int:
        runs = self.admin.rest("GET", "stage_runs", params={
            "job_id": f"eq.{self.job_id}", "select": "stage,attempt,manifest"})
        now = time.monotonic()
        n = 0
        for r in runs:
            for t in (r["manifest"] or {}).get("meshy_tasks", []):
                tid = t["task_id"]
                self.seen.setdefault(tid, now)
                if tid in self.sent or now - self.seen[tid] < self.delay_s:
                    continue
                event = {"id": tid, "status": "SUCCEEDED", "progress": 100}
                self.admin.rest("POST", "meshy_events", prefer="return=minimal",
                                body={"task_id": tid, "status": "SUCCEEDED", "event": event})
                self.sent[tid] = event
                n += 1
        return n


def run_smoke(settings: Settings, *, keep: bool = False, timeout_s: float = 240.0,
              log=print) -> Report:
    if not settings.use_supabase:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env")
    admin = Admin(settings.supabase_url, settings.supabase_service_key)
    job_id = str(uuid.uuid4())
    worker_id = f"smoke-{job_id[:8]}"
    rep = Report(job_id=job_id, worker_id=worker_id)
    tmp = Path(tempfile.mkdtemp(prefix="p2p_smoke_"))
    photo_key = f"{job_id}/input/photo_1.jpg"
    child: subprocess.Popen | None = None
    edge = EdgeFunction(admin, job_id)
    started = time.monotonic()
    try:
        # 1. job row, photo, prompt message, then queued (the web app's order)
        admin.rest("POST", "jobs", prefer="return=minimal", body={
            "id": job_id, "owner_id": None, "access": "code",
            "title": f"TEST worker smoke {time.strftime('%H:%M:%S')}",
            "prompt": "keychain of my dog", "product_type": "keychain",
            "profile": "thin_slice", "status": "draft"})
        admin.upload("inputs", photo_key, _photo_jpeg(), "image/jpeg")
        admin.rest("POST", "messages", prefer="return=minimal", body={
            "job_id": job_id, "owner_id": None, "role": "user", "kind": "prompt",
            "body": "keychain of my dog", "payload": {}})

        # 2. serve, isolated to this job
        port = _free_port()
        env = {**os.environ, "MESHY_MODE": "test", "MOCK_CLAUDE": "1", "CLAUDE_BACKEND": "mock",
               "FAKE_MESHY_WEBHOOKS": "0", "P2P_ONLY_JOBS": job_id, "WORKER_ID": worker_id,
               "P2P_JOBS_DIR": str(tmp / "jobs"), "P2P_LOG_DIR": str(tmp / "logs")}
        with open(tmp / "serve.out", "wb") as out:  # the child keeps its own copy of the fd
            child = subprocess.Popen(
                [sys.executable, "-m", "p2p_worker", "serve", "--port", str(port)],
                cwd=settings.repo_root, env=env, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if admin.rest("GET", "workers", params={"id": f"eq.{worker_id}", "select": "id"}):
                break
            if child.poll() is not None:
                raise RuntimeError(f"serve exited early: {(tmp / 'serve.out').read_text()[-800:]}")
            time.sleep(1)
        rep.check("serve_started", time.monotonic() < deadline)
        time.sleep(4)  # let the Realtime channel join before the job is queued
        admin.rest("PATCH", "jobs", params={"id": f"eq.{job_id}"}, prefer="return=minimal",
                   body={"photos": [{"path": "input/photo_1.jpg"}], "status": "queued"})
        log(f"job {job_id} queued; serve on port {port}")

        # 3. play the web app and the edge function until done
        web = WebApp(admin, job_id)
        status, last = "queued", None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            row = admin.rest("GET", "jobs", params={"id": f"eq.{job_id}",
                                                    "select": "status,stage,pause_reason"})[0]
            status = row["status"]
            if (status, row["stage"]) != last:
                log(f"  jobs.status={status} stage={row['stage']}")
                last = (status, row["stage"])
            if status in ("done", "failed", "paused"):
                break
            edge.step()
            if status == "waiting_user":
                kind = web.step()
                if kind:
                    log(f"  web app sent {kind}")
            time.sleep(1)
        rep.details["seconds"] = round(time.monotonic() - started, 1)
        rep.check("job_done", status == "done", {"status": status, "last": last})
        _verify(admin, rep, tmp, edge, worker_id)
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(20)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
        rep.details["serve_tail"] = (tmp / "serve.out").read_text()[-1500:] \
            if (tmp / "serve.out").is_file() else ""
        if not keep:
            rep.cleanup = _cleanup(admin, job_id, worker_id, photo_key, list(edge.seen))
    return rep


def _verify(admin: Admin, rep: Report, tmp: Path, edge: EdgeFunction, worker_id: str) -> None:
    job_id = rep.job_id
    job = admin.rest("GET", "jobs", params={"id": f"eq.{job_id}", "select": "*"})[0]
    rep.check("jobs_stage", job["stage"] == "s07_baseline", job["stage"])
    rep.check("jobs_result", bool(job.get("result") and job["result"].get("threemf")),
              job.get("result"))
    rep.check("jobs_spent", int(job["spent_credits"]) > 0, job["spent_credits"])

    runs = admin.rest("GET", "stage_runs", params={"job_id": f"eq.{job_id}",
                                                   "select": "stage,attempt,status"})
    by_stage = {r["stage"]: r["status"] for r in runs}
    rep.check("stage_runs", all(by_stage.get(s) == "succeeded" for s in THIN), by_stage)

    msgs = admin.rest("GET", "messages", params={"job_id": f"eq.{job_id}",
                                                 "select": "role,kind,consumed_at"})
    kinds = {m["kind"] for m in msgs if m["role"] == "assistant"}
    rep.check("assistant_messages", {"questions", "concepts", "mesh_review", "result"} <= kinds,
              sorted(kinds))
    gate_replies = [m for m in msgs if m["role"] == "user" and m["kind"] != "prompt"]
    rep.check("replies_consumed", bool(gate_replies) and all(m["consumed_at"]
                                                             for m in gate_replies),
              len(gate_replies))

    names = admin.list_all("artifacts", job_id)
    wanted = ("baseline_cartoon.3mf", "mesh_m1.glb", "approved_concept.png", "result.json")
    rep.check("storage_artifacts", all(any(n.endswith(w) for n in names) for w in wanted),
              len(names))
    rep.details["artifact_names"] = names

    spend = admin.rest("GET", "spend", params={"job_id": f"eq.{job_id}",
                                               "select": "vendor,credits,usd,test_mode"})
    rep.check("spend_rows", any(s["vendor"] == "meshy" for s in spend)
              and all(s["test_mode"] for s in spend if s["vendor"] == "meshy"),
              {"rows": len(spend), "credits": sum(int(s["credits"]) for s in spend)})

    tids = list(edge.sent)
    events = admin.rest("GET", "meshy_events", params={
        "task_id": f"in.({','.join(tids)})", "select": "task_id,processed_at"}) if tids else []
    rep.check("meshy_events_processed", bool(events) and all(e["processed_at"] for e in events),
              {"events": len(events), "tasks": len(tids)})

    w = admin.rest("GET", "workers", params={"id": f"eq.{worker_id}", "select": "*"})
    rep.check("worker_heartbeat", bool(w) and w[0]["meshy_mode"] == "test",
              w[0]["last_seen"] if w else None)

    log_path = tmp / "logs" / "worker.jsonl"
    lines = [json.loads(x) for x in log_path.read_text().splitlines()] \
        if log_path.is_file() else []
    text = log_path.read_text() if log_path.is_file() else ""
    rt_events = [x for x in lines if x.get("event") == "realtime_change"]
    confirms = [x for x in lines if x.get("event") == "meshy_get"]
    rep.check("logs_json", any(x.get("event") == "stage_end" and x.get("job_id") == job_id
                               for x in lines), len(lines))
    rep.check("realtime_delivered", any(x.get("table") == "meshy_events" for x in rt_events)
              and any(x.get("table") == "messages" for x in rt_events), len(rt_events))
    rep.check("one_get_per_event", len(confirms) == len(tids),
              {"gets": len(confirms), "events": len(tids)})
    rep.check("logs_redacted", admin.key not in text and "Signature=" not in text
              and ";base64," not in text)


def _cleanup(admin: Admin, job_id: str, worker_id: str, photo_key: str,
             task_ids: list[str]) -> dict[str, Any]:
    """Delete only what this run created: this job id, its task ids, this worker id."""
    out: dict[str, Any] = {}
    try:
        names = admin.list_all("artifacts", job_id)
        admin.remove("artifacts", names)
        admin.remove("inputs", [photo_key])
        out["objects"] = len(names) + 1
        if task_ids:
            admin.rest("DELETE", "meshy_events",
                       params={"task_id": f"in.({','.join(task_ids)})"})
        for table in ("spend", "messages", "stage_runs"):
            admin.rest("DELETE", table, params={"job_id": f"eq.{job_id}"})
        admin.rest("DELETE", "jobs", params={"id": f"eq.{job_id}"})
        admin.rest("DELETE", "workers", params={"id": f"eq.{worker_id}"})
        left = {
            "jobs": admin.rest("GET", "jobs", params={"id": f"eq.{job_id}", "select": "id"}),
            "messages": admin.rest("GET", "messages", params={"job_id": f"eq.{job_id}",
                                                              "select": "id"}),
            "stage_runs": admin.rest("GET", "stage_runs", params={"job_id": f"eq.{job_id}",
                                                                  "select": "id"}),
            "spend": admin.rest("GET", "spend", params={"job_id": f"eq.{job_id}",
                                                        "select": "id"}),
            "workers": admin.rest("GET", "workers", params={"id": f"eq.{worker_id}",
                                                            "select": "id"}),
            "meshy_events": admin.rest("GET", "meshy_events", params={
                "task_id": f"in.({','.join(task_ids)})", "select": "id"}) if task_ids else [],
            "artifacts": admin.list_all("artifacts", job_id),
            "inputs": admin.list_all("inputs", job_id),
        }
        out["left"] = {k: len(v) for k, v in left.items()}
        out["clean"] = not any(left.values())
    except Exception as e:  # noqa: BLE001 report, never hide, a failed cleanup
        out["error"] = f"{type(e).__name__}: {e}"
        out["clean"] = False
    return out


def main(settings: Settings, *, keep: bool = False, timeout_s: float = 240.0) -> int:
    rep = run_smoke(settings, keep=keep, timeout_s=timeout_s)
    for name, passed in rep.checks.items():
        detail = rep.details.get(name)
        print(f"{'ok  ' if passed else 'FAIL'} {name}" + (f"  {detail}" if detail is not None
                                                          else ""))
    print(f"seconds  {rep.details.get('seconds')}")
    print(f"cleanup  {rep.cleanup or 'kept (--keep)'}")
    if not rep.ok:
        print(rep.details.get("serve_tail", "")[-1200:])
    return 0 if rep.ok and (keep or rep.cleanup.get("clean")) else 1
