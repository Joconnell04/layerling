"""Front half of the fidelity fix (s01 to s05): the form and custom asks decided at intake, a
product concept derived from the figure with the figure's pixels kept, a checked concept with
regenerations that change their inputs, real views for s05, inputs by form, and the Meshy backup
status read. Fake Meshy and MockClaude only."""

from __future__ import annotations

import json
from datetime import timedelta

import numpy as np
import pytest
from conftest import REPO, FailFirstComposite, make_rt, run
from p2p_contracts import JobStatus, StageId, StageStatus
from p2p_contracts.job import MAX_CONCEPT_FIXES, AddOn, AddOnKind, CustomAsk, JobRecord
from p2p_contracts.payloads import ConceptCandidate, ProductSpec, ViewSet
from p2p_worker import imaging
from p2p_worker.clients.claude import MockClaude
from p2p_worker.errors import VendorError
from p2p_worker.fakes.meshy import FakeMeshy
from p2p_worker.intent import asks_from_text, form_from_text
from p2p_worker.local import AutoResponder, run_job, run_local
from p2p_worker.stages.base import StageContext, status_read_due
from p2p_worker.stages.product_plan import ProductPlan
from p2p_worker.stages.s05_mesh import regen_body
from p2p_worker.store import LocalStore
from p2p_worker.sweep import jobs_due
from p2p_worker.util import read_json, utcnow


def manifest(rt, job_id, stage):
    job = rt.store.load_job(job_id)
    return rt.store.load_manifest(job_id, stage, job.attempts[stage])


async def _local(rt, photos, prompt, **kw):
    try:
        return await run_local(rt, photos, prompt, auto=True, **kw)
    finally:
        await rt.fake.drain()


def concept_json(rt, job_id):
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    ref = next(o for o in m3.outputs if o.role == "concept")
    return json.loads((rt.store.job_dir(job_id) / ref.path).read_text())


def bodies(m, purpose_prefix):
    return [t.request for t in m.meshy_tasks if t.purpose.startswith(purpose_prefix)]


class AnswerForm(AutoResponder):
    """Answers the form question with a given option, everything else with the first option."""

    def __init__(self, form: str, **kw):
        super().__init__(**kw)
        self.form = form

    def respond(self, rt, job_id):
        from p2p_contracts.payloads import Answer, AnswersPayload, IntakeResult

        job = rt.store.load_job(job_id)
        m = rt.store.load_manifest(job_id, job.stage, job.attempts[job.stage]) if job.stage \
            else None
        if m is not None and m.gate is not None and m.gate.kind == "answer_questions":
            q = next(x for x in reversed(rt.store.list_messages(job_id, role="assistant"))
                     if x.kind == "questions")
            intake = IntakeResult.model_validate(q.payload)
            ans = [Answer(question_id=qq.id,
                          option_ids=[self.form if qq.id == "form" else qq.options[0].id])
                   for qq in intake.questions]
            rt.store.post_message(job_id, "user", "answers", AnswersPayload(answers=ans),
                                  stage=job.stage, reply_to=q.id)
            return True
        return super().respond(rt, job_id)


# ---------------------------------------------------------------- request parsing
def test_form_words_and_asks_in_casual_requests():
    assert form_from_text("3d keychain of my cat") == "figure"
    assert form_from_text("flat keychain of my calico") == "flat"
    assert form_from_text("keychain of my frenchie") is None
    assert form_from_text("magnet with a flat back of my dog") is None  # a back, not a form
    asks = {(a.kind, a.text) for a in asks_from_text(
        "keychain of my frenchie wearing a sombrero lol")}
    assert asks == {("outfit", "wearing a sombrero")}
    asks = {(a.kind, a.text) for a in asks_from_text(
        "nameplate for my beagle bronco with his name, give him a bow tie")}
    assert asks == {("outfit", "wearing a bow tie")}
    assert [a.text for a in asks_from_text("my golden holding a ball pls")] == ["holding a ball"]
    assert asks_from_text("keychain of me holding my chihuahua") == []  # a subject, not a prop
    assert [(a.kind, a.text) for a in asks_from_text("keychain of my teddy with his name Otis")] \
        == [("text", "Otis")]


def test_golden_prompts_are_casual_and_the_parser_finds_their_asks():
    sets = json.loads((REPO / "evals" / "golden" / "sets.json").read_text())
    assert len(sets) == 20
    asks = {s["id"]: s["expect"]["custom_asks"] for s in sets}
    assert "wearing a sombrero" in asks["bw_frenchie"]
    assert "wearing a bow tie" in asks["sp_beagle"]
    assert "holding a ball" in asks["sp_golden_face"]
    for s in sets:
        exp = s["expect"]
        assert s["prompt"] == s["prompt"].strip() and len(s["prompt"]) < 80
        if s["product_type"] in ("keychain", "magnet"):
            assert form_from_text(s["prompt"]) == exp["form"], s["id"]
            assert exp["form_asked"] == (exp["form"] is None)
        found = " ".join(a.text for a in asks_from_text(s["prompt"]))
        for want in exp["custom_asks"]:
            assert want in found, (s["id"], want, found)


