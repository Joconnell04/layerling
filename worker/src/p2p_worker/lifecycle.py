"""Stage lifecycle: time limits, the per-run watchdog record, run leases and retry plans.

What went wrong in live runs (docs/runs/live2): a Mac in lid-closed sleep for 9 h, a dropped
Supabase connection that failed a stage while it waited on a paid task, heavy steps that
silenced the event loop, a paint Claude call with no time limit, and jobs that needed a
hand-run mark_stale script and a worker restart to move again.

- Limits (STAGE_LIMITS): per stage, `idle_s` (no progress for that long) and `total_s` (active
  time in one run). Progress is a StageContext checkpoint (a step, a Claude call, a Meshy create
  or webhook, a file fetch, a gate), a heavy build starting or ending, or a line from the paint
  child's log. Time a stage spends waiting on a webhook in-process is not active time. Both
  limits read the monotonic clock, which stops while the Mac sleeps, so a closed lid never
  trips them. Every value is ASSUMED: a few times the longest run on record.
- RunInfo: one per running stage (RUNS, by job id). The orchestrator's supervisor checks it every
  WATCH_TICK_S, cancels a run over a limit (the child process or claude -p under it is killed)
  and logs one `stage_running` line per job per minute; /health reads it.
- Lease (`data.run` on the manifest): the worker process that runs the attempt. A RUNNING
  attempt whose worker is gone was interrupted. It replays in place (its steps are cached),
  except a stage that cannot replay (the paint agent: its conversation died with the worker),
  or one that keeps killing the worker at the same point (`data.run.last`, the last
  checkpoint, unchanged across MAX_INTERRUPTS + 1 deaths): a crash loop gets a fresh attempt,
  then the usual retry limit.
- Retry plans for a FAILED attempt: `resume` the same attempt when the failure left its state
  good (a network blip, a timeout, an interruption, a failed Claude call: nothing paid is lost,
  gate replies stand), `new` attempt when the attempt's own result failed (a Meshy task failed, a
  bug), else `pause`. Tries wait Settings.retry_backoff_s between them (wall clock, so a retry
  due during a sleep runs on wake).
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from p2p_contracts import Manifest, StageId
from p2p_contracts.job import MAX_STAGE_RETRIES

from .errors import is_transient_message
from .util import utcnow

MIN = 60.0
WATCH_TICK_S = 5.0  # how often the supervisor looks at a running stage
CANCEL_GRACE_S = 30.0  # how long a cancelled run gets to unwind
HOST = socket.gethostname()


@dataclass(frozen=True)
class Limits:
    idle_s: float  # longest stretch with no progress
    total_s: float  # longest active time in one run


# ASSUMED, from the longest runs on record (SOURCE for those: live2 worker log and manifests):
# s06 on the 1.29M-face sombrero took 8.9 min; a paint turn has 45 min of its own
# (p2p_mcp_paint TURN_TIMEOUT_S) and the longest turn logged is 965 s; s07 with the Bambu gate
# 68 s; a Haiku intake 7 s (130 s once); claude -p calls have 300 s each.
STAGE_LIMITS: dict[StageId, Limits] = {
    StageId.S01_INTAKE: Limits(15 * MIN, 30 * MIN),
    StageId.S02_COLOR_SPEC: Limits(20 * MIN, 45 * MIN),
    StageId.S03_CONCEPT: Limits(20 * MIN, 90 * MIN),
    StageId.S04_MULTIVIEW: Limits(20 * MIN, 60 * MIN),
    StageId.S05_MESH: Limits(20 * MIN, 60 * MIN),
    StageId.S06_PRINTABILITY: Limits(60 * MIN, 90 * MIN),
    StageId.S07_BASELINE: Limits(45 * MIN, 90 * MIN),
    StageId.S08_PALETTE: Limits(30 * MIN, 60 * MIN),
    StageId.S09_PAINT: Limits(60 * MIN, 6 * 60 * MIN),
    StageId.S10_WRITE: Limits(45 * MIN, 90 * MIN),
}

# Replays of the same attempt per failure kind before a fresh attempt is tried (ASSUMED).
RESUME_BUDGET = {"transient": 4, "timeout": 1, "interrupted": 3, "claude": 2, "child": 2}
# A crash loop: the worker died this many times in a row at the same point of an attempt (no
# checkpoint reached in between), or this many times in all (ASSUMED).
MAX_INTERRUPTS = RESUME_BUDGET["interrupted"]
MAX_INTERRUPTS_TOTAL = 50
# A stage that cannot replay gets a fresh attempt per interruption; this many in a row pause it.
MAX_FRESH_INTERRUPTS = 10
# A lease from another host counts as abandoned after this long (one worker per job is the
# deployment; this only guards a second machine by mistake).
LEASE_STALE_S = 6 * 60 * MIN


def limits_for(stage: StageId, settings: Any) -> Limits:
    base = STAGE_LIMITS.get(stage, Limits(30 * MIN, 60 * MIN))
    k = float(getattr(settings, "stage_limit_scale", 1.0) or 1.0)
    return Limits(base.idle_s * k, base.total_s * k)


# ---------------------------------------------------------------- running stages
@dataclass
class RunInfo:
    job_id: str
    stage: str
    attempt: int
    limits: Limits
    started: float = field(default_factory=time.monotonic)
    started_at: str = field(default_factory=lambda: utcnow().isoformat())
    last: float = field(default_factory=time.monotonic)
    last_what: str = "start"
    waited: float = 0.0
    waiting_since: float | None = None
    logged: float = field(default_factory=time.monotonic)

    def touch(self, what: str) -> None:
        self.last = time.monotonic()
        self.last_what = str(what)[:200]

    @contextlib.contextmanager
    def waiting(self) -> Iterator[None]:
        """Time in here is waiting on a vendor (a webhook), not work: not active, not idle."""
        self.waiting_since = time.monotonic()
        try:
            yield
        finally:
            now = time.monotonic()
            self.waited += now - (self.waiting_since or now)
            self.waiting_since = None
            self.last = now

    def active_s(self, now: float | None = None) -> float:
        now = now or time.monotonic()
        wait = self.waited + (now - self.waiting_since if self.waiting_since else 0.0)
        return max(0.0, now - self.started - wait)

    def idle_s(self, now: float | None = None) -> float:
        now = now or time.monotonic()
        return 0.0 if self.waiting_since else max(0.0, now - self.last)

    def over(self, now: float | None = None) -> Literal["idle", "total"] | None:
        now = now or time.monotonic()
        if self.idle_s(now) > self.limits.idle_s:
            return "idle"
        if self.active_s(now) > self.limits.total_s:
            return "total"
        return None

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        return {"job_id": self.job_id, "stage": self.stage, "attempt": self.attempt,
                "started_at": self.started_at, "active_s": round(self.active_s(now), 1),
                "idle_s": round(self.idle_s(now), 1), "waiting": self.waiting_since is not None,
                "last": self.last_what, "idle_limit_s": self.limits.idle_s,
                "total_limit_s": self.limits.total_s}


RUNS: dict[str, RunInfo] = {}


def start_run(job_id: str, stage: StageId, attempt: int, limits: Limits) -> RunInfo:
    info = RunInfo(job_id=job_id, stage=stage.value, attempt=attempt, limits=limits)
    RUNS[job_id] = info
    return info


def end_run(info: RunInfo) -> None:
    if RUNS.get(info.job_id) is info:
        del RUNS[info.job_id]


def timeout_message(info: RunInfo, over: str) -> str:
    if over == "idle":
        return (f"{info.stage} made no progress for {info.idle_s() / 60:.0f} min (limit "
                f"{info.limits.idle_s / 60:.0f} min; last: {info.last_what})")
    return (f"{info.stage} ran {info.active_s() / 60:.0f} min without finishing (limit "
            f"{info.limits.total_s / 60:.0f} min; last: {info.last_what})")


# ---------------------------------------------------------------- leases
def lease(boot: str, worker_id: str) -> dict[str, Any]:
    return {"boot": boot, "pid": os.getpid(), "host": HOST, "worker": worker_id,
            "at": utcnow().isoformat()}


def _pid_is_worker(pid: int) -> bool:
    """A live process with that pid that is a p2p_worker (a recycled pid is not)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5, check=False).stdout
    except Exception:  # noqa: BLE001 cannot tell: assume it is alive
        return True
    return "p2p_worker" in cmd


