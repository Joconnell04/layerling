"""JobStore: the worker's only view of job state, messages, spend, and Meshy webhook events.

LocalStore keeps everything on disk under jobs/ so the pipeline runs without Supabase:

jobs/<job_id>/job.json            JobRecord
jobs/<job_id>/job_row.json        jobs-row fields JobRecord has no slot for (pause_reason, result, ...)
jobs/<job_id>/messages.jsonl      append-only mirror of `messages` rows
jobs/<job_id>/inbox.json          local inbox state: message id -> consumed_at
jobs/<job_id>/ledger.jsonl        SpendEvents for this job
jobs/<job_id>/<stage>/a<N>/       stage attempts (written by stages)
jobs/_worker/spend.jsonl          every SpendEvent (the local `spend` table)
jobs/_worker/meshy_events.jsonl   local `meshy_events` table (append-only)
jobs/_worker/meshy_processed.json event id -> processed_at
jobs/_worker/task_index.json      Meshy task id -> job id
jobs/_worker/workers.json         heartbeat rows

SupabaseStore (clients/supabase.py) subclasses LocalStore: the job folder stays the working copy,
and rows go to PostgREST, files to Storage, notifications come from Realtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from p2p_contracts import JobRecord, Manifest, SpendEvent, StageId
from p2p_contracts.job import manifest_path
from p2p_contracts.payloads import MESSAGE_PAYLOADS
from pydantic import BaseModel

from .util import append_jsonl, read_json, read_jsonl, utcnow, write_json

TERMINAL_MESHY = {"SUCCEEDED", "FAILED", "CANCELED", "EXPIRED"}

# ---------------------------------------------------------------- idempotent message ids
# A message the worker posts inside a stage step or a gate gets an id derived from where it was
# posted (job, stage, attempt, step key, n-th post in the step). A replay after the worker died
# between the post and the manifest save posts the same id again, which the store ignores, so
# the customer never gets a question or a result twice. Other posts get a random id.
_NS = uuid.UUID("6f1c2a52-4bd0-4f43-9d1e-8a0c4b7d2e11")
_SCOPE: contextvars.ContextVar[list | None] = contextvars.ContextVar("p2p_msg_scope",
                                                                   default=None)


def message_id_for(key: str) -> str:
    return str(uuid.uuid5(_NS, key))


@contextlib.contextmanager
def message_scope(key: str) -> Iterator[None]:
    token = _SCOPE.set([key, 0])
    try:
        yield
    finally:
        _SCOPE.reset(token)


def next_message_id() -> str:
    scope = _SCOPE.get()
    if not scope:
        return str(uuid.uuid4())
    scope[1] += 1
    return message_id_for(f"{scope[0]}#{scope[1]}")


class MessageRow(BaseModel):
    """A `messages` row. Worker-internal; payloads are validated against MESSAGE_PAYLOADS."""

    id: str
    job_id: str
    created_at: datetime
    role: Literal["user", "assistant", "system"]
    kind: str
    stage: StageId | None = None
    body: str | None = None
    payload: dict[str, Any] = {}
    reply_to: str | None = None
    consumed_at: datetime | None = None


class MeshyEventRow(BaseModel):
    id: int
    received_at: datetime
    task_id: str
    status: str | None = None
    event: dict[str, Any]
    processed_at: datetime | None = None


def validate_message(role: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Check role and payload against the contract registry. Returns the normalized payload."""
    if kind not in MESSAGE_PAYLOADS:
        raise ValueError(f"unknown message kind {kind!r}")
    want_role, model = MESSAGE_PAYLOADS[kind]
    if role != want_role:
        raise ValueError(f"message kind {kind!r} must have role {want_role!r}, got {role!r}")
    if model is None:
        return payload or {}
    return model.model_validate(payload).model_dump(mode="json")