# ---------------------------------------------------------------- s01 intake
def test_keychain_form_is_asked_and_flat_answer_is_kept(settings, photos):
    rt = make_rt(settings)
    job = rt.orchestrator.create_job("keychain of my dog", photos[:1], product_type="keychain")
    status = run(run_job(rt, job.id, AnswerForm("flat")))
    run(rt.fake.drain())
    # the back half builds the Creative Lab charm flat (s06 and s07 send its model_url)
    assert status == JobStatus.DONE
    q = next(m for m in rt.store.list_messages(job.id) if m.kind == "questions")
    ids = [x["id"] for x in q.payload["questions"]]
    assert ids[0] == "form" and "loop" in ids
    job = rt.store.load_job(job.id)
    assert job.form == "flat"
    m1 = manifest(rt, job.id, StageId.S01_INTAKE)
    spec = ProductSpec.model_validate(read_json(rt.store.job_dir(job.id) / next(
        o.path for o in m1.outputs if o.role == "product_spec")))
    assert spec.form == "flat" and spec.decided_by["form"] == "user"
    assert rt.store.job_row(job.id)["form"] == "flat"
    assert any(d.key == "job.form" and d.by == "user" for d in m1.decisions)


def test_form_in_the_request_or_on_the_order_form_is_not_asked(settings, photos):
    rt = make_rt(settings)
    for prompt, form, want in (("3d keychain of my dog", None, "figure"),
                               ("keychain of my dog", "flat", "flat")):
        job = rt.orchestrator.create_job(prompt, photos[:1], product_type="keychain", form=form)
        assert run(rt.orchestrator.advance(job.id)) == JobStatus.WAITING_USER
        q = [m for m in rt.store.list_messages(job.id) if m.kind == "questions"][-1]
        assert "form" not in [x["id"] for x in q.payload["questions"]]
        assert q.payload["form"] == want
    run(rt.fake.drain())


def test_figurine_is_always_a_3d_figure_and_never_asked(settings, photos):
    rt = make_rt(settings)
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    run(rt.orchestrator.advance(job.id))
    q = next(m for m in rt.store.list_messages(job.id) if m.kind == "questions")
    assert "form" not in [x["id"] for x in q.payload["questions"]]
    run(rt.fake.drain())


def test_custom_asks_reach_the_concept_prompt_the_check_and_the_views(settings, photos):
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos[:1], "keychain of my frenchie wearing a sombrero",
                                   product_type="keychain"))
    assert status == JobStatus.DONE
    job = rt.store.load_job(job_id)
    assert [(a.kind, a.text) for a in job.custom_asks] == [("outfit", "wearing a sombrero")]
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    for body in bodies(m3, "concept r1"):
        assert "Must show: wearing a sombrero." in body["prompt"]
    reqs = rt.claude.check_requests
    assert reqs and all(any(i.kind == "ask" and i.label == "wearing a sombrero"
                            for i in r.items) for r in reqs)
    m4 = manifest(rt, job_id, StageId.S04_MULTIVIEW)
    assert "Keep showing: wearing a sombrero." in m4.meshy_tasks[0].request["prompt"]


def test_a_name_with_no_place_on_the_product_gets_a_note(settings, photos):
    rt = make_rt(settings)
    job = rt.orchestrator.create_job("3d keychain of my teddy with his name Otis on it",
                                     photos[:1], product_type="keychain")
    run(run_job(rt, job.id, AutoResponder(), max_steps=3))
    run(rt.fake.drain())
    job = rt.store.load_job(job.id)
    otis = next(a for a in job.custom_asks if a.kind == "text")
    assert otis.text == "Otis" and otis.buildable is False
    notes = [m.payload["text"] for m in rt.store.list_messages(job.id) if m.kind == "note"]
    assert "Not printable here: Otis" in notes


