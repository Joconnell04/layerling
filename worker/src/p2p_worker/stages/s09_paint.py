"""Stage 9, paint: the Opus 5.5 paint agent with the paint MCP server (p2p_mcp_paint).

1. Inputs: the s06 GeometryResult (figure and parts) and the s07 X2D baseline (Meshy's paint on
   the figure). The paint session reads them from the job folder itself; they are recorded here
   as inputs so a changed upstream makes this attempt stale.
2. p2p_mcp_paint.agent.run_paint in this attempt's folder: SAM 3 labels split by the baseline
   paint, at most 8 agent iterations (effort medium, checkpoint each), a sign-off at effort
   high, then write_3mf and slice_check. The agent judges simulate_print renders, never the
   ideal paint. Claude runs through the Claude Code login only (usd 0, list price recorded).
3. Outputs: paint_state.json, region_map.json, face_regions.npy, checklist.json (contracts
   Checklist), slice_report.json, final.3mf (role paint_3mf when the gate passes, debug_3mf
   when it fails) and the before/after previews.
4. The final print is checked against the approved concept from all eight proof views
   (p2p_mcp_paint.concept_check, Opus 5.5 at effort high), fixed where paint can fix it, and
   the proof set (model, paint, print; proof/final/) is recorded as outputs.
5. Confidence below the review threshold, a failed concept check, or a failed gate posts a
   review_request (with the concept next to the print sheets) and moves the job to REVIEW
   (NeedsReview). Nothing ships from here unless the gate passed. On a worker without Bambu
   Studio the final gate slices with p2p_slicer (printing.paint_slice_fn); s10 then holds the
   job for the Bambu gate.

Tests inject a driver (P2P_PAINT_DRIVER=stub, or PaintStage(driver_factory=...)) so no Claude
call happens; the default is the Claude Code driver.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from typing import Any

from p2p_contracts import SliceReport, StageId
from p2p_contracts.payloads import ReviewRequest

from .. import printing as pr
from ..errors import NeedsReview, VendorError
from ..logs import log_event
from ..util import read_json
from .base import Stage, StageContext

OUTPUT_ROLES = {"paint_state.json": "paint_state", "region_map.json": "region_map",
                "face_regions.npy": "face_regions", "checklist.json": "checklist",
                "checklist_baseline.json": "checklist_baseline",
                "slice_report.json": "slice_report", "paint_run.json": "paint_run",
                "palette_plan.json": "palette_plan"}


def run_summary(run) -> dict[str, Any]:
    """The paint run as the stage's cached step result (JSON)."""
    return {"iterations": run.iterations, "confidence": run.confidence,
            "review": run.review, "review_reason": run.review_reason,
            "threemf": run.threemf, "slice_ok": run.slice_ok,
            "signoff": run.signoff, "swaps": run.swaps, "baseline_sim": run.baseline,
            "final_sim": run.final_sim, "duration_s": run.duration_s,
            "concept_check": run.concept_check, "proof": run.proof,
            "review_images": run.review_images,
            "model_calls": [c.model_dump(mode="json") for c in run.model_calls]}


def paint_job(job_id: str, jobs_dir: str, attempt: int, sam3: str | None, max_iterations: int,
              settings: Any, concept: bytes | None, log_path: str) -> dict[str, Any]:
    """Child-process entry: open the attempt, run the paint agent (Claude through the Claude
    Code login, as in the worker), return run_summary. Its log lines go to log_path, which the
    worker tails into its own log."""
    import asyncio
    from pathlib import Path

    from p2p_contracts.mcp_tools import OpenJobIn
    from p2p_mcp_paint.agent import run_paint
    from p2p_mcp_paint.session import PaintSession

    from ..offload import FileLog

    root = Path(jobs_dir)
    session = PaintSession.open(OpenJobIn(job_id=job_id, attempt=attempt), root, sam3)
    run = asyncio.run(run_paint(job_id, jobs_dir=root, session=session, driver=None,
                                max_iterations=max_iterations, sam3_backend=sam3,
                                slice_fn=pr.paint_slice_fn(settings), concept_png=concept,
                                write_manifest=False, log=FileLog(log_path)))
    return run_summary(run)


