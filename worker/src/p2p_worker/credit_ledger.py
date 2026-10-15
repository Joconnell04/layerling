"""Paid-call ledger: every Meshy create is an intent first, and the same inputs are never paid
for twice.

Identity of a call is its input hash: sha256 of the endpoint and the request body, normalized so
the same inputs hash the same in any job and any run (`normalize`): a signed Storage URL becomes
the sha256 of the job file it points at, a data URI the sha256 of its payload; a Meshy task id
stays a task id. Stage, attempt, step key and purpose are not part of it.

Per job (the job folder is the working copy):

  paid_calls.jsonl   append-only events, one JSON line each
    intent     id, hash, stage, attempt, key, purpose, endpoint, expected, why   before the POST
    created    id, task (MeshyTask, manifest-safe)                              after the POST
    done       task                                                             terminal status
    reused     id, hash, stage, attempt, key, purpose, endpoint, task, how, from, saved
    abandoned  id, why       an intent whose POST never landed (refused, or lost in a crash)
    unusable   task_id, why  a task whose outputs can no longer be fetched (past retention)
    refused    hash, stage, attempt, key, purpose, endpoint, expected, reason   pre-flight said no
    asset      task_id, asset (URL without its query), path, sha256            a downloaded file
  credits.json       the per-job credit report, rebuilt after every event

Across jobs (MESHY_REUSE_ACROSS_JOBS; on for live Meshy by default):

  jobs/_worker/meshy_cache.jsonl   hash, test_mode, job_id, stage, attempt, key, task
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from p2p_contracts import MeshyTask

from .util import append_jsonl, manifest_safe, read_jsonl, sha256_file, utcnow, write_json

log = logging.getLogger("p2p_worker.credits")

LEDGER_NAME = "paid_calls.jsonl"
REPORT_NAME = "credits.json"
CACHE_NAME = "meshy_cache.jsonl"
LIVE = {"PENDING", "IN_PROGRESS", "SUCCEEDED"}
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "EXPIRED"}
# A task another job made is reused only inside Meshy's asset retention (3 days, SOURCE:
# https://docs.meshy.ai/api/asset-retention) with the same 60 h margin s03 and s05 chain with.
CROSS_JOB_MAX_AGE = timedelta(hours=60)

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(path), threading.RLock())


# ---------------------------------------------------------------------------- identity
def normalize(body: dict[str, Any], jobs_dir: Path) -> Any:
    """The body as content: manifest_safe (no signatures, data URIs reduced to their hash),
    then every `storage:<bucket>/<job_id>/<path>` replaced by the sha256 of that job file when
    it is on disk, so two jobs sending the same photo compare equal."""
    cache: dict[str, str] = {}

    def one(v: Any) -> Any:
        if isinstance(v, str) and v.startswith("storage:"):
            if v not in cache:
                _, _, rest = v.partition(":")
                parts = rest.split("/", 2)  # bucket, job_id, job-relative path
                p = Path(jobs_dir, parts[1], parts[2]) if len(parts) == 3 else None
                cache[v] = f"file:sha256={sha256_file(p)}" if p and p.is_file() else v
            return cache[v]
        if isinstance(v, dict):
            return {k: one(x) for k, x in v.items()}
        if isinstance(v, list):
            return [one(x) for x in v]
        return v

    return one(manifest_safe(body))


def input_hash(endpoint: str, body: dict[str, Any], jobs_dir: Path) -> str:
    blob = json.dumps({"endpoint": endpoint, "body": normalize(body, jobs_dir)},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------------------- entries
@dataclass
class Entry:
    """One call as the ledger folds it: an intent (new) or a reuse of an earlier task."""

    id: str
    hash: str
    stage: str
    attempt: int
    key: str
    purpose: str
    endpoint: str
    expected: int
    at: str
    how: str = "new"  # new | reused | adopted
    why: str | None = None
    task: dict[str, Any] | None = None
    source: dict[str, Any] | None = None  # reused/adopted: where the task was paid for
    saved: int = 0
    abandoned: str | None = None
    unusable: str | None = None
    order: int = 0

    @property
    def task_id(self) -> str | None:
        return (self.task or {}).get("task_id")

    @property
    def status(self) -> str:
        if self.abandoned:
            return "abandoned"
        if self.task is None:
            return "intent"
        return str(self.task.get("status") or "PENDING")

    @property
    def live(self) -> bool:
        """Succeeded or still running: its output can stand in for a new call."""
        return self.status in LIVE and not self.unusable and self.task_id is not None

    @property
    def assets_job(self) -> str | None:
        """The job whose folder holds this task's downloaded files (the payer's)."""
        return (self.source or {}).get("job_id")

    def meshy_task(self) -> MeshyTask:
        return MeshyTask.model_validate(self.task)


@dataclass
class Report:
    paid: int = 0
    reused: int = 0
    saved: int = 0
    refunded: int = 0
    pending: int = 0
    refused: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)

    def totals(self) -> dict[str, int]:
        return {"paid": self.paid, "reused_calls": self.reused, "saved": self.saved,
                "refunded": self.refunded, "pending": self.pending,
                "refused_calls": self.refused}


class CreditLedger:
    """The job's paid calls (paid_calls.jsonl) and its credit report (credits.json)."""

    def __init__(self, store, job_id: str):
        self.store = store
        self.job_id = job_id
        self.dir = store.job_dir(job_id)
        self.path = self.dir / LEDGER_NAME
        self.report_path = self.dir / REPORT_NAME
        self._lk = _lock(self.path)

    # ------------------------------------------------------------------ write
    def _append(self, event: str, **fields: Any) -> None:
        with self._lk:
            append_jsonl(self.path, {"event": event, "at": utcnow().isoformat(), **fields})
            try:
                self.write_report()
            except Exception as e:  # noqa: BLE001 a report error never blocks a paid call
                log.warning("credit report not written for %s: %s", self.job_id, e)

    def intent(self, *, h: str, stage: str, attempt: int, key: str, purpose: str,
               endpoint: str, expected: int, why: str | None) -> str:
        iid = f"{stage}/a{attempt}/{key}/{uuid.uuid4().hex[:8]}"
        self._append("intent", id=iid, hash=h, stage=stage, attempt=attempt, key=key,
                     purpose=purpose, endpoint=endpoint, expected=expected, why=why)
        return iid

    def created(self, intent_id: str, task: MeshyTask) -> None:
        self._append("created", id=intent_id, task=task.model_dump(mode="json"))

    def done(self, task: MeshyTask) -> None:
        self._append("done", task=task.model_dump(mode="json"))

    def reused(self, *, h: str, stage: str, attempt: int, key: str, purpose: str,
               endpoint: str, task: MeshyTask, how: str, source: dict[str, Any],
               saved: int, why: str | None = None) -> str:
        iid = f"{stage}/a{attempt}/{key}/{uuid.uuid4().hex[:8]}"
        self._append("reused", id=iid, hash=h, stage=stage, attempt=attempt, key=key,
                     purpose=purpose, endpoint=endpoint, task=task.model_dump(mode="json"),
                     how=how, source=source, saved=saved, why=why)
        return iid

    def abandoned(self, intent_id: str, why: str) -> None:
        self._append("abandoned", id=intent_id, why=why[:300])

    def unusable(self, task_id: str, why: str) -> None:
        self._append("unusable", task_id=task_id, why=why[:300])

    def refused(self, *, h: str, stage: str, attempt: int, key: str, purpose: str,
                endpoint: str, expected: int, reason: str) -> None:
        self._append("refused", hash=h, stage=stage, attempt=attempt, key=key, purpose=purpose,
                     endpoint=endpoint, expected=expected, reason=reason[:300])

    def asset(self, task_id: str, asset: str, path: str, sha256: str | None) -> None:
        self._append("asset", task_id=task_id, asset=asset, path=path, sha256=sha256)

    # ------------------------------------------------------------------ read
    def events(self) -> list[dict[str, Any]]:
        return read_jsonl(self.path)

    def entries(self) -> list[Entry]:
        """Every call in the order it was first recorded, with its latest task state."""
        out: dict[str, Entry] = {}
        by_task: dict[str, list[Entry]] = {}
        unusable: dict[str, str] = {}
        for n, ev in enumerate(self.events()):
            kind = ev.get("event")
            if kind in ("intent", "reused"):
                e = Entry(id=ev["id"], hash=ev["hash"], stage=ev["stage"],
                          attempt=int(ev["attempt"]), key=ev["key"], purpose=ev["purpose"],
                          endpoint=ev["endpoint"], expected=int(ev.get("expected") or 0),
                          at=ev["at"], why=ev.get("why"), order=n)
                if kind == "reused":
                    e.how = ev.get("how") or "reused"
                    e.task = ev.get("task")
                    e.source = ev.get("source")
                    e.saved = int(ev.get("saved") or 0)
                    e.expected = 0
                    if e.task_id:
                        by_task.setdefault(e.task_id, []).append(e)
                out[e.id] = e
            elif kind == "created" and ev.get("id") in out:
                e = out[ev["id"]]
                e.task = ev.get("task")
                if e.task_id:
                    by_task.setdefault(e.task_id, []).append(e)
            elif kind == "done":
                t = ev.get("task") or {}
                for e in by_task.get(t.get("task_id"), []):
                    if e.how == "reused":  # a finished copy keeps its own record
                        continue
                    credits = t.get("credits", 0) if e.how == "new" else (e.task or {}).get(
                        "credits", 0)
                    e.task = {**t, "credits": credits}
            elif kind == "abandoned" and ev.get("id") in out:
                out[ev["id"]].abandoned = ev.get("why") or "abandoned"
            elif kind == "unusable":
                unusable[ev.get("task_id")] = ev.get("why") or "unusable"
        for tid, why in unusable.items():
            for e in by_task.get(tid, []):
                e.unusable = why
        return sorted(out.values(), key=lambda e: e.order)

    def find(self, h: str) -> Entry | None:
        """The newest call with these inputs that can stand in for a new one (succeeded or
        running), else the newest one whose outcome is unknown (an intent with no task: a
        crash between the POST and the record). None when every earlier call failed, was
        refunded or never went out."""
        same = [e for e in self.entries() if e.hash == h]
        for e in reversed(same):
            if e.live:
                return e
        for e in reversed(same):
            if e.status == "intent" and e.how == "new":
                return e
        return None

    def known_task_ids(self) -> set[str]:
        return {e.task_id for e in self.entries() if e.task_id}

    def asset_path(self, task_id: str, asset: str) -> Path | None:
        """A file this job already downloaded from `task_id` at URL `asset` (no query)."""
        for ev in reversed(self.events()):
            if ev.get("event") == "asset" and ev.get("task_id") == task_id \
                    and ev.get("asset") == asset:
                p = self.dir / ev["path"]
                if p.is_file() and (not ev.get("sha256") or sha256_file(p) == ev["sha256"]):
                    return p
        return None

    def paid_edits(self, stage: str, prefix: str) -> int:
        """Paid (not reused, not refunded) calls of a stage whose purpose starts with prefix."""
        n = 0
        for e in self.entries():
            if e.how == "new" and e.stage == stage and e.purpose.startswith(prefix) \
                    and e.status not in ("abandoned", "FAILED", "CANCELED", "EXPIRED"):
                n += 1
        return n

    # ------------------------------------------------------------------ report
    def _from(self, src: dict[str, Any] | None) -> str | None:
        if not src:
            return None
        where = f"{src.get('stage')}/a{src.get('attempt')} {src.get('key')}"
        job = src.get("job_id")
        return where if job in (None, self.job_id) else f"job {str(job)[:8]} {where}"

    def report(self) -> Report:
        """paid: credits booked for this job's own calls (final once finished, the estimate
        while running), which is what its spend rows add up to; pending: the part of paid
        still running; refunded: credits Meshy gave back for failed tasks; saved: what the
        reused calls would have cost."""
        r = Report()
        for e in self.entries():
            t = e.task or {}
            credits = 0
            if e.how == "new":
                st = e.status
                if st == "intent":  # outcome unknown (a crash between POST and record)
                    r.pending += e.expected
                elif st in ("PENDING", "IN_PROGRESS"):
                    credits = int(t.get("credits") or 0)
                    r.pending += credits
                elif st != "abandoned":
                    credits = int(t.get("credits") or 0)
                    if t.get("refunded"):
                        r.refunded += e.expected
                r.paid += credits
            else:
                r.reused += 1
                r.saved += e.saved
            r.calls.append({
                "stage": e.stage, "attempt": e.attempt, "key": e.key, "purpose": e.purpose,
                "endpoint": e.endpoint, "why": e.why, "how": e.how, "credits": credits,
                "expected": e.expected, "saved": e.saved, "task_id": e.task_id,
                "status": e.status, "hash": e.hash[:16], "at": e.at,
                "from": self._from(e.source), "note": e.abandoned or e.unusable,
            })
        for ev in self.events():
            if ev.get("event") == "refused":
                r.refused += 1
                r.calls.append({
                    "stage": ev.get("stage"), "attempt": ev.get("attempt"), "key": ev.get("key"),
                    "purpose": ev.get("purpose"), "endpoint": ev.get("endpoint"), "why": None,
                    "how": "refused", "credits": 0, "expected": ev.get("expected", 0),
                    "saved": 0, "task_id": None, "status": "refused",
                    "hash": str(ev.get("hash", ""))[:16], "at": ev.get("at"), "from": None,
                    "note": ev.get("reason"),
                })
        r.calls.sort(key=lambda c: str(c.get("at")))
        return r

    def spend_table_credits(self) -> int:
        """Meshy credits in the job's spend rows (ledger.jsonl, the local mirror of `spend`)."""
        return sum(int(x.get("credits") or 0) for x in read_jsonl(self.dir / "ledger.jsonl")
                   if x.get("vendor") == "meshy")

    def write_report(self) -> dict[str, Any]:
        r = self.report()
        out = {"job_id": self.job_id, "updated_at": utcnow().isoformat(),
               "credits": {**r.totals(), "spend_table": self.spend_table_credits()},
               "calls": r.calls}
        write_json(self.report_path, out)
        return out

    def summary_line(self) -> str:
        t = self.report().totals()
        line = f"Meshy credits: {t['paid']} paid"
        if t["reused_calls"]:
            line += f", {t['reused_calls']} reused (saved {t['saved']})"
        if t["refunded"]:
            line += f", {t['refunded']} refunded"
        return line


