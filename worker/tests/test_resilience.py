"""Stage lifecycle and serve resilience: time limits and the watchdog, retries with backoff,
leases after a restart, killable child processes, keep-awake, clock jumps, /health, and
Supabase writes that are safe to repeat. The kill-anywhere chaos run is test_chaos.py."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from conftest import make_rt, run
from p2p_contracts import JobStatus, Manifest, MeshyTask, StageError, StageId, StageStatus
from p2p_worker import lifecycle, offload
from p2p_worker.errors import VendorError
from p2p_worker.local import AutoResponder, run_job
from p2p_worker.stages import RUNNERS
from p2p_worker.stages.base import Stage

S01 = StageId.S01_INTAKE


def events(caplog, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == name]


class Flaky(Stage):
    """Wraps a stage runner: the first `n` runs do `bad(ctx)` instead (hang, raise)."""

    def __init__(self, inner: Stage, bad, n: int = 1):
        self.id = inner.id
        self.inner, self.bad, self.n, self.runs = inner, bad, n, 0

    async def run(self, ctx):
        self.runs += 1
        if self.runs <= self.n:
            await self.bad(ctx)
        await self.inner.run(ctx)


async def hang(ctx):
    await asyncio.Event().wait()


@pytest.fixture
def fast_watch(monkeypatch):
    monkeypatch.setattr(lifecycle, "WATCH_TICK_S", 0.02)
    monkeypatch.setattr(lifecycle, "CANCEL_GRACE_S", 1.0)
    monkeypatch.setitem(lifecycle.STAGE_LIMITS, S01, lifecycle.Limits(0.25, 30.0))


# ---------------------------------------------------------------- limits and the watchdog
def test_a_hung_stage_is_cancelled_and_replays_its_attempt(settings, photos, fast_watch,
                                                           monkeypatch, caplog):
    """A stage with no progress for its idle limit is cancelled and fails as vendor_timeout,
    retryable; the orchestrator replays the same attempt once and the job goes on."""
    caplog.set_level(logging.INFO)
    rt = make_rt(settings)
    flaky = Flaky(RUNNERS[S01], hang)
    monkeypatch.setitem(RUNNERS, S01, flaky)
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    t0 = time.monotonic()
    assert run(rt.orchestrator.advance(job.id)) == JobStatus.WAITING_USER
    assert 0.25 < time.monotonic() - t0 < 10
    m = rt.store.load_manifest(job.id, S01, 1)
    assert m.status == StageStatus.WAITING_USER and flaky.runs == 2
    assert [r["kind"] for r in m.data["resumes"]] == ["timeout"]
    assert m.data["resumes"][0]["code"] == "vendor_timeout"
    assert "made no progress" in m.data["resumes"][0]["error"]
    assert events(caplog, "stage_timeout") and events(caplog, "stage_retry")
    run(rt.fake.drain())


def test_a_stage_that_always_hangs_pauses_after_its_retries(settings, photos, fast_watch,
                                                            monkeypatch):
    rt = make_rt(settings)
    monkeypatch.setitem(RUNNERS, S01, Flaky(RUNNERS[S01], hang, n=99))
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    assert run(rt.orchestrator.advance(job.id)) == JobStatus.PAUSED
    job = rt.store.load_job(job.id)
    assert job.attempts[S01] == 3  # each attempt: one run, one replay, then a fresh one
    reason = rt.store.job_row(job.id)["pause_reason"]
    assert "failed 3 times" in reason and "made no progress" in reason
    run(rt.fake.drain())


def test_time_waiting_on_a_webhook_is_not_counted(settings):
    info = lifecycle.RunInfo(job_id="j", stage="s05_mesh", attempt=1,
                             limits=lifecycle.Limits(0.05, 0.05))
    with info.waiting():
        time.sleep(0.12)
        assert info.over() is None and info.idle_s() == 0.0
    assert info.over() is None and info.active_s() < 0.05
    time.sleep(0.08)
    assert info.over() == "idle"


def test_one_log_line_per_job_per_interval_while_a_stage_runs(settings, photos, fast_watch,
                                                              monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    async def slow(ctx):
        for _ in range(4):
            await asyncio.sleep(0.1)
            ctx.checkpoint("working")

    rt = make_rt(settings.with_(progress_log_s=0.1))
    monkeypatch.setitem(RUNNERS, S01, Flaky(RUNNERS[S01], slow))
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    run(rt.orchestrator.advance(job.id))
    lines = [r for r in events(caplog, "stage_running")
             if getattr(r, "ctx", {}).get("job_id") == job.id]
    assert 2 <= len(lines) <= 6
    assert lines[0].fields["last"] in ("working", "start") and "active_s" in lines[0].fields
    run(rt.fake.drain())


# ---------------------------------------------------------------- retries
def test_a_network_blip_replays_the_attempt_after_a_backoff(settings, photos, monkeypatch):
    from p2p_worker.clients.supabase import SupabaseError

    async def blip(ctx):
        try:
            raise httpx.RemoteProtocolError("Server disconnected")
        except httpx.RemoteProtocolError as e:
            raise SupabaseError("Supabase GET /rest/v1/messages: RemoteProtocolError") from e

    rt = make_rt(settings.with_(retry_backoff_s=(0.3,)))
    monkeypatch.setitem(RUNNERS, S01, Flaky(RUNNERS[S01], blip))
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    assert run(rt.orchestrator.advance(job.id)) == JobStatus.RUNNING  # waiting out the backoff
    wait = rt.orchestrator.retry_wait_s(job.id)
    assert 0 < wait <= 0.3
    m = rt.store.load_manifest(job.id, S01, 1)
    assert m.status == StageStatus.FAILED and m.data["retry"]["mode"] == "resume"
    t0 = time.monotonic()
    assert run(run_job(rt, job.id, None)) == JobStatus.WAITING_USER
    assert time.monotonic() - t0 >= wait - 0.05
    m = rt.store.load_manifest(job.id, S01, 1)
    assert m.status == StageStatus.WAITING_USER and m.data["resumes"][0]["kind"] == "transient"
    assert rt.store.load_job(job.id).attempts[S01] == 1
    run(rt.fake.drain())


def test_a_failed_claude_call_replays_the_attempt(settings, photos):
    """A Claude call that fails leaves the attempt good: its Meshy tasks and gate replies
    stand, so it replays instead of starting over."""
    from p2p_worker.clients.claude import MockClaude

    class Once(MockClaude):
        failed = False

        async def check_concept(self, req):
            if not Once.failed:
                Once.failed = True
                raise VendorError("claude -p failed (exit 1): overloaded", retryable=True)
            return await super().check_concept(req)

    rt = make_rt(settings, claude=Once())
    job_id, status = _drive(rt, photos)
    assert status == JobStatus.DONE and Once.failed
    m3 = rt.store.load_manifest(job_id, StageId.S03_CONCEPT, 1)
    assert [r["kind"] for r in m3.data["resumes"]] == ["claude"]
    assert rt.store.load_job(job_id).attempts[StageId.S03_CONCEPT] == 1
    assert len(m3.meshy_tasks) == 4  # 2 figures, 2 loop edits: bought once, both kept
    assert rt.fake.count("POST", "image-to-image") == 5  # and s04's views
    assert len(rt.store.list_messages(job_id, kinds={"concepts"})[-1].payload["candidates"]) == 2


def _drive(rt, photos, **kw):
    from p2p_worker.local import run_local

    async def go():
        try:
            return await run_local(rt, photos[:1], "keychain of my dog", auto=True,
                                   product_type="keychain", **kw)
        finally:
            await rt.fake.drain()

    job_id, status, _ = run(go())
    return job_id, status


def test_plan_retry_modes_and_backoff():
    now = datetime(2026, 9, 25, tzinfo=UTC)

    def failed(detail=None, retryable=True, resumes=()):
        m = Manifest(job_id="j", stage=S01, attempt=1, status=StageStatus.FAILED,
                     finished_at=now)
        m.error = StageError(code="internal", message="x", retryable=retryable,
                             detail=detail or {})
        m.data["resumes"] = list(resumes)
        return m

    back = (10.0, 60.0)
    p = lifecycle.plan_retry(failed({"transient": True}), replay_safe=True, failed_in_row=1,
                             backoff=back, now=now)
    assert (p.mode, p.at - now) == ("resume", timedelta(seconds=10))
    p = lifecycle.plan_retry(failed({"transient": True}, resumes=[{"kind": "transient"}] * 4),
                             replay_safe=True, failed_in_row=1, backoff=back, now=now)
    assert p.mode == "new"  # the replays are used up: a fresh attempt
    p = lifecycle.plan_retry(failed({"timeout": "idle"}), replay_safe=False, failed_in_row=1,
                             backoff=back, now=now)
    assert p.mode == "new"  # the paint agent cannot replay
    p = lifecycle.plan_retry(failed({}), replay_safe=True, failed_in_row=2, backoff=back,
                             now=now)
    assert (p.mode, p.at - now) == ("new", timedelta(seconds=60))
    assert lifecycle.plan_retry(failed({}), replay_safe=True, failed_in_row=3, backoff=back,
                                now=now).mode == "pause"
    assert lifecycle.plan_retry(failed({}, retryable=False), replay_safe=True, failed_in_row=1,
                                backoff=back, now=now).mode == "pause"
    assert lifecycle.failure_kind(failed({"resume": "claude"}).error) == "claude"


def test_a_new_attempt_adopts_paid_tasks_and_unknown_creates():
    now = datetime.now(UTC)

    def task(tid, status):
        return MeshyTask(task_id=tid, endpoint="openapi/v1/image-to-image", purpose="p",
                         status=status, test_mode=True, credits=9, created_at=now, request={})

    prev = Manifest(job_id="j", stage=StageId.S03_CONCEPT, attempt=1,
                    status=StageStatus.FAILED)
    prev.meshy_tasks = [task("t1", "SUCCEEDED"), task("t2", "IN_PROGRESS"), task("t3", "FAILED")]
    prev.data = {"meshy_keys": {"r1c1": "t1", "r1c2": "t2", "r1c1.p0": "t3"},
                 "meshy_pending": {"r1c2.p0": {"at": now.isoformat(), "endpoint": "x"}}}
    new = Manifest(job_id="j", stage=StageId.S03_CONCEPT, attempt=2, status=StageStatus.RUNNING)
    assert lifecycle.default_adopt(prev, new) == {"r1c1": "t1", "r1c2": "t2"}
    assert {t.task_id for t in new.meshy_tasks} == {"t1", "t2"}  # the failed task is not
    assert list(new.data["meshy_pending"]) == ["r1c2.p0"]  # looked up before any create


# ---------------------------------------------------------------- leases and interruptions
def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_an_attempt_left_running_by_a_dead_worker_replays_in_place(settings, photos, caplog):
    caplog.set_level(logging.INFO)
    rt = make_rt(settings)
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    m = rt.orchestrator._new_attempt(rt.store.load_job(job.id), S01)
    m.data["run"] = {"boot": "gone", "pid": _dead_pid(), "host": lifecycle.HOST,
                     "worker": "old", "last": "step:intake"}
    rt.store.write_manifest(m)
    assert run(rt.orchestrator.advance(job.id)) == JobStatus.WAITING_USER
    m = rt.store.load_manifest(job.id, S01, 1)
    assert [i["last"] for i in m.data["interrupts"]] == ["step:intake"]
    assert m.data["run"]["boot"] == rt.orchestrator.boot
    assert events(caplog, "stage_interrupted")
    run(rt.fake.drain())


def test_an_attempt_held_by_a_live_worker_is_left_alone(settings, photos):
    rt = make_rt(settings)
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                              "p2p_worker"])
    try:
        m = rt.orchestrator._new_attempt(rt.store.load_job(job.id), S01)
        m.data["run"] = {"boot": "other", "pid": other.pid, "host": lifecycle.HOST,
                         "worker": "cli"}
        rt.store.write_manifest(m)
        assert run(rt.orchestrator.advance(job.id)) == JobStatus.RUNNING
        assert rt.store.load_manifest(job.id, S01, 1).status == StageStatus.RUNNING
    finally:
        other.kill()
        other.wait()
    assert run(rt.orchestrator.advance(job.id)) == JobStatus.WAITING_USER  # it is gone now
    run(rt.fake.drain())


def test_crash_loop_means_no_progress_between_deaths():
    assert not lifecycle.crash_loop(["a", "b", "c", "d", "e"])
    assert not lifecycle.crash_loop(["x", "a", "a", "a"])
    assert lifecycle.crash_loop(["x", "a", "a", "a", "a"])
    assert not lifecycle.crash_loop([None] * 5)  # died before recording anything
    assert lifecycle.crash_loop([None] * 11, cap=10)


def test_a_stage_that_kills_the_worker_at_the_same_point_gets_a_fresh_attempt(settings,
                                                                               photos):
    rt = make_rt(settings)
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    m = rt.orchestrator._new_attempt(rt.store.load_job(job.id), S01)
    for _ in range(4):
        m.data.setdefault("interrupts", []).append({"last": "step:intake"})
    m.data["interrupts"].pop()
    m.data["run"] = {"boot": "gone", "pid": _dead_pid(), "host": lifecycle.HOST,
                     "last": "step:intake"}
    rt.store.write_manifest(m)
    assert run(rt.orchestrator.advance(job.id)) == JobStatus.WAITING_USER
    a1 = rt.store.load_manifest(job.id, S01, 1)
    assert a1.status == StageStatus.FAILED and a1.error.detail["crash_loop"] is True
    assert rt.store.load_job(job.id).attempts[S01] == 2
    run(rt.fake.drain())


# ---------------------------------------------------------------- child processes
def test_cancelling_a_heavy_call_kills_its_child():
    async def go():
        task = asyncio.create_task(offload.run_cpu(time.sleep, 60))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if offload.CHILDREN:
                break
        pid = next(iter(offload.CHILDREN))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pid

    pid = run(go())
    assert not offload.CHILDREN
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_child_that_dies_fails_the_call_retryable():
    with pytest.raises(VendorError) as e:
        run(offload.run_cpu(os._exit, 7))
    assert e.value.retryable and e.value.detail == {"child_died": True, "exitcode": 7}
    assert lifecycle.failure_kind(StageError(code="internal", message=str(e.value),
                                             retryable=True, detail=e.value.detail)) == "child"


def test_a_child_exits_when_its_worker_is_killed(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text(
        "import asyncio, os, time\n"
        "from p2p_worker import offload\n"
        "async def main():\n"
        "    asyncio.create_task(offload.run_cpu(time.sleep, 60))\n"
        "    for _ in range(400):\n"
        "        await asyncio.sleep(0.05)\n"
        "        if offload.CHILDREN:\n"
        "            break\n"
        "    print(next(iter(offload.CHILDREN)), flush=True)\n"
        "    os._exit(9)  # the worker dies with no cleanup, as under SIGKILL\n"
        "if __name__ == '__main__':\n"
        "    asyncio.run(main())\n")
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                         timeout=60, check=False)
    pid = int(out.stdout.strip().splitlines()[-1])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise AssertionError(f"child {pid} outlived its worker")


# ---------------------------------------------------------------- keep-awake, clock, health
class FakeProc:
    def __init__(self, cmd):
        self.cmd, self.alive, self.pid = cmd, True, 4242

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False

    def wait(self, timeout=None):
        return 0


def test_keep_awake_holds_caffeinate_only_while_a_job_is_active():
    from p2p_worker.watchdog import KeepAwake

    spawned: list[FakeProc] = []
    ka = KeepAwake(True, pid=1234, spawn=lambda cmd: spawned.append(FakeProc(cmd))
                   or spawned[-1])
    ka.set(True, "j1 running")
    ka.set(True, "j1 running")
    assert len(spawned) == 1 and spawned[0].cmd == ["caffeinate", "-i", "-w", "1234"]
    assert ka.status()["held"] is True
    spawned[0].alive = False  # caffeinate died while still needed: a new one
    ka.set(True, "j1 running")
    assert len(spawned) == 2
    ka.set(False)
    assert not spawned[1].alive and ka.status()["held"] is False
    assert KeepAwake(False, spawn=lambda cmd: FakeProc(cmd)).status()["enabled"] is False


class FakeScheduler:
    def __init__(self, active=(False, "")):
        self.kicked: list[str] = []
        self._active = active

    def kick(self, job_id):
        self.kicked.append(job_id)

    def running(self, job_id):
        return False

    def active(self):
        return self._active

    def statuses(self):
        return {}


def test_a_wall_clock_jump_is_a_wake_that_resyncs_every_job(settings, photos, caplog):
    from p2p_worker.watchdog import KeepAwake, Watchdog

    caplog.set_level(logging.INFO)
    rt = make_rt(settings)
    a = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    b = rt.orchestrator.create_job("figurine of my cat", photos[:1])
    rt.store.save_job(rt.store.load_job(b.id).model_copy(update={"status": JobStatus.DONE}))
    rt.store.post_message(b.id, "user", "palette_edit", {"mode": "refit"})  # sent while asleep

    class Listener:
        reconnects = 0

        def reconnect(self):
            Listener.reconnects += 1

        def status(self):
            return {"connected": True}

    sched = FakeScheduler(active=(True, "a running"))
    spawned = []
    wd = Watchdog(rt, sched, listener=Listener(), tick_s=5.0,
                  keep_awake=KeepAwake(True, spawn=lambda c: spawned.append(FakeProc(c))
                                       or spawned[-1]))
    wd.clock.wall -= 9 * 3600  # the Mac slept 9 h: the wall clock moved, the monotonic did not
    wd.step()
    assert Listener.reconnects == 1 and wd.wakes == 1
    assert set(sched.kicked) == {a.id, b.id}  # the active job and the one with a reply waiting
    assert events(caplog, "clock_jump")[0].fields["slept_s"] >= 9 * 3600 - 10
    assert spawned  # a job is active: caffeinate held
    health = wd.health()
    assert health["clock"]["last_jump"]["slept_s"] >= 9 * 3600 - 10 and health["ok"]
    run(rt.fake.drain())


def test_a_webhook_that_landed_while_the_mac_slept_is_picked_up_on_wake(settings, photos):
    """The job waits on Meshy; the tasks finish and their meshy_events rows land while the Mac
    is asleep (nothing kicks the job). The wake resync advances it: one confirming GET per
    webhook, no backup status read."""
    from p2p_worker.app import Scheduler
    from p2p_worker.fakes.meshy import FakeMeshy
    from p2p_worker.store import LocalStore
    from p2p_worker.watchdog import Watchdog

    s = settings.with_(vendor_wait_s=0.5)
    store = LocalStore(s.jobs_dir)
    fake = FakeMeshy(auto_complete=False, fixtures_dir=None, delay_s=0.005,
                     on_event=lambda ev: store.insert_meshy_event(ev))
    rt = make_rt(s, store=store, fake=fake)
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    assert run(run_job(rt, job.id, AutoResponder(), max_steps=3)) == JobStatus.RUNNING
    m3 = rt.store.load_manifest(job.id, StageId.S03_CONCEPT, 1)
    assert m3.status == StageStatus.WAITING_VENDOR
    for t in list(fake.tasks):  # Meshy finishes while the worker sleeps
        fake.complete(t)
    fake.auto_complete = True

    async def wake():
        sched = Scheduler(rt)
        wd = Watchdog(rt, sched)
        wd.clock.wall -= 3600
        wd.step()
        await sched.idle()
        await fake.drain()

    run(wake())
    m3 = rt.store.load_manifest(job.id, StageId.S03_CONCEPT, 1)
    assert m3.status == StageStatus.WAITING_USER  # the concepts gate: it moved on
    for tid, ep in fake.endpoint_of.items():
        assert fake.count("GET", ep, task_id=tid) == 1  # the confirming GET only
    assert not m3.data.get("meshy_status_reads")


def test_health_reports_running_stages_and_problems(settings, photos, fast_watch,
                                                    monkeypatch):
    from fastapi.testclient import TestClient
    from p2p_worker.app import Scheduler, create_app
    from p2p_worker.watchdog import Watchdog

    rt = make_rt(settings)
    sched = Scheduler(rt)
    wd = Watchdog(rt, sched)
    info = lifecycle.start_run("job-x", StageId.S06_PRINTABILITY, 2,
                               lifecycle.Limits(3600, 7200))
    info.touch("heavy:build:start")
    try:
        with TestClient(create_app(rt, sched, wd)) as client:
            h = client.get("/health").json()
            assert h["ok"] is True and h["pid"] == os.getpid()
            st = h["stages"][0]
            assert (st["job_id"], st["stage"], st["attempt"], st["last"]) == (
                "job-x", "s06_printability", 2, "heavy:build:start")
            info.limits = lifecycle.Limits(0.0, 0.0)
            time.sleep(0.01)
            h = client.get("/health").json()
            assert h["ok"] is False and "over its idle limit" in h["problems"][0]
            assert client.get("/healthz").json()["ok"] is True
    finally:
        lifecycle.end_run(info)


def test_scheduler_says_when_the_mac_must_stay_awake(settings):
    from p2p_worker.app import Scheduler

    rt = make_rt(settings.with_(keep_awake_user_s=60.0))
    sched = Scheduler(rt)
    assert sched.active() == (False, "")
    sched._status["a"] = (JobStatus.WAITING_USER, time.monotonic())
    assert sched.active()[0]  # the customer was just asked
    sched._status["a"] = (JobStatus.WAITING_USER, time.monotonic() - 120)
    assert not sched.active()[0]
    sched._status["b"] = (JobStatus.RUNNING, time.monotonic() - 9999)  # waiting on Meshy
    assert sched.active() == (True, "b running")


# ---------------------------------------------------------------- Supabase writes
class Postgrest:
    """PostgREST enough for the idempotent writes: ignore-duplicates on id, JSON-path and
    null filters, and a switch that drops the connection after the server applied a write."""

    def __init__(self):
        self.tables: dict[str, list[dict]] = {t: [] for t in (
            "jobs", "stage_runs", "messages", "spend", "meshy_events", "workers")}
        self.drop_after: dict[str, int] = {}  # "METHOD table" -> writes to drop the reply of
        self.requests: list[tuple[str, str]] = []

    def match(self, row, params):
        for k, v in params.items():
            if k in ("select", "order", "limit", "on_conflict"):
                continue
            cur = row
            if "->>" in k:
                col, _, key = k.partition("->>")
                cur = (row.get(col) or {}).get(key)
            else:
                cur = row.get(k)
            op, _, val = v.partition(".")
            if op == "eq" and str(cur) != val:
                return False
            if op == "is" and cur is not None:
                return False
            if op == "in" and str(cur) not in val.strip("()").split(","):
                return False
            if op == "gte" and str(cur) < val:
                return False
        return True

    def handler(self, req: httpx.Request) -> httpx.Response:
        table = req.url.path.split("/rest/v1/", 1)[1]
        params = dict(req.url.params)
        self.requests.append((req.method, table))
        rows = self.tables[table]
        if req.method == "POST":
            body = json.loads(req.content)
            prefer = req.headers.get("prefer", "")
            out = []
            for it in body if isinstance(body, list) else [body]:
                it = dict(it)
                if "id" in it and any(r.get("id") == it["id"] for r in rows):
                    if "ignore-duplicates" in prefer:
                        continue
                    return httpx.Response(409, json={"message": "duplicate key"})
                it.setdefault("id", len(rows) + 1)
                it.setdefault("created_at", datetime.now(UTC).isoformat())
                it.setdefault("received_at", datetime.now(UTC).isoformat())
                it.setdefault("consumed_at", None)
                it.setdefault("processed_at", None)
                rows.append(it)
                out.append(it)
            key = f"POST {table}"
            if self.drop_after.get(key):
                self.drop_after[key] -= 1
                raise httpx.RemoteProtocolError("Server disconnected", request=req)
            return httpx.Response(201, json=out)
        hit = [r for r in rows if self.match(r, params)]
        if req.method == "PATCH":
            for r in hit:
                r.update(json.loads(req.content))
            key = f"PATCH {table}"
            if self.drop_after.get(key):
                self.drop_after[key] -= 1
                raise httpx.RemoteProtocolError("Server disconnected", request=req)
            return httpx.Response(204)
        return httpx.Response(200, json=hit)


@pytest.fixture
def sb(settings, monkeypatch):
    from p2p_worker.clients import supabase

    monkeypatch.setattr(supabase, "BACKOFF_S", (0.0, 0.0, 0.0))
    pg = Postgrest()
    pg.tables["jobs"].append({"id": "job-1", "owner_id": "u1"})
    s = settings.with_(supabase_url="https://p.supabase.co", supabase_service_key="k")
    store = supabase.SupabaseStore(s, transport=httpx.MockTransport(pg.handler))
    return store, pg


def test_a_message_insert_is_safe_to_repeat(sb):
    store, pg = sb
    pg.drop_after["POST messages"] = 1  # the row lands, the reply is lost
    row = store.post_message("job-1", "assistant", "note", {"text": "hi"})
    assert len(pg.tables["messages"]) == 1 and pg.tables["messages"][0]["id"] == row.id
    again = store.post_message("job-1", "assistant", "note", {"text": "hi"},
                               message_id=row.id)  # a replay after a crash
    assert again.id == row.id and len(pg.tables["messages"]) == 1
    lines = (store.job_dir("job-1") / "messages.jsonl").read_text().splitlines()
    assert len(lines) == 1


def test_a_spend_row_is_inserted_once_after_a_dropped_reply(sb):
    from p2p_contracts import SpendEvent

    store, pg = sb
    pg.drop_after["POST spend"] = 1
    store.record_spend(SpendEvent(at=datetime.now(UTC), job_id="job-1", vendor="meshy",
                                  kind="image-to-image", credits=12, usd=0.24, ref="t1",
                                  test_mode=True), billing="credits")
    assert len(pg.tables["spend"]) == 1  # found after the drop, not inserted again
    assert ("GET", "spend") in pg.requests


def test_a_status_read_event_is_inserted_once_after_a_dropped_reply(sb):
    store, pg = sb
    pg.drop_after["POST meshy_events"] = 1
    eid = store.insert_meshy_event({"id": "t9", "status": "SUCCEEDED"}, source="status_get")
    assert len(pg.tables["meshy_events"]) == 1 and eid == pg.tables["meshy_events"][0]["id"]


def test_consume_and_processed_keep_the_first_time_and_retry(sb):
    store, pg = sb
    row = store.post_message("job-1", "user", "answers", {"answers": []})
    pg.drop_after["PATCH messages"] = 1  # applied, reply lost: repeated safely
    store.consume_message("job-1", row.id)
    first = pg.tables["messages"][0]["consumed_at"]
    assert first
    store.consume_message("job-1", row.id)
    assert pg.tables["messages"][0]["consumed_at"] == first


def test_pending_work_lists_jobs_with_unconsumed_replies_or_events(sb, settings):
    store, pg = sb
    store.post_message("job-1", "user", "resume", {})
    pg.tables["meshy_events"].append({"id": 5, "task_id": "t5", "status": "SUCCEEDED",
                                      "event": {}, "processed_at": None,
                                      "received_at": datetime.now(UTC).isoformat()})
    store.index_task("t5", "job-2")
    assert store.pending_work() == {"job-1", "job-2"}


def test_realtime_reconciles_after_every_join():
    from p2p_worker.clients.supabase import RealtimeListener

    joins = []
    lst = RealtimeListener("https://p.supabase.co", "k", ["messages"], lambda *a: None,
                           on_connect=lambda: joins.append(1))
    ref = lst.join_message()["ref"]
    reply = {"event": "phx_reply", "topic": "realtime:p2p-worker", "ref": ref,
             "payload": {"status": "ok"}}
    lst.handle(json.dumps(reply))
    lst.handle(json.dumps({"event": "phx_reply", "topic": "realtime:p2p-worker", "ref": "99",
                           "payload": {"status": "ok"}}))  # not a new join
    assert joins == [1] and lst.status()["connected"] and lst.joins == 1
    lst.connected.clear()  # the socket dropped; run() joins again
    reply["ref"] = lst.join_message()["ref"]
    lst.handle(json.dumps(reply))
    assert joins == [1, 1] and lst.joins == 2


def test_local_store_dedupes_message_ids_and_derives_spend_from_the_ledger(settings):
    from p2p_contracts import JobRecord
    from p2p_worker.budget import BudgetGuard
    from p2p_worker.store import LocalStore, message_id_for, message_scope

    store = LocalStore(settings.jobs_dir)
    job = JobRecord(id="j1", created_at=datetime.now(UTC), prompt="x")
    store.save_job(job)
    with message_scope("j1/s01/a1/step:x"):
        a = store.post_message("j1", "assistant", "note", {"text": "1"})
    with message_scope("j1/s01/a1/step:x"):  # the same step replayed
        b = store.post_message("j1", "assistant", "note", {"text": "1"})
    assert a.id == b.id == message_id_for("j1/s01/a1/step:x#1")
    assert len(store.list_messages("j1")) == 1
    g = BudgetGuard(store, settings)
    for _ in range(2):  # booked, then booked again by a replay: once
        g.record(job, stage=None, vendor="meshy", kind="image-to-image", credits=12,
                 usd=0.24, ref="t1", test_mode=True)
    assert store.job_spend("j1") == (0.24, 12)
    assert store.load_job("j1").budget.spent_credits == 12
    stale = store.load_job("j1")
    stale.budget.spent_credits = 0  # a job save lost after the ledger row: healed next time
    g.record(stale, stage=None, vendor="meshy", kind="refund:image-to-image", credits=-12,
             usd=-0.24, ref="t1", test_mode=True)
    assert store.load_job("j1").budget.spent_credits == 0 and store.job_spend("j1")[1] == 0


def test_settings_load_the_lifecycle_values(tmp_path, monkeypatch):
    from p2p_worker.config import Settings

    monkeypatch.setenv("P2P_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("P2P_RETRY_BACKOFF_S", "5,30")
    monkeypatch.setenv("KEEP_AWAKE", "0")
    s = Settings.load()
    assert s.retry_backoff_s == (5.0, 30.0) and s.keep_awake is False
    assert s.progress_log_s == 60.0 and s.stage_limit_scale == 1.0
    monkeypatch.delenv("KEEP_AWAKE")
    assert Settings.load().keep_awake is (sys.platform == "darwin")
    assert Settings(repo_root=Path("."), jobs_dir=Path(".")).retry_backoff_s == (0.0,)
