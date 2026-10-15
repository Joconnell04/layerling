"""Stage 4, multi-view: real views of the approved figure, checked, ordered for s05.

docs/research/meshy-iteration.md section 3:
1. Input: the approved figure alone (s03 figure_concept), never the product concept, so no
   plate, text or loop reaches a view.
2. One Image to Image call with generate_multi_view, chained from the approved figure's task
   (input_task_id; a reference image when the task is older than the 60 h safety margin), on the
   approved candidate's own model so the style holds. No aspect_ratio (a 400 with multi-view)
   and no remove_background (single-image only).
3. Code: split a sheet into views, crop each to the subject and pad it onto the shared flat
   background; the approved figure goes through the same normalize. Palette gaps against the
   concept (CIEDE2000) are measured and handed to the check.
4. Check: Claude (Haiku, CHECK_MODEL) labels each view's angle and passes or fails it (same
   figure, markings where they belong, the customer's asks, nothing added). Code fails a view
   with no subject in it. Failing views are dropped. If no back view passes, one retry with the
   reasons in the prompt; if nothing passes after that, s05 builds from the figure alone.
5. Output: views.json (payloads.ViewSet): [approved figure, back, side, front], at most 4.

Skipped, and recorded why, only where the spec allows: a flat form (a relief of the front, side
and back views change nothing) and the dev cost profile (dev and golden-set runs).
"""

from __future__ import annotations

import asyncio
from typing import Any

from p2p_contracts import StageId
from p2p_contracts.payloads import CheckItem, ConceptCandidate, View, ViewSet

from .. import imaging
from ..clients.claude import ViewCheckRequest
from ..errors import VendorError
from ..util import read_json, utcnow
from .base import Stage, StageContext
from .product_plan import ProductPlan
from .prompts import for_model, view_prompt
from .s03_concept import RETENTION_SAFE, image_ref

ORDER = ("back", "left", "right", "front", "three_quarter", "top", "other")
SIDES = {"left", "right"}
MAX_INPUTS = 4  # Multi-Image to 3D takes 1 to 4 images
MIN_SUBJECT = 0.02  # a tile with less subject than this is empty (code check)


def order_views(views: list[View]) -> list[View]:
    """Back, one side, front, then anything else; one per angle; room for the concept first."""
    out: list[View] = []
    seen: set[str] = set()
    for angle in ORDER:
        for v in views:
            key = "side" if v.angle in SIDES else v.angle
            if v.angle == angle and key not in seen and len(out) < MAX_INPUTS - 1:
                out.append(v)
                seen.add(key)
    return out