def lease_state(m: Manifest, boot: str) -> Literal["none", "ours", "gone", "alive"]:
    """Who holds a RUNNING attempt: nobody recorded (older manifests), this worker, a worker
    that is gone (interrupted), or another worker that is alive (leave it alone)."""
    run = m.data.get("run")
    if not isinstance(run, dict) or not run.get("boot"):
        return "none"
    if run["boot"] == boot:
        return "ours"
    if run.get("host") == HOST:
        pid = run.get("pid")
        if not isinstance(pid, int) or pid == os.getpid() or not _pid_is_worker(pid):
            return "gone"
        return "alive"
    try:
        age = (utcnow() - datetime.fromisoformat(run.get("at"))).total_seconds()
    except (TypeError, ValueError):
        return "gone"
    return "gone" if age > LEASE_STALE_S else "alive"


# ---------------------------------------------------------------- retries
FailureKind = Literal["transient", "timeout", "interrupted", "claude", "child"]


def failure_kind(err: Any) -> FailureKind | None:
    """Failures that leave the attempt's state good, so the same attempt can replay."""
    if err is None:
        return None
    d = err.detail or {}
    if d.get("interrupted"):
        return "interrupted"
    if d.get("timeout"):
        return "timeout"
    if d.get("child_died"):
        return "child"
    if d.get("transient") or is_transient_message(err.message):
        return "transient"
    if d.get("resume") in RESUME_BUDGET:
        return d["resume"]
    return None