# ---------------------------------------------------------------------------- across jobs
class MeshyCache:
    """Succeeded tasks by input hash, across jobs (jobs/_worker/meshy_cache.jsonl)."""

    def __init__(self, jobs_dir: Path):
        self.path = Path(jobs_dir) / "_worker" / CACHE_NAME
        self._lk = _lock(self.path)

    def add(self, *, h: str, test_mode: bool, job_id: str, stage: str, attempt: int, key: str,
            task: MeshyTask) -> None:
        with self._lk:
            append_jsonl(self.path, {"hash": h, "test_mode": test_mode, "job_id": job_id,
                                     "stage": stage, "attempt": attempt, "key": key,
                                     "task": task.model_dump(mode="json")})

    def find(self, h: str, *, test_mode: bool, not_job: str,
             max_age: timedelta = CROSS_JOB_MAX_AGE, now: datetime | None = None
             ) -> dict[str, Any] | None:
        now = now or utcnow()
        for row in reversed(read_jsonl(self.path)):
            if row.get("hash") != h or bool(row.get("test_mode")) != test_mode \
                    or row.get("job_id") == not_job:
                continue
            t = row.get("task") or {}
            if t.get("status") != "SUCCEEDED" or not t.get("finished_at"):
                continue
            if now - datetime.fromisoformat(t["finished_at"]) > max_age:
                continue
            return row
        return None