def test_every_answer_reaches_the_product_concept(settings, photos):
    """Plate shape, loop spot and extras picks are honored downstream, not only stored."""
    from p2p_contracts.payloads import Answer, AnswersPayload

    class Picks(AutoResponder):
        def respond(self, rt, job_id):
            job = rt.store.load_job(job_id)
            m = rt.store.load_manifest(job_id, job.stage, job.attempts[job.stage])
            if m.gate is not None and m.gate.kind == "answer_questions":
                q = next(x for x in rt.store.list_messages(job_id, role="assistant")
                         if x.kind == "questions")
                pick = {"plate_shape": "oval", "name": "custom"}
                ans = [Answer(question_id=x["id"],
                              option_ids=[pick.get(x["id"], x["options"][0]["id"])],
                              text="Biscuit" if x["id"] == "name" else None)
                       for x in q.payload["questions"]]
                rt.store.post_message(job_id, "user", "answers", AnswersPayload(answers=ans),
                                      stage=job.stage, reply_to=q.id)
                return True
            return super().respond(rt, job_id)

    # the code composite goes first; failing it once makes the paid edit (and its prompt) run
    rt = make_rt(settings.with_(cost_profile="dev"), claude=FailFirstComposite())
    job = rt.orchestrator.create_job("nameplate for my dog", photos[:1],
                                     product_type="nameplate")
    # make sure the shape question is asked
    status = run(run_job(rt, job.id, Picks()))
    run(rt.fake.drain())
    assert status == JobStatus.DONE, rt.store.job_row(job.id)
    job = rt.store.load_job(job.id)
    plate = next(a for a in job.addons if a.kind == AddOnKind.NAMEPLATE)
    assert plate.text.text == "Biscuit"
    m3 = manifest(rt, job.id, StageId.S03_CONCEPT)
    edits = bodies(m3, "product")
    assert edits and all('the name "Biscuit"' in b["prompt"] for b in edits)
    assert plate.params["shape"] == "oval"
    assert all("flat oval name plate" in b["prompt"] for b in edits)
    assert any(i.label.startswith("An oval name plate") for i in
               rt.claude.check_requests[-1].items)
    req = rt.claude.check_requests[-1]
    assert req.expected_text == "Biscuit"
    assert any(i.id == "text" for i in req.items)


# ---------------------------------------------------------------- s03 concept
def test_product_concept_keeps_the_figure_pixels(settings, photos):
    rt = make_rt(settings, claude=FailFirstComposite())  # each composite fails: edits run
    job_id, status, _ = run(_local(rt, photos[:1], "keychain of my dog", product_type="keychain"))
    assert status == JobStatus.DONE
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    jdir = rt.store.job_dir(job_id)
    roles = {o.role for o in m3.outputs}
    assert {"approved_concept", "figure_concept", "figure_candidate", "product_edit",
            "concept_candidate"} <= roles
    c = concept_json(rt, job_id)
    approved = ConceptCandidate.model_validate(c["approved"])
    assert approved.product_source == "edit" and approved.form == "figure"
    assert approved.check.passed and approved.check.figure_identical == 1.0
    # the edit chains from the figure task; the figure render from the photos
    figs = {t.task_id: t for t in m3.meshy_tasks if t.purpose.startswith("concept r1")}
    for body in bodies(m3, "product"):
        assert body["input_task_id"] in figs
        assert "keyring loop tab on top of the head" in body["prompt"]
        assert "loop or hole" not in body["prompt"]  # the edit may draw the loop
    assert approved.meshy_task_id in figs
    # the approved product contains the approved figure, pixel for pixel
    fig = imaging.load_rgba(jdir / approved.figure_image)
    al = imaging.align(fig, imaging.load_rgba(jdir / approved.image))
    assert al is not None and al.scale == pytest.approx(1.0, abs=0.02) and al.rmse < 0.02
    assert (jdir / "s03_concept/a1/figure_concept.png").read_bytes() == \
        (jdir / approved.figure_image).read_bytes()
    assert c["addons_shown"] == ["keyring_loop"] and c["addon_hex"]
    assert approved.image.endswith("concept_r1c2_1.png")  # the edit after the composite


def test_nameplate_concept_shows_the_plate_and_name(settings, photos):
    rt = make_rt(settings, claude=FailFirstComposite())  # each composite fails: edits run
    job_id, status, _ = run(_local(rt, photos[:1], "nameplate for my dog named Bronco",
                                   product_type="nameplate"))
    assert status == JobStatus.DONE
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    edits = bodies(m3, "product")
    assert [b["ai_model"] for b in edits] == ["nano-banana-pro", "nano-banana-pro"]
    assert all(b["aspect_ratio"] == "16:9" for b in edits)  # a wide frame for a wide plate
    assert all('spelled exactly "Bronco"' in b["prompt"] for b in edits)
    assert all("plate" not in b["prompt"].split("Avoid:")[-1] for b in edits
               if "Avoid:" in b["prompt"])
    c = concept_json(rt, job_id)
    assert c["name_text"] == "Bronco"
    items = {i.id for i in ConceptCandidate.model_validate(c["approved"]).check.items}
    assert {"part.nameplate", "text", "markings", "no_extra_base", "figure_identical"} <= items


