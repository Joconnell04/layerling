"""Stage context: resumable steps, Meshy tasks, Claude calls, user gates, manifest bookkeeping.

A stage is a coroutine that is replayed from the top on every run. Anything expensive or
user-facing is wrapped so its result is cached in the attempt manifest (`data.steps`), so a replay
after a crash, a gate, or a webhook skips work already done:

- ctx.step(key, fn)            run fn once, cache its JSON result
- ctx.claude(purpose, ...)     budget check, call, record ModelCall and SpendEvent
- ctx.meshy_run(key, ...)      create a Meshy task once (budget checked), then wait for its webhook
- ctx.gate(key, ...)           post a prompt message once, then consume the user's reply

Waiting raises GateWaiting or VendorWaiting; the orchestrator records the status and returns.

Paid Meshy calls go through the job's credit ledger (credit_ledger.py): the same inputs in any
stage or attempt of the job (and, with cross-job reuse on, in another job inside Meshy's
retention) are reused at 0 credits; a new call passes the pre-flight (job cap, build caps, the
free Meshy balance) and is written down as an intent before the POST.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from p2p_contracts import (
    Decision,
    FileRef,
    InputRef,
    JobRecord,
    Manifest,
    MeshyTask,
    ModelCall,
    SpendEvent,
    StageId,
    StageStatus,
)
from p2p_contracts.job import stage_dir
from p2p_contracts.manifest import GateKind, GateRequest
from p2p_contracts.payloads import MESSAGE_PAYLOADS
from pydantic import BaseModel, ValidationError

from .. import trace
from ..budget import BudgetGuard, meshy_usd
from ..clients.claude import ClaudeClient
from ..clients.meshy import ENDPOINTS, TERMINAL, MeshyClient, apply_task_object, estimate_credits
from ..config import Settings
from ..credit_ledger import CreditLedger, Entry, MeshyCache, input_hash
from ..errors import BudgetExceeded, GateWaiting, StagePaused, VendorError, VendorWaiting
from ..logs import log_event
from ..store import JobStore, MessageRow, message_id_for, message_scope
from ..util import manifest_safe, read_jsonl, sha256_file, utcnow, write_json

ENDPOINT_BY_PATH = {e.path: e.name for e in ENDPOINTS.values()}


@dataclass
class Services:
    store: JobStore
    meshy: MeshyClient
    claude: ClaudeClient
    settings: Settings
    on_checkpoint: Callable[[str], None] | None = None
    # Carries a failed attempt's paid Meshy tasks into the next attempt (orchestrator
    # _adopt_paid_tasks). None: lifecycle.default_adopt. Interface: (prev, new) -> {key: task}.
    adopt_tasks: Callable[[Manifest, Manifest], dict[str, str]] | None = None
    budget: BudgetGuard = field(init=False)

    def __post_init__(self) -> None:
        self.budget = BudgetGuard(self.store, self.settings)
        self._create_locks: dict[int, asyncio.Lock] = {}

    def create_lock(self) -> asyncio.Lock:
        """One lock per event loop: budget check + create + record run as one unit."""
        loop_id = id(asyncio.get_running_loop())
        if loop_id not in self._create_locks:
            self._create_locks[loop_id] = asyncio.Lock()
        return self._create_locks[loop_id]


def _fingerprint(body: dict[str, Any]) -> dict[str, Any]:
    return {k: body[k] for k in ("ai_model", "prompt", "style", "max_colors") if k in body}


def body_key(safe_body: dict[str, Any]) -> str:
    """Identity of a request body as persisted (manifest_safe: signatures and data stripped, so
    two signings of the same file compare equal)."""
    import hashlib
    import json

    return hashlib.sha256(json.dumps(safe_body, sort_keys=True, default=str).encode()
                          ).hexdigest()[:16]


def status_read_due(m: Manifest, task: MeshyTask, every_s: float,
                    now: datetime | None = None) -> bool:
    """A non-terminal task older than every_s whose last backup status read (if any) is at least
    every_s old."""
    if task.status in TERMINAL:
        return False
    now = now or utcnow()
    if (now - task.created_at).total_seconds() < every_s:
        return False
    last = (m.data.get("meshy_status_reads") or {}).get(task.task_id)
    return last is None or (now - datetime.fromisoformat(last)).total_seconds() >= every_s


def _type_path(cls: type) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def _dump_payload(payload: Any) -> dict[str, Any]:
    """A Claude result as JSON plus how to rebuild it (a model, a list of models, plain JSON)."""
    if isinstance(payload, BaseModel):
        return {"type": _type_path(type(payload)), "value": payload.model_dump(mode="json")}
    if isinstance(payload, list) and payload and all(isinstance(x, BaseModel) for x in payload):
        return {"type": "list:" + _type_path(type(payload[0])),
                "value": [x.model_dump(mode="json") for x in payload]}
    return {"type": "json", "value": payload}


def _load_model(done: dict[str, Any]) -> Any:
    """A value cached by ctx.claude, rebuilt; None when it cannot be (then Claude is asked
    again)."""
    import importlib

    kind = str(done.get("type") or "")
    if kind == "json":
        return done.get("value")
    try:
        many = kind.startswith("list:")
        mod, _, name = kind.removeprefix("list:").partition(":")
        cls: Any = importlib.import_module(mod)
        for part in name.split("."):
            cls = getattr(cls, part)
        if many:
            return [cls.model_validate(x) for x in done["value"]]
        return cls.model_validate(done["value"])
    except Exception:  # noqa: BLE001 a type that moved: call again
        return None


class StageContext:
    def __init__(self, svc: Services, job: JobRecord, stage: StageId, attempt: int,
                 manifest: Manifest, run_started: datetime):
        self.svc = svc
        self.store = svc.store
        self.settings = svc.settings
        self.job = job
        self.stage = stage
        self.attempt = attempt
        self.m = manifest
        self.run_started = run_started
        self.rel_dir = stage_dir(stage, attempt).as_posix()
        self.job_dir = self.store.job_dir(job.id)
        self.dir = self.job_dir / self.rel_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._resume_gets: set[str] = set()
        # Full Meshy task objects from this run, signed asset URLs included. Never persisted:
        # manifests keep manifest_safe() copies (no signatures, tokens or data URIs).
        self._raw: dict[str, dict[str, Any]] = {}
        self._inputs_saved = len(manifest.inputs)
        self._ledger: CreditLedger | None = None
        # This run's watchdog record (lifecycle.RunInfo), set by the orchestrator: progress,
        # time spent waiting on a webhook in-process (not counted against the stage's limit).
        self.run = None
        self._step_key: str | None = None  # the step running now (Claude results cache by it)
        self._claude_n: dict[str, int] = {}

    @property
    def ledger(self) -> CreditLedger:
        if self._ledger is None:
            self._ledger = CreditLedger(self.store, self.job.id)
        return self._ledger

    # ------------------------------------------------------------------ manifest
    @property
    def steps(self) -> dict[str, Any]:
        return self.m.data.setdefault("steps", {})

    def save(self, checkpoint: str | None = None) -> None:
        run = self.m.data.get("run")
        if checkpoint and isinstance(run, dict):
            run["last"] = checkpoint  # how far this run got (a crash loop makes no progress)
        self.store.write_manifest(self.m)
        self._inputs_saved = len(self.m.inputs)
        if checkpoint:
            self.checkpoint(checkpoint)

    def touch(self, what: str) -> None:
        """Progress without a kill point (a line from the paint child's log)."""
        if self.run is not None:
            self.run.touch(what)

    def waiting(self):
        """Time spent waiting on a vendor in-process: not active time for the watchdog."""
        return self.run.waiting() if self.run is not None else contextlib.nullcontext()

    def checkpoint(self, name: str) -> None:
        """A point the stage got past: counts as progress for the stage watchdog
        (lifecycle.RunInfo) and calls Services.on_checkpoint (the chaos tests kill here)."""
        if self.run is not None:
            self.run.touch(name)
        if self.svc.on_checkpoint:
            self.svc.on_checkpoint(f"{self.stage.value}:{name}")

    def rel(self, name: str) -> str:
        return f"{self.rel_dir}/{name}"

    def path(self, name: str) -> Path:
        return self.dir / name

    def abs(self, rel_path: str) -> Path:
        return self.job_dir / rel_path

    def add_input(self, rel_path: str, *, from_stage: StageId | None = None,
                  from_attempt: int | None = None, role: str | None = None) -> InputRef:
        p = self.abs(rel_path)
        ref = InputRef(path=rel_path, sha256=sha256_file(p), bytes=p.stat().st_size,
                       kind=p.suffix.lstrip(".") or None, role=role, from_stage=from_stage,
                       from_attempt=from_attempt)
        self.m.inputs = [i for i in self.m.inputs if i.path != rel_path] + [ref]
        return ref

    def add_output(self, name: str, *, role: str, kind: str | None = None) -> FileRef:
        rel_path = self.rel(name)
        p = self.abs(rel_path)
        ref = FileRef(path=rel_path, sha256=sha256_file(p), bytes=p.stat().st_size,
                      kind=kind or p.suffix.lstrip(".") or None, role=role)
        self.m.outputs = [o for o in self.m.outputs if o.path != rel_path] + [ref]
        self.store.upload_artifacts(self.job.id, [rel_path])
        return ref

    def write_json_output(self, name: str, obj: Any, *, role: str) -> FileRef:
        if isinstance(obj, BaseModel):
            obj = obj.model_dump(mode="json")
        write_json(self.path(name), obj)
        return self.add_output(name, role=role, kind="json")

    def copy_output(self, src: Path, name: str, *, role: str) -> FileRef:
        dst = self.path(name)
        if not dst.exists():
            shutil.copyfile(src, dst)
        return self.add_output(name, role=role)

    def decide(self, key: str, value: Any, *, by: str, reason: str | None = None,
               model: str | None = None) -> None:
        self.m.decisions = [d for d in self.m.decisions if d.key != key]
        self.m.decisions.append(Decision(key=key, value=value, reason=reason, by=by, model=model,
                                         at=utcnow()))

    def upstream(self, stage: StageId) -> Manifest:
        attempt = self.job.attempts.get(stage)
        m = self.store.load_manifest(self.job.id, stage, attempt) if attempt else None
        if m is None or m.status != StageStatus.SUCCEEDED:
            raise VendorError(f"{self.stage.value} needs {stage.value}, which has not succeeded",
                              code="internal", retryable=False)
        return m

    def use_output(self, stage: StageId, role: str) -> tuple[Path, FileRef]:
        """Record an upstream output as an input of this attempt; return its absolute path."""
        m = self.upstream(stage)
        for o in m.outputs:
            if o.role == role:
                self.add_input(o.path, from_stage=stage, from_attempt=m.attempt, role=role)
                return self.abs(o.path), o
        raise VendorError(f"{stage.value} has no output with role {role}", code="internal",
                          retryable=False)

    # ------------------------------------------------------------------ steps
    async def step(self, key: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        if key in self.steps:
            return self.steps[key]
        if len(self.m.inputs) != self._inputs_saved:
            # Inputs go to disk before a long step (the paint run takes 20 minutes): a worker
            # restart mid-step then sees this attempt go stale when an upstream stage moved
            # (live2 run: the cat charm's s09 had no inputs saved, so its paint session resumed
            # after s06 and s08 were rebuilt).
            self.save()
        outer, self._step_key = self._step_key, key
        self._claude_n = {}
        try:
            with message_scope(f"{self.job.id}/{self.stage.value}/a{self.attempt}/step:{key}"):
                value = await fn()
        finally:
            self._step_key = outer
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        self.steps[key] = value
        self.save(f"step:{key}")
        return value

    # ------------------------------------------------------------------ claude
    async def claude(self, purpose: str, estimate_usd: float,
                     call: Callable[[], Awaitable[tuple[BaseModel, ModelCall]]]) -> BaseModel:
        client: ClaudeClient = self.svc.claude
        # A result recorded by an earlier run of this step (the worker died after the call and
        # before the step cached its value): used again, not bought again.
        ckey = None
        if self._step_key is not None:
            k = f"{self._step_key}|{purpose}"
            self._claude_n[k] = self._claude_n.get(k, 0) + 1
            ckey = f"{k}|{self._claude_n[k]}"
            done = (self.m.data.get("claude_results") or {}).get(ckey)
            if done:
                cached = _load_model(done)
                if cached is not None:
                    self._book_claude(purpose, done.get("spend") or {})
                    return cached
        # Subscription (Claude Code OAuth) and mock calls cost $0 and skip the USD caps.
        paid = client.billing == "api"
        self.svc.budget.check(self.job, vendor="anthropic", credits=0,
                              usd=estimate_usd if paid else 0.0, live=paid, what=purpose)
        try:
            payload, mc = await call()
        except VendorError as e:
            # Nothing this attempt paid for is lost when a Claude call fails: the orchestrator
            # replays the attempt (lifecycle.failure_kind "claude") instead of starting over.
            e.detail.setdefault("resume", "claude")
            raise
        log_event("claude_call", purpose=purpose, model=mc.model, billing=mc.billing,
                  duration_ms=round(mc.duration_s * 1000), input_tokens=mc.input_tokens,
                  output_tokens=mc.output_tokens, usd=mc.usd, usd_list=mc.usd_list)
        self.m.model_calls.append(mc)
        self.m.usd = round(self.m.usd + mc.usd, 6)
        spend = {"usd": mc.usd, "ref": mc.request_id, "usd_list": mc.usd_list,
                 "billing": mc.billing or client.billing, "test_mode": client.billing == "free"}
        if ckey is not None:
            # The result and the call record reach disk before the spend row: a worker that
            # dies in between replays this step with the result (no second call) and books
            # the row then, once.
            self.m.data.setdefault("claude_results", {})[ckey] = {
                **_dump_payload(payload), "spend": spend}
            self.save()
        self._book_claude(purpose, spend, force=ckey is None)
        self.save(f"claude:{purpose}")
        return payload

    def _book_claude(self, purpose: str, spend: dict[str, Any], *, force: bool = False) -> None:
        ref = spend.get("ref")
        if not spend or (not force and ref and self.store.spend_recorded(
                self.job.id, "anthropic", purpose, ref)):
            return
        self.svc.budget.record(self.job, stage=self.stage, vendor="anthropic", kind=purpose,
                               usd=float(spend.get("usd") or 0.0), ref=ref,
                               usd_list=spend.get("usd_list"), billing=spend.get("billing"),
                               test_mode=bool(spend.get("test_mode")))

    # ------------------------------------------------------------------ meshy
    def _keys(self) -> dict[str, str]:
        return self.m.data.setdefault("meshy_keys", {})

    def task(self, key: str) -> MeshyTask | None:
        tid = self._keys().get(key)
        return next((t for t in self.m.meshy_tasks if t.task_id == tid), None) if tid else None

    def _put_task(self, task: MeshyTask) -> None:
        self.m.meshy_tasks = [t for t in self.m.meshy_tasks if t.task_id != task.task_id] + [task]

    def _record_created(self, key: str, task: MeshyTask, endpoint: str) -> None:
        meshy: MeshyClient = self.svc.meshy
        self._put_task(task)
        self._keys()[key] = task.task_id
        self.m.data.setdefault("meshy_pending", {}).pop(key, None)
        self.m.credits += task.credits
        self.m.usd = round(self.m.usd + meshy_usd(task.credits), 6)
        self.store.index_task(task.task_id, self.job.id)
        self.svc.budget.record(self.job, stage=self.stage, vendor="meshy", kind=endpoint,
                               credits=task.credits, usd=meshy_usd(task.credits),
                               ref=task.task_id, test_mode=meshy.test_mode)

    async def _recover_create(self, key: str, endpoint: str, body: dict[str, Any],
                              pending: dict[str, Any], purpose: str) -> MeshyTask | None:
        """A create was sent but the worker died before recording the task id. Look it up once."""
        meshy: MeshyClient = self.svc.meshy
        since_ms = int(datetime.fromisoformat(pending["at"]).timestamp() * 1000) - 5000
        known = {t.task_id for t in self.m.meshy_tasks} | self.ledger.known_task_ids()
        fp = _fingerprint(body)

        def elsewhere(tid: Any) -> bool:  # a task another job recorded is not ours
            owner = self.store.job_for_task(str(tid)) if tid else None
            return owner is not None and owner != self.job.id

        cands = [
            t for t in await meshy.list_recent(endpoint)
            if int(t.get("created_at") or 0) >= since_ms and t.get("id") not in known
            and all(t.get(k) == v for k, v in fp.items() if k in t) and not elsewhere(t.get("id"))
        ]
        self.decide(f"meshy.recover.{key}", [t.get("id") for t in cands], by="code",
                    reason="create outcome unknown after crash; listed recent tasks once")
        if not cands:
            return None
        if len(cands) > 1:
            raise StagePaused(f"Cannot tell which Meshy {endpoint} task a crashed create made",
                              {"candidates": [t.get("id") for t in cands]})
        t = cands[0]
        return apply_task_object(
            MeshyTask(task_id=str(t["id"]), endpoint=ENDPOINTS[endpoint].path, purpose=purpose,
                      status="PENDING", test_mode=meshy.test_mode,
                      credits=estimate_credits(endpoint, body), created_at=utcnow(),
                      request=manifest_safe(body)),
            t,
        )

    def same_inputs(self, endpoint: str, body: dict[str, Any]) -> MeshyTask | None:
        """A task this attempt already sent with identical inputs. Meshy has no seed, so an
        identical re-send only buys a random re-roll; a regeneration must change an input."""
        path = ENDPOINTS[endpoint].path
        k = body_key(manifest_safe(body))
        return next((t for t in self.m.meshy_tasks
                     if t.endpoint == path and body_key(t.request) == k), None)

    def already_paid(self, endpoint: str, body: dict[str, Any]) -> MeshyTask | None:
        """A task anywhere in this job made from these exact inputs that succeeded or is still
        running (the one meshy_create would reuse), else None."""
        prior = self.ledger.find(input_hash(endpoint, body, self.store.jobs_dir))
        return prior.meshy_task() if prior is not None and prior.live else None

    def _reuse_across_jobs(self) -> bool:
        flag = getattr(self.settings, "meshy_reuse_across_jobs", None)
        return (not self.svc.meshy.test_mode) if flag is None else bool(flag)

    def _take_over(self, key: str, endpoint: str, purpose: str, prior: Entry, h: str, *,
                   source: dict[str, Any], why: str | None) -> MeshyTask:
        """Give `key` a task already paid for: reused when it has finished (0 credits here),
        adopted when it is still running (this attempt waits for it; its booking stays with
        the attempt that paid, and the final adjust or refund lands here)."""
        task = prior.meshy_task()
        if task.status not in TERMINAL and source.get("job_id") == self.job.id:
            m = self.store.load_manifest(self.job.id, StageId(prior.stage), prior.attempt)
            later = next((t for t in (m.meshy_tasks if m else []) if t.task_id == task.task_id
                          and t.status in TERMINAL), None)
            task = later or task  # the payer saw the webhook but the ledger missed "done"
        finished = task.status in TERMINAL
        how = "reused" if finished else "adopted"
        saved = int(task.credits or prior.expected or 0)
        if prior.how == "new" and source.get("job_id") == self.job.id:
            self._ensure_booked(task, endpoint)
        mine = next((t for t in self.m.meshy_tasks if t.task_id == task.task_id), None)
        if mine is None:  # the same attempt keeps its own record of a task it paid for
            mine = task.model_copy(update={
                "purpose": f"{purpose} ({how})",
                "credits": 0 if finished else task.credits})
            self._put_task(mine)
            self.m.data.setdefault("meshy_reused", {})[mine.task_id] = {
                "how": how, **source, "saved": saved}
        self._keys()[key] = mine.task_id
        self.ledger.reused(h=h, stage=self.stage.value, attempt=self.attempt, key=key,
                           purpose=purpose, endpoint=endpoint, task=mine, how=how,
                           source=source, saved=saved, why=why)
        where = (f"{source.get('stage')}/a{source.get('attempt')} {source.get('key')}"
                 + ("" if source.get("job_id") == self.job.id
                    else f" of job {str(source.get('job_id'))[:8]}"))
        self.decide(f"meshy.{how}.{key}", mine.task_id, by="code",
                    reason=f"same inputs as {where}: {saved} credits not spent again")
        log_event("meshy_reused", meshy_task_id=mine.task_id, endpoint=endpoint, key=key,
                  how=how, saved=saved, source=where)
        if finished and mine.credits == 0:
            trace.record_meshy(mine, mine.result)
        self.save(f"meshy_{how}:{key}")
        return mine

    def _ensure_booked(self, task: MeshyTask, endpoint: str) -> None:
        """A task the job paid for whose spend row never landed (the booking after the POST
        failed) is booked once here, by the attempt that takes it over."""
        rows = read_jsonl(self.job_dir / "ledger.jsonl")
        if any(r.get("ref") == task.task_id and r.get("kind") == endpoint for r in rows):
            return
        credits = int(task.credits or 0)
        self.m.credits += credits
        self.m.usd = round(self.m.usd + meshy_usd(credits), 6)
        self.svc.budget.record(self.job, stage=self.stage, vendor="meshy", kind=endpoint,
                               credits=credits, usd=meshy_usd(credits), ref=task.task_id,
                               test_mode=self.svc.meshy.test_mode)
        self.decide(f"meshy.booked_late.{task.task_id}", credits, by="code",
                    reason="paid for by an earlier attempt whose spend row never landed")

    def record_file_reuse(self, key: str, endpoint: str, purpose: str, task: MeshyTask,
                          attempt: int, why: str | None = None) -> None:
        """The credit report's line for an output a stage took from an earlier attempt's
        folder without asking Meshy (s07's downloaded 3MF for the same mesh)."""
        self.ledger.reused(
            h=input_hash(endpoint, task.request, self.store.jobs_dir), stage=self.stage.value,
            attempt=self.attempt, key=key, purpose=purpose, endpoint=endpoint,
            task=task.model_copy(update={"credits": 0}), how="reused",
            source={"job_id": self.job.id, "stage": self.stage.value, "attempt": attempt,
                    "key": key, "task_id": task.task_id},
            saved=int(task.credits or 0), why=why)

    async def _preflight(self, *, key: str, endpoint: str, h: str, credits: int,
                         purpose: str) -> None:
        """Before a paid create: the job's caps (with its spend read back from its spend rows),
        the build caps (live), and the free Meshy balance. A refusal pauses the stage with the
        reason, before anything is sent."""
        meshy: MeshyClient = self.svc.meshy
        try:
            self.svc.budget.sync_job(self.job)
            self.svc.budget.check(self.job, vendor="meshy", credits=credits,
                                  usd=meshy_usd(credits), live=not meshy.test_mode, what=purpose)
            if credits > 0 and getattr(self.settings, "meshy_balance_check", True):
                balance = await meshy.balance()
                self.m.data["meshy_balance"] = {"at": utcnow().isoformat(), "credits": balance}
                if balance < credits:
                    raise BudgetExceeded(
                        f"Meshy balance {balance} credits: {purpose} needs {credits}",
                        {"cap": "meshy_balance", "balance": balance, "need_credits": credits})
        except BudgetExceeded as e:
            self.ledger.refused(h=h, stage=self.stage.value, attempt=self.attempt, key=key,
                                purpose=purpose, endpoint=endpoint, expected=credits,
                                reason=e.reason)
            raise

    def _intent_row(self, endpoint: str, h: str, key: str, credits: int) -> None:
        """The intent in the spend table: 0 credits (the create row books them); ref is
        <input hash>|a<attempt>|<step key>|<expected credits>."""
        self.store.record_spend(
            SpendEvent(at=utcnow(), job_id=self.job.id, stage=self.stage, vendor="meshy",
                       kind=f"intent:{endpoint}", credits=0, usd=0.0,
                       ref=f"{h[:16]}|a{self.attempt}|{key}|{credits}",
                       test_mode=self.svc.meshy.test_mode),
            usd_list=0.0, billing="credits")

    async def meshy_create(self, key: str, endpoint: str, body: dict[str, Any],
                           purpose: str, *, allow_same: bool = False, fresh: bool = False,
                           why: str | None = None) -> MeshyTask:
        """The task for `key`, paid for at most once per set of inputs.

        In order: the task this attempt already has for `key`; a task anywhere in this job
        made from the same inputs (endpoint and body compared by content) that succeeded or is
        still running, taken over at 0 credits; a create whose outcome a crash hid, found with
        one list call; with cross-job reuse on, a succeeded task of another job inside Meshy's
        retention; else the pre-flight, an intent record (job folder and spend table), the
        POST, and the task id recorded against the intent.

        fresh: the caller wants a new sample (a tweak round, a regenerate). Meshy has no seed,
        so inputs identical to a task this job already has are refused: change an input.
        allow_same is kept for callers that retry after a refunded failure; a FAILED task is
        never reused, so that retry creates a new task either way."""
        existing = self.task(key)
        if existing is not None:
            return existing
        meshy: MeshyClient = self.svc.meshy
        h = input_hash(endpoint, body, self.store.jobs_dir)
        async with self.svc.create_lock():
            existing = self.task(key)
            if existing is not None:
                return existing
            prior = self.ledger.find(h)
            if prior is not None and prior.live:
                if fresh:
                    raise VendorError(f"{purpose}: identical inputs to Meshy task "
                                      f"{prior.task_id}; a fresh variation must change an "
                                      "input", code="internal", retryable=False)
                source = {"job_id": prior.assets_job or self.job.id, "stage": prior.stage,
                          "attempt": prior.attempt, "key": prior.key,
                          "task_id": prior.task_id}
                if prior.source:  # a reuse of a reuse points at the payer
                    source = {**prior.source, "task_id": prior.task_id}
                return self._take_over(key, endpoint, purpose, prior, h, source=source, why=why)
            pending = self.m.data.setdefault("meshy_pending", {})
            unknown = prior if prior is not None and prior.status == "intent" else None
            if key in pending or unknown is not None:
                at = (pending.get(key) or {}).get("at") or (unknown.at if unknown else None)
                adopted = await self._recover_create(key, endpoint, body, {"at": at}, purpose)
                if adopted is not None:
                    self._record_created(key, adopted, endpoint)
                    iid = (pending.get(key) or {}).get("intent") or (
                        unknown.id if unknown else None)
                    if iid is None:
                        iid = self.ledger.intent(
                            h=h, stage=self.stage.value, attempt=self.attempt, key=key,
                            purpose=purpose, endpoint=endpoint, expected=adopted.credits,
                            why=why)
                    self.ledger.created(iid, adopted)
                    self.save(f"meshy_adopted:{key}")
                    return adopted
                for iid in {(pending.get(key) or {}).get("intent"),
                            unknown.id if unknown else None} - {None}:
                    self.ledger.abandoned(iid, "no Meshy task found for it after a restart")
            if not fresh and self._reuse_across_jobs():
                hit = MeshyCache(self.store.jobs_dir).find(h, test_mode=meshy.test_mode,
                                                           not_job=self.job.id)
                if hit is not None:
                    task = MeshyTask.model_validate(hit["task"])
                    prior = Entry(id="cache", hash=h, stage=hit["stage"],
                                  attempt=int(hit["attempt"]), key=hit["key"],
                                  purpose=task.purpose, endpoint=endpoint,
                                  expected=task.credits, at=hit.get("at", ""),
                                  task=hit["task"])
                    source = {"job_id": hit["job_id"], "stage": hit["stage"],
                              "attempt": hit["attempt"], "key": hit["key"],
                              "task_id": task.task_id}
                    return self._take_over(key, endpoint, purpose, prior, h, source=source,
                                           why=why)
            credits = estimate_credits(endpoint, body)
            await self._preflight(key=key, endpoint=endpoint, h=h, credits=credits,
                                  purpose=purpose)
            iid = self.ledger.intent(h=h, stage=self.stage.value, attempt=self.attempt,
                                     key=key, purpose=purpose, endpoint=endpoint,
                                     expected=credits, why=why)
            self._intent_row(endpoint, h, key, credits)
            pending[key] = {"at": utcnow().isoformat(), "endpoint": endpoint, "intent": iid}
            self.save(f"meshy_pending:{key}")
            try:
                task = await meshy.create(endpoint, body, purpose)
            except VendorError as e:
                if e.status_code is None or e.status_code >= 500:
                    # a timeout, a dropped connection or a 5xx: Meshy may have made (and
                    # charged) the task. The intent stays open, so the next try lists recent
                    # tasks once and adopts it before paying again.
                    self.decide(f"meshy.create_unknown.{key}", str(e)[:300], by="code",
                                reason="create outcome unknown; the next try looks it up")
                    self.save()
                    raise
                pending.pop(key, None)
                self.ledger.abandoned(iid, f"create refused: {e}")
                self.save()
                if e.code == "budget_exceeded":  # 402: out of credits on Meshy's side
                    raise BudgetExceeded(f"Meshy refused {purpose}: not enough credits "
                                         f"({e})", {"cap": "meshy_402",
                                                    "need_credits": credits}) from e
                raise
            self.ledger.created(iid, task)  # the task id on disk first: never bought twice
            self._record_created(key, task, endpoint)
            log_event("meshy_create", meshy_task_id=task.task_id, endpoint=endpoint, key=key,
                      credits=task.credits, test_mode=task.test_mode)
            self.save(f"meshy_created:{key}")
            return task

    def _finish(self, task: MeshyTask) -> dict[str, Any]:
        if task.status == "SUCCEEDED":
            return task.result or {}
        err = (task.result or {}).get("task_error") or {}
        raise VendorError(
            f"Meshy task {task.task_id} ({task.purpose}) {task.status}: "
            f"{err.get('message', 'no message')}",
            code="vendor_failed", retryable=err.get("type") != "invalid_input", detail=err,
        )

    def _apply_terminal(self, task: MeshyTask, obj: dict[str, Any],
                        raw: dict[str, Any] | None = None) -> MeshyTask:
        meshy: MeshyClient = self.svc.meshy
        if raw is not None:
            self._raw[task.task_id] = raw
        updated = apply_task_object(task, obj)
        diff = updated.credits - task.credits
        if diff:
            kind = "refund" if diff < 0 else "adjust"
            self.m.credits += diff
            self.m.usd = round(self.m.usd + meshy_usd(diff), 6)
            self.svc.budget.record(self.job, stage=self.stage, vendor="meshy",
                                   kind=f"{kind}:{ENDPOINT_BY_PATH.get(task.endpoint, task.endpoint)}",
                                   credits=diff, usd=meshy_usd(diff), ref=task.task_id,
                                   test_mode=meshy.test_mode)
        trace.record_meshy(updated, raw if raw is not None else obj)
        self._put_task(updated)
        self._ledger_done(updated)
        return updated

    def _ledger_done(self, task: MeshyTask) -> None:
        """The terminal state in the job's ledger; a success also goes to the cross-job cache
        under the input hash of the call that paid for it."""
        self.ledger.done(task)
        if task.status != "SUCCEEDED":
            return
        payer = next((e for e in self.ledger.entries()
                      if e.how == "new" and e.task_id == task.task_id), None)
        if payer is not None:
            MeshyCache(self.store.jobs_dir).add(
                h=payer.hash, test_mode=task.test_mode, job_id=self.job.id, stage=payer.stage,
                attempt=payer.attempt, key=payer.key, task=task)

    async def _get(self, task: MeshyTask, why: str) -> dict[str, Any]:
        counts = self.m.data.setdefault("meshy_gets", {})
        counts[task.task_id] = counts.get(task.task_id, 0) + 1
        log_event("meshy_get", meshy_task_id=task.task_id, why=why)
        self.decide(f"meshy.get.{task.task_id}.{counts[task.task_id]}", why, by="code")
        return await self.svc.meshy.get_task(ENDPOINT_BY_PATH[task.endpoint], task.task_id)

    async def meshy_wait(self, key: str) -> dict[str, Any]:
        """Wait for the task's webhook, confirm it with one GET, return the trimmed result."""
        task = self.task(key)
        assert task is not None, key
        if task.status in TERMINAL:
            late = [e.id for e in self.store.meshy_events(task.task_id) if e.processed_at is None]
            if late:  # e.g. an IN_PROGRESS delivered after SUCCEEDED; terminal status wins
                self.store.mark_meshy_processed(late)
            return self._finish(task)
        deadline = time.monotonic() + self.settings.vendor_wait_s
        while True:
            ev = self.store.terminal_meshy_event(task.task_id)
            if ev is not None:
                raw = None
                if ev.event.get("_source") == "status_get":
                    obj = ev.event  # this run's resume GET, already cached in self._raw
                else:  # webhooks are unsigned: confirm with one GET before acting
                    obj = raw = await self._get(task, f"confirm webhook event {ev.id}")
                ids = [e.id for e in self.store.meshy_events(task.task_id) if e.processed_at is None]
                if str(obj.get("status", "")).upper() in TERMINAL:
                    task = self._apply_terminal(task, obj, raw)
                    self.save(f"meshy_done:{key}")
                    self.store.mark_meshy_processed(ids)
                    log_event("meshy_done", meshy_task_id=task.task_id, status=task.status,
                              credits=task.credits, events=len(ids),
                              duration_ms=round((utcnow() - task.created_at).total_seconds()
                                                * 1000))
                    return self._finish(task)
                self.store.mark_meshy_processed([ev.id])
                self.decide(f"meshy.event_mismatch.{ev.id}", obj.get("status"), by="code",
                            reason="webhook said terminal, GET did not; kept waiting")
                self.save()
                continue
            age = (utcnow() - task.created_at).total_seconds()
            if (task.created_at < self.run_started and task.task_id not in self._resume_gets
                    and status_read_due(self.m, task, self.settings.missed_webhook_s)):
                # Backup status read (FROM JACKSON: approved, 0 credits): a task older than
                # MESHY_MISSED_WEBHOOK_S with no webhook gets one GET, at most once per that
                # interval, whoever wakes the job (a resume, the serve sweep, a message).
                self._resume_gets.add(task.task_id)
                self.m.data.setdefault("meshy_status_reads", {})[task.task_id] = \
                    utcnow().isoformat()
                obj = await self._get(task, f"backup status read: no webhook after "
                                            f"{age / 60:.0f} min")
                if str(obj.get("status", "")).upper() in TERMINAL:
                    if not self.store.meshy_events(task.task_id):
                        log_event("meshy_missed_webhook", meshy_task_id=task.task_id,
                                  status=obj.get("status"), age_s=round(age))
                        self.decide(f"meshy.missed_webhook.{task.task_id}", obj.get("status"),
                                    by="code", reason="terminal on a status read, no webhook row")
                    self._raw[task.task_id] = obj
                    self.store.insert_meshy_event(manifest_safe(obj), source="status_get")
                    continue
                self.save()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.m.status = StageStatus.WAITING_VENDOR
                self.save()
                log_event("meshy_waiting", meshy_task_id=task.task_id)
                raise VendorWaiting([task.task_id])
            with self.waiting():
                await self.store.wait_meshy_event(remaining)

    async def meshy_run(self, key: str, endpoint: str, body: dict[str, Any],
                        purpose: str, *, allow_same: bool = False, fresh: bool = False,
                        why: str | None = None) -> dict[str, Any]:
        await self.meshy_create(key, endpoint, body, purpose, allow_same=allow_same,
                                fresh=fresh, why=why)
        return await self.meshy_wait(key)

    async def fresh_task(self, key: str) -> dict[str, Any]:
        """The task object with live signed asset URLs: from this run, else one GET."""
        task = self.task(key)
        assert task is not None, key
        if task.task_id not in self._raw:
            self._raw[task.task_id] = await self._get(task, "refresh signed asset URLs")
        return self._raw[task.task_id]

    async def fetch_asset(self, key: str, pick, name: str, *, role: str,
                          kind: str | None = None) -> FileRef | None:
        """Download one asset of a finished Meshy task into the attempt folder, once.

        pick(task_object) returns the URL. Assets expire after 3 days, so this runs right after
        the task finishes; on a replay the file is already there and nothing is fetched. URLs
        come from memory or a fresh GET, never from the manifest (which holds no signatures).
        """
        existing = next((o for o in self.m.outputs if o.path == self.rel(name)), None)
        if existing and self.path(name).is_file():
            return existing
        task = self.task(key)
        reused = (self.m.data.get("meshy_reused") or {}).get(task.task_id) if task else None
        if task is not None and reused is not None:
            local = self._local_asset(task, pick, reused)
            if local is not None:  # a reused task: its file is already on disk, 0 requests
                shutil.copyfile(local, self.path(name))
                ref = self.add_output(name, role=role, kind=kind)
                log_event("artifact_reused", name=name, role=role, meshy_task_id=task.task_id)
                self.save(f"fetch:{name}")
                trace.link(task.task_id, ref.path)
                return ref
        try:
            url = pick(await self.fresh_task(key))
            if not url:
                if reused is not None:
                    raise VendorError(f"reused Meshy task {task.task_id} has no {name}",
                                      retryable=True)
                return None
            ref = await self.fetch(url, name, role=role, kind=kind)
        except VendorError as e:
            if reused is not None:  # past retention with no local copy: never reuse it again
                self.ledger.unusable(task.task_id, f"{name}: {e}")
                raise VendorError(f"Reused Meshy task {task.task_id} can no longer give "
                                  f"{name}; the next attempt makes a new one", retryable=True,
                                  code="vendor_failed") from e
            raise
        trace.link(self._keys().get(key), ref.path)
        if task is not None:
            self.ledger.asset(task.task_id, manifest_safe(url), ref.path, ref.sha256)
        return ref

    def _local_asset(self, task: MeshyTask, pick, reused: dict[str, Any]) -> Path | None:
        """The file an earlier attempt or job downloaded from this task at the same URL."""
        try:
            url = pick(task.result or {})
        except Exception:  # noqa: BLE001 a pick written for the live object
            return None
        if not url:
            return None
        asset = manifest_safe(url)
        found = self.ledger.asset_path(task.task_id, asset)
        src_job = reused.get("job_id")
        if found is None and src_job and src_job != self.job.id:
            found = CreditLedger(self.store, str(src_job)).asset_path(task.task_id, asset)
        return found

    async def fetch(self, url: str, name: str, *, role: str, kind: str | None = None) -> FileRef:
        """Download a URL into the attempt folder once. Prefer fetch_asset for Meshy outputs."""
        existing = next((o for o in self.m.outputs if o.path == self.rel(name)), None)
        if existing and self.path(name).is_file():
            return existing
        n = await self.svc.meshy.download(url, self.path(name))
        log_event("artifact_fetch", name=name, bytes=n, role=role)
        ref = self.add_output(name, role=role, kind=kind)
        self.save(f"fetch:{name}")
        return ref

    # ------------------------------------------------------------------ gates
    async def gate(self, key: str, *, gate_kind: GateKind, accept: set[str],
                   prompt_kind: str | None = None, prompt: BaseModel | None = None,
                   body: str | None = None) -> MessageRow:
        """Open a user gate once; return the user's reply or raise GateWaiting."""
        skey = f"gate:{key}"
        g = self.steps.get(skey)
        if g and "reply" in g:
            if not g.get("consumed"):  # the worker died between saving the reply and this
                self.store.consume_message(self.job.id, g["reply"]["id"])
                g["consumed"] = True
            return MessageRow.model_validate(g["reply"])
        if g is None:
            msg_id = None
            if prompt_kind:
                # The prompt's id comes from the gate, so a replay after a crash between the
                # post and the save below finds it instead of asking the customer twice.
                msg = self.store.post_message(
                    self.job.id, "assistant", prompt_kind, prompt, stage=self.stage, body=body,
                    message_id=message_id_for(
                        f"{self.job.id}/{self.stage.value}/a{self.attempt}/gate:{key}"))
                msg_id = msg.id
            g = {"message_id": msg_id, "opened_at": utcnow().isoformat()}
            self.steps[skey] = g
            self.save(f"gate_open:{key}")
            log_event("gate_open", gate=gate_kind, key=key, message_id=msg_id)
        for row in self.store.list_messages(self.job.id, role="user", kinds=accept,
                                            unconsumed=True):
            if row.stage not in (None, self.stage):
                continue
            model = MESSAGE_PAYLOADS[row.kind][1]
            try:
                if model is not None:
                    model.model_validate(row.payload)
            except ValidationError:
                self.store.consume_message(self.job.id, row.id)
                self.store.post_message(self.job.id, "assistant", "note",
                                        {"text": f"Could not read that {row.kind} reply"},
                                        stage=self.stage, reply_to=row.id)
                continue
            # The reply is saved before the message is marked consumed: a worker that dies in
            # between finds the reply here (and consumes it then) instead of waiting forever
            # for an answer the customer already sent.
            log_event("gate_reply", gate=gate_kind, key=key, kind=row.kind, message_id=row.id)
            g["reply"] = row.model_dump(mode="json")
            self.m.gate = None
            self.save()
            self.store.consume_message(self.job.id, row.id)
            g["consumed"] = True
            self.save(f"gate_reply:{key}")
            return row
        self.m.gate = GateRequest(kind=gate_kind, message_id=g["message_id"],
                                  opened_at=datetime.fromisoformat(g["opened_at"]))
        self.m.status = StageStatus.WAITING_USER
        self.save()
        raise GateWaiting(gate_kind)

    def note(self, text: str) -> None:
        self.store.post_message(self.job.id, "assistant", "note", {"text": text}, stage=self.stage)


class Stage:
    id: StageId

    async def run(self, ctx: StageContext) -> None:  # pragma: no cover - interface
        raise NotImplementedError
