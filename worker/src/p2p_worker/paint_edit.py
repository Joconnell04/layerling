"""paint_edit messages (contracts payloads.PaintEdit): the studio paint editor, or Claude through
the CLI, repaints a finished job.

A job with a result (DONE, or REVIEW after one) consumes each edit in order:
1. Checks: the payload reads, the job has a result, and `base_attempt` (when given) is still
   the paint behind the result (p2p_mcp_paint.edits.current_paint); otherwise a `note` and
   nothing changes. Edits on a running or waiting job wait until it finishes; a closed job
   answers "Job is closed".
2. s09 as a new attempt with stages.s09_paint_edit.PaintEditStage: ops, simulated print,
   final.3mf as s10 writes it, the p2p_slicer preview (paint_preview.json), Meshy off. A
   refused op fails the attempt; the job goes back to the base paint (its s09 attempt number
   and status) with a note, so the result it had stays current.
3. s10 as a new attempt: the same paint through the slicer gate (Bambu Studio where installed,
   else p2p_slicer and "Bambu gate pending") and a new `result`. Pass: DONE. Gate fail:
   REVIEW with the review_request, as for any s10. Any other failure pauses the job.
No vendor or Claude call is made on this path.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from p2p_contracts import JobRecord, JobStatus, StageId, StageStatus
from p2p_contracts.payloads import PaintEdit

from .errors import review_kind
from .logs import log_event

if TYPE_CHECKING:
    from .orchestrator import Orchestrator


def current_paint(orch: Orchestrator, job: JobRecord) -> str | None:
    from p2p_mcp_paint.edits import current_paint as current

    return current(orch.store.job_dir(job.id))


async def apply_paint_edits(orch: Orchestrator, job: JobRecord) -> JobRecord:
    store = orch.store
    rows = store.list_messages(job.id, role="user", kinds={"paint_edit"}, unconsumed=True)
    for row in rows:
        job = store.load_job(job.id)
        closed = job.status in (JobStatus.FAILED, JobStatus.CANCELLED)
        if not closed and job.status not in (JobStatus.DONE, JobStatus.REVIEW):
            break  # still working: apply once it finishes
        store.consume_message(job.id, row.id)

        def note(text: str, row=row, job_id=job.id) -> None:
            store.post_message(job_id, "assistant", "note", {"text": text}, reply_to=row.id)

        if closed:
            note("Job is closed")
            continue
        if not job.result:
            note("No result to edit yet")
            continue
        try:
            edit = PaintEdit.model_validate(row.payload)
        except ValueError:
            note("Could not read that paint edit")
            continue
        base = current_paint(orch, job)
        if edit.base_attempt and edit.base_attempt != base:
            log_event("paint_edit", job_id=job.id, rejected="stale base",
                      base=edit.base_attempt, current=base)
            note(f"Paint changed since {edit.base_attempt}; reload")
            continue
        await run_paint_edit(orch, job, row.id, edit, base)
    return store.load_job(job.id)


def _sub(orch: Orchestrator) -> Orchestrator:
    from .orchestrator import NoMeshy, Orchestrator

    return Orchestrator(replace(orch.svc, meshy=NoMeshy()))  # this path never calls Meshy


def _attempt_of(folder: str | None) -> int | None:
    """s09_paint/a3 -> 3; None for an s08 base or none (the job had no s09 paint)."""
    if not folder or not folder.startswith(f"{StageId.S09_PAINT.value}/a"):
        return None
    return int(folder.rsplit("/a", 1)[1])


async def run_paint_edit(orch: Orchestrator, job: JobRecord, message_id: str, edit: PaintEdit,
                         base: str | None) -> JobStatus:
    log_event("paint_edit", job_id=job.id, ops=len(edit.ops), base=base, source=edit.source)
    before_status, before_stage = job.status, job.stage
    before_s09 = job.attempts.get(StageId.S09_PAINT)
    sub = _sub(orch)
    m9 = sub._new_attempt(job, StageId.S09_PAINT)
    m9.data["paint_edit"] = {**edit.model_dump(mode="json"), "message_id": message_id,
                             "base": base}
    orch.store.write_manifest(m9)
    return await _finish(orch, job, m9, before_status, before_stage, before_s09)


async def resume_open_paint_edit(orch: Orchestrator, job: JobRecord) -> JobRecord:
    """A worker that stopped inside a paint edit's s09 (the job left RUNNING on it) finishes the
    edit: s09 again (finished steps are cached), then s10, as run_paint_edit would have."""
    n = job.attempts.get(StageId.S09_PAINT)
    m9 = orch.store.load_manifest(job.id, StageId.S09_PAINT, n) if n else None
    if (job.status != JobStatus.RUNNING or job.stage != StageId.S09_PAINT or m9 is None
            or "paint_edit" not in m9.data
            or m9.status not in (StageStatus.RUNNING, StageStatus.PENDING)):
        return job
    log_event("paint_edit", job_id=job.id, resumed=f"s09_paint/a{n}")
    data = m9.data["paint_edit"]
    await _finish(orch, job, m9, JobStatus.DONE, StageId.S10_WRITE,
                  _attempt_of(data.get("base")))
    return orch.store.load_job(job.id)


async def _finish(orch: Orchestrator, job: JobRecord, m9, before_status: JobStatus,
                  before_stage: StageId | None, before_s09: int | None) -> JobStatus:
    from .stages.s09_paint_edit import PaintEditStage

    store = orch.store
    sub = _sub(orch)
    message_id = (m9.data.get("paint_edit") or {}).get("message_id")
    status = await sub._run_stage(job, StageId.S09_PAINT, m9, PaintEditStage())
    job = store.load_job(job.id)
    if status != StageStatus.SUCCEEDED:
        reason = m9.error.message if m9.error else status.value
        if before_s09 is None:
            job.attempts.pop(StageId.S09_PAINT, None)
        else:
            job.attempts[StageId.S09_PAINT] = before_s09
        store.save_job(job)
        orch._set_job(job, before_status, before_stage)
        log_event("paint_edit", job_id=job.id, rejected=reason[:200])
        store.post_message(job.id, "assistant", "note",
                           {"text": f"Paint edit not applied: {reason}"[:500]},
                           reply_to=message_id)
        return before_status

    m10 = sub._new_attempt(job, StageId.S10_WRITE)
    m10.data["paint_edit_message"] = message_id
    store.write_manifest(m10)
    status = await sub._run_stage(job, StageId.S10_WRITE, m10)
    job = store.load_job(job.id)
    if status == StageStatus.SUCCEEDED:
        orch._set_job(job, JobStatus.DONE, StageId.S10_WRITE, pause_reason=None,
                      review_reason=None)
        return JobStatus.DONE
    if status == StageStatus.PAUSED and review_kind(m10) is not None:
        orch._to_review(job, StageId.S10_WRITE, m10)
        return JobStatus.REVIEW
    reason = (f"paint edit failed at s10_write: "
              f"{m10.error.message if m10.error else status.value}")[:500]
    orch._set_job(job, JobStatus.PAUSED, StageId.S10_WRITE, pause_reason=reason)
    store.post_message(job.id, "assistant", "note", {"text": reason}, reply_to=message_id)
    return JobStatus.PAUSED