def test_failed_figure_check_regenerates_with_changed_inputs(settings, photos):
    claude = MockClaude()
    claude.check_script = [{"ask"}]  # the first candidate's figure lost the sombrero
    rt = make_rt(settings.with_(cost_profile="dev"), claude=claude)
    job_id, status, _ = run(_local(rt, photos[:1], "keychain of my dog wearing a sombrero",
                                   product_type="keychain"))
    assert status == JobStatus.DONE
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    first = [t.request for t in m3.meshy_tasks if t.purpose == "concept r1c1 nano-banana-2"]
    assert len(first) == 1
    fix = next(t.request for t in m3.meshy_tasks if t.purpose.endswith("fix"))
    assert fix["prompt"] != first[0]["prompt"]
    assert "Correct these problems from the last try: wearing a sombrero" in fix["prompt"]
    assert m3.data["concept_fixes"] == ["r1c1.1:figure"]
    c = concept_json(rt, job_id)
    approved = ConceptCandidate.model_validate(c["approved"])
    assert approved.check.passed and approved.figure_image.endswith("figure_r1c1_1.png")


def test_failed_name_check_buys_an_edit_then_switches_model_then_stops_on_code(settings,
                                                                               photos):
    claude = MockClaude()
    claude.check_script = [{"text"}, {"text"}, {"text"}]  # composite, edit, second model
    rt = make_rt(settings.with_(cost_profile="dev"), claude=claude)
    job_id, status, _ = run(_local(rt, photos[:1], "nameplate for my dog named Bronco",
                                   product_type="nameplate"))
    assert status == JobStatus.DONE
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    edits = bodies(m3, "product")
    assert [b["ai_model"] for b in edits] == ["nano-banana-2", "nano-banana-pro"]
    assert all("Correct these problems" in b["prompt"] for b in edits)
    c = concept_json(rt, job_id)
    approved = ConceptCandidate.model_validate(c["approved"])
    # both paid edits failed: the candidate is shown on its free code composite
    assert approved.product_source == "code" and not approved.check.passed
    assert approved.image.endswith("concept_r1c1.png")
    actions = [d.value for d in m3.decisions if d.key.startswith("concept.fix.")]
    assert [a["action"] for a in actions] == ["edit", "edit", "stop"]
    assert "switch model" in actions[1]["reason"]
    assert "2 paid product edits failed" in actions[2]["reason"]


def test_concept_fixes_are_capped_per_job(settings, photos, monkeypatch):
    from p2p_worker.stages import s03_concept

    assert MAX_CONCEPT_FIXES == 4
    monkeypatch.setattr(s03_concept, "MAX_CONCEPT_FIXES", 1)
    claude = MockClaude()
    claude.check_script = [{"markings"}] * 10  # never right
    rt = make_rt(settings, claude=claude)
    job_id, status, _ = run(_local(rt, photos[:1], "figurine of my dog"))
    assert status == JobStatus.DONE  # shown with its failed check; the customer decides
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    assert len(m3.data["concept_fixes"]) == 1
    fixes = [t for t in m3.meshy_tasks if t.purpose.endswith("fix")]
    assert len(fixes) == 1  # the cap, not one per candidate
    assert any("fix cap reached" in str(d.value) for d in m3.decisions
               if d.key.startswith("concept.fix."))
    approved = ConceptCandidate.model_validate(concept_json(rt, job_id)["approved"])
    assert approved.check is not None and not approved.check.passed
    assert any(d.key == "concept.approved_with_failed_check" for d in m3.decisions)


def test_identical_meshy_inputs_reuse_the_task_already_paid_for(settings, photos):
    rt = make_rt(settings)
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    m = rt.orchestrator._new_attempt(job, StageId.S03_CONCEPT)
    ctx = StageContext(rt.orchestrator.svc, job, StageId.S03_CONCEPT, m.attempt, m, utcnow())
    body = {"ai_model": "nano-banana", "prompt": "a dog", "reference_image_urls": ["data:x,1"]}

    async def go():
        a = await ctx.meshy_create("a", "image-to-image", body, "first")
        b = await ctx.meshy_create("b", "image-to-image", dict(body), "again")
        assert b.task_id == a.task_id
        with pytest.raises(VendorError, match="fresh variation must change an input"):
            await ctx.meshy_create("c", "image-to-image", body, "regenerate", fresh=True)
        await ctx.meshy_create("d", "image-to-image", {**body, "prompt": "a dog, fixed"}, "fix")
        await rt.fake.drain()

    run(go())
    assert rt.fake.count("POST", "image-to-image") == 2


