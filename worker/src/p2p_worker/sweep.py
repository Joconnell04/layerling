"""Backup status read for Meshy tasks whose webhook never came.

Webhooks stay primary (CLAUDE.md). FROM JACKSON, approved: a job waiting on Meshy for more than
10 minutes gets a 0-credit status GET per waiting task, at most every 10 minutes. `serve` runs
`sweep_loop` every MESHY_SWEEP_S (600 s); it only kicks the job, and the stage's own meshy_wait
makes the GET (stages/base.py status_read_due), so a lost terminal event no longer stalls a job
until something else wakes it (docs/research/meshy-iteration.md section 1, gap 1).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from p2p_contracts import JobStatus, StageStatus

from .logs import log_event
from .stages.base import status_read_due
from .store import JobStore
from .util import utcnow

log = logging.getLogger("p2p_worker.sweep")
WAITING = {JobStatus.QUEUED, JobStatus.RUNNING}


def jobs_due(store: JobStore, every_s: float, now: datetime | None = None) -> list[str]:
    """Jobs whose current stage waits on a Meshy task that is due a backup status read."""
    now = now or utcnow()
    out = []
    for job_id in store.list_jobs():
        try:
            job = store.load_job(job_id)
        except KeyError:
            continue
        if job.status not in WAITING or job.stage is None:
            continue
        n = job.attempts.get(job.stage)
        m = store.load_manifest(job.id, job.stage, n) if n else None
        if m is None or m.status != StageStatus.WAITING_VENDOR:
            continue
        if any(status_read_due(m, t, every_s, now) for t in m.meshy_tasks):
            out.append(job.id)
    return out


async def sweep_loop(store: JobStore, kick, every_s: float, read_after_s: float) -> None:
    """Every every_s: kick each job due a backup status read. Never raises."""
    while True:
        await asyncio.sleep(every_s)
        try:
            due = jobs_due(store, read_after_s)
            log_event("meshy_sweep", due=len(due), jobs=due[:20])
            for job_id in due:
                kick(job_id)
        except Exception:  # a sweep error must never stop serve
            log.exception("meshy sweep failed")
