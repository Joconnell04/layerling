"""Helpers shared by the print stages (s07, s08, s10): upstream lookups, result, review and
spool_suggestion messages."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from p2p_contracts import Manifest, SliceReport, StageId, StageStatus
from p2p_contracts.geometry import GeometryResult
from p2p_contracts.payloads import (
    FilamentUsage,
    JobResult,
    ReviewRequest,
    SpoolSuggestion,
)

from ..errors import NeedsReview
from ..util import read_json
from .base import StageContext


def optional(ctx: StageContext, stage: StageId) -> Manifest | None:
    """The accepted attempt of `stage` if it succeeded, else None (skipped or not run)."""
    n = ctx.job.attempts.get(stage)
    m = ctx.store.load_manifest(ctx.job.id, stage, n) if n else None
    return m if m is not None and m.status == StageStatus.SUCCEEDED else None


def geometry_of(ctx: StageContext) -> GeometryResult | None:
    """The s06 GeometryResult, when s06 ran and built one (recorded as an input)."""
    m6 = optional(ctx, StageId.S06_PRINTABILITY)
    if m6 is None:
        return None
    if not any(o.role == "geometry" for o in m6.outputs):
        if any(o.role == "geometry_warnings" for o in m6.outputs):
            ctx.use_output(StageId.S06_PRINTABILITY, "geometry_warnings")  # still an input
        return None
    path, _ = ctx.use_output(StageId.S06_PRINTABILITY, "geometry")
    return GeometryResult.model_validate(read_json(path))


def meshy_3mf_of(ctx: StageContext) -> Path:
    """s07's Meshy Multi-Color 3MF of the primary style."""
    m7 = ctx.upstream(StageId.S07_BASELINE)
    style = next((d.value for d in m7.decisions if d.key == "baseline.primary_style"), None)
    outs = [o for o in m7.outputs if o.role == "baseline_3mf_meshy"]
    pick = next((o for o in outs if style and o.path.endswith(f"baseline_{style}.3mf")),
                outs[0] if outs else None)
    if pick is None:
        from ..errors import VendorError

        raise VendorError("s07_baseline has no Meshy 3MF", code="internal", retryable=False)
    ctx.add_input(pick.path, from_stage=StageId.S07_BASELINE, from_attempt=m7.attempt,
                  role="baseline_3mf_meshy")
    return ctx.abs(pick.path)


def filament_usage(report: SliceReport, palette: list[str]) -> list[FilamentUsage]:
    return [FilamentUsage(slot=f.slot, grams=f.grams, meters=f.meters,
                          filament_id=f.filament_id or (palette[f.slot - 1]
                                                        if 0 < f.slot <= len(palette) else None))
            for f in report.filaments if f.grams > 0]


async def post_result(ctx: StageContext, result: JobResult) -> None:
    """result.json, the `result` message, and job.result. The job's credit report goes with
    it: credits.json (role credit_report) in this attempt, its totals in manifest data
    `credits`, and one line in the message body ("Meshy credits: 75 paid, 2 reused (saved
    21)"). JobResult has no credits field yet (worker README contract change request 9)."""
    ctx.write_json_output("result.json", result, role="job_result")
    report = ctx.ledger.write_report()
    ctx.write_json_output("credits.json", report, role="credit_report")
    ctx.m.data["credits"] = report["credits"]
    line = ctx.ledger.summary_line()

    async def post():
        msg = ctx.store.post_message(ctx.job.id, "assistant", "result", result, stage=ctx.stage,
                                     body=line)
        return {"message_id": msg.id}

    await ctx.step("result_message", post)
    ctx.job.result = result.model_dump(mode="json")
    ctx.store.save_job(ctx.job)
    ctx.store.update_job_row(ctx.job.id, result=result)


async def post_spool_suggestions(ctx: StageContext, items: list[dict]) -> int:
    """One `spool_suggestion` message per suggested spool (payloads.SpoolSuggestion)."""
    from .. import printing as pr

    payloads = pr.spool_payloads(items)
    for i, p in enumerate(payloads):
        async def post(p=p):
            msg = ctx.store.post_message(ctx.job.id, "assistant", "spool_suggestion",
                                         SpoolSuggestion.model_validate(p), stage=ctx.stage)
            return {"message_id": msg.id}

        await ctx.step(f"spool_suggestion.{i}", post)
    return len(payloads)


async def to_review(ctx: StageContext, report: SliceReport, built: Path,
                    images: list[str]) -> None:
    """Gate failed: keep the file for debugging only, ask Jackson, never ship."""
    if built.is_file():
        debug = ctx.path("debug_x2d.3mf")
        if not debug.exists():
            shutil.move(built, debug)
        ctx.add_output(debug.name, role="debug_3mf", kind="3mf")
    errors = report.errors or ["no errors reported"]
    reason = "slicer gate failed: " + "; ".join(errors)[:900]
    req = ReviewRequest(reason=reason, confidence=0.0, images=images[:6])

    async def post():
        msg = ctx.store.post_message(ctx.job.id, "assistant", "review_request", req,
                                     stage=ctx.stage)
        return {"message_id": msg.id}

    await ctx.step("review_message", post)
    ctx.m.confidence = 0.0
    raise NeedsReview(reason, {"errors": errors[:20], "slicer": report.slicer,
                               "slicer_version": report.slicer_version})


BAMBU_PENDING = "Bambu gate pending"


async def pending_review(ctx: StageContext, report: SliceReport, images: list[str]) -> None:
    """A p2p_slicer pass is an estimate, not the ship gate. The file, its result and numbers
    stay; the job waits in REVIEW until a worker with Bambu Studio reruns the stage. Approve is
    refused (orchestrator), so nothing ships on a native slice alone."""
    reason = (f"{BAMBU_PENDING}: only {report.slicer} {report.slicer_version} sliced this file; "
              "rerun on a Mac worker with Bambu Studio to gate it")
    req = ReviewRequest(reason=reason, confidence=None, images=images[:6])

    async def post():
        msg = ctx.store.post_message(ctx.job.id, "assistant", "review_request", req,
                                     stage=ctx.stage)
        return {"message_id": msg.id}

    await ctx.step("review_message", post)
    raise NeedsReview(reason, {"slicer": report.slicer, "slicer_version": report.slicer_version,
                               "gate": "estimate"}, kind="estimate")


def thumbnails(ctx: StageContext) -> list[str]:
    m5 = optional(ctx, StageId.S05_MESH)
    return [o.path for o in (m5.outputs if m5 else []) if o.role == "approved_thumbnail"]


def jsonable(d: dict[str, Any]) -> dict[str, Any]:
    """Drop non-JSON values (numpy scalars become floats) for manifest data."""
    import json

    return json.loads(json.dumps(d, default=lambda o: o.item() if hasattr(o, "item")
                                 else str(o)))