class Notifier:
    """Wake coroutines on any event loop when something new arrives. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []

    def notify(self) -> None:
        with self._lock:
            waiters, self._waiters = self._waiters, []
        for loop, fut in waiters:
            if not loop.is_closed():
                loop.call_soon_threadsafe(lambda f=fut: f.done() or f.set_result(True))

    async def wait(self, timeout: float) -> bool:
        if timeout <= 0:
            return False
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        with self._lock:
            self._waiters.append((loop, fut))
        try:
            await asyncio.wait_for(fut, timeout)
            return True
        except TimeoutError:
            return False
        finally:
            with self._lock:
                self._waiters = [(lp, f) for lp, f in self._waiters if f is not fut]


class JobStore(ABC):
    """Everything the orchestrator and stages read or write outside their attempt folder."""

    jobs_dir: Path

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    # ---------------------------------------------------------------- jobs
    @abstractmethod
    def load_job(self, job_id: str) -> JobRecord: ...

    @abstractmethod
    def save_job(self, job: JobRecord) -> None: ...

    @abstractmethod
    def update_job_row(self, job_id: str, **fields: Any) -> None:
        """Set jobs-row columns JobRecord lacks: pause_reason, result, confidence, review_reason."""

    @abstractmethod
    def job_row(self, job_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def list_jobs(self) -> list[str]: ...

    # ---------------------------------------------------------------- stage runs
    def load_manifest(self, job_id: str, stage: StageId, attempt: int) -> Manifest | None:
        p = self.job_dir(job_id) / manifest_path(stage, attempt)
        data = read_json(p)
        return Manifest.model_validate(data) if data else None

    @abstractmethod
    def write_manifest(self, manifest: Manifest) -> None: ...

    def upload_artifacts(self, job_id: str, rel_paths: list[str]) -> None:
        """Copy job-relative files to Storage. No-op locally."""

    def signed_url(self, job_id: str, rel_path: str, expires_s: int = 3600) -> str | None:
        """A URL Meshy can fetch, or None (caller falls back to a data URI)."""
        return None

    def fetch_input(self, job_id: str, rel_path: str) -> Path:
        """A job-relative file the web uploaded after the job started (input/paint/<name>.txt,
        a large paint stroke), on local disk. FileNotFoundError when it is nowhere."""
        p = self.job_dir(job_id) / rel_path
        if not p.is_file():
            raise FileNotFoundError(rel_path)
        return p

    # ---------------------------------------------------------------- messages
    @abstractmethod
    def post_message(
        self,
        job_id: str,
        role: str,
        kind: str,
        payload: dict[str, Any] | BaseModel | None = None,
        *,
        stage: StageId | None = None,
        body: str | None = None,
        reply_to: str | None = None,
        message_id: str | None = None,
    ) -> MessageRow:
        """Post a message. message_id: the row id to use (idempotent: posting an id that exists
        returns the existing row); default: from the current message_scope, else random."""

    @abstractmethod
    def list_messages(
        self,
        job_id: str,
        *,
        role: str | None = None,
        kinds: set[str] | None = None,
        unconsumed: bool = False,
    ) -> list[MessageRow]: ...

    @abstractmethod
    def consume_message(self, job_id: str, message_id: str) -> None: ...

    @abstractmethod
    async def wait_message(self, job_id: str, timeout: float) -> bool: ...

    # ---------------------------------------------------------------- spend
    @abstractmethod
    def record_spend(self, event: SpendEvent, *, usd_list: float = 0.0,
                     billing: str = "api") -> None: ...

    @abstractmethod
    def build_totals(self) -> dict[str, float]:
        """Real (non test-mode) spend so far across all jobs: {"usd": ..., "meshy_credits": ...}."""

    def spend_recorded(self, job_id: str, vendor: str, kind: str, ref: str) -> bool:
        """A spend row with this (vendor, kind, ref) is already on the job's ledger. A replay
        after the worker died between booking a Meshy task and saving the manifest books it
        once (BudgetGuard.record)."""
        return any(e.get("vendor") == vendor and e.get("kind") == kind and e.get("ref") == ref
                   for e in read_jsonl(self.job_dir(job_id) / "ledger.jsonl"))

    def job_spend(self, job_id: str) -> tuple[float, int]:
        """(usd, credits) the job has spent: its ledger rows plus the spend it arrived with
        (job_row spend_baseline: a job ingested from Supabase onto a machine with no ledger
        for it). JobRecord.budget is set from this after every row, so a worker that dies
        between the ledger row and the job save cannot lose or double the job's spend."""
        base = self.job_row(job_id).get("spend_baseline") or {}
        usd, credits = float(base.get("usd") or 0.0), int(base.get("credits") or 0)
        for e in read_jsonl(self.job_dir(job_id) / "ledger.jsonl"):
            usd += float(e.get("usd") or 0.0)
            credits += int(e.get("credits") or 0)
        return round(usd, 6), credits

    def pending_work(self) -> set[str]:
        """Jobs something is waiting on the worker for, whatever their status: an unconsumed
        user message (a reply, a resume, a review decision, a palette or paint edit) or an
        unprocessed Meshy event. serve kicks them after a restart, a Realtime reconnect and a
        wake from sleep, so nothing that arrived while it was not listening is missed."""
        return set()

    def filament_rows(self, job_id: str) -> list[dict[str, Any]] | None:
        """The job owner's `filaments` rows (Supabase), or None when the store has no table."""
        return None

    # ---------------------------------------------------------------- meshy events
    @abstractmethod
    def insert_meshy_event(self, event: dict[str, Any], *, source: str = "webhook") -> int: ...

    @abstractmethod
    def meshy_events(self, task_id: str) -> list[MeshyEventRow]: ...

    @abstractmethod
    def mark_meshy_processed(self, event_ids: list[int]) -> None: ...

    def terminal_meshy_event(self, task_id: str, *, unprocessed: bool = True) -> MeshyEventRow | None:
        for ev in reversed(self.meshy_events(task_id)):
            if unprocessed and ev.processed_at is not None:
                continue
            if (ev.status or "").upper() in TERMINAL_MESHY:
                return ev
        return None

    @abstractmethod
    async def wait_meshy_event(self, timeout: float) -> bool: ...

    async def wait_for_terminal(self, task_id: str, timeout: float) -> MeshyEventRow | None:
        deadline = time.monotonic() + timeout
        while True:
            ev = self.terminal_meshy_event(task_id)
            if ev is not None:
                return ev
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await self.wait_meshy_event(remaining)

    @abstractmethod
    def index_task(self, task_id: str, job_id: str) -> None: ...

    @abstractmethod
    def job_for_task(self, task_id: str) -> str | None: ...

    # ---------------------------------------------------------------- workers
    @abstractmethod
    def heartbeat(self, worker_id: str, *, version: str, meshy_mode: str, info: dict) -> None: ...