@dataclass
class RetryPlan:
    mode: Literal["resume", "new", "pause"]
    at: datetime
    n: int  # the try this will be (1 = first retry)
    why: str

    def json(self) -> dict[str, Any]:
        return {"mode": self.mode, "at": self.at.isoformat(), "n": self.n, "why": self.why}


def backoff_s(backoff: tuple[float, ...] | list[float], n: int) -> float:
    if not backoff:
        return 0.0
    return float(backoff[min(max(n, 1), len(backoff)) - 1])


def plan_retry(m: Manifest, *, replay_safe: bool, failed_in_row: int,
               backoff: tuple[float, ...], now: datetime | None = None) -> RetryPlan:
    """What to do with a FAILED attempt, and when (see the module docstring)."""
    now = now or utcnow()
    since = m.finished_at or now
    err = m.error
    if err is not None and not err.retryable:
        return RetryPlan("pause", now, 0, "not retryable")
    kind = failure_kind(err)
    resumes = [r for r in m.data.get("resumes") or [] if r.get("kind") == kind]
    crash_loop = bool(err is not None and (err.detail or {}).get("crash_loop"))
    if kind and replay_safe and not crash_loop and len(resumes) < RESUME_BUDGET[kind]:
        n = len(m.data.get("resumes") or []) + 1
        return RetryPlan("resume", since + timedelta(seconds=backoff_s(backoff, n)), n,
                         f"{kind}: replay attempt {m.attempt}")
    if failed_in_row > MAX_STAGE_RETRIES:
        return RetryPlan("pause", now, failed_in_row, "retry limit")
    return RetryPlan("new", since + timedelta(seconds=backoff_s(backoff, failed_in_row)),
                     failed_in_row, f"{kind or 'failed'}: fresh attempt")


def replay_safe(runner: Any, m: Manifest, settings: Any) -> bool:
    """Whether an interrupted or timed-out run may replay in the same attempt. A runner can
    say no (the paint agent: a replay would start from half-painted state)."""
    f = getattr(runner, "replay_safe", True)
    return bool(f(m, settings) if callable(f) else f)


# ---------------------------------------------------------------- paid tasks across attempts
UNUSABLE = {"FAILED", "CANCELED", "EXPIRED"}


def default_adopt(prev: Manifest, new: Manifest) -> dict[str, str]:
    """The TaskAdopter interface (Services.adopt_tasks; the credit guard may supply its own):
    carry `prev`'s paid Meshy tasks into `new`, a fresh attempt of the same stage after `prev`
    FAILED. Tasks in flight or succeeded move under the same step keys (meshy_keys and
    meshy_tasks), so the new attempt waits for them instead of paying again; a create whose
    outcome `prev` never learned (meshy_pending) moves too, so the new attempt looks it up
    before it creates. Returns {step key: task id}. Spend stays on `prev`."""
    usable = {t.task_id: t for t in prev.meshy_tasks if t.status not in UNUSABLE}
    mine = new.data.setdefault("meshy_keys", {})
    adopted: dict[str, str] = {}
    for key, tid in (prev.data.get("meshy_keys") or {}).items():
        if key in mine or tid not in usable:
            continue
        mine[key] = tid
        new.meshy_tasks = [t for t in new.meshy_tasks if t.task_id != tid] + [usable[tid]]
        adopted[key] = tid
    pending = new.data.setdefault("meshy_pending", {})
    for key, p in (prev.data.get("meshy_pending") or {}).items():
        if key not in mine and key not in pending:
            pending[key] = p
    if not pending:
        new.data.pop("meshy_pending", None)
    return adopted


def crash_loop(lasts: list[str | None], cap: int = MAX_INTERRUPTS_TOTAL) -> bool:
    """`lasts`: the last checkpoint of each death in a row, oldest first. A loop: the worker
    died MAX_INTERRUPTS + 1 times at the same recorded point, or more than `cap` times. A death
    before any recorded point says nothing about where it died, so it counts only to the cap."""
    if len(lasts) > cap:
        return True
    tail = lasts[-(MAX_INTERRUPTS + 1):]
    return len(tail) > MAX_INTERRUPTS and len(set(tail)) == 1 and tail[0] is not None