def test_magnet_as_a_3d_figure_needs_no_edit(settings, photos):
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos[:1], "3d magnet of my dog", product_type="magnet"))
    assert status == JobStatus.DONE
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    assert not bodies(m3, "product")
    approved = ConceptCandidate.model_validate(concept_json(rt, job_id)["approved"])
    assert approved.product_source == "figure" and approved.check.passed


# ---------------------------------------------------------------- flat charm
def test_flat_keychain_charm_path(settings, photos):
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos[:1], "flat keychain of my dog with his name Otis",
                                   product_type="keychain"))
    # s03 to s05 build the charm; s06 and s07 send the Creative Lab build by model_url (Meshy
    # does not take its task id) and geometry keeps it flat
    assert status == JobStatus.DONE
    job = rt.store.load_job(job_id)
    assert job.form == "flat"
    for s in (StageId.S01_INTAKE, StageId.S03_CONCEPT, StageId.S04_MULTIVIEW, StageId.S05_MESH):
        assert manifest(rt, job_id, s).status == StageStatus.SUCCEEDED, s
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    protos = [t for t in m3.meshy_tasks if t.endpoint.endswith("keychain/v1/prototype")]
    assert len(protos) == 2 and all(t.request["name_text"] == "Otis" for t in protos)
    assert all(t.request["remove_background"] for t in protos)
    for body in bodies(m3, "concept r1"):
        assert "Art for a flat relief keychain charm" in body["prompt"]
        assert "3/4 hero view" not in body["prompt"]
    approved = ConceptCandidate.model_validate(concept_json(rt, job_id)["approved"])
    assert approved.product_source == "prototype" and approved.form == "flat"
    assert {i.id for i in approved.check.items} >= {"part.relief", "part.keyring_loop", "text",
                                                    "text.engraving"}
    assert all(i.on == "product" for i in approved.check.items)
    views = ViewSet.model_validate(read_json(rt.store.job_dir(job_id)
                                             / "s04_multiview/a1/views.json"))
    assert views.skipped and "flat relief" in views.reason
    m5 = manifest(rt, job_id, StageId.S05_MESH)
    [build] = m5.meshy_tasks
    assert build.endpoint.endswith("keychain/v1/build")
    assert build.request["input_task_id"] == approved.product_task_id
    assert build.request["options"] == {"base_thickness_mm": 2.0, "size_mm": 35.0}
    mesh = read_json(rt.store.job_dir(job_id) / "s05_mesh/a1/mesh.json")
    assert mesh["chainable"] is False and mesh["form"] == "flat"
    assert m5.meshy_tasks and rt.fake.count("POST", "multi-image-to-3d") == 0
    for stage in (StageId.S06_PRINTABILITY, StageId.S07_BASELINE):
        req = manifest(rt, job_id, stage).meshy_tasks[0].request
        assert "model_url" in req and "input_task_id" not in req, stage
    m6 = manifest(rt, job_id, StageId.S06_PRINTABILITY)
    assert m6.data["form"] == "flat"


def test_flat_magnet_uses_the_fridge_magnet_api(settings, photos):
    rt = make_rt(settings.with_(cost_profile="dev"))
    run(_local(rt, photos[:1], "magnet of my dog", product_type="magnet"))
    job = next(iter(rt.store.list_jobs()))
    assert rt.store.load_job(job).form == "flat"  # the first form option for magnets
    m3 = manifest(rt, job, StageId.S03_CONCEPT)
    protos = [t for t in m3.meshy_tasks if "fridge-magnet" in t.endpoint]
    assert protos and all("name_text" not in t.request for t in protos)
    m5 = manifest(rt, job, StageId.S05_MESH)
    assert m5.meshy_tasks[0].endpoint.endswith("fridge-magnet/v1/build")


# ---------------------------------------------------------------- s04 views and s05 inputs
def test_views_are_checked_ordered_and_sent_without_duplicates(settings, photos):
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos[:1], "figurine of my dog"))
    assert status == JobStatus.DONE
    m4 = manifest(rt, job_id, StageId.S04_MULTIVIEW)
    [task] = m4.meshy_tasks
    body = task.request
    approved = ConceptCandidate.model_validate(concept_json(rt, job_id)["approved"])
    assert body["generate_multi_view"] is True and body["input_task_id"] == approved.meshy_task_id
    assert body["ai_model"] == approved.image_model
    assert "aspect_ratio" not in body and "remove_background" not in body
    assert "No base, plate, stand, text, keyring or loop" in body["prompt"]
    views = ViewSet.model_validate(read_json(rt.store.job_dir(job_id)
                                             / "s04_multiview/a1/views.json"))
    # SOURCE for the angles and their order: the live probe (meshy-iteration.md section 3)
    assert [v.angle for v in views.views] == ["concept", "back", "front", "three_quarter"]
    jdir = rt.store.job_dir(job_id)
    for v in views.views:  # every view on the same flat background, square
        img = imaging.load_rgba(jdir / v.image)
        assert img.shape[0] == img.shape[1] and np.allclose(img[0, 0, :3], 1.0, atol=0.01)
    m5 = manifest(rt, job_id, StageId.S05_MESH)
    gen = m5.meshy_tasks[0].request
    assert m5.meshy_tasks[0].endpoint.endswith("multi-image-to-3d")
    assert "input_task_id" not in gen
    assert len(gen["image_urls"]) == 4 and gen["texture_image_urls"] == gen["image_urls"]
    assert len(set(gen["image_urls"])) == 4  # no image twice
    assert any(i.from_stage == StageId.S04_MULTIVIEW for i in m5.inputs)