class MultiViewStage(Stage):
    id = StageId.S04_MULTIVIEW

    def skip_reason(self, ctx: StageContext, plan: ProductPlan) -> str | None:
        if plan.flat:
            return ("flat relief: the print is a relief of the front, so side and back views "
                    "change nothing")
        if ctx.settings.cost_profile == "dev":
            return "dev cost profile: builds from the approved figure alone"
        return None

    async def run(self, ctx: StageContext) -> None:
        concept_json, _ = ctx.use_output(StageId.S03_CONCEPT, "concept")
        concept = read_json(concept_json)
        approved = ConceptCandidate.model_validate(concept["approved"])
        s03 = ctx.upstream(StageId.S03_CONCEPT)
        role = ("figure_concept" if any(o.role == "figure_concept" for o in s03.outputs)
                else "approved_concept")
        fig_abs, fig_ref = ctx.use_output(StageId.S03_CONCEPT, role)
        s01 = ctx.upstream(StageId.S01_INTAKE)
        regions: list[dict] = []
        if any(o.role == "color_regions" for o in s01.outputs):
            path, _ = ctx.use_output(StageId.S01_INTAKE, "color_regions")
            regions = read_json(path) or []
        plan = ProductPlan.from_job(ctx.job, regions)

        reason = self.skip_reason(ctx, plan)
        if reason:
            ctx.write_json_output("views.json", ViewSet(skipped=True, reason=reason),
                                  role="views")
            ctx.decide("multiview.skipped", True, by="code", reason=reason)
            ctx.m.data.update({"views": 0, "skipped": True})
            return

        async def concept_view() -> str:
            img = await asyncio.to_thread(imaging.normalize_view, imaging.load_rgba(fig_abs))
            imaging.save_rgba(img, ctx.path("view_concept.png"))
            ctx.add_output("view_concept.png", role="view")
            return ctx.rel("view_concept.png")

        concept_view_path: str = await ctx.step("concept_view", concept_view)
        ctask = next((t for t in s03.meshy_tasks if t.task_id == approved.meshy_task_id), None)
        chain = bool(ctask and ctask.status == "SUCCEEDED" and ctask.finished_at
                     and utcnow() - ctask.finished_at < RETENTION_SAFE)

        rounds: list[dict[str, Any]] = []
        fix: list[str] | None = None
        for k in range(2):
            prompt = view_prompt(plan.asks, fix)
            body: dict[str, Any] = {"ai_model": approved.image_model, "generate_multi_view": True,
                                    "prompt": for_model(prompt, approved.image_model)}
            if chain:
                body["input_task_id"] = approved.meshy_task_id
            else:
                body["reference_image_urls"] = [image_ref(ctx, fig_ref.path)]
            got = await self._views(ctx, k, body, concept_view_path, plan, regions)
            rounds.append(got)
            if got.get("failed"):
                break
            back_ok = any(v["passed"] and v["angle"] == "back" for v in got["views"])
            if back_ok:
                break
            fix = [f"view {i + 1} ({v['angle']}): {v.get('note') or 'failed'}"
                   for i, v in enumerate(got["views"]) if not v["passed"]] or \
                  ["no view from the back"]
            ctx.decide(f"multiview.retry.{k}", fix, by="code",
                       reason="the back view failed the check; retry once with the reasons")

        best = next((r for r in reversed(rounds) if any(v["passed"] for v in r["views"])), None)
        passed = [View.model_validate(v) for v in (best or {}).get("views", []) if v["passed"]]
        dropped = [View.model_validate(v) for r in rounds for v in r["views"] if not v["passed"]]
        checks = [CheckItem.model_validate(c) for r in rounds for c in r.get("checks", [])]
        concept_v = View(angle="concept", image=concept_view_path, source="concept",
                         meshy_task_id=approved.meshy_task_id)
        if not passed:
            why = ("the views failed the check twice; s05 builds from the approved figure "
                   "alone" if not any(r.get("failed") for r in rounds)
                   else f"multi-view failed: {rounds[-1].get('failed')}")
            ctx.write_json_output("views.json", ViewSet(views=[], reason=why, dropped=dropped,
                                                        checks=checks), role="views")
            ctx.decide("multiview.fallback", "single_image", by="code", reason=why)
            ctx.m.data.update({"views": 0, "rounds": len(rounds), "fallback": True})
            ctx.m.confidence = 0.5
            return
        ordered = [concept_v, *order_views(passed)]
        ctx.write_json_output("views.json", ViewSet(views=ordered, dropped=dropped,
                                                    checks=checks), role="views")
        ctx.decide("multiview.views", [v.angle for v in ordered], by="code",
                   reason="approved figure first, then back, side, front")
        ctx.m.data.update({"views": len(ordered), "angles": [v.angle for v in ordered],
                           "rounds": len(rounds), "dropped": len(dropped)})
        ctx.m.confidence = 1.0

    async def _views(self, ctx: StageContext, k: int, body: dict[str, Any], concept_view: str,
                     plan: ProductPlan, regions: list[dict]) -> dict[str, Any]:
        """One multi-view task: create, download, normalize, check. {"views": [...]} with each
        view's angle and verdict, or {"failed": reason, "views": []}."""
        key = f"views{k}"
        purpose = "multi-view" + (" retry" if k else "")
        why = ("views of the approved figure for the 3D model" if not k
               else "views retry: the back view failed the check")
        try:
            try:
                result = await ctx.meshy_run(key, "image-to-image", body, purpose, why=why)
            except VendorError as e:
                if e.code != "vendor_failed":
                    raise
                ctx.decide(f"multiview.retry_failed.{key}", str(e), by="code",
                           reason="Meshy task failed (refunded); retry once with the same inputs")
                key = f"{key}.retry"
                result = await ctx.meshy_run(key, "image-to-image", body, f"{purpose} retry",
                                             allow_same=True, why=f"{why} (after a refunded "
                                             "failure)")
        except VendorError as e:
            if e.code != "vendor_failed":
                raise
            return {"failed": str(e)[:300], "views": []}
        raw = []
        for i in range(len(result.get("image_urls") or [])):
            ref = await ctx.fetch_asset(
                key, lambda t, i=i: ((t.get("image_urls") or []) + [None] * (i + 1))[i],
                f"{key}_raw_{i}.png", role="view_raw")
            if ref is not None:
                raw.append(ref.path)

        async def normalize() -> dict[str, Any]:
            def work() -> dict[str, Any]:
                tiles = imaging.split_views([ctx.abs(p) for p in raw])
                concept_img = imaging.load_rgba(ctx.abs(concept_view))
                cpal = imaging.palette(concept_img)
                out, metrics = [], []
                for j, tile in enumerate(tiles, start=1):
                    share = imaging.subject_share(tile)
                    img = imaging.normalize_view(tile)
                    name = f"{key}_{j}.png"
                    imaging.save_rgba(img, ctx.path(name))
                    gap = imaging.palette_gap(cpal, imaging.palette(img))
                    out.append({"image": ctx.rel(name), "share": round(share, 4),
                                "same_as_concept": imaging.near_identical(img, concept_img),
                                **gap})
                    metrics.append(f"view {j}: concept color {gap['missing_hex']} nearest "
                                   f"dE00 {gap['missing_de']}; new color {gap['new_hex']} "
                                   f"dE00 {gap['new_de']}")
                return {"tiles": out, "metrics": metrics, "raw": len(raw)}

            res = await asyncio.to_thread(work)
            for t in res["tiles"]:
                ctx.add_output(t["image"].rsplit("/", 1)[-1], role="view_candidate")
            return res

        norm = await ctx.step(f"{key}.normalized", normalize)
        tiles = norm["tiles"]
        if not tiles:
            return {"failed": "multi-view returned no image", "views": []}
        claude = ctx.svc.claude

        async def check() -> list[dict[str, Any]]:
            req = ViewCheckRequest(
                concept=ctx.abs(concept_view), views=[ctx.abs(t["image"]) for t in tiles],
                regions=[f"{r.get('color')} {r.get('region')}" for r in regions
                         if r.get("kind") in ("base", "marking")],
                asks=[a.text for a in plan.asks if a.kind != "text"], metrics=norm["metrics"])
            answers = await ctx.claude(f"check.views.{key}", claude.estimate_check(
                len(req.images())), lambda: claude.check_views(req))
            return [a.model_dump(mode="json") if hasattr(a, "model_dump") else a
                    for a in answers]  # type: ignore[union-attr]

        answers = await ctx.step(f"{key}.check", check)
        task = ctx.task(key)
        views, checks = [], []
        for t, a in zip(tiles, answers, strict=False):
            ok, note = bool(a.get("ok")), a.get("why")
            if t["share"] < MIN_SUBJECT:
                ok, note = False, "no subject in this view (code)"
            elif t.get("same_as_concept"):
                ok, note = False, "the approved figure itself, not a new view (code)"
            views.append(View(angle=a.get("angle", "other"), image=t["image"],
                              source="multiview", meshy_task_id=task.task_id if task else None,
                              passed=ok, note=note).model_dump(mode="json"))
            checks.append(CheckItem(
                id=f"{key}.{a.get('index')}", kind="view", on="view",
                label=f"View {a.get('index')} ({a.get('angle')}) is the approved figure",
                passed=ok, note=note, by="model").model_dump(mode="json"))
        return {"views": views, "checks": checks}