async def paint_in_child(ctx: StageContext, sam3: str | None, max_iter: int) -> dict[str, Any]:
    """PaintStage's run in a child process, its log forwarded as `paint` events meanwhile.
    live2: the in-process run blocked the event loop for up to two minutes at a time (proof
    renders, simulated prints, the final write) and Realtime timed out three times."""
    import asyncio

    from p2p_mcp_paint.proof import concept_png

    from ..offload import run_cpu, tail_log

    log_path = ctx.path("paint.log")
    def line(msg: str) -> None:
        ctx.touch(f"paint: {msg[:120]}")  # the stage watchdog counts a log line as progress
        log_event("paint", msg=msg)

    from ..offload import JOB

    tail = asyncio.create_task(tail_log(log_path, line))
    token = JOB.set(ctx.job.id)
    try:
        return await run_cpu(paint_job, ctx.job.id, str(ctx.job_dir.parent), ctx.attempt, sam3,
                             max_iter, ctx.job.settings, concept_png(ctx.job_dir),
                             str(log_path))
    finally:
        JOB.reset(token)
        tail.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await tail


class PaintStage(Stage):
    id = StageId.S09_PAINT

    def __init__(self, driver_factory: Callable[..., Any] | None = None,
                 slice_fn: Callable | None = None, max_iterations: int | None = None):
        self.driver_factory = driver_factory
        self.slice_fn = slice_fn
        self.max_iterations = max_iterations

    def _driver(self, session):
        if self.driver_factory is not None:
            return self.driver_factory(session)
        if os.environ.get("P2P_PAINT_DRIVER") == "stub":
            from p2p_mcp_paint.agent import StubDriver
            from p2p_mcp_paint.session import PaintSession

            return StubDriver(lambda: PaintSession.resume(session.inp, session.dir,
                                                          session.attempt))
        return None  # run_paint builds the Claude Code driver

    async def run(self, ctx: StageContext) -> None:
        try:
            from p2p_contracts.mcp_tools import OpenJobIn
            from p2p_mcp_paint.agent import MAX_ITERATIONS, run_paint
            from p2p_mcp_paint.proof import concept_png
            from p2p_mcp_paint.session import PaintSession
        except ImportError as e:  # pragma: no cover - workspace always has it
            raise VendorError(f"p2p_mcp_paint is not available: {e}", code="internal",
                              retryable=False) from e
        job = ctx.job
        ctx.use_output(StageId.S06_PRINTABILITY, "geometry")
        ctx.use_output(StageId.S07_BASELINE, "baseline_3mf")
        # subscription Claude costs $0: the check only refuses when the job is already paused
        ctx.svc.budget.check(job, vendor="anthropic", credits=0, usd=0.0, live=False,
                             what="paint agent")
        jobs_dir = ctx.job_dir.parent
        sam3 = os.environ.get("P2P_PAINT_SAM3")

        max_iter = self.max_iterations or MAX_ITERATIONS
        in_child = (ctx.settings.offload == "process" and self.driver_factory is None
                    and self.slice_fn is None and os.environ.get("P2P_PAINT_DRIVER") != "stub")

        async def run_it() -> dict[str, Any]:
            import asyncio

            if in_child:  # the whole paint run in a child process (offload.py)
                return await paint_in_child(ctx, sam3, max_iter)
            session = await asyncio.to_thread(
                PaintSession.open, OpenJobIn(job_id=job.id, attempt=ctx.attempt), jobs_dir,
                sam3)
            run = await run_paint(
                job.id, jobs_dir=jobs_dir, session=session, driver=self._driver(session),
                max_iterations=max_iter, sam3_backend=sam3,
                slice_fn=self.slice_fn or pr.paint_slice_fn(job.settings),
                concept_png=concept_png(ctx.job_dir),
                write_manifest=False,
                log=lambda msg: (ctx.touch(f"paint: {msg[:120]}"), log_event("paint", msg=msg)))
            return run_summary(run)

        async def paint() -> dict[str, Any]:
            # Each Claude call in the run has a time limit (p2p_mcp_paint.agent.bounded). One
            # that runs out fails this attempt retryable, so the orchestrator starts a fresh
            # one instead of s09 waiting as long as the worker lives.
            try:
                return await run_it()
            except TimeoutError as e:
                raise VendorError(f"paint agent: {e}", code="vendor_timeout",
                                  retryable=True) from e

        res = await ctx.step("paint", paint)
        self._record_calls(ctx, res.get("model_calls") or [])
        for name, role in OUTPUT_ROLES.items():
            if ctx.path(name).is_file():
                ctx.add_output(name, role=role)
        for p in sorted(ctx.path("previews").glob("*.png")) if ctx.path("previews").is_dir() \
                else []:
            ctx.add_output(f"previews/{p.name}", role="preview", kind="png")
        if ctx.path("final.3mf").is_file():
            ctx.add_output("final.3mf", role="paint_3mf" if res.get("slice_ok") else "debug_3mf",
                           kind="3mf")
        for row in res.get("proof") or []:
            name = row["path"].removeprefix(ctx.rel_dir + "/")
            if ctx.path(name).is_file():
                ctx.add_output(name, role="proof_sheet" if row["view"] == "sheet"
                               else f"proof_{row['kind']}", kind="png")
        for rel in res.get("review_images") or []:
            name = rel.removeprefix(ctx.rel_dir + "/")
            if ctx.path(name).is_file():
                ctx.add_output(name, role="concept_compare", kind="png")
        plan = read_json(ctx.path("palette_plan.json")) if ctx.path(
            "palette_plan.json").is_file() else None
        if plan:
            from p2p_mcp_paint.palette_plan import summary_line

            # the cost-aware palette decision (p2p_mcp_paint.palette_plan) and its tradeoff
            ctx.decide("palette.plan", plan.get("palette"), by="code",
                       reason=summary_line(plan)[:1000])
            ctx.m.data["palette_plan"] = {
                k: plan.get(k) for k in ("palette", "buy", "cap", "score", "added", "refused")}
            ctx.m.data["palette_plan"]["dropped"] = [
                {"filament": d.get("filament"), "why": d.get("why")}
                for d in plan.get("dropped") or []]
        ctx.decide("paint.iterations", res.get("iterations"), by="model",
                   model="claude-opus-5-5")
        gate = "pass" if res.get("slice_ok") else "fail"
        rep_path = ctx.path("slice_report.json")
        if gate == "pass" and rep_path.is_file() and not pr.ship_gated(
                SliceReport.model_validate(read_json(rep_path))):
            gate = "estimate"  # p2p_slicer only: s10 asks for the Bambu gate
        ctx.decide("paint.slicer_gate", gate, by="code")
        ctx.m.confidence = float(res.get("confidence") or 0.0)
        ctx.m.data.update({k: res.get(k) for k in ("iterations", "confidence", "slice_ok",
                                                   "threemf", "swaps", "baseline_sim",
                                                   "final_sim", "duration_s")})
        cc = res.get("concept_check") or {}
        if cc:
            ctx.decide("paint.concept_check", "pass" if cc.get("passed") else "fail",
                       by="model", model="claude-opus-5-5",
                       reason=((cc.get("final") or {}).get("summary") or "")[:500] or None)
            ctx.m.data["concept_check"] = {"passed": cc.get("passed"),
                                           "rounds": len(cc.get("rounds") or []),
                                           "final": cc.get("final")}
        ctx.save("paint:outputs")
        if res.get("review"):
            await self._to_review(ctx, res)

    def _record_calls(self, ctx: StageContext, calls: list[dict]) -> None:
        from p2p_contracts.manifest import ModelCall

        if ctx.steps.get("paint_calls_recorded"):
            return
        for c in calls:
            mc = ModelCall.model_validate(c)
            ctx.m.model_calls.append(mc)
            ctx.svc.budget.record(ctx.job, stage=ctx.stage, vendor="anthropic",
                                  kind=f"paint:{mc.billing}", usd=mc.usd,
                                  ref=mc.request_id, usd_list=mc.usd_list,
                                  billing=mc.billing, test_mode=mc.billing == "free")
        ctx.steps["paint_calls_recorded"] = True
        ctx.save("paint:calls")

    async def _to_review(self, ctx: StageContext, res: dict) -> None:
        """Review with the reason classified (live2 sombrero: the paint's concept check failed,
        the Bambu gate passed, and the review said the slicer gate failed, so approve was
        refused). A failed gate is a gate review; anything else is a fidelity review that
        Jackson may approve (the file still ships only through s10's gate)."""
        images = [r for r in res.get("review_images") or [] if ctx.abs(r).is_file()]
        images += [ctx.rel(f"previews/{n}") for n in ("after_print.png", "before_print.png")
                   if ctx.path(f"previews/{n}").is_file()]
        why = res.get("review_reason") or "low confidence"
        if res.get("slice_ok"):
            kind = "fidelity"
            reason = f"Paint check failed; slicer gate passed: {why}"
        else:
            kind = "gate"
            reason = f"Slicer gate failed at paint: {why}"
        req = ReviewRequest(reason=reason[:1000], confidence=float(res.get("confidence") or 0),
                            images=images)

        async def post():
            msg = ctx.store.post_message(ctx.job.id, "assistant", "review_request", req,
                                         stage=ctx.stage)
            return {"message_id": msg.id}

        await ctx.step("review_message", post)
        raise NeedsReview(reason, {"confidence": res.get("confidence"),
                                   "slice_ok": res.get("slice_ok"),
                                   "swaps": res.get("swaps")}, kind=kind)