def test_failed_back_view_retries_once_then_falls_back_to_one_image(settings, photos):
    claude = MockClaude()
    claude.view_script = [{3}, {1, 2, 3}]  # the back fails, then everything fails
    rt = make_rt(settings, claude=claude)
    job_id, status, _ = run(_local(rt, photos[:1], "figurine of my dog"))
    assert status == JobStatus.DONE
    m4 = manifest(rt, job_id, StageId.S04_MULTIVIEW)
    assert len(m4.meshy_tasks) == 2
    retry = m4.meshy_tasks[1].request
    assert "Correct these problems from the last try: view 3 (back)" in retry["prompt"]
    views = ViewSet.model_validate(read_json(rt.store.job_dir(job_id)
                                             / "s04_multiview/a1/views.json"))
    # the first round's 3/4 and front passed; the retry's did not: keep the round that passed
    assert [v.angle for v in views.views] == ["concept", "front", "three_quarter"]

    claude2 = MockClaude()
    claude2.view_script = [{1, 2, 3}, {1, 2, 3}]
    rt2 = make_rt(settings.with_(jobs_dir=settings.jobs_dir.parent / "jobs2"), claude=claude2)
    job2, status, _ = run(_local(rt2, photos[:1], "figurine of my dog"))
    assert status == JobStatus.DONE
    views = ViewSet.model_validate(read_json(rt2.store.job_dir(job2)
                                             / "s04_multiview/a1/views.json"))
    assert views.views == [] and "failed the check twice" in views.reason
    m5 = manifest(rt2, job2, StageId.S05_MESH)
    gen = m5.meshy_tasks[0]
    assert gen.endpoint.endswith("openapi/v1/image-to-3d")
    assert set(gen.request) >= {"input_task_id"} and "image_url" not in gen.request
    assert "texture_image_url" not in gen.request  # one input, not the same image twice


def test_dev_profile_skips_views_and_says_why(settings, photos):
    rt = make_rt(settings.with_(cost_profile="dev"))
    job_id, status, _ = run(_local(rt, photos[:1], "figurine of my dog"))
    assert status == JobStatus.DONE
    views = ViewSet.model_validate(read_json(rt.store.job_dir(job_id)
                                             / "s04_multiview/a1/views.json"))
    assert views.skipped and "dev cost profile" in views.reason
    assert not manifest(rt, job_id, StageId.S04_MULTIVIEW).meshy_tasks


def test_regenerate_changes_an_input_every_time(settings, photos):
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos[:1], "figurine of my dog",
                                   mesh_actions=["regenerate", "regenerate"]))
    assert status == JobStatus.DONE
    m5 = manifest(rt, job_id, StageId.S05_MESH)
    gens = [t.request for t in m5.meshy_tasks if t.purpose.endswith("(generate)")]
    assert len(gens) == 3
    assert gens[1]["image_enhancement"] is False
    assert len(gens[2]["image_urls"]) == 3 and "image_enhancement" not in gens[2]
    keys = {json.dumps(g, sort_keys=True) for g in gens}
    assert len(keys) == 3
    assert regen_body("keychain-build", {"input_task_id": "x", "options": {}}, 1, False)[
        "options"]["relief_curve"] == "s-curve"
    assert regen_body("image-to-3d", {"input_task_id": "x"}, 9, False) is None


