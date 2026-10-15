"""Stage 3, concept: the customer approves the real product before any 3D credits are spent.

Recipe (docs/research/meshy-iteration.md section 4; FROM JACKSON: the core idea stays the same
from concept to 3D model to print):

3D figure (figurine, nameplate, ornament, 3D keychain or magnet), per candidate:
1. Figure render: Meshy Image to Image from the photos (round 1: gpt-image-2 and nano-banana-pro
   in parallel, remove_background on; dev profile: nano-banana-2). The figure alone, never
   inpainted: it is the 3D input (s04, s05). File figure_<cid>.png.
2. Product concept, cheapest first: the add-ons s06 builds (the loop, the base, or the plate
   with the name) drawn in code around the unchanged figure (free; live2 Bronco: the plate the
   customer approved was this composite after 60 credits of plate edits). Only when that fails
   the concept check does a paid Image to Image edit chained from the figure draw them, and
   imaging.paste_back copies the figure's own pixels back over the edit, so the figure the
   customer approves is pixel for pixel the one Meshy models. Products without a visible add-on
   (a magnet's back) show the figure itself. File concept_<cid>.png.
3. Check: Claude (Haiku, CHECK_MODEL) looks at the photos, the figure and the product against
   the job's checklist (product_plan.check_items): product parts present in the picked shape
   and spot, the name spelled exactly, every visible intake pick (pose, look, form), custom
   asks present, markings true to the photos, no stray base or plinth, and nothing nobody
   asked for (a frame, a ring, a block). Code adds "figure pixels unchanged" and, for a flat
   charm, the exact engraving Meshy echoes.
4. A failed check regenerates with changed inputs (Meshy has no seed; identical inputs reuse
   the task already paid for, base.meshy_create): a figure problem gets one new figure render
   with the problems in its prompt (and its product starts from code again); a product problem
   on the code composite gets one paid edit with the problems named, and a failed edit switches
   to the other edit model. A model whose product edit failed is never used again in the job,
   after MAX_FAILED_EDITS failed edits no more are bought, and at most MAX_PAID_EDITS paid
   edits are made in the stage across its attempts (the credit ledger counts them). Paid
   regenerations are capped per job (job.MAX_CONCEPT_FIXES); a candidate that still fails is
   shown with its check, on the code composite when one was drawn for its figure.

Flat charm (keychain or magnet with form flat): the figure render is front-view charm art; a
Creative Lab prototype (6 credits; keychain name_text when the job has a name of 10 characters
or less) turns it into the relief that s05 builds, and the loop is drawn on in code. Checks
judge the charm.

Gate: the customer picks a product concept (approve) or sends a tweak. Haiku routes tweaks:
- rerender_concept: a new round that edits the base candidate's figure (chained with
  input_task_id) and re-adds the product (max 5 rounds; then tweaks_exhausted).
- ask_followup: the tweak_decision message carries the question.
- retexture, regenerate_mesh, paint_edit, geometry_op: recorded for later stages.

Outputs: figure_<cid>.png, edit_<cid>_<n>.png, concept_<cid>[_<n>].png, approved_concept.png
(role approved_concept: the product the customer approved), figure_concept.png (role
figure_concept: the 3D input), concept.json.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import timedelta
from typing import Any

from p2p_contracts import StageId
from p2p_contracts.job import MAX_CONCEPT_FIXES, MAX_TWEAK_ROUNDS
from p2p_contracts.payloads import (
    AnswersPayload,
    CheckItem,
    ConceptCandidate,
    ConceptCheck,
    ConceptPick,
    ConceptsPayload,
    IntakeResult,
    Tweak,
    TweakDecision,
    TweakRoute,
)

from .. import imaging
from ..clients.claude import ConceptCheckRequest, TweakRequest, same_text
from ..errors import VendorError, VendorWaiting, is_transient
from ..util import data_uri, downscale_image, read_json, utcnow
from .base import Stage, StageContext
from .product_plan import ProductPlan
from .prompts import (
    concept_prompt,
    edit_aspect,
    fix_prompt,
    for_model,
    for_model_edit,
    tweak_prompt,
)

PRODUCT_MODELS = ("gpt-image-2", "nano-banana-pro")
DEV_MODELS = ("nano-banana-2",)
# Add-on edit models, first try then second try (meshy-iteration.md section 4): a name goes to
# nano-banana-pro (9) then GPT (12); a loop or base to nano-banana-2 (6) then nano-banana-pro.
EDIT_MODELS = {"text": ("nano-banana-pro", "gpt-image-2"),
               "plain": ("nano-banana-2", "nano-banana-pro")}
DEV_EDIT_MODELS = {"text": ("nano-banana-2", "nano-banana-pro"),
                   "plain": ("nano-banana-2", "nano-banana-pro")}
RETENTION_SAFE = timedelta(hours=60)  # Meshy keeps assets 3 days; chain only well inside that
IDENTICAL_MIN = 0.98  # share of figure pixels a pasted product must keep (loops keep a band)
# Paid product edits that failed their check, per job, before every later product is drawn in
# code (FROM JACKSON: stop paying after 2 failed edits). live2 Bronco: nano-banana-pro failed
# the plate edit on both candidates, failed again on both after the figure fixes, and only then
# did the code composite draw the plate: 36 credits on 4 repeats of a model that had failed.
MAX_FAILED_EDITS = 2
# Paid product edits per s03 stage, across its attempts, whatever their outcome (ASSUMED 2: one
# edit and one switch of model). The code composite is always tried first and is free.
MAX_PAID_EDITS = 2
# An add-on edit must draw something outside the figure: add-on pixels relative to the figure's
# area (imaging.PasteResult.addon_share; a base drew 0.32, a plate about 1.0, live2 run). An
# edit that returns the figure unchanged scores 0.0 and the vision check can still pass it.
ADDON_MIN = 0.002


_DATA_URIS: dict[tuple[str, int, int, int], str] = {}  # (path, mtime_ns, size, max_px) -> URI


def image_ref(ctx: StageContext, rel_path: str, max_px: int = 1536) -> str:
    """A URL or data URI Meshy can read for a job file. Signed URLs are made fresh every call;
    data URIs are cached per file version, since stages rebuild bodies on every replay."""
    url = ctx.store.signed_url(ctx.job.id, rel_path)
    if url:
        return url
    path = ctx.abs(rel_path)
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size, max_px)
    if key not in _DATA_URIS:
        if len(_DATA_URIS) > 64:
            _DATA_URIS.clear()
        data, mime = downscale_image(path, max_px=max_px)
        _DATA_URIS[key] = data_uri(data, mime)
    return _DATA_URIS[key]


async def gather_tasks(coros: list) -> list[Any]:
    """Run in parallel; surface waiting and budget states before failures."""
    results = await asyncio.gather(*coros, return_exceptions=True)
    waiting = [r for r in results if isinstance(r, VendorWaiting)]
    if waiting:
        raise VendorWaiting([t for w in waiting for t in w.task_ids])
    for r in results:
        if isinstance(r, BaseException) and not isinstance(r, VendorError):
            raise r
    return results


def issue_lines(items: list[CheckItem]) -> list[str]:
    return [f"{i.label} ({i.note})" if i.note else i.label for i in items][:6]


class ConceptStage(Stage):
    id = StageId.S03_CONCEPT

    def models(self, ctx: StageContext) -> tuple[str, ...]:
        return DEV_MODELS if ctx.settings.cost_profile == "dev" else PRODUCT_MODELS

    def edit_models(self, ctx: StageContext, kind: str) -> tuple[str, str]:
        table = DEV_EDIT_MODELS if ctx.settings.cost_profile == "dev" else EDIT_MODELS
        return table[kind]  # type: ignore[return-value]

    # ------------------------------------------------------------ Meshy image calls
    async def _image_task(self, ctx: StageContext, key: str, endpoint: str,
                          body: dict[str, Any], purpose: str, name: str,
                          role: str, *, why: str | None = None,
                          fresh: bool = False) -> tuple[str, str | None, dict[str, Any]]:
        """One image task, retried once with the same inputs if Meshy fails it (refunded);
        image 0 saved as `name`. Returns (job path, task id, trimmed result)."""
        try:
            result = await ctx.meshy_run(key, endpoint, body, purpose, why=why, fresh=fresh)
        except VendorError as e:
            if e.code != "vendor_failed":
                raise
            ctx.decide(f"concept.retry.{key}", str(e), by="code",
                       reason="Meshy task failed (refunded); retry once")
            key = f"{key}.retry"
            result = await ctx.meshy_run(key, endpoint, body, f"{purpose} retry",
                                         allow_same=True, why=f"retry after a refunded "
                                         f"failure: {why or purpose}")
        if not result.get("image_urls"):
            raise VendorError(f"Meshy {purpose} returned no image", retryable=True)
        ref = await ctx.fetch_asset(key, lambda t: (t.get("image_urls") or [None])[0], name,
                                    role=role)
        if ref is None:
            raise VendorError(f"Meshy {purpose} image could not be fetched", retryable=True)
        task = ctx.task(key)
        return ref.path, (task.task_id if task else None), result

    def _chainable_id(self, ctx: StageContext, task_id: str | None) -> bool:
        t = next((t for t in ctx.m.meshy_tasks if t.task_id == task_id), None)
        return bool(t and t.status == "SUCCEEDED" and t.finished_at
                    and utcnow() - t.finished_at < RETENTION_SAFE)

    def _chainable(self, ctx: StageContext, cand: ConceptCandidate) -> bool:
        return self._chainable_id(ctx, cand.meshy_task_id)

    def _figure_body(self, ctx: StageContext, model: str, prompt: str,
                     src: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {"ai_model": model, "prompt": for_model(prompt, model),
                                "remove_background": True, "aspect_ratio": "1:1"}
        if src.get("task") and self._chainable_id(ctx, src["task"]):
            body["input_task_id"] = src["task"]
        elif src.get("image"):
            body["reference_image_urls"] = [image_ref(ctx, src["image"])]
        else:
            body["reference_image_urls"] = [image_ref(ctx, p.path) for p in ctx.job.photos[:5]]
        return body

    # ------------------------------------------------------------ edit model memory
    def _edit_failures(self, ctx: StageContext) -> list[dict[str, Any]]:
        """Paid product edits that failed their check, in this attempt and earlier ones."""
        out = list(ctx.m.data.get("edit_failures", []))
        for a in range(1, ctx.attempt):
            m = ctx.store.load_manifest(ctx.job.id, self.id, a)
            if m is not None:
                out += list(m.data.get("edit_failures", []))
        return out

    def _edit_pick(self, ctx: StageContext, kind: str) -> dict[str, Any]:
        """The model for the next paid product edit: the first edit model that has not failed
        an edit in this job; none (draw it in code) once MAX_FAILED_EDITS edits have failed or
        every model has failed once. A model that failed is never repeated."""
        failed = self._edit_failures(ctx)
        if len(failed) >= MAX_FAILED_EDITS:
            return {"model": None, "reason": f"{len(failed)} paid product edits failed in this "
                    f"job (cap {MAX_FAILED_EDITS}); drawn in code"}
        tried = {f.get("model") for f in failed}
        left = [m for m in self.edit_models(ctx, kind) if m not in tried]
        failed_models = sorted(t for t in tried if t)
        if not left:
            return {"model": None, "reason": "every edit model failed once "
                    f"({', '.join(failed_models)}); drawn in code"}
        return {"model": left[0], "avoided": failed_models}

    def _record_edit_failure(self, ctx: StageContext, plan: ProductPlan, cid: str, n: int,
                             product: dict[str, Any], issues: list[str]) -> None:
        ctx.m.data.setdefault("edit_failures", []).append(
            {"cid": cid, "n": n, "model": product.get("model"), "kind": plan.edit_kind(),
             "task_id": product.get("task_id"), "issues": issues[:3]})

    # ------------------------------------------------------------ product concept
    async def _product(self, ctx: StageContext, plan: ProductPlan, cid: str, n: int,
                       fig_path: str, fig_task: str | None, *, model: str | None = None,
                       fix: list[str] | None = None) -> dict[str, Any]:
        """The product concept for one figure, cheapest first: the add-ons drawn in code
        around the unchanged figure (free), or, when a fix names a model after the composite
        failed its check, a paid edit plus paste-back; the figure itself when nothing is added;
        (flat) a Creative Lab charm with the loop drawn on. An edit whose model has failed in
        this job, or past MAX_FAILED_EDITS, is drawn in code instead."""
        name = f"concept_{cid}.png" if n == 0 else f"concept_{cid}_{n}.png"
        if f"product.{cid}.{n}" in ctx.steps:
            return ctx.steps[f"product.{cid}.{n}"]
        if plan.flat:
            return await self._charm(ctx, plan, cid, n, fig_path, name)
        kind = plan.edit_kind()
        if kind is None:
            async def same() -> dict[str, Any]:
                shutil.copyfile(ctx.abs(fig_path), ctx.path(name))
                ctx.add_output(name, role="concept_candidate")
                return {"image": ctx.rel(name), "source": "figure", "task_id": None,
                        "ok": True, "paste": {"identical": 1.0}}

            return await ctx.step(f"product.{cid}.{n}", same)
        started_as_edit = (ctx.task(f"{cid}.p{n}") is not None
                           or f"edit_model.{cid}.{n}" in ctx.steps)  # an attempt before this rule
        if model is None and not started_as_edit:
            ctx.decide(f"concept.cheapest.{cid}.{n}", "code", by="code",
                       reason="add-ons drawn in code first (0 credits); a paid edit only if "
                       "the concept check fails")
            return await self._composite(ctx, plan, cid, n, fig_path, name=name)

        async def pick() -> dict[str, Any]:
            bought = ctx.task(f"{cid}.p{n}")  # an edit already paid for keeps its model
            if bought is not None and bought.request.get("ai_model"):
                return {"model": bought.request["ai_model"]}
            return {"model": model} if model else self._edit_pick(ctx, kind)

        choice = await ctx.step(f"edit_model.{cid}.{n}", pick)
        if not choice.get("model"):
            ctx.decide(f"concept.edit_fallback.{cid}.{n}", "code", by="code",
                       reason=choice.get("reason"))
            return await self._composite(ctx, plan, cid, n, fig_path, name=name)
        model = str(choice["model"])
        prompt, allowed = plan.edit(fix)
        body: dict[str, Any] = {"ai_model": model,
                                "prompt": for_model_edit(prompt, model, allowed),
                                "remove_background": True,
                                "aspect_ratio": edit_aspect(plan.visible(), model)}
        if self._chainable_id(ctx, fig_task):
            body["input_task_id"] = fig_task
        else:
            body["reference_image_urls"] = [image_ref(ctx, fig_path)]
        edit_path, edit_task, _ = await self._image_task(
            ctx, f"{cid}.p{n}", "image-to-image", body, f"product {cid} {model}",
            f"edit_{cid}_{n}.png", role="product_edit",
            why=("add-on edit after the code composite failed: " + "; ".join(fix or [])
                 if fix else "add-on edit")[:300])

        async def paste() -> dict[str, Any]:
            res = await asyncio.to_thread(imaging.paste_back, ctx.abs(fig_path),
                                          ctx.abs(edit_path), ctx.path(name),
                                          mode=plan.paste_mode())
            if res.ok:
                ctx.add_output(name, role="concept_candidate")
            return {"image": ctx.rel(name) if res.ok else None, "source": "edit",
                    "task_id": edit_task, "model": model, "ok": res.ok, "reason": res.reason,
                    "paste": res.as_dict()}

        return await ctx.step(f"product.{cid}.{n}", paste)

    async def _composite(self, ctx: StageContext, plan: ProductPlan, cid: str, n: int,
                         fig_path: str, name: str | None = None) -> dict[str, Any]:
        """Free fallback: the add-ons drawn in code around the unchanged figure."""
        name = name or f"concept_{cid}_{n}.png"

        async def draw() -> dict[str, Any]:
            info = await asyncio.to_thread(plan.composite, ctx.abs(fig_path), ctx.path(name))
            ctx.add_output(name, role="concept_candidate")
            return {"image": ctx.rel(name), "source": "code", "task_id": None, "ok": True,
                    "paste": {"identical": 1.0, **info}}

        return await ctx.step(f"product.{cid}.{n}", draw)

    async def _charm(self, ctx: StageContext, plan: ProductPlan, cid: str, n: int,
                     fig_path: str, name: str) -> dict[str, Any]:
        ep = f"{plan.creative_lab}-prototype"
        body: dict[str, Any] = {"image_url": image_ref(ctx, fig_path), "remove_background": True}
        if plan.creative_lab == "keychain" and plan.name_text:
            body["name_text"] = plan.name_text
        proto_path, proto_task, result = await self._image_task(
            ctx, f"{cid}.charm{n}", ep, body, f"charm {cid}", f"charm_{cid}_{n}.png",
            role="charm_prototype", why="flat charm: the Creative Lab relief s05 builds")

        async def draw() -> dict[str, Any]:
            loop = plan.loop()
            info: dict[str, Any] = {}
            if loop is not None:
                pos = str(loop.params.get("position", "top"))
                info = await asyncio.to_thread(imaging.composite_loop, ctx.abs(proto_path),
                                               ctx.path(name), "top" if pos == "back" else pos)
            else:
                shutil.copyfile(ctx.abs(proto_path), ctx.path(name))
            ctx.add_output(name, role="concept_candidate")
            return {"image": ctx.rel(name), "source": "prototype", "task_id": proto_task,
                    "ok": True, "charm": proto_path, "name_text": result.get("name_text"),
                    "paste": {"identical": 1.0, **info}}

        return await ctx.step(f"product.{cid}.{n}", draw)

    # ------------------------------------------------------------ check
    async def _check(self, ctx: StageContext, plan: ProductPlan, cid: str, n: int,
                     fig_path: str, product: dict[str, Any]) -> ConceptCheck:
        claude = ctx.svc.claude

        async def run() -> ConceptCheck:
            code: list[CheckItem] = []
            ident = (product.get("paste") or {}).get("identical")
            if product["source"] == "edit":
                ok = bool(product["ok"] and ident is not None and ident >= IDENTICAL_MIN)
                code.append(CheckItem(
                    id="figure_identical", kind="figure_identical", on="product", by="code",
                    label="The figure is unchanged in the product", passed=ok,
                    note=None if ok else (product.get("reason")
                                          or f"{(ident or 0):.1%} of the figure unchanged")))
                share = (product.get("paste") or {}).get("addon_share")
                if product["ok"] and share is not None and plan.visible():
                    ok = float(share) >= ADDON_MIN
                    what = "; ".join(lbl[:1].lower() + lbl[1:] for lbl in
                                     (plan.part_label(a) for a in plan.visible()))
                    code.append(CheckItem(
                        id="addon_drawn", kind="part", on="product", by="code",
                        label=f"The edit drew {what}"[:160], passed=ok,
                        note=None if ok else "The edit came back with nothing added outside "
                        "the figure"))
            if product["source"] == "prototype" and plan.name_text:
                ok = same_text(product.get("name_text"), plan.name_text)
                code.append(CheckItem(
                    id="text.engraving", kind="text", on="product", by="code",
                    label=f"Meshy engraves exactly {plan.name_text}", passed=ok,
                    note=None if ok else f"task says {product.get('name_text')!r}"))
            if not product["ok"]:
                return ConceptCheck(passed=False, items=code, figure_identical=ident)
            show = product["source"] != "figure"
            req = ConceptCheckRequest(
                request=ctx.job.prompt, photos=[ctx.abs(p.path) for p in ctx.job.photos],
                figure=ctx.abs(fig_path), product=ctx.abs(product["image"]) if show else None,
                items=plan.check_items(), expected_text=plan.name_text)
            chk = await ctx.claude(f"check.concept.{cid}.{n}",
                                   claude.estimate_check(len(req.images())),
                                   lambda: claude.check_concept(req))
            items = [*chk.items, *code]  # type: ignore[attr-defined]
            return ConceptCheck(passed=all(i.passed for i in items), items=items,
                                text_read=chk.text_read, model=chk.model,  # type: ignore[attr-defined]
                                figure_identical=ident)

        return ConceptCheck.model_validate(await ctx.step(f"check.{cid}.{n}", run))

    def _fixes_used(self, ctx: StageContext) -> int:
        used = len(ctx.m.data.get("concept_fixes", []))
        for a in range(1, ctx.attempt):
            m = ctx.store.load_manifest(ctx.job.id, self.id, a)
            if m is not None:
                used += len(m.data.get("concept_fixes", []))
        return used

    def _paid_edits(self, ctx: StageContext) -> int:
        """Paid product edits in this stage, every attempt (the credit ledger; a reused edit
        or a refunded failure does not count)."""
        return ctx.ledger.paid_edits(self.id.value, "product ")

    def _next_fix(self, ctx: StageContext, plan: ProductPlan, cid: str, n: int,
                  check: ConceptCheck, product: dict[str, Any], done: set[str]) -> dict[str, Any]:
        """What to change after a failed check, cheapest first. Paid actions count against the
        job's cap (MAX_CONCEPT_FIXES); paid edits also against MAX_PAID_EDITS per stage.

        - figure problem: one new figure render (its product starts from code again)
        - product problem on the code composite: a paid edit on the first model that has not
          failed in this job
        - product problem on a paid edit: that model is never used again; switch to the other
          model once (at most two edits per candidate), else stop on the code composite
        """
        fails = [i for i in check.items if not i.passed]
        fig = [i for i in fails if i.on == "figure"]
        prod = [i for i in fails if i.on != "figure"]
        left = MAX_CONCEPT_FIXES - self._fixes_used(ctx)
        action: dict[str, Any] = {"action": "stop", "issues": issue_lines(fails)}
        edit_failed = bool(prod and product["source"] == "edit" and not plan.flat)
        if edit_failed:  # the paid edit's model is not used again in this job
            self._record_edit_failure(ctx, plan, cid, n, product, issue_lines(prod))
        needs_product = bool(prod) and not plan.flat and bool(plan.visible())
        pick = self._edit_pick(ctx, plan.edit_kind() or "plain") if needs_product else {}
        edits_here = sum(1 for d in done if d.startswith("edit"))
        edits_left = MAX_PAID_EDITS - self._paid_edits(ctx)
        if plan.flat:
            if "figure" not in done and left > 0:
                action = {"action": "figure", "issues": issue_lines(fails)}
        elif fig and "figure" not in done and left > 0:
            action = {"action": "figure", "issues": issue_lines(fig)}
        elif needs_product and pick.get("model") and left > 0 and edits_left > 0 \
                and edits_here < 2:
            action = {"action": "edit", "issues": issue_lines(prod), "model": pick["model"],
                      "reason": ("the code composite failed the check" if not edit_failed
                                 else f"{product.get('model')} failed; switch model")}
        elif edit_failed and "code" not in done:  # switch approach: draw it in code (free)
            action = {"action": "code", "issues": issue_lines(prod)}
            if not pick.get("model"):
                action["reason"] = pick.get("reason")
        elif needs_product:
            action = {"action": "stop", "issues": issue_lines(prod), "reason": (
                pick.get("reason") if not pick.get("model")
                else f"paid edit cap reached ({MAX_PAID_EDITS} per stage)" if edits_left <= 0
                else f"concept fix cap reached ({MAX_CONCEPT_FIXES} per job)" if left <= 0
                else "both edit models tried for this candidate")}
        if action["action"] in ("figure", "edit"):
            ctx.m.data.setdefault("concept_fixes", []).append(f"{cid}.{n}:{action['action']}")
        if action["action"] == "stop" and fails and left <= 0 and "reason" not in action:
            action["reason"] = f"concept fix cap reached ({MAX_CONCEPT_FIXES} per job)"
        ctx.decide(f"concept.fix.{cid}.{n}", action, by="code",
                   reason="concept check failed")
        return action

    # ------------------------------------------------------------ one candidate
    async def _candidate(self, ctx: StageContext, plan: ProductPlan,
                         spec: dict[str, Any]) -> ConceptCandidate:
        cid, model, prompt = spec["cid"], spec["model"], spec["prompt"]
        tweak = spec["round"] > 1  # the customer asked for a new render: a fresh sample
        body = self._figure_body(ctx, model, prompt, spec["src"])
        if tweak and ctx.task(cid) is None and ctx.already_paid("image-to-image", body):
            # the same tweak again: Meshy has no seed, so change an input rather than pay for
            # (or silently reuse) the render this job already has
            prompt = f"{prompt} Variation {spec['round']}."
            body = self._figure_body(ctx, model, prompt, spec["src"])
            ctx.decide(f"concept.variation.{cid}", spec["round"], by="code",
                       reason="same tweak as an earlier round; prompt changed for a new sample")
        fig_path, fig_task, _ = await self._image_task(
            ctx, cid, "image-to-image", body, f"concept {cid} {model}", f"figure_{cid}.png",
            role="figure_candidate", fresh=tweak,
            why=f"tweak round {spec['round']}" if tweak else "concept render, round 1")
        product = await self._product(ctx, plan, cid, 0, fig_path, fig_task)
        check = await self._check(ctx, plan, cid, 0, fig_path, product)
        done: set[str] = set()
        # the code composite of the current figure and its check (shown if paid edits fail)
        coded = (product, check) if product["source"] == "code" else None
        n = 0
        while not check.passed and n < 6:
            n += 1
            seen = done | ({"code"} if coded else set())
            action = await ctx.step(
                f"fix.{cid}.{n}",
                lambda n=n, check=check, product=product, seen=seen: self._fix_step(
                    ctx, plan, cid, n, check, product, seen))
            kind = action["action"]
            if kind == "stop":
                if coded is not None and product["source"] == "edit":
                    product, check = coded  # show the free composite, not the failed edit
                break
            done.add("edit2" if kind == "edit" and "edit" in done else kind)
            if kind == "figure":
                fbody = self._figure_body(ctx, model, fix_prompt(prompt, action["issues"]),
                                          spec["src"])
                fig_path, fig_task, _ = await self._image_task(
                    ctx, f"{cid}.fix{n}", "image-to-image", fbody,
                    f"concept {cid} {model} fix", f"figure_{cid}_{n}.png",
                    role="figure_candidate",
                    why="figure fix: " + "; ".join(action["issues"])[:280])
                product = await self._product(ctx, plan, cid, n, fig_path, fig_task)
                coded = None
            elif kind == "edit":
                model_n = action.get("model") or self.edit_models(  # older cached fix steps
                    ctx, plan.edit_kind() or "plain")[1]
                product = await self._product(ctx, plan, cid, n, fig_path, fig_task,
                                              model=model_n, fix=action["issues"])
            else:
                product = await self._composite(ctx, plan, cid, n, fig_path)
            check = await self._check(ctx, plan, cid, n, fig_path, product)
            if product["source"] == "code":
                coded = (product, check)
        image = product.get("image") or fig_path
        return ConceptCandidate(
            id=cid, round=spec["round"], image_model=model, image=image,
            meshy_task_id=fig_task, prompt=body["prompt"], figure_image=fig_path,
            form=plan.form, product_task_id=product.get("task_id"),
            product_source=product["source"] if product.get("image") else "figure",
            check=check)

    async def _fix_step(self, ctx: StageContext, plan: ProductPlan, cid: str, n: int,
                        check: ConceptCheck, product: dict[str, Any],
                        done: set[str]) -> dict[str, Any]:
        return self._next_fix(ctx, plan, cid, n, check, product, done)

    async def _render(self, ctx: StageContext, plan: ProductPlan, rnd: int, base_prompt: str,
                      base: ConceptCandidate | None, instruction: str | None) -> list[dict]:
        specs: list[dict[str, Any]] = []
        if rnd == 1:
            for k, model in enumerate(self.models(ctx), start=1):
                specs.append({"cid": f"r1c{k}", "round": 1, "model": model,
                              "prompt": base_prompt, "src": {}})
        else:
            assert base is not None and instruction
            src = ({"task": base.meshy_task_id} if self._chainable(ctx, base)
                   else {"image": base.figure_image or base.image})
            specs.append({"cid": f"r{rnd}c1", "round": rnd, "model": base.image_model,
                          "prompt": tweak_prompt(base_prompt, instruction, plan.asks,
                                                 plan.form.value), "src": src})
        results = await gather_tasks([self._candidate(ctx, plan, s) for s in specs])
        cands = [r for r in results if isinstance(r, ConceptCandidate)]
        failed = [r for r in results if isinstance(r, VendorError)]
        # A failed Claude check or a dropped connection says nothing about the candidate: the
        # stage fails and replays (lifecycle), the paid images kept. Only a candidate whose
        # Meshy task failed twice is dropped.
        replay = [e for e in failed if e.detail.get("resume") or e.detail.get("transient")
                  or is_transient(e)]
        if replay:
            raise replay[0]
        if not cands:
            raise failed[0]
        for e in failed:
            ctx.decide(f"concept.dropped.r{rnd}.{len(ctx.m.decisions)}", str(e), by="code",
                       reason="candidate failed twice; continuing with the others")
        return [c.model_dump(mode="json") for c in cands]

    # ------------------------------------------------------------ stage
    async def run(self, ctx: StageContext) -> None:
        job = ctx.job
        intake_path, _ = ctx.use_output(StageId.S01_INTAKE, "intake_result")
        answers_path, _ = ctx.use_output(StageId.S01_INTAKE, "answers")
        s01 = ctx.upstream(StageId.S01_INTAKE)
        regions: list[dict] = []
        if any(o.role == "color_regions" for o in s01.outputs):
            regions_path, _ = ctx.use_output(StageId.S01_INTAKE, "color_regions")
            regions = read_json(regions_path) or []
        if any(o.role == "product_spec" for o in s01.outputs):
            ctx.use_output(StageId.S01_INTAKE, "product_spec")
        for p in job.photos:
            ctx.add_input(p.path, role="photo")
        intake = IntakeResult.model_validate(read_json(intake_path))
        answers = AnswersPayload.model_validate(read_json(answers_path))
        plan = ProductPlan.from_job(job, regions, answers)

        async def build_prompt() -> str:
            return concept_prompt(job.prompt, job.product_type.value, intake, answers,
                                  job.target_size_mm, regions, form=plan.form.value,
                                  asks=plan.asks)

        base_prompt: str = await ctx.step("prompt", build_prompt)
        claude = ctx.svc.claude

        candidates: list[ConceptCandidate] = []
        rnd, tweaks_used, gate_i = 1, 0, 0
        base: ConceptCandidate | None = None
        instruction: str | None = None
        history: list[str] = []
        deferred: list[dict[str, Any]] = []
        need_render, post_prompt = True, True
        approved: ConceptCandidate | None = None

        def find(cid: str | None) -> ConceptCandidate | None:
            return next((c for c in candidates if c.id == cid), None)

        while approved is None:
            if need_render:
                new = await ctx.step(
                    f"r{rnd}.candidates",
                    lambda rnd=rnd, base=base, instruction=instruction: self._render(
                        ctx, plan, rnd, base_prompt, base, instruction),
                )
                candidates += [ConceptCandidate.model_validate(c) for c in new]
                need_render, post_prompt = False, True
            payload = ConceptsPayload(round=rnd, candidates=candidates,
                                      rounds_left=MAX_TWEAK_ROUNDS - tweaks_used,
                                      tweaks_exhausted=tweaks_used >= MAX_TWEAK_ROUNDS,
                                      form=plan.form)
            reply = await ctx.gate(
                f"r{rnd}.g{gate_i}", gate_kind="pick_concept", accept={"concept_pick", "tweak"},
                prompt_kind="concepts" if post_prompt else None,
                prompt=payload if post_prompt else None,
            )
            gate_i += 1
            post_prompt = False

            if reply.kind == "concept_pick":
                pick = ConceptPick.model_validate(reply.payload)
                cand = find(pick.candidate_id)
                if cand is None:
                    ctx.note(f"No concept with id {pick.candidate_id}")
                    continue
                if pick.approve:
                    approved = cand
                else:
                    base = cand
                    ctx.decide(f"concept.base.g{gate_i}", cand.id, by="user")
                continue

            tweak = Tweak.model_validate(reply.payload)
            base = find(tweak.candidate_id) or base or candidates[-1]
            req = TweakRequest(tweak=tweak.text, concept_prompt=base_prompt,
                               image=ctx.abs(base.image),
                               rounds_left=MAX_TWEAK_ROUNDS - tweaks_used, history=list(history))

            async def route(req=req, reply_id=reply.id):
                d = await ctx.claude("route.tweak", claude.estimate_route(req),
                                     lambda: claude.route_tweak(req))
                ctx.store.post_message(job.id, "assistant", "tweak_decision", d,
                                       stage=ctx.stage, reply_to=reply_id)
                return d

            decision = TweakDecision.model_validate(await ctx.step(f"route.g{gate_i}", route))
            history.append(tweak.text)
            ctx.decide(f"route.tweak.g{gate_i}", decision.route.value, by="model",
                       model=decision.model, reason=tweak.text)
            if decision.route == TweakRoute.RERENDER_CONCEPT:
                if tweaks_used >= MAX_TWEAK_ROUNDS:
                    async def exhausted(reply_id=reply.id):
                        msg = ctx.store.post_message(
                            job.id, "assistant", "note",
                            {"text": f"All {MAX_TWEAK_ROUNDS} tweak rounds used. Pick a concept."},
                            stage=ctx.stage, reply_to=reply_id)
                        return {"message_id": msg.id}

                    await ctx.step(f"exhausted.g{gate_i}", exhausted)
                    continue
                tweaks_used += 1
                rnd += 1
                instruction = decision.instruction
                need_render = True
            elif decision.route == TweakRoute.ASK_FOLLOWUP:
                pass  # the tweak_decision message asks; the next tweak answers
            else:
                deferred.append({"route": decision.route.value,
                                 "instruction": decision.instruction, "base": base.id})

        figure = approved.figure_image or approved.image
        ctx.copy_output(ctx.abs(approved.image), "approved_concept.png", role="approved_concept")
        ctx.copy_output(ctx.abs(figure), "figure_concept.png", role="figure_concept")
        paste = {}
        for key in sorted(k for k in ctx.steps if k.startswith(f"product.{approved.id}.")):
            if (ctx.steps[key] or {}).get("image") == approved.image:
                paste = ctx.steps[key].get("paste") or {}
        ctx.write_json_output("concept.json", {
            "approved": approved.model_dump(mode="json"),
            "prompt": base_prompt,
            "candidates": [c.model_dump(mode="json") for c in candidates],
            "deferred_tweaks": deferred,
            "tweaks_used": tweaks_used,
            "form": plan.form.value,
            "custom_asks": [a.model_dump(mode="json") for a in plan.asks],
            "name_text": plan.name_text,
            "addons_shown": [a.kind.value for a in plan.visible()],
            # what the concept drew, for s06 onward to keep (measured from the pixels)
            "addon_hex": paste.get("addon_hex"),
            "text_hex": paste.get("text_hex"),
        }, role="concept")
        ctx.decide("concept.pick", approved.id, by="user")
        ctx.decide("concept.image_model", approved.image_model, by="user",
                   reason="which image model the user picked")
        ctx.decide("concept.product_source", approved.product_source, by="code")
        if approved.check is not None and not approved.check.passed:
            ctx.decide("concept.approved_with_failed_check",
                       [i.id for i in approved.check.items if not i.passed], by="user")
        for n, d in enumerate(deferred):
            ctx.decide(f"tweak.deferred.{n}", d, by="model")
        ctx.m.data.update({
            "approved_candidate": approved.id,
            "meshy_task_id": approved.meshy_task_id,
            "product_task_id": approved.product_task_id,
            "image_model": approved.image_model,
            "form": plan.form.value,
            "product_source": approved.product_source,
            "check_passed": approved.check.passed if approved.check else None,
            "rounds": rnd,
            "tweaks_used": tweaks_used,
        })
        ctx.m.confidence = 1.0 if approved.check is None or approved.check.passed else 0.6
