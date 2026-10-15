"""Chaos: kill or restart the worker at every stage boundary and at points inside stages, on the
fake Meshy server and the stub Claude, and check the job still finishes the same.

One full job (all ten stages, the paint agent with its scripted driver, a fidelity review
approved) runs once uninterrupted as the reference. Then the same job (same job id, a fresh
folder, a fresh fake Meshy) runs again while the worker is killed at chosen points: a kill
raises SimulatedCrash (a BaseException, so no stage error handling sees it), the event loop
dies with it, and a new store, runtime and Claude client start on the same job folder, like a
new process. Meshy outlives the worker: its tasks either finish while the worker is down (the
webhook row lands in meshy_events) or after the restart.

After every scenario:
- the job is DONE and its outputs match the reference (JSON compared without timestamps and
  attempt numbers; binaries byte for byte),
- the fake Meshy saw exactly the reference's paid POSTs (no paid call made twice),
- the ledger books each Meshy task once, and the customer saw each question once (a reply the
  worker lost would leave the job waiting forever: the scripted customer answers each gate
  message once, like a person).

Kill points:
- stage boundaries: `<stage>:start`, `<stage>:end:<status>`, `job:<status>`,
- inside stages (StageContext checkpoints): before a paid call (`meshy_pending`), after the
  create and before the webhook (`meshy_created`), after the webhook (`meshy_done`), each step,
  Claude call, gate open and gate reply, file fetch,
- store side effects, right after they land and before the manifest knows: the Meshy task
  indexed (after the POST), a spend row, an assistant message, a consumed reply, a status-read
  event, a processed webhook,
- mid geometry and every other heavy build (`heavy:<build>:start` and `:end`: files on disk,
  result not cached), mid slice (inside the slicer call), mid paint (inside a paint turn).

The gauntlet test kills once at every point of that list in one job. CHAOS_EACH=1 also runs
one job per kill point (a few minutes; not in the default run).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import REPO, SimulatedCrash, make_photo
from p2p_contracts import JobStatus
from p2p_contracts.payloads import ReviewDecision
from p2p_contracts.stages import PROFILE_FULL
from p2p_worker.clients.claude import MockClaude
from p2p_worker.config import Settings
from p2p_worker.fakes.meshy import FakeMeshy
from p2p_worker.local import AutoResponder
from p2p_worker.runtime import build_runtime
from p2p_worker.store import LocalStore
from p2p_worker.util import read_jsonl

JOB_ID = "c4a05000-0000-4000-8000-00000000c4a0"
PAID = ("image-to-image", "multi-image-to-3d", "image-to-3d", "retexture", "print-multi-color",
        "print-repair", "print-split", "keychain-prototype", "keychain-build",
        "magnet-prototype", "magnet-build")
MAX_RESTARTS = 400


# ---------------------------------------------------------------- the killer
class Killer:
    """on_checkpoint hook. `plan`: point names to die at; each kills once, the first time the
    run meets it. What is left in `plan` at the end was never met (a replay can take a path
    without it: a Claude result or a gate reply already on disk)."""

    def __init__(self, plan: list[str] | None = None):
        self.plan = list(plan or [])
        self.trace: list[str] = []
        self.kills: list[str] = []
        self.missed: list[str] = []
        # Set at a kill until the restart: a killed process writes nothing more. (The crash
        # is raised in one coroutine; without this a sibling in the same gather would go on
        # saving, which a real SIGKILL never allows.)
        self.dead = False

    def __call__(self, point: str) -> None:
        if self.dead:
            raise SimulatedCrash("dead")
        self.trace.append(point)
        if point in self.plan:
            self.plan.remove(point)
            self.kills.append(point)
            self.dead = True
            raise SimulatedCrash(point)


class ChaosStore(LocalStore):
    """LocalStore that calls the killer right after each side effect the worker makes."""

    def __init__(self, jobs_dir: Path, hook: Killer):
        super().__init__(jobs_dir)
        self.hook = hook

    def _alive(self) -> None:
        if self.hook.dead:
            raise SimulatedCrash("dead")

    def write_manifest(self, manifest):
        self._alive()
        super().write_manifest(manifest)

    def save_job(self, job):
        self._alive()
        super().save_job(job)

    def update_job_row(self, job_id, **fields):
        self._alive()
        super().update_job_row(job_id, **fields)

    def post_message(self, job_id, role, kind, payload=None, *, stage=None, body=None,
                     reply_to=None, **kw):
        if role == "assistant":
            self._alive()
        row = super().post_message(job_id, role, kind, payload, stage=stage, body=body,
                                   reply_to=reply_to, **kw)
        if role == "assistant":  # the customer's own posts are not the worker's
            self.hook(f"store:post_message:{kind}:{stage.value if stage else '-'}")
        return row

    def consume_message(self, job_id, message_id):
        self._alive()
        super().consume_message(job_id, message_id)
        kind = next((m.kind for m in super().list_messages(job_id) if m.id == message_id), "?")
        self.hook(f"store:consume_message:{kind}")

    def record_spend(self, event, **kw):
        self._alive()
        super().record_spend(event, **kw)
        ref = event.ref if event.vendor == "meshy" else "-"
        self.hook(f"store:record_spend:{event.vendor}:{event.kind}:{ref}")

    def index_task(self, task_id, job_id):
        self._alive()
        super().index_task(task_id, job_id)
        self.hook(f"store:index_task:{task_id}")  # after the POST, before the manifest

    def insert_meshy_event(self, event, *, source="webhook"):
        if source != "webhook":  # Meshy's own rows land whether the worker lives or not
            self._alive()
        eid = super().insert_meshy_event(event, source=source)
        if source != "webhook":  # webhook rows come from Meshy's side (the edge function)
            self.hook(f"store:insert_meshy_event:{source}")
        return eid

    def mark_meshy_processed(self, event_ids):
        self._alive()
        super().mark_meshy_processed(event_ids)
        if event_ids:
            self.hook("store:mark_meshy_processed")


class ChaosMeshy(FakeMeshy):
    """The fake Meshy server, which outlives worker restarts. A task in flight when the worker
    died lost its runner with that event loop: `rearm` restarts it on the new loop (Meshy
    finishes after the restart); `complete_pending` finishes it at once (while the worker is
    down, so only the meshy_events row tells the worker)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._seen: Counter[str] = Counter()
        self.lose_reply: set[str] = set()  # endpoints whose next create lands, reply lost

    def _create(self, ep: str, body: dict[str, Any]) -> str:
        """Task ids from the request, not the order of creates, so a replay whose creates
        interleave differently still gets the same ids (and the fake draws the same images)."""
        key = hashlib.sha256(json.dumps([ep, body], sort_keys=True, default=str).encode())
        n = self._seen[key.hexdigest()] = self._seen[key.hexdigest()] + 1
        seed = int(key.hexdigest()[:24], 16) ^ n
        ids = self._ids
        self._ids = iter([seed])
        try:
            return super()._create(ep, body)
        finally:
            self._ids = ids

    async def handle(self, request):
        resp = await super().handle(request)
        for ep in list(self.lose_reply):
            if (request.method == "POST" and resp.status_code == 202
                    and request.url.path.rstrip("/").endswith(ep)):
                self.lose_reply.discard(ep)
                raise httpx.ReadTimeout("reply lost after the task was made", request=request)
        return resp

    def rearm(self) -> None:
        loop = asyncio.get_running_loop()
        for tid, t in self.tasks.items():
            if t["status"] in ("PENDING", "IN_PROGRESS"):
                task = loop.create_task(self._run(tid, "SUCCEEDED"))
                self._bg.add(task)
                task.add_done_callback(self._bg.discard)