# ---------------------------------------------------------------- backup status read
def test_backup_status_read_after_ten_minutes_without_a_webhook(settings, photos):
    s = settings.with_(vendor_wait_s=0.2)
    store = LocalStore(s.jobs_dir)
    fake = FakeMeshy(auto_complete=False, webhooks=False, fixtures_dir=None,
                     on_event=lambda ev: store.insert_meshy_event(ev))
    rt = make_rt(s, store=store, fake=fake)
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    status = run(run_job(rt, job.id, AutoResponder(), max_steps=3))
    assert status == JobStatus.RUNNING
    m3 = rt.store.load_manifest(job.id, StageId.S03_CONCEPT, 1)
    assert m3.status == StageStatus.WAITING_VENDOR
    assert jobs_due(store, 600) == []  # waiting, but not 10 minutes yet

    # Meshy finishes; the webhook is lost. Ten minutes later the sweep finds the job.
    for t in list(fake.tasks):
        fake.complete(t)
    later = utcnow() + timedelta(minutes=11)
    assert jobs_due(store, 600, now=later) == [job.id]
    for t in m3.meshy_tasks:  # age the tasks for the real clock the stage reads
        t.created_at -= timedelta(minutes=11)
    store.write_manifest(m3)
    ledger = (rt.store.job_dir(job.id) / "ledger.jsonl").read_text()
    gets = fake.count("GET", "image-to-image")
    status = run(rt.orchestrator.advance(job.id))
    assert fake.count("GET", "image-to-image") - gets == 2  # one per waiting task
    m3 = rt.store.load_manifest(job.id, StageId.S03_CONCEPT, 1)
    figs = {t.task_id for t in m3.meshy_tasks if t.purpose.startswith("concept r1")}
    assert len(figs) == 2 and set(m3.data["meshy_status_reads"]) == figs
    assert any(d.key.startswith("meshy.missed_webhook.") for d in m3.decisions)
    reads = [d.value for d in m3.decisions if d.key.startswith("meshy.get.")]
    assert all(v.startswith("backup status read") for v in reads[:2])
    # the reads cost nothing: the ledger only gained the next paid steps, never a GET row
    new = (rt.store.job_dir(job.id) / "ledger.jsonl").read_text()[len(ledger):]
    assert all(json.loads(line)["kind"] != "get" for line in new.splitlines())
    run(fake.drain())


def test_backup_status_read_happens_at_most_every_ten_minutes():
    from p2p_contracts import Manifest, MeshyTask

    now = utcnow()
    t = MeshyTask(task_id="t1", endpoint="openapi/v1/image-to-image", purpose="p",
                  status="IN_PROGRESS", test_mode=True, created_at=now - timedelta(minutes=30))
    m = Manifest(job_id="j", stage=StageId.S03_CONCEPT, attempt=1, status=StageStatus.RUNNING,
                 meshy_tasks=[t])
    assert status_read_due(m, t, 600, now)
    m.data["meshy_status_reads"] = {"t1": (now - timedelta(minutes=4)).isoformat()}
    assert not status_read_due(m, t, 600, now)
    assert status_read_due(m, t, 600, now + timedelta(minutes=7))
    young = t.model_copy(update={"created_at": now - timedelta(minutes=5)})
    assert not status_read_due(Manifest(job_id="j", stage=StageId.S03_CONCEPT, attempt=1,
                                        status=StageStatus.RUNNING), young, 600, now)
    done = t.model_copy(update={"status": "SUCCEEDED"})
    assert not status_read_due(m, done, 600, now + timedelta(hours=1))


# ---------------------------------------------------------------- plan and imaging units
def test_product_plan_checklist_per_form():
    job = JobRecord(id="j", created_at=utcnow(), prompt="p", product_type="nameplate",
                    form="figure", target_size_mm=120,
                    addons=[AddOn(kind=AddOnKind.NAMEPLATE, text={"text": "Rex"},
                                  params={"shape": "oval"})],
                    custom_asks=[CustomAsk(kind="outfit", text="wearing a bow tie"),
                                 CustomAsk(kind="text", text="Good boy", buildable=False)])
    plan = ProductPlan.from_job(job, [{"subject_id": "s1", "region": "saddle", "color": "black",
                                       "kind": "marking"}])
    items = {i.id: i for i in plan.check_items()}
    assert items["part.nameplate"].label.startswith("An oval name plate")
    assert items["text"].label == "The name reads exactly Rex"
    assert items["ask.1"].label == "wearing a bow tie" and items["ask.1"].on == "figure"
    assert "black saddle" in items["markings"].label
    assert plan.edit_kind() == "text" and plan.paste_mode() == "full"
    prompt, allowed = plan.edit()
    assert "flat oval name plate" in prompt and "plate" in allowed


def test_paste_back_recovers_a_moved_and_scaled_figure(tmp_path):
    from p2p_worker.fakes import assets

    fig = assets.figure_image("unit")
    fig.save(tmp_path / "fig.png")
    edit = assets.edit_image((tmp_path / "fig.png").read_bytes(), 'a name plate, the name "Rex"')
    (tmp_path / "edit.png").write_bytes(edit)
    res = imaging.paste_back(tmp_path / "fig.png", tmp_path / "edit.png", tmp_path / "out.png")
    assert res.ok and res.identical == 1.0 and res.addon_share > 0.3
    assert res.alignment["scale"] == pytest.approx(0.55, abs=0.02)
    # a different figure is refused, not pasted
    other = assets.figure_image("someone else")
    other.save(tmp_path / "other.png")
    res = imaging.paste_back(tmp_path / "other.png", tmp_path / "edit.png", tmp_path / "x.png")
    assert not res.ok and "changed the figure" in (res.reason or "")


