"""CPU-heavy stage work in a child process, so the event loop keeps serving heartbeats,
Realtime, webhooks and HTTP while geometry, paint, renders and the slicer run.

Why a process and not a thread: live2 sombrero s06 already ran build_product in
asyncio.to_thread, yet the log went silent for minutes, Realtime timed out and a Supabase
keep-alive dropped (the Bronco's s05 then failed on RemoteProtocolError). manifold3d's
CrossSection union holds the GIL: on the sombrero's 1,291,050-face mesh, projected_silhouette
took 83 s per call, 6 calls, 496 of the 516 s build, and a loop ticking every 50 ms next to that
thread saw gaps of up to 138 s (SOURCE: a scratch probe on a copy of the sombrero GLB). A child
process has its own GIL.

- run_cpu(fn, *args, **kw): fn(*args, **kw) in its own spawn-started child process. fn and its
  arguments must be picklable (module-level functions, bound methods of plain stage objects,
  paths, dicts, pydantic models). At most P2P_OFFLOAD_WORKERS (2) run at once.
- run_heavy(ctx, method, *args): a stage's blocking `_build(wctx, ...)` with a WorkCtx, the
  picklable slice of the StageContext that build code uses. Decisions the build records are
  replayed on the real context in the parent.
- Settings.offload: "process" (Settings.load default, env P2P_OFFLOAD) or "thread" (the
  dataclass default: tests, which also stub the slicer in this process only).
- FileLog: a picklable log callback for a child; tail_log() forwards its lines to the worker
  log while the child runs.

One process per call, not a pool, so a call can be stopped: the stage watchdog cancels a run
over its time limit (lifecycle.py), and cancelling run_cpu kills that child and everything it
started (its own process group: a claude CLI, the Bambu slicer). A pool can only be shut down
whole, and a hung child held its slot for good. A child also exits when the worker that started
it is gone (a watch thread sees its parent change), so a killed worker leaves no build running
that could write into the attempt folder while the restarted worker replays it. The cost is the
spawn and imports per call (2 to 4 s, ASSUMED), on builds that take tens of seconds to minutes;
memory goes back to the OS after every build.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import multiprocessing as mp
import os
import signal
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import VendorError

# Two heavy steps at once (ASSUMED): three live jobs overlapped in live2; each build holds one
# core and up to a few GB for a million-face mesh. P2P_OFFLOAD_WORKERS overrides.
MAX_WORKERS = 2
PARENT_CHECK_S = 1.0  # how often a child checks that its worker is still there

# pid -> what runs there (health endpoint, stage_running lines)
CHILDREN: dict[int, dict[str, Any]] = {}
JOB: contextvars.ContextVar[str | None] = contextvars.ContextVar("p2p_offload_job", default=None)
_slots: dict[int, asyncio.Semaphore] = {}
_procs: dict[int, Any] = {}


def _workers() -> int:
    try:
        return max(1, int(os.environ.get("P2P_OFFLOAD_WORKERS", MAX_WORKERS)))
    except ValueError:
        return MAX_WORKERS


def _slot() -> asyncio.Semaphore:
    loop_id = id(asyncio.get_running_loop())
    if loop_id not in _slots:
        _slots[loop_id] = asyncio.Semaphore(_workers())
    return _slots[loop_id]


def _kill_group(pid: int) -> None:
    for kill in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            kill()


def _watch_parent(parent: int) -> None:
    while True:
        time.sleep(PARENT_CHECK_S)
        if os.getppid() != parent:  # the worker died: take this group down with it
            with contextlib.suppress(Exception):
                os.killpg(0, signal.SIGKILL)
            os._exit(3)


def _child_main(conn, fn, args, kwargs, parent: int) -> None:
    with contextlib.suppress(Exception):
        os.setpgid(0, 0)  # its own group: a kill reaches what it started
    threading.Thread(target=_watch_parent, args=(parent,), daemon=True).start()
    try:
        out = ("ok", fn(*args, **kwargs))
    except BaseException as e:  # noqa: BLE001 sent back and raised in the worker
        out = ("err", e, traceback.format_exc()[-4000:])
    try:
        conn.send(out)
    except Exception as e:  # noqa: BLE001 an exception or result that does not pickle
        conn.send(("err", RuntimeError(f"{type(e).__name__} sending the result back: {e}; "
                                       f"{str(out[1])[:500]}"), ""))
    finally:
        conn.close()


def shutdown() -> None:
    """Kill every running child (serve's exit; tests)."""
    for pid in list(_procs):
        _kill_group(pid)


def children_of(job_id: str | None = None) -> list[dict[str, Any]]:
    now = time.monotonic()
    return [{"pid": pid, "fn": c["fn"], "running_s": round(now - c["started"], 1),
             "job_id": c.get("job_id")}
            for pid, c in CHILDREN.items() if job_id is None or c.get("job_id") == job_id]


async def run_cpu(fn, /, *args: Any, **kwargs: Any) -> Any:
    """fn(*args, **kwargs) in a child process. Cancelling the call kills the child. A child
    that dies (a crash in native code, the OS out of memory) fails the call retryable
    (detail child_died)."""
    name = getattr(fn, "__qualname__", str(fn))
    target = args[0] if name == "_call" and args else None
    label = getattr(target, "__qualname__", None) or name
    async with _slot():
        ctx = mp.get_context("spawn")
        recv, send = ctx.Pipe(duplex=False)
        proc = ctx.Process(target=_child_main, args=(send, fn, args, kwargs, os.getpid()),
                           name=f"p2p-offload:{label}")
        # start() pickles fn and its arguments into the child's pipe, which blocks until the
        # child reads them (after its imports) when they are large: not on the event loop.
        await asyncio.to_thread(proc.start)
        send.close()
        pid = proc.pid
        _procs[pid] = proc
        CHILDREN[pid] = {"fn": label, "started": time.monotonic(), "job_id": JOB.get()}
        try:
            try:
                msg = await asyncio.to_thread(recv.recv)
            except (EOFError, OSError) as e:
                await asyncio.to_thread(proc.join, 5)
                raise VendorError(
                    f"the worker's child process died while running {label} "
                    f"(exit {proc.exitcode})", code="internal", retryable=True,
                    detail={"child_died": True, "exitcode": proc.exitcode}) from e
        finally:
            if proc.is_alive():  # cancelled (a stage timeout, a shutdown) or it died oddly
                _kill_group(pid)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(proc.join, 5)
            recv.close()
            _procs.pop(pid, None)
            CHILDREN.pop(pid, None)
    if msg[0] == "ok":
        return msg[1]
    err = msg[1]
    if len(msg) > 2 and msg[2] and isinstance(err, BaseException):
        with contextlib.suppress(Exception):
            err.add_note(f"in the child process ({label}):\n{msg[2]}")
    raise err


# ---------------------------------------------------------------- stage build context
class _Rows:
    """Stands in for the store in a child: the filament rows the parent read."""

    def __init__(self, rows: list[dict[str, Any]] | None):
        self.rows = rows

    def filament_rows(self, job_id: str) -> list[dict[str, Any]] | None:
        return self.rows


@dataclass
class _Paths:
    repo_root: Path


@dataclass
class WorkCtx:
    """What a stage's blocking build code reads from its StageContext, picklable."""

    job: Any
    job_dir: Path
    dir: Path
    rel_dir: str
    settings: _Paths
    store: _Rows
    decisions: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def of(cls, ctx) -> WorkCtx:
        try:
            rows = ctx.store.filament_rows(ctx.job.id)
        except Exception:  # noqa: BLE001 library_for falls back to the file, as before
            rows = None
        return cls(job=ctx.job, job_dir=ctx.job_dir, dir=ctx.dir, rel_dir=ctx.rel_dir,
                   settings=_Paths(ctx.settings.repo_root), store=_Rows(rows))

    def path(self, name: str) -> Path:
        return self.dir / name

    def rel(self, name: str) -> str:
        return f"{self.rel_dir}/{name}"

    def abs(self, rel_path: str) -> Path:
        return self.job_dir / rel_path

    def decide(self, key: str, value: Any, *, by: str, reason: str | None = None,
               model: str | None = None) -> None:
        self.decisions.append({"key": key, "value": value, "by": by, "reason": reason,
                               "model": model})


def _call(method, wctx: WorkCtx, *args: Any) -> tuple[Any, list[dict[str, Any]]]:
    out = method(wctx, *args)
    return out, wctx.decisions


async def run_heavy(ctx, method, *args: Any) -> Any:
    """method(wctx, *args) off the event loop: a child process (Settings.offload process) or
    a thread. The build's decisions land on ctx in the parent."""
    wctx = WorkCtx.of(ctx)
    name = getattr(method, "__name__", "build").strip("_") or "build"
    mark = getattr(ctx, "checkpoint", None)
    if mark:
        mark(f"heavy:{name}:start")
    if getattr(ctx.settings, "offload", "thread") == "process":
        token = JOB.set(ctx.job.id)
        try:
            out, decisions = await run_cpu(_call, method, wctx, *args)
        finally:
            JOB.reset(token)
    else:
        out, decisions = await asyncio.to_thread(_call, method, wctx, *args)
    if mark:  # the build's files are on disk; the stage has not cached its result yet
        mark(f"heavy:{name}:end")
    for d in decisions:
        ctx.decide(d["key"], d["value"], by=d["by"], reason=d["reason"], model=d["model"])
    return out


# ---------------------------------------------------------------- logs from a child
@dataclass(frozen=True)
class FileLog:
    """A picklable log callback: appends one line per message to a file the parent tails."""

    path: str

    def __call__(self, msg: str) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(str(msg).replace("\n", " ") + "\n")


async def tail_log(path: Path, emit, every_s: float = 1.0) -> None:
    """Forward new lines of `path` to emit(line) until cancelled (then one last read)."""
    pos = 0

    def read() -> list[str]:
        nonlocal pos
        if not path.is_file():
            return []
        with open(path, encoding="utf-8") as fh:
            fh.seek(pos)
            chunk = fh.read()
            pos = fh.tell()
        return [ln for ln in chunk.splitlines() if ln.strip()]

    try:
        while True:
            for line in read():
                emit(line)
            await asyncio.sleep(every_s)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            for line in read():
                emit(line)
        raise
