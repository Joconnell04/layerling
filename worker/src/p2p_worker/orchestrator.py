"""Orchestrator: the per-job state machine over the profile's stages.

advance(job_id) runs stages in order until the job is blocked (a user gate, a Meshy webhook, a
pause) or done. It is idempotent: calling it again resumes where the manifests say the job is.

Per stage:
- SUCCEEDED and inputs unchanged: skip. Inputs changed (file hash, or the upstream stage's
  accepted attempt moved): mark STALE, start a new attempt.
- RUNNING / WAITING_*: resume the same attempt (crash recovery, gate reply, webhook).
- FAILED and retryable: a retry plan (lifecycle.plan_retry) after a backoff. A failure that left
  the attempt's state good (a network blip, a timeout, an interrupted worker, a failed Claude
  call) replays the same attempt: nothing paid is bought again, gate replies stand. Any other
  failure starts a new attempt that adopts the failed one's paid Meshy tasks
  (Services.adopt_tasks, lifecycle.default_adopt), up to MAX_STAGE_RETRIES retries, then PAUSED
  (retry_limit).
- A RUNNING attempt whose worker is gone (its lease, `data.run`) was interrupted: it replays in
  place, unless its runner cannot replay (the paint agent) or it keeps killing the worker.
- Every run is supervised: limits per stage (lifecycle.STAGE_LIMITS), one `stage_running` log
  line per minute, and a run over its limit is cancelled and fails as vendor_timeout.
- PAUSED: stays paused until someone resumes the job (status back to queued/running).
- Stages outside the profile get a SKIPPED manifest.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from p2p_contracts import (
    STAGES,
    JobRecord,
    JobStatus,
    Manifest,
    StageError,
    StageId,
    StageStatus,
)
from p2p_contracts.job import (
    AddOn,
    Budget,
    Photo,
    PrintSettings,
    ProductType,
)
from p2p_contracts.payloads import PaletteEdit, Resume
from p2p_contracts.stages import PROFILE_FULL, PROFILE_THIN_SLICE

from . import __version__, lifecycle, trace
from .errors import (
    REVIEW_CODES,
    BudgetExceeded,
    GateWaiting,
    NeedsReview,
    StagePaused,
    VendorError,
    VendorWaiting,
    is_transient,
    review_kind,
)
from .logs import log_context, log_event
from .paint_edit import apply_paint_edits, resume_open_paint_edit
from .stages import RUNNERS
from .stages.base import Services, StageContext
from .util import sha256_file, utcnow, write_json


class NoMeshy:
    """Stands in for the Meshy client in run-stages: any use fails the stage, spends nothing."""

    test_mode = True

    def __getattr__(self, name: str):
        raise VendorError(f"Meshy is off for run-stages (tried {name}); pass --allow-meshy",
                          code="validation", retryable=False)

log = logging.getLogger("p2p_worker")

PROFILES = {"thin_slice": PROFILE_THIN_SLICE, "full": PROFILE_FULL}
OPEN = {StageStatus.PENDING, StageStatus.RUNNING, StageStatus.WAITING_USER,
        StageStatus.WAITING_VENDOR}
# DONE is not blocking: advance re-checks input hashes and reruns stale stages.
BLOCKING = {JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.PAUSED, JobStatus.REVIEW}


class Orchestrator:
    def __init__(self, svc: Services):
        self.svc = svc
        self.store = svc.store
        # This worker process (a new one after every restart): the lease on attempts it runs.
        self.boot = uuid.uuid4().hex[:12]
        # job id -> wall-clock time a stage retry is due (serve schedules a kick for it)
        self.wake_at: dict[str, float] = {}

    def retry_wait_s(self, job_id: str) -> float:
        """Seconds until the job's stage retry is due (0 when none is waiting)."""
        at = self.wake_at.get(job_id)
        return max(0.0, at - time.time()) if at else 0.0

    # ------------------------------------------------------------------ jobs
    def create_job(
        self,
        prompt: str,
        photos: list[Path],
        *,
        product_type: str = "figurine",
        target_size_mm: float | None = None,
        addons: list | None = None,
        ams_slots: int | None = None,
        profile: str = "thin_slice",
        job_id: str | None = None,
        form: str | None = None,
    ) -> JobRecord:
        job_id = job_id or str(uuid.uuid4())
        jdir = self.store.job_dir(job_id)
        (jdir / "input").mkdir(parents=True, exist_ok=True)
        recs = []
        for i, src in enumerate(photos, start=1):
            ext = src.suffix.lower() or ".jpg"
            rel = f"input/photo_{i}{ext}"
            (jdir / rel).write_bytes(Path(src).read_bytes())
            recs.append(Photo(path=rel, sha256=sha256_file(jdir / rel)))
        form_addons = [AddOn.model_validate(a) for a in addons or []]
        write_json(jdir / "input" / "request.json", {  # what the new-job form set
            "prompt": prompt, "product_type": product_type, "target_size_mm": target_size_mm,
            "addons": [a.model_dump(mode="json") for a in form_addons], "form": form,
        })
        s = self.svc.settings
        job = JobRecord(
            id=job_id, created_at=utcnow(), prompt=prompt,
            product_type=ProductType(product_type), target_size_mm=target_size_mm, photos=recs,
            addons=form_addons,
            settings=PrintSettings(ams_slots=ams_slots) if ams_slots else PrintSettings(),
            budget=Budget(cap_usd=s.job_cap_usd, cap_credits=s.job_cap_credits),
            status=JobStatus.QUEUED, profile=profile,  # type: ignore[arg-type]
        )
        self.store.save_job(job)
        self.store.post_message(job_id, "user", "prompt", {}, body=prompt)
        return job

    def _set_job(self, job: JobRecord, status: JobStatus, stage: StageId | None = None,
                 **row: Any) -> None:
        job.status = status
        if stage is not None:
            job.stage = stage
        if "pause_reason" in row:
            job.pause_reason = row["pause_reason"]
        self.store.save_job(job)
        log_event("job_status", job_id=job.id, status=status.value,
                  stage=job.stage.value if job.stage else None,
                  pause_reason=job.pause_reason if status == JobStatus.PAUSED else None)
        self.store.update_job_row(job.id, status=status.value,
                                  stage=job.stage.value if job.stage else None, **row)

    def resume_job(self, job_id: str) -> None:
        """Clear a pause (after Jackson raises a cap or fixes the cause).

        A budget pause resumes the same attempt: the check ran before any spend. Any other pause
        (retry limit, round caps) starts a fresh attempt.
        """
        job = self.store.load_job(job_id)
        if job.status not in (JobStatus.PAUSED, JobStatus.REVIEW):
            return
        for sid in PROFILES[job.profile]:
            n = job.attempts.get(sid)
            m = self.store.load_manifest(job_id, sid, n) if n else None
            if m and m.status == StageStatus.PAUSED:
                budget = m.error is not None and m.error.code == "budget_exceeded"
                m.status = StageStatus.RUNNING if budget else StageStatus.STALE
                m.data["resumed_at"] = utcnow().isoformat()
                m.data.pop("run", None)  # not interrupted: the next run takes the lease
                m.pause_reason = None
                self.store.write_manifest(m)
        self._set_job(job, JobStatus.QUEUED, pause_reason=None, review_reason=None)

    def rerun_stage(self, job_id: str, stage: StageId) -> None:
        """Start a fresh attempt of one stage (and, through staleness, everything after it)."""
        job = self.store.load_job(job_id)
        n = job.attempts.get(stage)
        m = self.store.load_manifest(job_id, stage, n) if n else None
        if m is not None:
            m.status = StageStatus.STALE
            m.data["stale_reason"] = "rerun requested"
            self.store.write_manifest(m)
        self._set_job(job, JobStatus.QUEUED, stage, pause_reason=None, review_reason=None)

    def _to_review(self, job: JobRecord, stage: StageId, m: Manifest) -> None:
        """Job -> REVIEW with the stage's reason. The row's confidence is the stage's own on a
        fidelity review (the gate passed) and 0 when the gate failed or has not run."""
        conf = (m.confidence if m.confidence is not None else 0.0) \
            if review_kind(m) == "fidelity" else 0.0
        self._set_job(job, JobStatus.REVIEW, stage, review_reason=m.pause_reason,
                      confidence=conf)

    def _review_manifest(self, job: JobRecord) -> Manifest | None:
        n = job.attempts.get(job.stage) if job.stage else None
        return self.store.load_manifest(job.id, job.stage, n) if n else None

    def _approve_review(self, job: JobRecord, row: Any, m: Manifest) -> None:
        """A fidelity review approved as it is: the paused stage's attempt is accepted (its
        outputs stand), the decision is recorded, and the job goes on to the next stage. The
        file still ships only through the s10 slicer gate."""
        from p2p_contracts import Decision

        notes = (row.payload or {}).get("notes")
        m.data["review_approved"] = {"message_id": row.id, "reason": m.pause_reason,
                                     "notes": notes, "at": utcnow().isoformat()}
        m.decisions = [d for d in m.decisions if d.key != "review.approved"] + [
            Decision(key="review.approved", value="approve", by="user",
                     reason=(notes or m.pause_reason or "")[:500] or None, at=utcnow())]
        m.status, m.error, m.pause_reason = StageStatus.SUCCEEDED, None, None
        self.store.write_manifest(m)
        self._set_job(job, JobStatus.QUEUED, pause_reason=None, review_reason=None)
        log_event("review_approved", job_id=job.id, stage=m.stage.value, attempt=m.attempt)

    def _apply_review(self, job: JobRecord) -> JobRecord:
        """Consume review_decision (or resume) messages on a job in REVIEW.

        rerun_paint or resume: fresh attempt of the stage that asked for review.
        reject: the job fails. approve: a fidelity review (a paint or concept check failed, the
        slicer gate passed or runs later at s10) is accepted and the job goes on; the file
        still ships only through s10's gate. Refused when the file failed the slicer gate, or
        when only p2p_slicer checked it ("Bambu gate pending"): those never ship.

        A decision is applied first and marked consumed after, so a worker that dies in between
        still has it (a decision consumed first and lost there left the job in REVIEW with the
        customer's answer gone). A decision older than the review request it would answer is
        left over from an earlier review: consumed, not applied.
        """
        rows = self.store.list_messages(job.id, role="user",
                                        kinds={"review_decision", "resume"}, unconsumed=True)
        asked = [m for m in self.store.list_messages(job.id, role="assistant",
                                                     kinds={"review_request"})]
        opened = asked[-1].created_at if asked else None
        for row in rows:
            job = self.store.load_job(job.id)
            if job.status != JobStatus.REVIEW or (opened and row.created_at < opened):
                self.store.consume_message(job.id, row.id)
                continue
            action = row.payload.get("action") if row.kind == "review_decision" else "rerun_paint"
            log_event("review_decision", job_id=job.id, action=action)
            if action == "reject":
                self._set_job(job, JobStatus.FAILED, review_reason=None)
                self._reply(job.id, row, "Rejected. Job closed.")
            elif action == "approve":
                m = self._review_manifest(job)
                kind = review_kind(m) if m is not None else None
                if kind == "fidelity" and m is not None and m.status == StageStatus.PAUSED:
                    self._approve_review(job, row, m)
                    self._reply(job.id, row, "Approved as it is. Writing the final file and "
                                             "running the slicer gate.")
                else:
                    self._reply(job.id, row, (
                        "Cannot approve: only p2p_slicer checked this file. Rerun on a Mac "
                        "worker, where Bambu Studio gates it, or reject."
                        if kind == "estimate" else
                        "Cannot approve: the file failed the slicer gate. Rerun or reject."))
            else:
                self.resume_job(job.id)
                self._reply(job.id, row, "Rerunning.")
            self.store.consume_message(job.id, row.id)
        return self.store.load_job(job.id)

    def _reply(self, job_id: str, row: Any, text: str, tag: str = "") -> None:
        """A note answering a user message, once (its id comes from the message it answers)."""
        from .store import message_id_for

        self.store.post_message(job_id, "assistant", "note", {"text": text}, reply_to=row.id,
                                message_id=message_id_for(f"reply/{row.id}/{tag}"))

    def _apply_resume(self, job: JobRecord) -> JobRecord:
        """Consume `resume` messages: apply new caps, clear the pause."""
        rows = self.store.list_messages(job.id, role="user", kinds={"resume"}, unconsumed=True)
        for row in rows:
            self.store.consume_message(job.id, row.id)
            try:
                r = Resume.model_validate(row.payload)
            except ValueError:
                self.store.post_message(job.id, "assistant", "note",
                                        {"text": "Could not read that resume request"},
                                        reply_to=row.id)
                continue
            job = self.store.load_job(job.id)
            if job.status != JobStatus.PAUSED:
                self.store.post_message(job.id, "assistant", "note",
                                        {"text": "Job is not paused"}, reply_to=row.id)
                continue
            if r.cap_usd is not None:
                job.budget.cap_usd = r.cap_usd
            if r.cap_credits is not None:
                job.budget.cap_credits = r.cap_credits
            self.store.save_job(job)
            self.store.update_job_row(job.id, budget_usd=job.budget.cap_usd,
                                      budget_credits=job.budget.cap_credits)
            self.resume_job(job.id)
            log_event("job_resumed", job_id=job.id, cap_usd=job.budget.cap_usd,
                      cap_credits=job.budget.cap_credits)
            self.store.post_message(job.id, "assistant", "note", {
                "text": f"Resumed. Caps ${job.budget.cap_usd:.2f}, {job.budget.cap_credits} credits"
            }, reply_to=row.id)
        return self.store.load_job(job.id)

    # ------------------------------------------------------------------ palette edits
    def _shipped_paint_state(self, job: JobRecord) -> str | None:
        """Job path of the paint state the current result was written from: the paint_state
        input of s10's accepted attempt, else s09's or s08's output. None on a thin-slice job
        (its result came from s07)."""
        n = job.attempts.get(StageId.S10_WRITE)
        m10 = self.store.load_manifest(job.id, StageId.S10_WRITE, n) if n else None
        if m10 is not None:
            p = next((i.path for i in m10.inputs if i.role == "paint_state"), None)
            if p:
                return p
        for sid in (StageId.S09_PAINT, StageId.S08_PALETTE):
            n = job.attempts.get(sid)
            m = self.store.load_manifest(job.id, sid, n) if n else None
            if m is not None and m.status == StageStatus.SUCCEEDED:
                p = next((o.path for o in m.outputs if o.role == "paint_state"), None)
                if p:
                    return p
        return None

    def _check_palette_edit(self, job: JobRecord, edit: PaletteEdit
                            ) -> tuple[str | None, str | None]:
        """(problem for a note, or None; the shipped paint state path)."""
        from p2p_contracts import PaintState

        from . import printing as pr
        from .stages.adapters import library_for
        from .util import read_json

        root = self.svc.settings.repo_root
        library, _ = library_for(self.store, job.id, root)
        known = {f.id for f in (library.filaments if library else [])}
        known |= {f.id for f in pr.load_catalog(root)}
        unknown = [i for i in edit.slots if i not in known]
        if unknown:
            return f"Unknown filaments: {', '.join(unknown)}", None
        if len(set(edit.slots)) != len(edit.slots):
            return "Each filament can take one slot", None
        ams = job.settings.ams_slots
        if len(edit.slots) > ams:
            return f"{len(edit.slots)} slots, AMS has {ams}", None
        if edit.max_colors is not None and edit.max_colors < 1:
            return "max_colors must be 1 or more", None
        prev = self._shipped_paint_state(job)
        if edit.mode == "recolor":
            if prev:
                count = len(PaintState.model_validate(
                    read_json(self.store.job_dir(job.id) / prev)).palette)
            else:  # thin slice: s08's first pass uses the loaded spools
                count = len(pr.candidates(library)) if library else 0
            if len(edit.slots) != count:
                return f"Recolor needs {count} slots, got {len(edit.slots)}", None
        return None, prev

    async def _apply_palette_edits(self, job: JobRecord) -> JobRecord:
        """Consume `palette_edit` messages on a job that has a result (DONE, or REVIEW after a
        result). Each valid edit runs s08 (the edit applied), s09 (the stub carries s08's paint
        state, so the paint agent never undoes the edit) when the profile has it, and s10 as new
        attempts. Pass: a new `result`, job DONE. Gate fail: REVIEW as usual. Invalid edits get
        a `note`. Edits on a running or waiting job wait until it finishes."""
        rows = self.store.list_messages(job.id, role="user", kinds={"palette_edit"},
                                        unconsumed=True)
        for row in rows:
            job = self.store.load_job(job.id)
            closed = job.status in (JobStatus.FAILED, JobStatus.CANCELLED)
            if not closed and job.status not in (JobStatus.DONE, JobStatus.REVIEW):
                break  # still working: apply once it finishes
            self.store.consume_message(job.id, row.id)

            def note(text: str, row=row, job_id=job.id) -> None:
                self.store.post_message(job_id, "assistant", "note", {"text": text},
                                        reply_to=row.id)

            if closed:
                note("Job is closed")
                continue
            if not job.result:
                note("No result to edit yet")
                continue
            try:
                edit = PaletteEdit.model_validate(row.payload)
            except ValueError:
                note("Could not read that palette edit")
                continue
            problem, prev = self._check_palette_edit(job, edit)
            if problem:
                log_event("palette_edit", job_id=job.id, mode=edit.mode, rejected=problem)
                note(problem)
                continue
            await self._run_palette_edit(job, row.id, edit, prev)
        return self.store.load_job(job.id)

    async def _run_palette_edit(self, job: JobRecord, message_id: str, edit: PaletteEdit,
                                prev: str | None) -> JobStatus:
        from .stages.s09_stub import PaintStub

        log_event("palette_edit", job_id=job.id, mode=edit.mode, source=edit.source,
                  slots=len(edit.slots), max_colors=edit.max_colors)
        orch = Orchestrator(replace(self.svc, meshy=NoMeshy()))  # this path never calls Meshy
        stages = [StageId.S08_PALETTE]
        if StageId.S09_PAINT in PROFILES[job.profile]:
            stages.append(StageId.S09_PAINT)
        stages.append(StageId.S10_WRITE)
        data = {**edit.model_dump(mode="json"), "message_id": message_id,
                "previous_paint_state": prev}
        for stage in stages:
            m = orch._new_attempt(job, stage)
            if stage == StageId.S08_PALETTE:
                m.data["palette_edit"] = data  # s08 applies it; replays re-apply it
            else:
                m.data["palette_edit_message"] = message_id
            self.store.write_manifest(m)
            runner = PaintStub() if stage == StageId.S09_PAINT else None
            status = await orch._run_stage(job, stage, m, runner)
            job = self.store.load_job(job.id)
            if status == StageStatus.SUCCEEDED:
                continue
            if status == StageStatus.PAUSED and review_kind(m) is not None:
                self._to_review(job, stage, m)
                return JobStatus.REVIEW
            reason = (f"palette edit failed at {stage.value}: "
                      f"{m.error.message if m.error else status.value}")[:500]
            self._set_job(job, JobStatus.PAUSED, stage, pause_reason=reason)
            self.store.post_message(job.id, "assistant", "note", {"text": reason},
                                    reply_to=message_id)
            return JobStatus.PAUSED
        self._set_job(job, JobStatus.DONE, StageId.S10_WRITE, pause_reason=None,
                      review_reason=None)
        return JobStatus.DONE

    # ------------------------------------------------------------------ staleness
    def _stale_reason(self, job: JobRecord, m: Manifest) -> str | None:
        jdir = self.store.job_dir(job.id)
        for i in m.inputs:
            p = jdir / i.path
            if not p.is_file():
                return f"input missing: {i.path}"
            if sha256_file(p) != i.sha256:
                return f"input changed: {i.path}"
            if i.from_stage and job.attempts.get(i.from_stage) != i.from_attempt:
                return f"{i.from_stage.value} moved to attempt {job.attempts.get(i.from_stage)}"
        return None

    # ------------------------------------------------------------------ stages
    def _new_attempt(self, job: JobRecord, stage: StageId) -> Manifest:
        """The next attempt number after the job's record AND any folder already on disk (an
        evidence run such as p2p_mcp_paint.proof can leave a<N> folders the record does not
        know; reusing one would load its stale paint state)."""
        n = job.attempts.get(stage, 0) + 1
        d = self.store.job_dir(job.id) / stage.value if hasattr(self.store, "job_dir") else None
        if d is not None and d.is_dir():
            on_disk = [int(p.name[1:]) for p in d.glob("a*") if p.name[1:].isdigit()]
            n = max([n] + [k + 1 for k in on_disk])
        job.attempts[stage] = n
        m = Manifest(job_id=job.id, stage=stage, attempt=n, status=StageStatus.RUNNING,
                     started_at=utcnow())
        self.store.write_manifest(m)
        self.store.save_job(job)
        return m

    def _count_failures(self, job: JobRecord, stage: StageId) -> int:
        """Consecutive FAILED attempts, newest first (a STALE or resumed attempt resets it). An
        attempt closed only because the worker stopped under it (interrupted, no crash loop)
        is not the stage's failure: skipped, not counted."""
        n = 0
        for a in range(job.attempts.get(stage, 0), 0, -1):
            m = self.store.load_manifest(job.id, stage, a)
            if not m or m.status != StageStatus.FAILED:
                break
            d = (m.error.detail if m.error else None) or {}
            if d.get("interrupted") and not d.get("crash_loop"):
                continue
            n += 1
        return n

    def _interrupted_attempts(self, job: JobRecord, stage: StageId, upto: int) -> list:
        """The last checkpoints of the attempts before `upto` (inclusive) that were closed as
        interrupted, in a row, oldest first."""
        out: list = []
        for a in range(upto, 0, -1):
            m = self.store.load_manifest(job.id, stage, a)
            d = (m.error.detail if m and m.error else None) or {}
            if not m or m.status != StageStatus.FAILED or not d.get("interrupted"):
                break
            out.append(d.get("last"))
        return out[::-1]

    def _close(self, m: Manifest, status: StageStatus) -> None:
        m.status = status
        m.finished_at = utcnow()
        if m.started_at:
            m.duration_s = round((m.finished_at - m.started_at).total_seconds(), 3)
        self.store.write_manifest(m)

    def _hook(self, point: str) -> None:
        """Stage boundaries for Services.on_checkpoint (the chaos tests kill the worker here)."""
        if self.svc.on_checkpoint:
            self.svc.on_checkpoint(point)

    async def _run_stage(self, job: JobRecord, stage: StageId, m: Manifest,
                         runner: Any = None) -> StageStatus:
        t0 = time.monotonic()
        with log_context(job_id=job.id, stage=stage.value, attempt=m.attempt):
            log_event("stage_start", resumed=m.status != StageStatus.RUNNING or None)
            self._hook(f"{stage.value}:start")
            status = await self._run_stage_inner(job, stage, m, runner)
            self._hook(f"{stage.value}:end:{status.value}")
            log_event("stage_end", status=status.value,
                      duration_ms=round((time.monotonic() - t0) * 1000),
                      pause_reason=m.pause_reason if status == StageStatus.PAUSED else None,
                      error=m.error.message if status == StageStatus.FAILED and m.error else None,
                      level=logging.WARNING if status in (StageStatus.FAILED, StageStatus.PAUSED)
                      else logging.INFO)
            return status

    async def _run_stage_inner(self, job: JobRecord, stage: StageId, m: Manifest,
                               runner: Any = None) -> StageStatus:
        runner = runner or RUNNERS[stage]
        self._adopt_paid_tasks(job, stage, m)
        ctx = StageContext(self.svc, job, stage, m.attempt, m, run_started=utcnow())
        m.status = StageStatus.RUNNING
        m.data["run"] = lifecycle.lease(self.boot, self.svc.settings.worker_id)
        ctx.save()
        self._set_job(job, JobStatus.RUNNING, stage)
        try:
            with trace.attempt(ctx):
                await self._supervise(ctx, runner)
        except GateWaiting:
            return StageStatus.WAITING_USER
        except VendorWaiting:
            return StageStatus.WAITING_VENDOR
        except NeedsReview as e:
            m.pause_reason = e.reason
            m.error = StageError(code=REVIEW_CODES[e.kind], message=e.reason,  # type: ignore[arg-type]
                                 retryable=False, detail=e.detail)
            self._close(m, StageStatus.PAUSED)
            return StageStatus.PAUSED
        except (BudgetExceeded, StagePaused) as e:
            m.pause_reason = e.reason
            m.error = StageError(
                code="budget_exceeded" if isinstance(e, BudgetExceeded) else "retry_limit",
                message=e.reason, retryable=False, detail=e.detail,
            )
            self._close(m, StageStatus.PAUSED)
            return StageStatus.PAUSED
        except VendorError as e:
            code = e.code if e.code in {"budget_exceeded", "vendor_failed", "vendor_timeout",
                                        "validation", "internal"} else "vendor_failed"
            detail = dict(e.detail)
            if e.retryable and is_transient(e) and not detail.get("transient"):
                detail["transient"] = True
            m.error = StageError(code=code, message=str(e), retryable=e.retryable,  # type: ignore[arg-type]
                                 detail=detail)
            self._close(m, StageStatus.FAILED)
            return StageStatus.FAILED
        except Exception as e:  # a bug or an unexpected vendor shape: retryable once or twice
            log.exception("stage %s failed", stage.value)
            m.error = StageError(code="internal", message=f"{type(e).__name__}: {e}",
                                 retryable=True,
                                 detail={"transient": True} if is_transient(e) else {})
            self._close(m, StageStatus.FAILED)
            return StageStatus.FAILED
        m.error = None
        m.gate = None
        self._close(m, StageStatus.SUCCEEDED)
        return StageStatus.SUCCEEDED

    async def _supervise(self, ctx: StageContext, runner: Any) -> None:
        """runner.run(ctx) as a task under the stage watchdog: over its limit (no progress for
        idle_s, or total_s of active time; lifecycle.STAGE_LIMITS) the run is cancelled, which
        kills a child process or claude -p under it, and fails as vendor_timeout. Logs one
        `stage_running` line per job every progress_log_s (60 s) while it runs."""
        from .offload import children_of

        stage = ctx.stage
        info = lifecycle.start_run(ctx.job.id, stage, ctx.attempt,
                                   lifecycle.limits_for(stage, self.svc.settings))
        ctx.run = info
        every = float(getattr(self.svc.settings, "progress_log_s", 60.0) or 60.0)
        task = asyncio.create_task(runner.run(ctx))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=min(lifecycle.WATCH_TICK_S, every))
                if done:
                    task.result()
                    return
                over = info.over()
                if over:
                    msg = lifecycle.timeout_message(info, over)
                    log_event("stage_timeout", level=logging.WARNING, msg=msg, over=over,
                              last=info.last_what)
                    task.cancel()
                    await asyncio.wait({task}, timeout=lifecycle.CANCEL_GRACE_S)
                    raise VendorError(msg, code="vendor_timeout", retryable=True,
                                      detail={"timeout": over, "last": info.last_what,
                                              "limit_s": info.limits.idle_s if over == "idle"
                                              else info.limits.total_s})
                now = time.monotonic()
                if now - info.logged >= every:
                    info.logged = now
                    lease = ctx.m.data.get("run")
                    if isinstance(lease, dict) and lease.get("last") != info.last_what:
                        lease["last"] = info.last_what  # how far it got, for crash loops
                        self.store.write_manifest(ctx.m)
                    snap = info.snapshot()
                    log_event("stage_running", active_s=snap["active_s"], idle_s=snap["idle_s"],
                              waiting=snap["waiting"] or None, last=snap["last"],
                              children=children_of(ctx.job.id) or None)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.wait({task}, timeout=lifecycle.CANCEL_GRACE_S)
            if task.done() and not task.cancelled():
                task.exception()  # retrieved: no "never retrieved" warning after a timeout
            lifecycle.end_run(info)

    def _adopt_paid_tasks(self, job: JobRecord, stage: StageId, m: Manifest) -> None:
        """A new attempt after a FAILED one carries over that attempt's paid Meshy tasks that
        are in flight or succeeded (and creates whose outcome it never learned), under the same
        step keys, so it waits for them instead of paying again (live2 Bronco s05: a dropped
        Supabase connection failed the attempt and the retry went to buy a second 3D model).
        Spend stays on the attempt that made them. Runs once per attempt.

        The carry-over is Services.adopt_tasks when set (the credit guard's), else
        lifecycle.default_adopt. Interface: adopt(prev, new) -> {step key: task id}, editing
        `new` in place (data.meshy_keys, meshy_tasks, data.meshy_pending)."""
        if m.attempt < 2 or "adopted_from" in m.data:
            return
        prev = self.store.load_manifest(job.id, stage, m.attempt - 1)
        if prev is None or prev.status != StageStatus.FAILED or prev.error is None:
            return
        if not prev.error.retryable:
            return
        adopt = getattr(self.svc, "adopt_tasks", None) or lifecycle.default_adopt
        adopted = adopt(prev, m)
        m.data["adopted_from"] = {"attempt": prev.attempt, "tasks": adopted,
                                  "reason": prev.error.message[:200]}
        self.store.write_manifest(m)
        if adopted:
            log_event("meshy_adopted", job_id=job.id, stage=stage.value, attempt=m.attempt,
                      from_attempt=prev.attempt, tasks=adopted)

    # ------------------------------------------------------------------ retries, leases
    def _take_over(self, job: JobRecord, stage: StageId, m: Manifest) -> str:
        """A RUNNING attempt found at advance: "run" it (no lease, ours, or its worker is gone
        and it may replay), leave it to the live worker that holds it ("busy"), or close it
        FAILED ("failed": it cannot replay, or it has killed the worker MAX_INTERRUPTS times)."""
        state = lifecycle.lease_state(m, self.boot)
        if state in ("none", "ours"):
            return "run"
        run = m.data.get("run") or {}
        if state == "alive":
            log_event("stage_busy", job_id=job.id, stage=stage.value, attempt=m.attempt,
                      pid=run.get("pid"), worker=run.get("worker"))
            return "busy"
        ints = m.data.setdefault("interrupts", [])
        ints.append({"at": utcnow().isoformat(), "boot": run.get("boot"), "pid": run.get("pid"),
                     "worker": run.get("worker"), "last": run.get("last")})
        n = len(ints)
        safe = lifecycle.replay_safe(RUNNERS[stage], m, self.svc.settings)
        if safe:
            loop = lifecycle.crash_loop([i.get("last") for i in ints])
        else:  # each interruption was its own attempt: look back across them
            lasts = self._interrupted_attempts(job, stage, m.attempt - 1) + [run.get("last")]
            loop = lifecycle.crash_loop(lasts, cap=lifecycle.MAX_FRESH_INTERRUPTS)
        log_event("stage_interrupted", level=logging.WARNING, job_id=job.id, stage=stage.value,
                  attempt=m.attempt, times=n, last=run.get("last"), replay=safe and not loop)
        if safe and not loop:
            self.store.write_manifest(m)
            return "run"
        m.error = StageError(
            code="internal", retryable=True,
            message=(f"{stage.value} stopped the worker {n} times" if loop else
                     f"{stage.value} was interrupted (the worker stopped mid-stage) and "
                     f"starts over"),
            detail={"interrupted": n, "crash_loop": loop, "last": run.get("last")})
        self._close(m, StageStatus.FAILED)
        return "failed"

    def _retry_plan(self, job: JobRecord, stage: StageId, m: Manifest) -> lifecycle.RetryPlan:
        """The plan for a FAILED attempt, made once and kept in data.retry (so its backoff
        survives restarts)."""
        saved = m.data.get("retry")
        if isinstance(saved, dict) and saved.get("mode"):
            from datetime import datetime

            return lifecycle.RetryPlan(saved["mode"], datetime.fromisoformat(saved["at"]),
                                       int(saved.get("n", 0)), saved.get("why", ""))
        plan = lifecycle.plan_retry(
            m, replay_safe=lifecycle.replay_safe(RUNNERS[stage], m, self.svc.settings),
            failed_in_row=self._count_failures(job, stage),
            backoff=tuple(getattr(self.svc.settings, "retry_backoff_s", ()) or ()))
        if plan.mode != "pause":
            m.data["retry"] = plan.json()
            self.store.write_manifest(m)
            log_event("stage_retry", job_id=job.id, stage=stage.value, attempt=m.attempt,
                      mode=plan.mode, n=plan.n, at=plan.at.isoformat(), why=plan.why,
                      error=m.error.message[:300] if m.error else None)
        return plan

    def _resume_failed(self, m: Manifest, plan: lifecycle.RetryPlan) -> None:
        """Replay a FAILED attempt in place: its cached steps, Meshy tasks and gate replies
        stand; the failure moves to data.resumes."""
        err = m.error
        m.data.setdefault("resumes", []).append({
            "at": utcnow().isoformat(), "kind": lifecycle.failure_kind(err),
            "code": err.code if err else None,
            "error": (err.message if err else "")[:300]})
        m.data.pop("retry", None)
        m.data.pop("run", None)  # not interrupted: the next run takes the lease
        m.status, m.error, m.finished_at = StageStatus.RUNNING, None, None
        self.store.write_manifest(m)

    def _write_skipped(self, job: JobRecord, stage: StageId) -> None:
        if job.attempts.get(stage):
            return
        job.attempts[stage] = 1
        m = Manifest(job_id=job.id, stage=stage, attempt=1, status=StageStatus.SKIPPED)
        self.store.write_manifest(m)
        self.store.save_job(job)

    async def run_stages(self, job_id: str, stages: list[StageId], *,
                         allow_meshy: bool = False) -> dict[str, str]:
        """Ops: a fresh attempt of each stage, in order, whatever the job's profile, then stop.
        Meshy is switched off unless allow_meshy (a stage that needs it fails and says so);
        s07 reuses the Meshy 3MF an earlier attempt downloaded. Stops at the first stage that
        does not succeed. Returns {stage: status}."""
        svc = self.svc if allow_meshy else replace(self.svc, meshy=NoMeshy())
        orch = Orchestrator(svc)
        out: dict[str, str] = {}
        job = self.store.load_job(job_id)
        for stage in stages:
            m = orch._new_attempt(job, stage)
            status = await orch._run_stage(job, stage, m)
            out[stage.value] = status.value
            job = self.store.load_job(job_id)
            if status != StageStatus.SUCCEEDED:
                if status == StageStatus.PAUSED and review_kind(m) is not None:
                    self._to_review(job, stage, m)
                elif status == StageStatus.PAUSED:
                    self._set_job(job, JobStatus.PAUSED, stage, pause_reason=m.pause_reason)
                else:
                    self._set_job(job, JobStatus.FAILED if status == StageStatus.FAILED
                                  else job.status, stage)
                return out
        self._set_job(job, JobStatus.DONE, stages[-1], pause_reason=None)
        return out

    async def advance(self, job_id: str) -> JobStatus:
        job = self.store.load_job(job_id)
        self.store.heartbeat(self.svc.settings.worker_id, version=__version__,
                             meshy_mode=self.svc.settings.meshy_mode,
                             info={"job": job_id, "at": utcnow().isoformat()})
        job = await resume_open_paint_edit(self, job)  # a worker stopped inside a paint edit
        if job.status == JobStatus.PAUSED:
            job = self._apply_resume(job)
        elif job.status == JobStatus.REVIEW:
            job = self._apply_review(job)
        if job.status in (JobStatus.DONE, JobStatus.REVIEW, JobStatus.FAILED,
                          JobStatus.CANCELLED):
            job = await self._apply_palette_edits(job)
            job = await apply_paint_edits(self, job)
        if job.status in BLOCKING:
            return job.status
        profile = PROFILES[job.profile]
        for s in STAGES:
            if s.id not in profile:
                self._write_skipped(job, s.id)

        for stage in profile:
            while True:
                n = job.attempts.get(stage)
                m = self.store.load_manifest(job.id, stage, n) if n else None
                if m is not None and (m.status == StageStatus.SUCCEEDED or m.status in OPEN):
                    reason = self._stale_reason(job, m)
                    if reason is None and m.status == StageStatus.SUCCEEDED:
                        break
                    if reason is not None:
                        m.status, m.pause_reason = StageStatus.STALE, None
                        m.data["stale_reason"] = reason
                        self.store.write_manifest(m)
                        m = None
                if m is not None and m.status == StageStatus.PAUSED:
                    if review_kind(m) is not None:  # a review whose job move a restart cut off
                        self._to_review(job, stage, m)
                        return JobStatus.REVIEW
                    self._set_job(job, JobStatus.PAUSED, stage, pause_reason=m.pause_reason)
                    return JobStatus.PAUSED
                if m is not None and m.status == StageStatus.RUNNING:
                    held = self._take_over(job, stage, m)
                    if held == "busy":
                        return JobStatus.RUNNING
                if m is not None and m.status == StageStatus.FAILED:
                    plan = self._retry_plan(job, stage, m)
                    if plan.mode == "pause":
                        m.status = StageStatus.PAUSED
                        m.pause_reason = (f"{stage.value} failed "
                                          f"{self._count_failures(job, stage)} times: "
                                          f"{m.error.message if m.error else 'unknown'}")
                        self.store.write_manifest(m)
                        self._set_job(job, JobStatus.PAUSED, stage, pause_reason=m.pause_reason)
                        return JobStatus.PAUSED
                    wait = (plan.at - utcnow()).total_seconds()
                    if wait > 0:  # backoff: serve kicks the job when it is due
                        self.wake_at[job.id] = time.time() + wait
                        return JobStatus.RUNNING
                    self.wake_at.pop(job.id, None)
                    if plan.mode == "resume":
                        self._resume_failed(m, plan)
                    else:
                        m = None
                if m is None or m.status in {StageStatus.STALE, StageStatus.SKIPPED}:
                    m = self._new_attempt(job, stage)
                status = await self._run_stage(job, stage, m)
                job = self.store.load_job(job_id)  # stages save job updates (budget, photos)
                if status == StageStatus.SUCCEEDED:
                    break
                if status == StageStatus.WAITING_USER:
                    self._set_job(job, JobStatus.WAITING_USER, stage)
                    return JobStatus.WAITING_USER
                if status == StageStatus.WAITING_VENDOR:
                    self._set_job(job, JobStatus.RUNNING, stage)
                    return JobStatus.RUNNING
                if status == StageStatus.PAUSED:
                    if review_kind(m) is not None:
                        self._to_review(job, stage, m)
                        return JobStatus.REVIEW
                    self._set_job(job, JobStatus.PAUSED, stage, pause_reason=m.pause_reason)
                    return JobStatus.PAUSED
                # FAILED: loop decides retry or pause

        self._set_job(job, JobStatus.DONE, profile[-1], pause_reason=None)
        self._hook("job:done")
        return JobStatus.DONE