def test_split_views_cuts_a_sheet_into_tiles(tmp_path):
    from p2p_worker.fakes import assets
    from PIL import Image

    tiles = [assets.open_image(b) for b in assets.view_images(
        assets._png(assets.figure_image("sheet")), 3)]
    sheet = Image.new("RGBA", (256 * 3 + 40, 256), (255, 255, 255, 255))
    for i, t in enumerate(tiles):
        sheet.alpha_composite(t, (i * (256 + 20), 0))
    sheet.save(tmp_path / "sheet.png")
    out = imaging.split_views([tmp_path / "sheet.png"])
    assert len(out) == 3
    assert all(imaging.subject_share(imaging.normalize_view(o)) > 0.2 for o in out)


def test_an_edit_that_adds_nothing_fails_the_code_check(settings, photos, monkeypatch):
    """live2 run: the round-base edit came back as the figure alone (addon_share 0.0) and the
    vision check still passed "A round base under the figure". Code now fails it, and the fix
    path is the second edit model."""
    from p2p_worker.stages import s03_concept

    real = s03_concept.imaging.paste_back
    calls = []

    def paste_back(figure_path, edit_path, out_path, *, mode="full"):
        res = real(figure_path, edit_path, out_path, mode=mode)
        calls.append(res.addon_share)
        if len(calls) == 1:  # the first edit drew nothing outside the figure
            import dataclasses
            import shutil

            shutil.copyfile(figure_path, out_path)
            return dataclasses.replace(res, addon_share=0.0, addon_hex=None)
        return res

    monkeypatch.setattr(s03_concept.imaging, "paste_back", paste_back)
    rt = make_rt(settings.with_(cost_profile="dev"), claude=FailFirstComposite())
    job_id, status, _ = run(_local(rt, photos[:1], "figurine of my dog wearing a sombrero"))
    assert status == JobStatus.DONE
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    edits = bodies(m3, "product")
    assert [b["ai_model"] for b in edits] == ["nano-banana-2", "nano-banana-pro"]
    assert "Correct these problems" in edits[1]["prompt"]
    assert m3.data["concept_fixes"] == ["r1c1.1:edit", "r1c1.2:edit"]
    c = concept_json(rt, job_id)
    approved = ConceptCandidate.model_validate(c["approved"])
    item = next(i for i in approved.check.items if i.id == "addon_drawn")
    assert item.passed and item.by == "code" and "round base" in item.label
    assert approved.check.passed and approved.image.endswith("concept_r1c1_2.png")


def test_a_network_blip_while_waiting_does_not_buy_the_3d_model_twice(settings, photos):
    """live2 run: s05 lost its Supabase connection while waiting for the 3D model's webhook,
    failed, and the retry attempt went to create (and pay for) a second model. A network blip
    now replays the same attempt (lifecycle.py), which still holds the task, and waits for it."""
    import httpx
    from p2p_worker.clients.supabase import SupabaseError

    rt = make_rt(settings)
    real = rt.store.terminal_meshy_event
    dropped = []

    def flaky(task_id, **kw):
        if rt.fake.endpoint_of.get(task_id) == "multi-image-to-3d" and not dropped:
            dropped.append(task_id)
            try:
                raise httpx.RemoteProtocolError("Server disconnected")
            except httpx.RemoteProtocolError as e:
                raise SupabaseError("Supabase GET /rest/v1/meshy_events: "
                                    "RemoteProtocolError") from e
        return real(task_id, **kw)

    rt.store.terminal_meshy_event = flaky
    job_id, status, _ = run(_local(rt, photos[:1], "figurine of my dog"))
    assert status == JobStatus.DONE and dropped
    assert rt.fake.count("POST", "multi-image-to-3d") == 1  # paid once
    store = rt.store
    a1 = store.load_manifest(job_id, StageId.S05_MESH, 1)
    assert a1.status == StageStatus.SUCCEEDED
    assert [r["kind"] for r in a1.data["resumes"]] == ["transient"]
    assert "RemoteProtocolError" in a1.data["resumes"][0]["error"]
    assert store.load_job(job_id).attempts[StageId.S05_MESH] == 1
    assert [t.task_id for t in a1.meshy_tasks] == [dropped[0]]
    ledger = [json.loads(x) for x in (store.job_dir(job_id) / "ledger.jsonl").read_text()
              .splitlines()]
    assert sum(1 for e in ledger if e.get("ref") == dropped[0]) == 1  # booked once