class Customer(AutoResponder):
    """Answers each gate message once and approves a fidelity review once, like a person: a
    reply the worker consumed and then lost is never sent again, so the job would hang."""

    def __init__(self):
        super().__init__()
        self.answered: set[str] = set()
        self.reviews: set[str] = set()

    def respond(self, rt, job_id) -> bool:
        job = rt.store.load_job(job_id)
        m = rt.store.load_manifest(job_id, job.stage, job.attempts[job.stage])
        if m is None or m.gate is None:
            return False
        key = m.gate.message_id or f"{job.stage.value}:{m.gate.kind}"
        if key in self.answered:
            return False
        self.answered.add(key)
        return super().respond(rt, job_id)

    def review(self, rt, job_id) -> bool:
        req = [x for x in rt.store.list_messages(job_id, role="assistant")
               if x.kind == "review_request"]
        if not req or req[-1].id in self.reviews:
            return False
        self.reviews.add(req[-1].id)
        rt.store.post_message(job_id, "user", "review_decision",
                              ReviewDecision(action="approve", notes="chaos"),
                              reply_to=req[-1].id)
        return True


class Claudes:
    """A fresh MockClaude per worker process, with the calls counted across restarts."""

    def __init__(self):
        self.calls: list[str] = []

    def new(self) -> MockClaude:
        c = MockClaude()
        c.calls = self.calls
        return c


