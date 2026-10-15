"""Stage 1, intake: Haiku reads the photos and request, maps subjects to photos, lists every
color region (markings included), and asks 2 to 4 tappable questions from the per-product
question bank. Gate: the user answers.

What the new-job form already set (input/request.json: target_size_mm, addons such as the
nameplate text or keyring position, form) is passed to intake and never asked again. It is kept
unless an answer changes it: size = answer > form > intake > product default; add-ons = product
defaults < intake < form < answers.

Form (FROM JACKSON): a keychain or magnet is a flat charm or a 3D figure, the customer's choice.
The form question is asked unless the order form or the request's own words ("3d", "flat") set
it: form = answer > order form > request words > FORM_DEFAULTS (only if unanswered). Every
other product is a 3D figure.

Custom asks (props, outfits, poses, names): from the request (Haiku's list with the code parser
under it), from bank options marked `ask`, and from text typed into a question. A name the
product has no place for (a 3D keychain has no text surface) is kept but marked not buildable,
and the customer gets one note per such ask.

Outputs (s01_intake/a<N>/): intake.json (IntakeResult), color_regions.json, answers.json,
subjects.json, addons.json, product.json (ProductSpec, role product_spec). Job updates: photo
subject ids, target size, add-ons, form, custom asks.
"""

from __future__ import annotations

from p2p_contracts import StageId
from p2p_contracts.job import (
    FORM_CHOICE_PRODUCTS,
    FORM_DEFAULTS,
    AddOn,
    AddOnKind,
    CustomAsk,
    ProductForm,
    ProductType,
)
from p2p_contracts.payloads import AnswersPayload, IntakeResult, ProductSpec

from ..clients.claude import IntakeRequest
from ..intent import merge_asks
from ..util import read_json
from .base import Stage, StageContext
from .product_plan import FLAT_NAME_MAX
from .prompts import answer_asks, bank_for, default_size, option_effects, resolve_addons


def form_values(ctx: StageContext) -> tuple[float | None, list[AddOn], ProductForm | None]:
    """What the new-job form set, from the job's own input file."""
    req = read_json(ctx.abs("input/request.json"), {}) or {}
    addons = []
    for a in req.get("addons") or []:
        try:
            addons.append(AddOn.model_validate(a))
        except ValueError:
            continue
    form = req.get("form")
    return (req.get("target_size_mm"), addons,
            ProductForm(form) if form in ProductForm._value2member_map_ else None)


def skipped_questions(bank: list[dict], size: float | None, addons: list[AddOn],
                      form: ProductForm | None = None) -> set[str]:
    """Bank questions the form already answered."""
    known = {a.kind.value for a in addons if a.text or a.params}
    if size:
        known.add("target_size_mm")
    if form:
        known.add("form")
    return {q["id"] for q in bank if q.get("covers") in known}


def decide_form(product: ProductType, answered: str | None, form_form: ProductForm | None,
                request_form: ProductForm | None) -> tuple[ProductForm, str]:
    """(form, who set it): user > form > request > default. Non-choice products: figure."""
    if product not in FORM_CHOICE_PRODUCTS:
        return ProductForm.FIGURE, "default"
    if answered:
        return ProductForm(answered), "user"
    if form_form:
        return form_form, "form"
    if request_form:
        return request_form, "request"
    return FORM_DEFAULTS[product], "default"


def mark_buildable(asks: list[CustomAsk], product: ProductType, form: ProductForm,
                   addons: list[AddOn]) -> list[CustomAsk]:
    """Text asks need a surface: the nameplate carries its own name (a matching ask is dropped),
    a flat keychain engraves one name of up to 10 characters, nothing else prints text."""
    plate = next((a for a in addons if a.kind == AddOnKind.NAMEPLATE), None)
    plate_text = (plate.text.text if plate and plate.text else "").strip().lower()
    out, engraved = [], False
    for a in asks:
        if a.kind != "text":
            out.append(a)
            continue
        if plate is not None and a.text.strip().lower() in plate_text:
            continue  # the plate already says it
        ok = (product == ProductType.KEYCHAIN and form == ProductForm.FLAT and not engraved
              and len(a.text.strip()) <= FLAT_NAME_MAX)
        engraved = engraved or ok
        out.append(a.model_copy(update={"buildable": ok}))
    return out