class LocalStore(JobStore):
    def __init__(self, jobs_dir: Path):
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.worker_dir = self.jobs_dir / "_worker"
        self.worker_dir.mkdir(parents=True, exist_ok=True)
        self._msg_notifier = Notifier()
        self._meshy_notifier = Notifier()
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- jobs
    def load_job(self, job_id: str) -> JobRecord:
        data = read_json(self.job_dir(job_id) / "job.json")
        if data is None:
            raise KeyError(f"job {job_id} not found")
        return JobRecord.model_validate(data)

    def save_job(self, job: JobRecord) -> None:
        write_json(self.job_dir(job.id) / "job.json", job.model_dump(mode="json"))

    def update_job_row(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            p = self.job_dir(job_id) / "job_row.json"
            row = read_json(p, {})
            row.update({k: (v.model_dump(mode="json") if isinstance(v, BaseModel) else v)
                        for k, v in fields.items()})
            write_json(p, row)

    def job_row(self, job_id: str) -> dict[str, Any]:
        return read_json(self.job_dir(job_id) / "job_row.json", {})

    def list_jobs(self) -> list[str]:
        return sorted(p.parent.name for p in self.jobs_dir.glob("*/job.json"))

    # ---------------------------------------------------------------- stage runs
    def write_manifest(self, manifest: Manifest) -> None:
        p = self.job_dir(manifest.job_id) / manifest_path(manifest.stage, manifest.attempt)
        write_json(p, manifest.model_dump(mode="json"))

    # ---------------------------------------------------------------- messages
    def _inbox(self, job_id: str) -> dict[str, str]:
        return read_json(self.job_dir(job_id) / "inbox.json", {})

    def post_message(self, job_id, role, kind, payload=None, *, stage=None, body=None,
                     reply_to=None, message_id=None):
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        payload = validate_message(role, kind, payload or {})
        mid = message_id or next_message_id()
        with self._lock:
            path = self.job_dir(job_id) / "messages.jsonl"
            for d in read_jsonl(path):
                if d.get("id") == mid:  # already posted (a replay): nothing twice
                    return MessageRow.model_validate(d)
            row = MessageRow(
                id=mid, job_id=job_id, created_at=utcnow(), role=role, kind=kind,
                stage=stage, body=body, payload=payload, reply_to=reply_to,
            )
            append_jsonl(path, row.model_dump(mode="json"))
        self._msg_notifier.notify()
        return row

    def list_messages(self, job_id, *, role=None, kinds=None, unconsumed=False):
        inbox = self._inbox(job_id)
        rows = []
        for d in read_jsonl(self.job_dir(job_id) / "messages.jsonl"):
            row = MessageRow.model_validate(d)
            if inbox.get(row.id):
                row.consumed_at = datetime.fromisoformat(inbox[row.id])
            if role and row.role != role:
                continue
            if kinds and row.kind not in kinds:
                continue
            if unconsumed and row.consumed_at is not None:
                continue
            rows.append(row)
        return rows

    def consume_message(self, job_id: str, message_id: str) -> None:
        with self._lock:
            p = self.job_dir(job_id) / "inbox.json"
            inbox = read_json(p, {})
            inbox.setdefault(message_id, utcnow().isoformat())
            write_json(p, inbox)

    async def wait_message(self, job_id: str, timeout: float) -> bool:
        return await self._msg_notifier.wait(timeout)

    # ---------------------------------------------------------------- spend
    def record_spend(self, event: SpendEvent, *, usd_list: float = 0.0,
                     billing: str = "api") -> None:
        d = event.model_dump(mode="json") | {"usd_list": usd_list, "billing": billing}
        with self._lock:
            append_jsonl(self.worker_dir / "spend.jsonl", d)
            if event.job_id:
                append_jsonl(self.job_dir(event.job_id) / "ledger.jsonl", d)

    def pending_work(self) -> set[str]:
        out: set[str] = set()
        for p in self.jobs_dir.glob("*/messages.jsonl"):
            inbox = self._inbox(p.parent.name)
            if any(d.get("role") == "user" and not inbox.get(d.get("id"))
                   for d in read_jsonl(p)):
                out.add(p.parent.name)
        processed = read_json(self.worker_dir / "meshy_processed.json", {})
        index = read_json(self.worker_dir / "task_index.json", {})
        for d in read_jsonl(self.worker_dir / "meshy_events.jsonl"):
            if not processed.get(str(d.get("id"))) and index.get(d.get("task_id")):
                out.add(index[d["task_id"]])
        return out

    def build_totals(self) -> dict[str, float]:
        usd, credits = 0.0, 0
        for e in read_jsonl(self.worker_dir / "spend.jsonl"):
            if e.get("test_mode"):
                continue
            usd += float(e.get("usd", 0))
            if e.get("vendor") == "meshy":
                credits += int(e.get("credits", 0))
        return {"usd": round(usd, 6), "meshy_credits": credits}

    # ---------------------------------------------------------------- meshy events
    def insert_meshy_event(self, event: dict[str, Any], *, source: str = "webhook") -> int:
        task_id = str(event.get("id") or event.get("task_id") or "")
        if not task_id:
            raise ValueError("Meshy event has no task id")
        with self._lock:
            p = self.worker_dir / "meshy_events.jsonl"
            new_id = len(read_jsonl(p)) + 1
            row = MeshyEventRow(
                id=new_id, received_at=utcnow(), task_id=task_id,
                status=(event.get("status") or None), event={**event, "_source": source},
            )
            append_jsonl(p, row.model_dump(mode="json"))
        self._meshy_notifier.notify()
        return new_id

    def meshy_events(self, task_id: str) -> list[MeshyEventRow]:
        processed = read_json(self.worker_dir / "meshy_processed.json", {})
        out = []
        for d in read_jsonl(self.worker_dir / "meshy_events.jsonl"):
            if d.get("task_id") != task_id:
                continue
            row = MeshyEventRow.model_validate(d)
            if processed.get(str(row.id)):
                row.processed_at = datetime.fromisoformat(processed[str(row.id)])
            out.append(row)
        return out

    def mark_meshy_processed(self, event_ids: list[int]) -> None:
        with self._lock:
            p = self.worker_dir / "meshy_processed.json"
            d = read_json(p, {})
            for i in event_ids:
                d.setdefault(str(i), utcnow().isoformat())
            write_json(p, d)

    async def wait_meshy_event(self, timeout: float) -> bool:
        return await self._meshy_notifier.wait(timeout)

    def index_task(self, task_id: str, job_id: str) -> None:
        with self._lock:
            p = self.worker_dir / "task_index.json"
            d = read_json(p, {})
            d[task_id] = job_id
            write_json(p, d)

    def job_for_task(self, task_id: str) -> str | None:
        return read_json(self.worker_dir / "task_index.json", {}).get(task_id)

    # ---------------------------------------------------------------- workers
    def heartbeat(self, worker_id: str, *, version: str, meshy_mode: str, info: dict) -> None:
        with self._lock:
            p = self.worker_dir / "workers.json"
            d = read_json(p, {})
            d[worker_id] = {
                "id": worker_id, "last_seen": utcnow().isoformat(), "version": version,
                "meshy_mode": meshy_mode, "info": info,
            }
            write_json(p, d)