# ---------------------------------------------------------------- one job, many lives
async def _drive(rt, job_id: str, customer: Customer, max_steps: int = 300) -> JobStatus:
    status = JobStatus.QUEUED
    for _ in range(max_steps):
        status = await rt.orchestrator.advance(job_id)
        if status == JobStatus.WAITING_USER:
            if not customer.respond(rt, job_id):
                return status
            continue
        if status == JobStatus.REVIEW:
            if not customer.review(rt, job_id):
                return status
            continue
        if status in (JobStatus.RUNNING, JobStatus.QUEUED):
            wait = getattr(rt.orchestrator, "retry_wait_s", lambda _j: 0.0)(job_id)
            await asyncio.sleep(min(max(wait or 0.0, 0.0), 1.0))
            continue
        return status
    return status


class Scenario:
    def __init__(self, root: Path, plan: list[str] | None = None, *,
                 down_policy: str = "alternate"):
        self.root = root
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        shutil.copytree(REPO / "filaments", root / "filaments")
        self.settings = Settings(
            repo_root=root, jobs_dir=root / "jobs", mock_claude=True, claude_backend="mock",
            vendor_wait_s=2.0, missed_webhook_s=600.0, fake_meshy_delay_s=0.002,
            paint_agent="agent")
        self.photo = make_photo(root / "dog.jpg")
        self.killer = Killer(plan)
        self.fake = ChaosMeshy(delay_s=self.settings.fake_meshy_delay_s)
        self.customer = Customer()
        self.claudes = Claudes()
        self.down_policy = down_policy
        self.restarts = 0
        self.rt = None

    def _runtime(self):
        store = ChaosStore(self.settings.jobs_dir, self.killer)
        self.fake.on_event = lambda ev, s=store: s.insert_meshy_event(ev)
        return build_runtime(self.settings, store=store, fake=self.fake,
                             claude=self.claudes.new(), on_checkpoint=self.killer)

    def run(self) -> JobStatus:
        rt = self._runtime()
        rt.orchestrator.create_job("keychain of my dog", [self.photo], product_type="keychain",
                                   profile="full", job_id=JOB_ID)
        while True:
            self.rt = rt

            async def life(rt=rt):
                self.fake.rearm()
                status = await _drive(rt, JOB_ID, self.customer)
                await self.fake.drain()
                return status

            try:
                return asyncio.run(life())
            except SimulatedCrash:
                self.killer.dead = False  # the new process
                self.restarts += 1
                assert self.restarts < MAX_RESTARTS, "restart loop"
                if self.down_policy == "while_down" or (
                        self.down_policy == "alternate" and self.restarts % 2):
                    self.fake.complete_pending()  # Meshy finishes while the worker is down
                rt = self._runtime()


