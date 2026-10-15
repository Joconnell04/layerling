"""run-local: drive one job end to end on this machine, answering gates automatically.

The auto-responder plays the web app: it answers intake questions with the first option, sends
any scripted tweaks (one per concept gate), then approves the newest concept, applies scripted
mesh actions (retexture / regenerate), then approves the mesh.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from p2p_contracts import JobStatus, StageStatus
from p2p_contracts.payloads import (
    Answer,
    AnswersPayload,
    ConceptPick,
    ConceptsPayload,
    IntakeResult,
    MeshDecision,
    Tweak,
)

from .runtime import Runtime


@dataclass
class AutoResponder:
    tweaks: list[str] = field(default_factory=list)
    mesh_actions: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    def respond(self, rt: Runtime, job_id: str) -> bool:
        """Post one user reply for the open gate. False if there is nothing to answer."""
        store = rt.store
        job = store.load_job(job_id)
        stage = job.stage
        m = store.load_manifest(job_id, stage, job.attempts[stage]) if stage else None
        if m is None or m.status != StageStatus.WAITING_USER or m.gate is None:
            return False
        msgs = store.list_messages(job_id, role="assistant")
        kind = m.gate.kind
        if kind == "answer_questions":
            q = next(x for x in reversed(msgs) if x.kind == "questions")
            intake = IntakeResult.model_validate(q.payload)
            ans = AnswersPayload(answers=[
                Answer(question_id=qq.id, option_ids=[qq.options[0].id]) for qq in intake.questions
            ])
            store.post_message(job_id, "user", "answers", ans, stage=stage, reply_to=q.id)
            self.log.append(f"answered {len(intake.questions)} questions")
            return True
        if kind == "pick_concept":
            c = next(x for x in reversed(msgs) if x.kind == "concepts")
            payload = ConceptsPayload.model_validate(c.payload)
            newest = payload.candidates[-1]
            if self.tweaks:
                text = self.tweaks.pop(0)
                store.post_message(job_id, "user", "tweak",
                                   Tweak(text=text, candidate_id=newest.id), stage=stage)
                self.log.append(f"tweak on {newest.id}: {text}")
            else:
                store.post_message(job_id, "user", "concept_pick",
                                   ConceptPick(candidate_id=newest.id, approve=True),
                                   stage=stage, reply_to=c.id)
                self.log.append(f"approved concept {newest.id} ({newest.image_model})")
            return True
        if kind == "approve_mesh":
            r = next(x for x in reversed(msgs) if x.kind == "mesh_review")
            action = self.mesh_actions.pop(0) if self.mesh_actions else "approve"
            action, _, text = action.partition(":")  # "regenerate:make the saddle darker"
            store.post_message(job_id, "user", "mesh_decision",
                               MeshDecision(action=action, text=text.strip() or None),
                               stage=stage, reply_to=r.id)
            self.log.append(f"mesh: {action}" + (f" ({text.strip()})" if text.strip() else ""))
            return True
        return False


async def run_job(rt: Runtime, job_id: str, responder: AutoResponder | None,
                  max_steps: int = 200) -> JobStatus:
    status = JobStatus.QUEUED
    for _ in range(max_steps):
        status = await rt.orchestrator.advance(job_id)
        if status == JobStatus.WAITING_USER:
            if responder is None or not responder.respond(rt, job_id):
                return status
            continue
        if status == JobStatus.RUNNING:
            # a webhook wait timed out (advance again), or a stage retry waits out its backoff
            wait = rt.orchestrator.retry_wait_s(job_id)
            if wait > 0:
                await asyncio.sleep(wait)
            continue
        return status
    return status


async def run_local(rt: Runtime, photos: list[Path], prompt: str, *, auto: bool,
                    product_type: str = "figurine", ams_slots: int | None = None,
                    target_size_mm: float | None = None, addons: list | None = None,
                    tweaks: list[str] | None = None, mesh_actions: list[str] | None = None,
                    profile: str = "thin_slice", form: str | None = None,
                    ) -> tuple[str, JobStatus, AutoResponder | None]:
    job = rt.orchestrator.create_job(prompt, photos, product_type=product_type,
                                     ams_slots=ams_slots, target_size_mm=target_size_mm,
                                     addons=addons, profile=profile, form=form)
    responder = AutoResponder(list(tweaks or []), list(mesh_actions or [])) if auto else None
    status = await run_job(rt, job.id, responder)
    return job.id, status, responder
