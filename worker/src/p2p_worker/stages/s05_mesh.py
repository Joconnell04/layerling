"""Stage 5, 3D model: the approved figure (or charm) to a mesh, then a review gate.

Inputs follow the job's form (FROM JACKSON: never generate a 3D figure and flatten it later) and
never send one image twice (docs/runs/fidelity/DEEP-DIVE.md item 5):

- 3D figure with s04 views: Multi-Image to 3D, image_urls = the ordered views (approved figure
  first, then back, side, front; at most 4), texture_image_urls = the same list, and no
  input_task_id (it would silently replace the list). Product profile: latest (Meshy 7.1),
  geometry_resolution 2k, multi_view_thumbnails (35 credits).
- 3D figure without views (s04 skipped or its views failed): Image to 3D from the approved
  figure's task (input_task_id), or the figure image once that task is past the 60 h margin.
  Same price, one input (meshy-iteration.md section 3). Dev profile: meshy-6-lite (15).
- Flat charm: Creative Lab build (keychain or fridge magnet, 30 credits) from the approved
  prototype task, at the job's size, with a printable base (ASSUMED 2.0 mm; the keychain API
  defaults to 0.1). The prototype must be inside Meshy's 3-day retention.

Review: approve, retexture (10) or regenerate. A regenerate whose note only changes colors or
markings ("make the saddle darker", intent.color_only_fix) is done as a Retexture instead (10,
not 35), except on a flat charm. Retexture: Image to 3D meshes by task id, others by model_url;
with the customer's text by text_style_prompt, else from the views (multiview_image_urls,
meshy-7), else from the figure; a style input this job already paid for moves to the next one.
Regenerate changes one input each time (REGEN_VARIANTS) and skips a variant the job already
bought; Meshy has no seed, and identical inputs reuse the task already paid for
(base.meshy_create).

Outputs: mesh_m<N>.glb, thumb_m<N>_<view>.png, mesh.json; the approved GLB carries role
approved_mesh.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from p2p_contracts import StageId
from p2p_contracts.payloads import ConceptCandidate, MeshDecision, MeshReview, ViewSet

from ..clients.meshy import estimate_credits
from ..errors import StagePaused, VendorError
from ..intent import color_only_fix
from ..util import data_uri, glb_face_count, read_json, utcnow
from .base import Stage, StageContext
from .common import optional
from .product_plan import ProductPlan
from .s03_concept import image_ref

MAX_MESH_ROUNDS = 5
RETENTION_SAFE = timedelta(hours=60)
VIEWS = ("front", "right", "back", "left")
FLAT_BASE_MM = 2.0  # ASSUMED printable base under a Creative Lab relief (docs default 0.1)
# One changed input per regenerate, in order (meshy-iteration.md section 2, lever 3).
REGEN_VARIANTS: dict[str, list[dict[str, Any]]] = {
    "multi-image-to-3d": [{"image_enhancement": False}, {"_drop_last_view": True},
                          {"remove_lighting": False},
                          {"image_enhancement": False, "_drop_last_view": True}],
    "image-to-3d": [{"image_enhancement": False}, {"remove_lighting": False},
                    {"image_enhancement": False, "remove_lighting": False},
                    {"geometry_resolution": "standard"}],
    "lite": [{"should_remesh": True}, {"should_remesh": True, "target_polycount": 60000},
             {"should_remesh": True, "target_polycount": 120000},
             {"should_remesh": True, "target_polycount": 200000}],
    "build": [{"relief_curve": "s-curve"}, {"relief_curve": "gamma", "curve_param": 0.7},
              {"smoothing": 0.5}, {"relief_height_mm": 3.0}],
}


def fresh(finished_at) -> bool:
    return bool(finished_at and utcnow() - finished_at < RETENTION_SAFE)


def regen_body(endpoint: str, body: dict[str, Any], n: int, dev: bool) -> dict[str, Any] | None:
    """The first generation's body with the n-th applicable variant (n from 1), or None when
    they are used up. Variants replace each other; they do not stack."""
    kind = "build" if endpoint.endswith("-build") else "lite" if dev else endpoint
    urls = list(body.get("image_urls") or [])
    variants = [v for v in REGEN_VARIANTS.get(kind, [])
                if not v.get("_drop_last_view") or len(urls) > 2]
    if n > len(variants):
        return None
    v = dict(variants[n - 1])
    out = {**body}
    if kind == "build":
        out["options"] = {**(body.get("options") or {}), **v}
        return out
    if v.pop("_drop_last_view", False):
        out["image_urls"] = urls[:-1]
        if "texture_image_urls" in out:
            out["texture_image_urls"] = urls[:-1]
    out.update(v)
    return out


class MeshStage(Stage):
    id = StageId.S05_MESH

    def views_of(self, ctx: StageContext) -> ViewSet | None:
        if optional(ctx, StageId.S04_MULTIVIEW) is None:
            return None
        m4 = ctx.upstream(StageId.S04_MULTIVIEW)
        if not any(o.role == "views" for o in m4.outputs):
            return None
        path, _ = ctx.use_output(StageId.S04_MULTIVIEW, "views")
        data = read_json(path) or {}
        try:
            return ViewSet.model_validate(data)
        except ValueError:  # views.json from before the ViewSet contract
            return None

    def gen_request(self, ctx: StageContext, plan: ProductPlan, approved: ConceptCandidate,
                    figure_path: str, views: ViewSet | None,
                    s03_tasks: dict[str, Any]) -> tuple[str, dict[str, Any], list[str]]:
        """(endpoint, body, job paths of the images sent) for the first generation."""
        dev = ctx.settings.cost_profile == "dev"
        if plan.flat:
            proto = s03_tasks.get(approved.product_task_id or "")
            if not approved.product_task_id or proto is None or not fresh(proto.finished_at):
                raise StagePaused("The approved charm is older than Meshy keeps it (3 days); "
                                  "approve the concept again")
            options: dict[str, Any] = {"base_thickness_mm": FLAT_BASE_MM}
            if ctx.job.target_size_mm:
                options["size_mm"] = float(ctx.job.target_size_mm)
            return (f"{plan.creative_lab}-build",
                    {"input_task_id": approved.product_task_id, "options": options,
                     "output": {"format": "glb"}}, [approved.image])
        common: dict[str, Any] = {"should_texture": True, "multi_view_thumbnails": True,
                                  "target_formats": ["glb"]}
        if dev:
            common["ai_model"] = "meshy-6-lite"
        else:
            common.update({"ai_model": "latest", "geometry_resolution": "2k",
                           "should_remesh": False})
        if views and views.views:
            paths = [v.image for v in views.views[:4]]
            urls = [image_ref(ctx, p) for p in paths]
            body = {"image_urls": urls, **common}
            if not dev:  # texture_image_urls needs meshy-7.1 or latest
                body["texture_image_urls"] = urls
            return "multi-image-to-3d", body, paths
        task = s03_tasks.get(approved.meshy_task_id or "")
        if task is not None and fresh(task.finished_at):
            return "image-to-3d", {"input_task_id": approved.meshy_task_id, **common}, \
                [figure_path]
        return "image-to-3d", {"image_url": image_ref(ctx, figure_path), **common}, [figure_path]

    def retexture_options(self, ctx: StageContext, base: dict[str, Any], text: str | None,
                          view_urls: list[str], figure_path: str
                          ) -> list[tuple[str, dict[str, Any]]]:
        """Retexture bodies in order of preference, each with why it is sent: the customer's
        text (then the same text marked as a new variation, since Meshy has no seed), the s04
        views (multiview_image_urls needs meshy-7), the approved figure."""
        out: list[tuple[str, dict[str, Any]]] = []
        if text:
            for k in range(3):
                t = text if k == 0 else f"{text} Variation {k + 1}."
                out.append((f"retexture: {text}"[:300],
                            {**base, "text_style_prompt": t[:800], "ai_model": "latest"}))
        if view_urls:
            out.append(("retexture from the s04 views",
                        {**base, "multiview_image_urls": view_urls[:4], "ai_model": "meshy-7"}))
        out.append(("retexture from the approved figure",
                    {**base, "image_style_url": image_ref(ctx, figure_path),
                     "ai_model": "latest"}))
        return out

    async def _fetch(self, ctx: StageContext, tag: str, key: str,
                     result: dict[str, Any]) -> dict[str, Any]:
        glb = await ctx.fetch_asset(key, lambda t: (t.get("model_urls") or {}).get("glb"),
                                    f"mesh_{tag}.glb", role="mesh_candidate", kind="glb")
        if glb is None:
            raise VendorError("Meshy 3D result has no GLB", retryable=True)
        thumbs = []
        for v in VIEWS:
            if (result.get("thumbnail_urls") or {}).get(v):
                ref = await ctx.fetch_asset(
                    key, lambda t, v=v: (t.get("thumbnail_urls") or {}).get(v),
                    f"thumb_{tag}_{v}.png", role="mesh_thumbnail")
                if ref is not None:
                    thumbs.append(ref.path)
        if not thumbs and result.get("thumbnail_url"):
            ref = await ctx.fetch_asset(key, lambda t: t.get("thumbnail_url"),
                                        f"thumb_{tag}_front.png", role="mesh_thumbnail")
            if ref is not None:
                thumbs.append(ref.path)
        return {"glb": glb.path, "thumbnails": thumbs,
                "face_count": glb_face_count(ctx.abs(glb.path))}

    async def run(self, ctx: StageContext) -> None:
        concept_json, _ = ctx.use_output(StageId.S03_CONCEPT, "concept")
        concept = read_json(concept_json)
        approved = ConceptCandidate.model_validate(concept["approved"])
        s03 = ctx.upstream(StageId.S03_CONCEPT)
        role = ("figure_concept" if any(o.role == "figure_concept" for o in s03.outputs)
                else "approved_concept")
        _fig, fig_ref = ctx.use_output(StageId.S03_CONCEPT, role)
        ctx.use_output(StageId.S03_CONCEPT, "approved_concept")
        plan = ProductPlan.from_job(ctx.job)
        views = None if plan.flat else self.views_of(ctx)
        tasks = {t.task_id: t for t in s03.meshy_tasks}
        endpoint0, gen_body, sent = self.gen_request(ctx, plan, approved, fig_ref.path, views,
                                                     tasks)
        dev = ctx.settings.cost_profile == "dev"
        ctx.decide("mesh.inputs", {"endpoint": endpoint0, "images": sent}, by="code",
                   reason=("flat charm: Creative Lab build of the approved prototype"
                           if plan.flat else f"{len(sent)} image(s), approved figure first"))
        view_urls = list(gen_body.get("image_urls") or []) if endpoint0 == "multi-image-to-3d" \
            else []

        m, action, text, regens = 1, "generate", None, 0
        endpoint, first_body = endpoint0, gen_body
        while True:
            if action == "generate":
                key, endpoint, body = f"m{m}.gen", endpoint0, gen_body
                why = "3D model from the approved concept" if m == 1 else \
                    f"regenerate: {text or 'no note'}"[:300]
            else:
                prev = ctx.steps[f"m{m - 1}.mesh"]
                prev_task = ctx.task(prev["key"])
                if prev["endpoint"] == "image-to-3d" and prev_task \
                        and fresh(prev_task.finished_at):
                    src: dict[str, Any] = {"input_task_id": prev_task.task_id}
                else:  # sign at call time: Storage first, else a fresh Meshy URL, else bytes
                    model_url = ctx.store.signed_url(ctx.job.id, prev["glb"])
                    if not model_url and prev_task and fresh(prev_task.finished_at):
                        model_url = ((await ctx.fresh_task(prev["key"])).get("model_urls")
                                     or {}).get("glb")
                    if not model_url:
                        model_url = data_uri(ctx.abs(prev["glb"]).read_bytes(),
                                             "application/octet-stream")
                    src = {"model_url": model_url}
                options = self.retexture_options(
                    ctx, {**src, "enable_original_uv": True, "target_formats": ["glb"]}, text,
                    view_urls, fig_ref.path)

                async def pick_retexture(options=options) -> int | None:
                    """The first style input this job has not paid for: Meshy has no seed,
                    so the same retexture again would only buy a random re-roll."""
                    for i, (_why, b) in enumerate(options):
                        if not ctx.already_paid("retexture", b):
                            return i
                    return None

                i = await ctx.step(f"m{m}.retex_input", pick_retexture)
                if i is None:
                    ctx.save()
                    raise StagePaused("Every retexture input was already used on this model; "
                                      "send the change as text")
                why, body = options[i]
                key, endpoint = f"m{m}.retex", "retexture"
            result = await ctx.meshy_run(key, endpoint, body, f"3D model round {m} ({action})",
                                         fresh=m > 1, why=why)

            async def fetch(key=key, result=result, endpoint=endpoint, m=m):
                info = await self._fetch(ctx, f"m{m}", key, result)
                return {**info, "key": key, "endpoint": endpoint}

            mesh = await ctx.step(f"m{m}.mesh", fetch)
            task = ctx.task(mesh["key"])
            nxt = regen_body(endpoint0, first_body, regens + 1, dev)
            review = MeshReview(
                glb=mesh["glb"], thumbnails=mesh["thumbnails"], meshy_task_id=task.task_id,
                face_count=mesh["face_count"],
                actions=["approve", "regenerate"] if plan.flat else
                ["approve", "retexture", "regenerate"],
                retexture_credits=estimate_credits("retexture", {}),
                regenerate_credits=estimate_credits(endpoint0, nxt or gen_body),
            )
            reply = await ctx.gate(f"m{m}.review", gate_kind="approve_mesh",
                                   accept={"mesh_decision"}, prompt_kind="mesh_review",
                                   prompt=review)
            d = MeshDecision.model_validate(reply.payload)
            ctx.decide(f"mesh.decision.m{m}", d.action, by="user", reason=d.text)
            if d.action == "approve":
                break
            if m >= MAX_MESH_ROUNDS:
                ctx.save()
                raise StagePaused(f"3D review rounds used up ({MAX_MESH_ROUNDS})")
            if d.action == "retexture" and plan.flat:
                ctx.note("Charms regenerate only")
                d = MeshDecision(action="regenerate", text=d.text)
            elif d.action == "regenerate" and not plan.flat and color_only_fix(d.text):
                # a color or marking change on a good shape: Retexture (10) instead of a new
                # model (35), meshy-iteration.md section 2, lever 2
                retex = estimate_credits("retexture", {})
                ctx.decide(f"mesh.cheaper.m{m}", "retexture", by="code", reason=(
                    f"color-only change: retexture {retex} credits instead of a new model "
                    f"{review.regenerate_credits}"))

                async def tell(retex=retex, regen=review.regenerate_credits):
                    ctx.note(f"Color change: retexture ({retex} credits), not a new model "
                             f"({regen})")
                    return True

                await ctx.step(f"m{m}.cheaper_note", tell)
                d = MeshDecision(action="retexture", text=d.text)
            m += 1
            action = "retexture" if d.action == "retexture" else "generate"
            text = d.text
            if action == "generate":
                async def pick_variant(first=regens + 1) -> int | None:
                    """The next regenerate variant this job has not paid for (a variant an
                    earlier attempt already bought is a model the customer has seen)."""
                    n = first
                    while (b := regen_body(endpoint0, first_body, n, dev)) is not None:
                        if not ctx.already_paid(endpoint0, b):
                            return n
                        n += 1
                    return None

                n = await ctx.step(f"m{m}.regen_variant", pick_variant)
                nxt = regen_body(endpoint0, first_body, n, dev) if n else None
                if nxt is None:
                    ctx.save()
                    raise StagePaused("No input left to change for another 3D generation")
                regens = n
                gen_body = nxt
                ctx.decide(f"mesh.regen.{regens}", {k: v for k, v in nxt.items()
                                                    if k not in ("image_urls", "image_url",
                                                                 "texture_image_urls")},
                           by="code", reason="each regenerate changes one input")

        ctx.add_output(mesh["glb"].split("/")[-1], role="approved_mesh", kind="glb")
        for t in mesh["thumbnails"]:
            ctx.add_output(t.split("/")[-1], role="approved_thumbnail")
        ctx.write_json_output("mesh.json", {
            "meshy_task_id": task.task_id,
            "endpoint": mesh["endpoint"],
            "glb": mesh["glb"],
            "thumbnails": mesh["thumbnails"],
            "face_count": mesh["face_count"],
            "finished_at": task.finished_at.isoformat() if task.finished_at else None,
            "rounds": m,
            "form": plan.form.value,
            "inputs": sent,
            # Creative Lab builds are not accepted as input_task_id by Analyze or Multi-Color;
            # s06 and s07 must send model_url for them.
            "chainable": not mesh["endpoint"].endswith("-build"),
        }, role="mesh")
        ctx.decide("mesh.approved", task.task_id, by="user")
        ctx.m.data.update({"meshy_task_id": task.task_id, "endpoint": mesh["endpoint"],
                           "rounds": m, "face_count": mesh["face_count"],
                           "form": plan.form.value, "inputs": len(sent)})
        ctx.m.confidence = 1.0