# ---------------------------------------------------------------- comparing jobs
IGNORE_KEYS = {"at", "created_at", "started_at", "finished_at", "opened_at", "received_at",
               "consumed_at", "processed_at", "duration_s", "duration_ms", "resumed_at",
               "timings", "t", "elapsed_s", "expires_at", "run", "beat_at", "wall_s",
               "paint_attempt"}
ATTEMPT = re.compile(r"/a\d+/")


def _norm(v: Any) -> Any:
    """JSON without times, attempt numbers, and the hash and size of files that live in an
    attempt folder (a fresh attempt writes the same content under another folder)."""
    if isinstance(v, dict):
        in_attempt = isinstance(v.get("path"), str) and ATTEMPT.search(v["path"])
        return {k: _norm(x) for k, x in v.items() if k not in IGNORE_KEYS
                and not (in_attempt and k in ("sha256", "bytes"))}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, str):
        return ATTEMPT.sub("/aN/", v)
    return v


def _json_digest(data: bytes) -> str | None:
    try:
        obj = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return hashlib.sha256(json.dumps(_norm(obj), sort_keys=True).encode()).hexdigest()[:16]


def _digest(p: Path) -> str:
    if p.suffix == ".json":
        d = _json_digest(p.read_bytes())
        if d:
            return d
    if p.suffix == ".3mf":  # members compared one by one, JSON metadata normalized
        import zipfile

        h = hashlib.sha256()
        with zipfile.ZipFile(p) as z:
            for name in sorted(z.namelist()):
                data = z.read(name)
                h.update(name.encode())
                h.update(((_json_digest(data) if name.endswith(".json") else None)
                          or hashlib.sha256(data).hexdigest()).encode())
        return h.hexdigest()[:16]
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


SKIP_ROLES = {"trace", "paint_run"}  # call logs: timings and request ids by design


def fingerprint(sc: Scenario) -> dict[str, Any]:
    store = LocalStore(sc.settings.jobs_dir)
    job = store.load_job(JOB_ID)
    jdir = store.job_dir(JOB_ID)
    out: dict[str, Any] = {"status": job.status.value, "result": _norm(job.result)}
    for stage in PROFILE_FULL:
        m = store.load_manifest(JOB_ID, stage, job.attempts[stage])
        files = {}
        for o in m.outputs:
            if o.role in SKIP_ROLES or "/trace/" in o.path:
                continue
            files[f"{o.role}:{Path(o.path).name}"] = _digest(jdir / o.path)
        out[stage.value] = {"status": m.status.value, "outputs": files,
                            "tasks": sorted(t.request.get("ai_model", "") + ":" + t.endpoint
                                            for t in m.meshy_tasks)}
    ledger = read_jsonl(jdir / "ledger.jsonl")
    out["meshy_ledger"] = sorted((e["kind"], e["credits"], e["ref"]) for e in ledger
                                 if e["vendor"] == "meshy")
    out["spent_credits"] = job.budget.spent_credits
    out["assistant_messages"] = Counter(m.kind for m in store.list_messages(JOB_ID)
                                        if m.role == "assistant" and m.kind != "note")
    out["paid_posts"] = Counter(ep for ep in PAID
                                for _ in range(sc.fake.count("POST", ep)))
    return out


def diff(a: dict, b: dict, path: str = "") -> list[str]:
    out = []
    for k in sorted(set(a) | set(b), key=str):
        x, y = a.get(k), b.get(k)
        if isinstance(x, dict) and isinstance(y, dict):
            out += diff(x, y, f"{path}{k}.")
        elif x != y:
            out.append(f"{path}{k}: {x!r} != {y!r}")
    return out