class IntakeStage(Stage):
    id = StageId.S01_INTAKE

    async def run(self, ctx: StageContext) -> None:
        job = ctx.job
        ctx.add_input("input/request.json", role="request")
        photos = []
        for p in job.photos:
            ctx.add_input(p.path, role="photo")
            photos.append(ctx.abs(p.path))
        ctx.save()

        product = job.product_type.value
        bank = bank_for(product)
        form_size, form_addons, form_form = form_values(ctx)
        choice = job.product_type in FORM_CHOICE_PRODUCTS
        skip = skipped_questions(bank, form_size, form_addons, form_form if choice else None)
        req = IntakeRequest(prompt=job.prompt, product_type=product, photos=photos,
                            question_bank=bank, form_target_size_mm=form_size,
                            form_addons=form_addons, skip_questions=skip,
                            form_form=form_form if choice else None)
        claude = ctx.svc.claude

        async def call_intake():
            async def call():
                res, regions, mc = await claude.intake(req)
                return {"intake": res.model_dump(mode="json"),
                        "color_regions": [r.model_dump(mode="json") for r in regions]}, mc

            return await ctx.claude("intake", claude.estimate_intake(req), call)

        out = await ctx.step("intake", call_intake)
        if "intake" not in out:  # attempts written before color regions existed
            out = {"intake": out, "color_regions": []}
        intake = IntakeResult.model_validate(out["intake"])
        regions = out["color_regions"]
        ctx.write_json_output("intake.json", intake, role="intake_result")
        ctx.write_json_output("color_regions.json", regions, role="color_regions")
        ctx.decide("intake.questions", [q.id for q in intake.questions], by="model",
                   model=ctx.m.model_calls[-1].model if ctx.m.model_calls else None)
        if skip:
            ctx.decide("intake.skipped", sorted(skip), by="code",
                       reason="already set on the new-job form")
        ctx.save()

        answers: AnswersPayload | None = None
        if intake.questions:
            reply = await ctx.gate("questions", gate_kind="answer_questions", accept={"answers"},
                                   prompt_kind="questions", prompt=intake)
            answers = AnswersPayload.model_validate(reply.payload)
            ctx.decide("intake.answers", reply.payload, by="user")
        ctx.write_json_output("answers.json", answers or AnswersPayload(answers=[]),
                              role="answers")

        subject_map = {
            "subjects": [s.model_dump(mode="json") for s in intake.subjects],
            "photos": [
                {"path": p.path, "subject_ids": [s.id for s in intake.subjects
                                                 if i in s.photo_indices]}
                for i, p in enumerate(job.photos)
            ],
        }
        ctx.write_json_output("subjects.json", subject_map, role="subject_map")

        effects = option_effects(product, answers)
        if "target_size_mm" in effects:
            size, size_by = effects["target_size_mm"], "user"
        elif form_size:
            size, size_by = form_size, "form"
        elif intake.target_size_mm:
            size, size_by = intake.target_size_mm, "model"
        else:
            size, size_by = default_size(product), "default"
        addons = resolve_addons(product, intake, answers, form_addons)
        ctx.write_json_output("addons.json", [a.model_dump(mode="json") for a in addons],
                              role="addons")
        form, form_by = decide_form(job.product_type, effects.get("form"), form_form,
                                    intake.form)
        words = {w for s in intake.subjects for w in f"{s.name} {s.kind}".lower().split()}
        asks = merge_asks(list(intake.custom_asks), answer_asks(product, answers), words)
        asks = mark_buildable(asks, job.product_type, form, addons)
        picks = {a.question_id: list(a.option_ids) for a in (answers.answers if answers else [])}
        spec = ProductSpec(product_type=job.product_type, form=form, target_size_mm=size,
                           addons=addons, custom_asks=asks, picks=picks,
                           decided_by={"form": form_by,  # type: ignore[dict-item]
                                       "target_size_mm": {"user": "user", "form": "form",
                                                          "model": "request"}.get(size_by,
                                                                                  "default")})
        ctx.write_json_output("product.json", spec, role="product_spec")
        for i, p in enumerate(job.photos):
            p.subject_ids = subject_map["photos"][i]["subject_ids"]
        job.target_size_mm = size
        job.addons = addons
        job.form = form
        job.custom_asks = asks
        ctx.store.save_job(job)
        ctx.store.update_job_row(job.id, target_size_mm=size,
                                 addons=[a.model_dump(mode="json") for a in addons],
                                 form=form.value,
                                 custom_asks=[a.model_dump(mode="json") for a in asks])
        ctx.decide("job.target_size_mm", size, by="user" if size_by == "user" else "code",
                   reason=f"from {size_by}")
        ctx.decide("job.addons", [a.model_dump(mode="json") for a in addons],
                   by="user" if answers and answers.answers else "code")
        ctx.decide("job.form", form.value, by="user" if form_by == "user" else "code",
                   reason=f"from {form_by}")
        if asks:
            ctx.decide("job.custom_asks", [a.model_dump(mode="json") for a in asks], by="model",
                       reason="request, bank picks and typed answers")
        for n, a in enumerate(a for a in asks if not a.buildable):
            async def note(a=a):
                msg = ctx.store.post_message(job.id, "assistant", "note",
                                             {"text": f"Not printable here: {a.text}"},
                                             stage=ctx.stage)
                return {"message_id": msg.id}

            await ctx.step(f"unbuildable.{n}", note)

        ctx.m.data.update({
            "subjects": len(intake.subjects),
            "questions": len(intake.questions),
            "color_regions": len(regions),
            "markings": sum(1 for r in regions if r.get("kind") == "marking"),
            "target_size_mm": size,
            "addons": [a.kind.value for a in addons],
            "form": form.value,
            "custom_asks": len(asks),
        })
        ctx.m.confidence = 1.0 if intake.subjects else 0.5
