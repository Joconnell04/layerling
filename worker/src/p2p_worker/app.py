"""`serve`: the FastAPI app plus the job loop.

Endpoints
- POST /webhooks/meshy?token=...   Meshy task object -> meshy_events (for local tunnels; in
                                    production the Supabase meshy-webhook edge function does this)
- GET  /healthz                     liveness
- GET  /health                      what runs now: stages with their time against their limits,
                                    child processes, retries waiting, Realtime, keep-awake, clock
                                    jumps, event-loop lag; ok false with the problems
- POST /jobs/{job_id}/messages      post a user message (local store only; the web app writes
                                    Supabase rows directly)
- POST /jobs/{job_id}/advance       kick a job

The loop advances a job when something happens to it:
- startup: every active job (crash recovery),
- Realtime (Supabase): a user message insert, a queued job, a meshy_events insert,
- the local webhook route or the fake Meshy server's events,
- the Meshy backup sweep (sweep.py): every MESHY_SWEEP_S (600 s) a job waiting on a Meshy task
  older than MESHY_MISSED_WEBHOOK_S (600 s) is kicked and its stage makes one 0-credit status
  GET per waiting task, at most once per 10 minutes (FROM JACKSON: approved). Webhooks stay
  primary; nothing else reads vendor status.
A heartbeat row goes to `workers` every WORKER_HEARTBEAT_S (15 s). Geometry, paint, renders and
the slicer run in child processes (offload.py), so none of this waits on them.
The watch loop (watchdog.py) holds caffeinate while a job is active, resyncs every job after a
sleep, reconciles from the tables after a restart or a Realtime reconnect, and kicks a job when
its stage retry is due (the orchestrator's backoff) or its stage has no worker on it.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from p2p_contracts import JobStatus

from . import __version__
from .config import Settings
from .logs import log_event
from .runtime import Runtime, build_runtime
from .store import LocalStore

log = logging.getLogger("p2p_worker.app")


class Scheduler:
    """Runs orchestrator.advance per job, one at a time per job, coalescing repeat kicks. Keeps
    each job's last status (keep-awake, /health) and schedules the kick for a stage retry the
    orchestrator put off (its backoff)."""

    def __init__(self, rt: Runtime):
        self.rt = rt
        self._running: dict[str, asyncio.Task] = {}
        self._dirty: set[str] = set()
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._status: dict[str, tuple[JobStatus, float]] = {}
        self.runs = 0

    def kick(self, job_id: str | None) -> None:
        if not job_id:
            return
        only = self.rt.settings.only_jobs
        if only and job_id not in only:
            return
        if job_id in self._running and not self._running[job_id].done():
            self._dirty.add(job_id)
            return
        self._running[job_id] = asyncio.get_running_loop().create_task(self._run(job_id))

    def running(self, job_id: str) -> bool:
        t = self._running.get(job_id)
        return t is not None and not t.done()

    async def _run(self, job_id: str) -> None:
        while True:
            self._dirty.discard(job_id)
            try:
                self.runs += 1
                status = await self.rt.orchestrator.advance(job_id)
                self._note(job_id, status)
                log_event("advance_done", job_id=job_id, status=status.value)
            except Exception:
                log.exception("advance failed for job %s", job_id,
                              extra={"event": "advance_error", "fields": {"job_id": job_id}})
            if job_id not in self._dirty:
                return

    def _note(self, job_id: str, status: JobStatus) -> None:
        prev = self._status.get(job_id)
        self._status[job_id] = (status, prev[1] if prev and prev[0] == status
                                else time.monotonic())
        wait = self.rt.orchestrator.retry_wait_s(job_id)
        old = self._timers.pop(job_id, None)
        if old is not None:
            old.cancel()
        if wait > 0:  # a stage retry after its backoff (a sleep delays it; the wake kicks all)
            self._timers[job_id] = asyncio.get_running_loop().call_later(
                wait + 0.05, self.kick, job_id)

    def statuses(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        return {j: {"status": st.value, "for_s": round(now - t), "advancing": self.running(j)}
                for j, (st, t) in self._status.items()}

    def active(self) -> tuple[bool, str]:
        """Whether a job needs the Mac awake: a job advancing, queued or running (waiting on
        Meshy included), or waiting on the customer for less than keep_awake_user_s."""
        grace = float(getattr(self.rt.settings, "keep_awake_user_s", 0.0) or 0.0)
        now = time.monotonic()
        for j in self._running:
            if self.running(j):
                return True, f"{j} advancing"
        for j, (st, t) in self._status.items():
            if st in (JobStatus.QUEUED, JobStatus.RUNNING):
                return True, f"{j} {st.value}"
            if st == JobStatus.WAITING_USER and now - t < grace:
                return True, f"{j} waiting on the customer"
        return False, ""

    async def idle(self) -> None:
        while any(not t.done() for t in self._running.values()):
            await asyncio.gather(*self._running.values(), return_exceptions=True)


def check_token(settings: Settings, token: str | None) -> None:
    if not settings.webhook_token:
        raise HTTPException(503, "webhook token not configured")
    if not token or not hmac.compare_digest(token, settings.webhook_token):
        raise HTTPException(401, "bad token")


def create_app(rt: Runtime, scheduler: Scheduler, watchdog=None) -> FastAPI:
    app = FastAPI(title="p2p worker", version=__version__)
    store = rt.store
    if watchdog is None:
        from .watchdog import Watchdog

        watchdog = Watchdog(rt, scheduler)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "meshy_mode": rt.settings.meshy_mode,
                "store": type(store).__name__, "claude": type(rt.claude).__name__}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return watchdog.health()

    @app.post("/webhooks/meshy")
    async def meshy_webhook(request: Request, token: str | None = None) -> dict[str, Any]:
        check_token(rt.settings, token)
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(400, "body must be JSON") from e
        if not isinstance(body, dict) or not body.get("id"):
            raise HTTPException(400, "task object with id required")
        event_id = store.insert_meshy_event(body, source="local_webhook")
        log_event("webhook_received", meshy_task_id=str(body["id"]), status=body.get("status"))
        scheduler.kick(store.job_for_task(str(body["id"])))
        return {"ok": True, "event_id": event_id}

    @app.post("/jobs/{job_id}/messages")
    async def post_message(job_id: str, request: Request) -> dict[str, Any]:
        if type(store) is not LocalStore:
            raise HTTPException(409, "with Supabase, the web app writes messages rows directly")
        data = await request.json()
        job = store.load_job(job_id)
        try:
            row = store.post_message(job_id, "user", data.get("kind", ""), data.get("payload"),
                                     stage=job.stage, body=data.get("body"),
                                     reply_to=data.get("reply_to"))
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        scheduler.kick(job_id)
        return {"id": row.id}

    @app.post("/jobs/{job_id}/advance")
    async def advance(job_id: str) -> dict[str, Any]:
        scheduler.kick(job_id)
        return {"ok": True}

    return app


async def run_service(settings: Settings, host: str, port: int) -> None:
    import uvicorn

    from .watchdog import KeepAwake, Watchdog

    rt = build_runtime(settings)
    scheduler = Scheduler(rt)
    if rt.fake is not None and not settings.use_supabase:
        # MESHY_MODE=test on the local store: fake events land in the local inbox and kick.
        # With Supabase the fake only inserts the meshy_events row, like the edge function,
        # and the Realtime listener below delivers it.
        def on_fake_event(ev: dict[str, Any]) -> None:
            rt.store.insert_meshy_event(ev)
            scheduler.kick(rt.store.job_for_task(str(ev.get("id"))))

        rt.fake.on_event = on_fake_event
    tasks: list[asyncio.Task] = []
    listener = None
    watchdog = Watchdog(rt, scheduler, keep_awake=KeepAwake(settings.keep_awake))
    if settings.use_supabase:
        from .clients.supabase import RealtimeListener

        def on_change(table: str, typ: str, rec: dict[str, Any]) -> None:
            log_event("realtime_change", table=table, type=typ,
                      job_id=rec.get("job_id") or (rec.get("id") if table == "jobs" else None),
                      meshy_task_id=rec.get("task_id"), status=rec.get("status"))
            if table == "meshy_events" and typ == "INSERT":
                rt.store.notify_meshy()  # type: ignore[attr-defined]
                scheduler.kick(rt.store.job_for_task(str(rec.get("task_id"))))
            elif table == "messages" and typ == "INSERT" and rec.get("role") == "user":
                rt.store.notify_message()  # type: ignore[attr-defined]
                scheduler.kick(rec.get("job_id"))
            elif table == "jobs" and rec.get("status") == "queued":
                scheduler.kick(rec.get("id"))

        def on_connect() -> None:  # after every (re)join: whatever was inserted meanwhile
            watchdog.reconcile("realtime_join", everything=True)

        listener = RealtimeListener(settings.supabase_url, settings.supabase_service_key,
                                    ["messages", "jobs", "meshy_events"], on_change,
                                    on_connect=on_connect)
        watchdog.listener = listener
    app = create_app(rt, scheduler, watchdog)

    async def heartbeat() -> None:
        while True:
            try:
                rt.store.heartbeat(settings.worker_id, version=__version__,
                                   meshy_mode=settings.meshy_mode, info={"serve": True})
            except Exception:
                log.exception("heartbeat failed")
            await asyncio.sleep(settings.heartbeat_s)

    tasks.append(asyncio.create_task(heartbeat()))
    from .sweep import sweep_loop

    tasks.append(asyncio.create_task(sweep_loop(rt.store, scheduler.kick, settings.meshy_sweep_s,
                                                settings.missed_webhook_s)))
    tasks.append(asyncio.create_task(watchdog.run()))
    if listener is not None:
        tasks.append(asyncio.create_task(listener.run()))
    # resume whatever was in flight, and whatever arrived while no worker listened
    watchdog.reconcile("startup", everything=True)
    # access_log off: request lines would print the webhook ?token= secret
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info",
                                           access_log=False))
    try:
        await server.serve()
    finally:
        for t in tasks:
            t.cancel()
        from .offload import shutdown

        shutdown()  # the child processes that run heavy stage work (offload.py)
        watchdog.keep_awake.close()
        await rt.aclose()


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8787) -> int:
    # Short in-process webhook waits: the loop re-advances a job when its event arrives.
    settings = settings.with_(vendor_wait_s=min(settings.vendor_wait_s, 5.0))
    asyncio.run(run_service(settings, host, port))
    return 0