# ---------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def paint_stub(monkeypatch):
    monkeypatch.setenv("P2P_PAINT_DRIVER", "stub")  # the paint agent with a scripted driver
    monkeypatch.setenv("P2P_PAINT_SAM3", "mock")


@pytest.fixture(autouse=True)
def chaos_hooks(monkeypatch, fake_slicer):
    """Kill points inside the slicer call and inside paint turns (mid slice, mid paint)."""
    import p2p_threemf
    from p2p_mcp_paint import agent

    hook: dict[str, Killer | None] = {"k": None}

    def slice_check(path, *a, **kw):
        k = hook["k"]
        if k:
            k(f"slice:{Path(str(path)).parent.parent.name}")
        return fake_slicer(path, *a, **kw)

    real_turn = agent.StubDriver.turn

    async def turn(self, content, effort):
        k = hook["k"]
        text = content if isinstance(content, str) else " ".join(
            b.get("text", "") for b in content if isinstance(b, dict))
        if k:
            k("paint:signoff" if text.startswith("Final sign-off") else f"paint:turn{self.k + 1}")
        return await real_turn(self, content, effort)

    monkeypatch.setattr(p2p_threemf, "slice_check", slice_check)
    monkeypatch.setattr(agent.StubDriver, "turn", turn)
    return hook


REF: dict[str, Any] = {}


def reference(tmp: Path, hooks) -> dict[str, Any]:
    """One uninterrupted run (computed once per session): fingerprint, checkpoint trace."""
    if not REF:
        sc = Scenario(tmp / "reference")
        hooks["k"] = sc.killer
        t0 = time.monotonic()
        status = sc.run()
        REF["took"] = time.monotonic() - t0
        assert status == JobStatus.DONE, (status, sc.rt.store.job_row(JOB_ID))
        REF["fp"] = fingerprint(sc)
        REF["trace"] = list(sc.killer.trace)
        REF["claude_calls"] = len(sc.claudes.calls)
    hooks["k"] = None
    return REF


KILL_PATTERNS = (
    r"^s\d\d_\w+:start$", r"^s\d\d_\w+:end:\w+$", r"^job:\w+$",
    r":meshy_pending:", r":meshy_created:", r":meshy_done:", r":meshy_adopted:",
    r":step:", r":claude:", r":gate_open:", r":gate_reply:", r":fetch:",
    r":heavy:", r"^slice:", r"^paint:",
    r"^store:",
)


def kill_points(trace: list[str]) -> list[str]:
    seen, out = set(), []
    for p in trace:
        if p in seen:
            continue
        seen.add(p)
        if any(re.search(r, p) for r in KILL_PATTERNS):
            out.append(p)
    return out


def check_against_reference(sc: Scenario, ref: dict[str, Any], status: JobStatus) -> None:
    last = sc.killer.kills[-3:]
    report = {"status": status.value, "kills": sc.killer.kills, "missed": sc.killer.missed,
              "unmet": sc.killer.plan, "restarts": sc.restarts,
              "claude_calls": len(sc.claudes.calls)}
    if status == JobStatus.DONE:
        report["problems"] = diff(ref["fp"], fingerprint(sc))
    (sc.root / "chaos_report.json").write_text(json.dumps(report, indent=1, default=str))
    if os.environ.get("CHAOS_REPORT_DIR"):
        dst = Path(os.environ["CHAOS_REPORT_DIR"]) / f"{sc.root.name}.json"
        dst.write_text(json.dumps(report, indent=1, default=str))
    assert status == JobStatus.DONE, (status.value, "last kills", last,
                                      sc.rt.store.job_row(JOB_ID).get("pause_reason"))
    problems = diff(ref["fp"], fingerprint(sc))
    assert not problems, ("kills", sc.killer.kills, problems)


