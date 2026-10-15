"""serve's watch loop: sleep and clock jumps, keep-awake, reconcile, and what /health reports.

live2 catfish: the Mac went to lid-closed sleep on battery 2 s after a paint checkpoint and
woke 9 h 22 min later (SOURCE: `pmset -g log`). Nothing in the worker noticed.

- KeepAwake: while any job is active (a stage running, a job queued or running, which includes
  waiting on Meshy, or a job that started waiting on the customer less than keep_awake_user_s
  ago) serve holds `caffeinate -i -w <worker pid>`, so idle sleep cannot stop a job. `-w` ends
  the assertion when the worker dies, even by SIGKILL. A closed lid on battery still sleeps.
- ClockWatch: macOS stops the monotonic clock while it sleeps and the wall clock keeps going, so
  after a sleep the wall clock is ahead by the time asleep. A jump over JUMP_S is a wake: log
  `clock_jump`, drop the Realtime socket (it died during the sleep but may not know yet), and
  kick every active job and every job with work waiting in the tables. A job waiting on Meshy
  then reads meshy_events (the edge function kept writing while the Mac slept) and, when due,
  makes its one backup status read (the approved 10 minute rule), instead of timing out. Stage
  limits and paint turn limits use the monotonic clock, so the sleep counts against none of
  them. The same watch sees a stalled event loop (`loop_stall`).
- Reconcile, every RECONCILE_S: kick jobs whose stage is RUNNING with no worker on it, whose
  retry is due, or that are queued, so nothing waits on an event that already passed. After a
  restart, a Realtime reconnect and a wake it also kicks every job with an unconsumed user
  message or an unprocessed Meshy event (store.pending_work).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from p2p_contracts import JobStatus, StageStatus

from . import lifecycle
from .logs import log_event
from .util import utcnow

log = logging.getLogger("p2p_worker.watchdog")

TICK_S = 5.0
JUMP_S = 30.0  # wall clock ahead of the monotonic clock by more than this: the Mac slept
LAG_WARN_S = 2.0  # the loop woke this late: something blocked it
RECONCILE_S = 60.0
ACTIVE = {JobStatus.QUEUED, JobStatus.RUNNING}


class KeepAwake:
    """`caffeinate -i -w <pid>` while wanted. `spawn` is for tests."""

    def __init__(self, enabled: bool, *, pid: int | None = None,
                 spawn: Callable[[list[str]], Any] | None = None):
        self.cmd = ["caffeinate", "-i", "-w", str(pid or os.getpid())]
        self.spawn = spawn or (lambda cmd: subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL))
        self.enabled = enabled and (spawn is not None or shutil.which("caffeinate") is not None)
        self.proc: Any = None
        self.since: str | None = None
        self.why = ""

    @property
    def held(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def set(self, want: bool, why: str = "") -> None:
        if not self.enabled:
            return
        if want and not self.held:
            try:
                self.proc = self.spawn(self.cmd)
            except OSError as e:
                log.warning("caffeinate failed: %s", e)
                self.enabled = False
                return
            self.since, self.why = utcnow().isoformat(), why
            log_event("keep_awake", held=True, why=why, pid=getattr(self.proc, "pid", None))
        elif not want and self.held:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self.proc.kill()
            self.proc, self.since = None, None
            log_event("keep_awake", held=False)

    def close(self) -> None:
        self.set(False)

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "held": self.held, "since": self.since,
                "why": self.why if self.held else None}


class ClockWatch:
    """Wall clock against monotonic clock between ticks: a jump is sleep (or a clock step),
    extra monotonic time is a blocked loop."""

    def __init__(self) -> None:
        self.wall, self.mono = time.time(), time.monotonic()
        self.last_jump: dict[str, Any] | None = None
        self.lags: list[tuple[float, float]] = []  # (monotonic time, lag s)

    def tick(self, expected_s: float) -> tuple[float, float]:
        w, m = time.time(), time.monotonic()
        jump = (w - self.wall) - (m - self.mono)
        lag = max(0.0, (m - self.mono) - expected_s)
        self.wall, self.mono = w, m
        self.lags = [(t, x) for t, x in self.lags if m - t < 300] + [(m, lag)]
        return jump, lag

    def max_lag(self, window_s: float = 60.0) -> float:
        now = time.monotonic()
        return max([x for t, x in self.lags if now - t < window_s] or [0.0])


class Watchdog:
    def __init__(self, rt, scheduler, *, listener=None, keep_awake: KeepAwake | None = None,
                 tick_s: float = TICK_S, reconcile_s: float = RECONCILE_S,
                 jump_s: float = JUMP_S):
        self.rt = rt
        self.scheduler = scheduler
        self.listener = listener
        self.keep_awake = keep_awake or KeepAwake(False)
        self.tick_s = tick_s
        self.reconcile_s = reconcile_s
        self.jump_s = jump_s
        self.clock = ClockWatch()
        self.started = time.monotonic()
        self.last_reconcile = time.monotonic()
        self.wakes = 0

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            try:
                self.step()
            except Exception:  # a watch error must never stop serve
                log.exception("watchdog step failed")

    def step(self) -> None:
        jump, lag = self.clock.tick(self.tick_s)
        if lag > LAG_WARN_S:
            log_event("loop_stall", level=logging.WARNING, lag_ms=round(lag * 1000))
        if abs(jump) > self.jump_s:
            self.on_wake(jump)
        elif time.monotonic() - self.last_reconcile >= self.reconcile_s:
            self.reconcile("periodic")
        active, why = self.scheduler.active()
        self.keep_awake.set(active, why)

    def on_wake(self, jump_s: float) -> None:
        """After a sleep (or a clock step): fresh Realtime socket, then resync every job."""
        self.wakes += 1
        self.clock.last_jump = {"at": utcnow().isoformat(), "slept_s": round(jump_s)}
        log_event("clock_jump", level=logging.WARNING, slept_s=round(jump_s),
                  msg=f"wall clock jumped {jump_s / 60:.1f} min (sleep); resyncing jobs")
        if self.listener is not None:
            self.listener.reconnect()
        self.reconcile("wake", everything=True)

    def reconcile(self, why: str, *, everything: bool = False) -> list[str]:
        """Kick jobs that need it (see the module docstring). Returns the kicked ids."""
        self.last_reconcile = time.monotonic()
        store = self.rt.store
        jobs = set(store.list_jobs())
        if everything:
            try:
                jobs |= store.pending_work()
            except Exception:
                log.exception("pending_work failed")
        kicked = []
        for job_id in sorted(jobs):
            if self.scheduler.running(job_id):
                continue
            if everything or self.needs_kick(job_id):
                kicked.append(job_id)
                self.scheduler.kick(job_id)
        if kicked or why != "periodic":
            log_event("reconcile", why=why, jobs=len(jobs), kicked=kicked[:20])
        return kicked

    def needs_kick(self, job_id: str) -> str | None:
        """Why a job no worker is advancing should be advanced now, or None."""
        store = self.rt.store
        try:
            job = store.load_job(job_id)
        except KeyError:
            return None
        if job.status == JobStatus.QUEUED:
            return "queued"
        if job.status != JobStatus.RUNNING or job.stage is None:
            return None
        n = job.attempts.get(job.stage)
        m = store.load_manifest(job_id, job.stage, n) if n else None
        if m is None:
            return "no manifest"
        if m.status in (StageStatus.RUNNING, StageStatus.PENDING, StageStatus.STALE):
            return f"stage {m.status.value} with no worker on it"
        if m.status == StageStatus.FAILED:
            retry = m.data.get("retry") or {}
            try:
                due = retry and datetime.fromisoformat(retry["at"]) <= utcnow()
            except (KeyError, ValueError):
                due = True
            return "retry due" if (due or not retry) else None
        return None

    def health(self) -> dict[str, Any]:
        from . import __version__
        from .offload import children_of

        stages = [r.snapshot() for r in lifecycle.RUNS.values()]
        problems = []
        for r in lifecycle.RUNS.values():
            over = r.over()
            if over:
                problems.append(f"{r.job_id} {r.stage} over its {over} limit")
        lag = self.clock.max_lag()
        if lag > 10:
            problems.append(f"event loop blocked {lag:.0f} s in the last minute")
        rt_status = self.listener.status() if self.listener is not None else None
        if rt_status and not rt_status["connected"] and time.monotonic() - self.started > 120:
            problems.append("Realtime disconnected")
        s = self.rt.settings
        return {
            "ok": not problems, "problems": problems, "worker_id": s.worker_id,
            "version": __version__, "pid": os.getpid(), "boot": self.rt.orchestrator.boot,
            "uptime_s": round(time.monotonic() - self.started), "meshy_mode": s.meshy_mode,
            "stages": stages, "children": children_of(),
            "jobs": self.scheduler.statuses(),
            "retries": {j: round(self.rt.orchestrator.retry_wait_s(j), 1)
                        for j in list(self.rt.orchestrator.wake_at)},
            "realtime": rt_status, "keep_awake": self.keep_awake.status(),
            "clock": {"last_jump": self.clock.last_jump, "wakes": self.wakes,
                      "loop_lag_max_s": round(lag, 3)},
        }