def test_gauntlet_kills_at_every_point(tmp_path, chaos_hooks):
    """The worker dies at every kill point of the reference, one after another, in one job:
    stage boundaries, before each paid call, after each create and before its webhook, after
    each webhook, every step, Claude call, gate and store write, mid geometry and every other
    heavy build, mid slice and mid paint. Meshy finishes in-flight tasks while the worker is
    down after odd kills and after the restart after even ones. A kill can take the replay
    down a path without a later point (a create recovered after a crash is saved as
    meshy_adopted, not meshy_created): those points get another job, until every one fired."""
    ref = reference(tmp_path, chaos_hooks)
    plan = kill_points(ref["trace"])
    assert len(plan) > 100
    left, kills, passes = list(plan), [], 0
    while left and passes < 4:
        passes += 1
        sc = Scenario(tmp_path / f"gauntlet{passes}", left)
        chaos_hooks["k"] = sc.killer
        t0 = time.monotonic()
        status = sc.run()
        print(f"gauntlet pass {passes}: {len(sc.killer.kills)} kills of {len(left)} points, "
              f"{sc.restarts} restarts, {time.monotonic() - t0:.1f} s (reference "
              f"{ref['took']:.1f} s); Claude calls {len(sc.claudes.calls)} (reference "
              f"{ref['claude_calls']})")
        check_against_reference(sc, ref, status)
        assert len(sc.claudes.calls) == ref["claude_calls"]  # no Claude call bought twice
        assert sc.killer.kills, "no progress"
        kills += sc.killer.kills
        left = list(sc.killer.plan)
    assert not left, left
    assert sorted(kills) == sorted(plan)


@pytest.mark.skipif(os.environ.get("CHAOS_EACH") != "1", reason="CHAOS_EACH=1 runs one job "
                    "per kill point (a few minutes)")
def test_each_kill_point_in_its_own_job(tmp_path, chaos_hooks):
    ref = reference(tmp_path, chaos_hooks)
    pts = kill_points(ref["trace"])
    shard = os.environ.get("CHAOS_SHARD")
    if shard:
        i, n = (int(x) for x in shard.split("/"))
        pts = pts[i::n]
    failures = []
    for k, point in enumerate(pts):
        meshy = ":meshy_" in point or "index_task" in point or "record_spend:meshy" in point
        for policy in ("while_down", "after_restart") if meshy else ("while_down",):
            sc = Scenario(tmp_path / f"each{k}{policy[0]}", [point], down_policy=policy)
            chaos_hooks["k"] = sc.killer
            try:
                status = sc.run()
                check_against_reference(sc, ref, status)
            except AssertionError as e:
                failures.append(f"{point} ({policy}): {str(e)[:600]}")
            finally:
                chaos_hooks["k"] = None
    print(f"{len(pts)} points, {len(failures)} failures")
    for f in failures:
        print("FAIL", f)
    assert not failures


@pytest.mark.parametrize("endpoint", ["image-to-image", "multi-image-to-3d",
                                      "print/multi-color"])
def test_a_create_whose_reply_is_lost_is_not_bought_again(tmp_path, chaos_hooks, endpoint):
    """Meshy makes the task but the reply never reaches the worker (a dropped connection, a
    read timeout). The worker cannot know whether it paid, so it looks the task up before it
    creates again: the same POSTs as the reference, each task booked once."""
    ref = reference(tmp_path, chaos_hooks)
    sc = Scenario(tmp_path / f"lost-{endpoint.replace('/', '-')}")
    sc.fake.lose_reply = {endpoint}
    status = sc.run()
    assert not sc.fake.lose_reply, "the reply was never lost"
    check_against_reference(sc, ref, status)


def test_reference_run_and_kill_points(tmp_path, chaos_hooks):
    ref = reference(tmp_path, chaos_hooks)
    pts = kill_points(ref["trace"])
    print(f"reference: {ref['took']:.1f} s, {len(ref['trace'])} checkpoints, "
          f"{len(pts)} kill points")
    assert ref["fp"]["status"] == "done"
