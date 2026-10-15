"""Fixes from live run 2 (docs/runs/live2): what the concept check verifies, which edit model
it pays for, what nothing-extra means, how reviews are classified, and that heavy work never
stalls the event loop. Fake Meshy and MockClaude only; no live calls."""

from __future__ import annotations

import json

from conftest import FailFirstComposite, make_rt, run
from p2p_contracts import JobStatus, StageId
from p2p_contracts.job import AddOn, AddOnKind, CustomAsk, JobRecord
from p2p_contracts.payloads import (
    Answer,
    AnswersPayload,
    ConceptCandidate,
    IntakeResult,
)
from p2p_worker.clients.claude import MockClaude
from p2p_worker.local import AutoResponder, run_job, run_local
from p2p_worker.stages.product_plan import ProductPlan
from p2p_worker.util import utcnow


def manifest(rt, job_id, stage):
    job = rt.store.load_job(job_id)
    return rt.store.load_manifest(job_id, stage, job.attempts[stage])


def concept_json(rt, job_id):
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    ref = next(o for o in m3.outputs if o.role == "concept")
    return json.loads((rt.store.job_dir(job_id) / ref.path).read_text())


def edit_models(m3) -> list[str]:
    return [t.request["ai_model"] for t in m3.meshy_tasks if t.purpose.startswith("product")]


class Picks(AutoResponder):
    """Answers the intake questions with the given options, the first option otherwise."""

    def __init__(self, picks: dict[str, str], **kw):
        super().__init__(**kw)
        self.picks = picks

    def respond(self, rt, job_id):
        job = rt.store.load_job(job_id)
        m = rt.store.load_manifest(job_id, job.stage, job.attempts[job.stage]) \
            if job.stage else None
        if m is not None and m.gate is not None and m.gate.kind == "answer_questions":
            q = next(x for x in reversed(rt.store.list_messages(job_id, role="assistant"))
                     if x.kind == "questions")
            intake = IntakeResult.model_validate(q.payload)
            ans = [Answer(question_id=qq.id, option_ids=[self.picks.get(qq.id, qq.options[0].id)])
                   for qq in intake.questions]
            rt.store.post_message(job_id, "user", "answers", AnswersPayload(answers=ans),
                                  stage=job.stage, reply_to=q.id)
            return True
        return super().respond(rt, job_id)


def _answers(**picks: str) -> AnswersPayload:
    return AnswersPayload(answers=[Answer(question_id=k, option_ids=[v])
                                   for k, v in picks.items()])


# ---------------------------------------------------------------- 1. every visible answer
def test_every_visible_intake_answer_and_ask_is_on_the_checklist():
    """live2 Bronco: the answer was sitting, the approved concept stood, and the check never
    looked at the pose. Pose, plate shape, loop spot, form, name and every ask are checked."""
    plate = JobRecord(id="j", created_at=utcnow(), prompt="nameplate for my beagle Bronco",
                      product_type="nameplate", form="figure", target_size_mm=120,
                      addons=[AddOn(kind=AddOnKind.NAMEPLATE, text={"text": "Bronco"},
                                    params={"shape": "oval"})],
                      custom_asks=[CustomAsk(kind="outfit", text="wearing a bow tie")])
    items = {i.id: i for i in ProductPlan.from_job(
        plate, answers=_answers(plate_shape="oval", pose="sitting", size="medium")).check_items()}
    assert items["answer.pose"].label == "Pose: a sitting pose"
    assert items["answer.pose"].on == "figure" and items["answer.pose"].kind == "ask"
    assert items["part.nameplate"].label.startswith("An oval name plate")  # the shape pick
    assert items["text"].label == "The name reads exactly Bronco"
    assert items["ask.1"].label == "wearing a bow tie"
    assert not any(k.startswith(("answer.size", "answer.plate")) for k in items)
    assert items["no_extras"].on == "figure" and items["no_extras.product"].on == "product"

    key3d = JobRecord(id="k", created_at=utcnow(), prompt="3d keychain of my cat",
                      product_type="keychain", form="figure", target_size_mm=45,
                      addons=[AddOn(kind=AddOnKind.KEYRING_LOOP, params={"position": "back"})])
    items = {i.id: i for i in ProductPlan.from_job(
        key3d, answers=_answers(form="figure", loop="back", style="chibi")).check_items()}
    assert items["answer.form"].label.startswith("Form: a 3D sculpted figure")
    assert "on the middle of the back" in items["part.keyring_loop"].label  # the loop pick
    assert items["answer.style"].label == "Look: cute chibi proportions with a big head"

    flat = key3d.model_copy(update={"form": "flat"})
    items = {i.id: i for i in ProductPlan.from_job(
        flat, answers=_answers(form="flat", style="cartoon")).check_items()}
    assert items["part.relief"].on == "product" and "answer.form" not in items
    assert items["answer.style"].on == "product"  # a flat charm is judged on the charm
    assert "decorative frame, ring" in items["no_extras"].label
    assert items["no_extras"].on == "product" and "no_extras.product" not in items


def test_a_concept_in_the_wrong_pose_fails_and_is_redrawn_sitting(settings, photos):
    claude = MockClaude()
    claude.check_script = [{"answer.pose"}]  # the figure stands; the customer picked sitting
    rt = make_rt(settings.with_(cost_profile="dev"), claude=claude)
    job = rt.orchestrator.create_job("nameplate for my dog named Bronco", photos[:1],
                                     product_type="nameplate")
    status = run(run_job(rt, job.id, Picks({"pose": "sitting"})))
    run(rt.fake.drain())
    assert status == JobStatus.DONE, rt.store.job_row(job.id)
    first = claude.check_requests[0]
    pose = next(i for i in first.items if i.id == "answer.pose")
    assert pose.label == "Pose: a sitting pose" and pose.on == "figure"
    assert "sitting is haunches on the ground" in first_system_prompt()
    m3 = manifest(rt, job.id, StageId.S03_CONCEPT)
    fix = next(t.request for t in m3.meshy_tasks if t.purpose.endswith("fix"))
    assert "Correct these problems from the last try: Pose: a sitting pose" in fix["prompt"]
    approved = ConceptCandidate.model_validate(concept_json(rt, job.id)["approved"])
    assert approved.check.passed and approved.figure_image.endswith("figure_r1c1_1.png")


def first_system_prompt() -> str:
    from p2p_worker.clients.claude import CONCEPT_CHECK_SYSTEM

    return CONCEPT_CHECK_SYSTEM


# ---------------------------------------------------------------- 3. nothing extra
def test_an_unrequested_frame_on_a_flat_charm_is_flagged_not_passed(settings, photos):
    """live2 catfish: both charms came back inside a decorative ring frame and the check passed
    them. The art prompt forbids a frame, the check flags one, one new figure is tried, and a
    charm that still has it is shown with the failed item, never as passed."""
    claude = MockClaude()
    claude.check_script = [{"no_extras"}] * 4  # the prototype keeps adding the ring frame
    rt = make_rt(settings.with_(cost_profile="dev"), claude=claude)
    job_id, status, _ = run(_local(rt, photos[:1], "keychain of my cat holding a fish",
                                   product_type="keychain", form="flat"))
    assert status == JobStatus.DONE
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    art = [t.request["prompt"] for t in m3.meshy_tasks if t.purpose.startswith("concept r1")]
    assert art and all("no frame, ring, border, card or badge around it" in p for p in art)
    assert all("frame, ring or border" in p for p in art)  # the per-model avoid list
    req = claude.check_requests[0]
    extra = next(i for i in req.items if i.id == "no_extras")
    assert extra.on == "product" and "decorative frame, ring" in extra.label
    fix = next(t.request for t in m3.meshy_tasks if t.purpose.endswith("fix"))
    assert "Nothing the customer did not ask for around the subject" in fix["prompt"]
    approved = ConceptCandidate.model_validate(concept_json(rt, job_id)["approved"])
    assert not approved.check.passed
    assert [i.id for i in approved.check.items if not i.passed] == ["no_extras"]
    assert any(d.key == "concept.approved_with_failed_check" for d in m3.decisions)


def test_a_stray_object_in_a_3d_figure_is_redrawn(settings, photos):
    claude = MockClaude()
    claude.check_script = [{"no_extras"}]  # live2 Bronco r1c2: a wedge block by the dog
    rt = make_rt(settings.with_(cost_profile="dev"), claude=claude)
    job_id, status, _ = run(_local(rt, photos[:1], "figurine of my dog"))
    assert status == JobStatus.DONE
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    assert m3.data["concept_fixes"] == ["r1c1.1:figure"]
    fix = next(t.request for t in m3.meshy_tasks if t.purpose.endswith("fix"))
    assert "Nothing the customer did not ask for" in fix["prompt"]


# ---------------------------------------------------------------- 2. edit models
def _fail_paste(monkeypatch, n: int):
    """The first n add-on edits come back with the figure redrawn (paste-back refuses)."""
    import dataclasses

    from p2p_worker.stages import s03_concept

    real = s03_concept.imaging.paste_back
    calls = {"n": 0}

    def paste_back(figure_path, edit_path, out_path, *, mode="full"):
        res = real(figure_path, edit_path, out_path, mode=mode)
        calls["n"] += 1
        if calls["n"] <= n:
            return dataclasses.replace(res, ok=False, reason="the edit changed the figure "
                                       "(rmse 0.177, scale 0.37)")
        return res

    monkeypatch.setattr(s03_concept.imaging, "paste_back", paste_back)
    return calls


def test_a_model_whose_edit_failed_is_not_used_again(settings, photos):
    """live2 Bronco: after nano-banana-pro failed the plate edit and a figure problem forced a
    new figure, the plate went back to nano-banana-pro and failed again. Now the new figure's
    plate starts from code again and its paid edit goes to the other model."""
    claude = MockClaude()
    # composite misspells; the edit misspells AND the figure stands on a base (figure fix);
    # the new figure's composite misspells; the other model's edit passes
    claude.check_script = [{"text"}, {"text", "no_extra_base"}, {"text"}, set()]
    rt = make_rt(settings.with_(cost_profile="dev"), claude=claude)
    job_id, status, _ = run(_local(rt, photos[:1], "nameplate for my dog named Bronco",
                                   product_type="nameplate"))
    assert status == JobStatus.DONE
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    assert edit_models(m3) == ["nano-banana-2", "nano-banana-pro"]  # never the failed one twice
    assert [f["model"] for f in m3.data["edit_failures"]] == ["nano-banana-2"]
    fixes = [d.value["action"] for d in m3.decisions if d.key.startswith("concept.fix.")]
    assert fixes == ["edit", "figure", "edit"]
    approved = ConceptCandidate.model_validate(concept_json(rt, job_id)["approved"])
    assert approved.product_source == "edit" and approved.check.passed


def test_two_failed_edits_stop_paid_edits_for_the_whole_job(settings, photos, monkeypatch):
    """Both candidates' code composites fail and their plate edits fail on nano-banana-pro (as
    on the live Bronco). The stage buys no third edit and shows both on the code composite:
    18 credits of edits where the live Bronco spent 60 (SOURCE: its s03 manifest)."""
    calls = _fail_paste(monkeypatch, 2)
    rt = make_rt(settings, claude=FailFirstComposite())
    job_id, status, _ = run(_local(rt, photos[:1], "nameplate for my dog named Bronco",
                                   product_type="nameplate"))
    assert status == JobStatus.DONE
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    models = edit_models(m3)
    # the two candidates run in parallel: both usually pick nano-banana-pro before either
    # fails; a second pick after a failure goes to the other model
    assert len(models) == 2 and models[0] == "nano-banana-pro", models
    assert [f["model"] for f in m3.data["edit_failures"]] == models
    assert calls["n"] == len(models)
    cands = [ConceptCandidate.model_validate(c) for c in concept_json(rt, job_id)["candidates"]]
    assert {c.product_source for c in cands} == {"code"}
    credits = sum(t.credits for t in m3.meshy_tasks if t.purpose.startswith("product"))
    assert credits <= 9 + 12  # the stage cap: no third edit


def test_a_bought_edit_keeps_its_model_on_replay(settings, photos, monkeypatch):
    """The model choice is a cached step, so a replay (after a webhook wait or a crash) never
    swaps the model of an edit already paid for, even when failures landed meanwhile."""
    from p2p_worker.stages import s03_concept

    rt = make_rt(settings.with_(cost_profile="dev"), claude=FailFirstComposite())
    job_id, status, _ = run(_local(rt, photos[:1], "nameplate for my dog named Bronco",
                                   product_type="nameplate"))
    assert status == JobStatus.DONE
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    assert m3.data["steps"]["edit_model.r1c1.1"] == {"model": "nano-banana-2"}
    stage = s03_concept.ConceptStage()

    class Ctx:
        m = m3
        attempt = m3.attempt
        store = rt.store
        job = rt.store.load_job(job_id)
        settings_ = None

    Ctx.m.data["edit_failures"] = [{"model": "nano-banana-2"}]
    Ctx.settings = rt.settings.with_(cost_profile="dev")
    assert stage._edit_pick(Ctx, "text") == {"model": "nano-banana-pro",
                                             "avoided": ["nano-banana-2"]}
    Ctx.m.data["edit_failures"] += [{"model": "nano-banana-pro"}]
    assert stage._edit_pick(Ctx, "text")["model"] is None


async def _local(rt, photos, prompt, **kw):
    try:
        return await run_local(rt, photos, prompt, auto=True, **kw)
    finally:
        await rt.fake.drain()


# ---------------------------------------------------------------- 4. review kinds
def _fake_paint_agent(monkeypatch, res: dict):
    """An s09 agent module that keeps s08's paint and asks for review through the real
    PaintStage._to_review with the paint run's result `res`, on its first attempt only."""
    import sys
    import types

    from p2p_worker.stages.base import Stage
    from p2p_worker.stages.s09_paint import PaintStage
    from p2p_worker.stages.s09_stub import PaintStub

    class FakeAgent(Stage):
        id = StageId.S09_PAINT

        async def run(self, ctx):
            await PaintStub().run(ctx)
            ctx.m.confidence = res.get("confidence")
            if ctx.attempt == 1:
                await PaintStage()._to_review(ctx, res)

    mod = types.ModuleType("p2p_worker.stages.s09_paint")
    FakeAgent.__module__ = mod.__name__
    mod.FakeAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "p2p_worker.stages.s09_paint", mod)


def _full_job(rt, photos):
    async def go():
        try:
            return await run_local(rt, photos, "keychain of my dog", auto=True,
                                   product_type="keychain", profile="full")
        finally:
            await rt.fake.drain()

    return run(go())


def _review(rt, job_id, action):
    from p2p_contracts.payloads import ReviewDecision

    rt.store.post_message(job_id, "user", "review_decision", ReviewDecision(action=action),
                          stage=rt.store.load_job(job_id).stage)
    status = run(rt.orchestrator.advance(job_id))
    notes = [m.payload["text"] for m in rt.store.list_messages(job_id, role="assistant")
             if m.kind == "note"]
    return status, notes


SOMBRERO = {"confidence": 0.6, "slice_ok": True, "swaps": [],
            "review_reason": "confidence 0.6 (failed: face_parts, islands); final print differs "
                             "from the approved concept: Hat trim prints as patchy red spots "
                             "instead of a continuous colorful band (paint_region)"}


def test_a_paint_check_failure_with_a_passing_gate_is_a_fidelity_review(settings, photos,
                                                                        monkeypatch):
    """live2 sombrero: the final-vs-concept check failed, the Bambu gate passed, the review said
    the slicer gate failed and approve was refused. Now it reads as a paint check failure with
    the gate passed, keeps its confidence, and approve accepts the paint and ships through the
    s10 gate."""
    _fake_paint_agent(monkeypatch, SOMBRERO)
    rt = make_rt(settings.with_(paint_agent="agent"))
    job_id, status, _ = _full_job(rt, photos[:1])
    assert status == JobStatus.REVIEW
    m9 = manifest(rt, job_id, StageId.S09_PAINT)
    assert m9.error.code == "validation" and m9.error.detail["review"] == "fidelity"
    row = rt.store.job_row(job_id)
    assert row["review_reason"].startswith("Paint check failed; slicer gate passed: ")
    assert "slicer gate failed" not in row["review_reason"].lower()
    assert row["confidence"] == 0.6
    req = next(m for m in rt.store.list_messages(job_id, role="assistant")
               if m.kind == "review_request")
    assert req.payload["reason"].startswith("Paint check failed; slicer gate passed")

    status, notes = _review(rt, job_id, "approve")
    assert status == JobStatus.DONE, notes
    assert "Approved as it is. Writing the final file and running the slicer gate." in notes
    assert not any(n.startswith("Cannot approve") for n in notes)
    m9 = manifest(rt, job_id, StageId.S09_PAINT)
    assert m9.attempt == 1 and m9.status.value == "succeeded"  # the reviewed paint, kept
    assert any(d.key == "review.approved" and d.by == "user" for d in m9.decisions)
    m10 = manifest(rt, job_id, StageId.S10_WRITE)
    assert m10.status.value == "succeeded"
    assert rt.store.load_job(job_id).result["slice_ok"] is True


def test_a_failed_gate_is_still_a_gate_review_and_approve_is_refused(settings, photos,
                                                                     monkeypatch):
    _fake_paint_agent(monkeypatch, {**SOMBRERO, "slice_ok": False,
                                    "review_reason": "slicer gate failed: floating region"})
    rt = make_rt(settings.with_(paint_agent="agent"))
    job_id, status, _ = _full_job(rt, photos[:1])
    assert status == JobStatus.REVIEW
    m9 = manifest(rt, job_id, StageId.S09_PAINT)
    assert m9.error.code == "slicer_failed" and m9.error.detail["review"] == "gate"
    assert rt.store.job_row(job_id)["confidence"] == 0.0
    status, notes = _review(rt, job_id, "approve")
    assert status == JobStatus.REVIEW
    assert "Cannot approve: the file failed the slicer gate. Rerun or reject." in notes


def test_the_live_sombrero_review_classifies_as_fidelity():
    """Manifests written before detail.review: the live sombrero's s09 (code slicer_failed,
    slice_ok true) is a fidelity review; an s10 gate failure stays a gate review."""
    from p2p_contracts import Manifest, StageError, StageStatus
    from p2p_worker.errors import review_kind

    def paused(stage, code, detail):
        return Manifest(job_id="j", stage=stage, attempt=1, status=StageStatus.PAUSED,
                        error=StageError(code=code, message="m", retryable=False, detail=detail))

    assert review_kind(paused(StageId.S09_PAINT, "slicer_failed",
                              {"confidence": 0.6, "slice_ok": True})) == "fidelity"
    assert review_kind(paused(StageId.S09_PAINT, "slicer_failed",
                              {"confidence": 0.0, "slice_ok": False})) == "gate"
    assert review_kind(paused(StageId.S10_WRITE, "slicer_failed", {"errors": ["x"]})) == "gate"
    assert review_kind(paused(StageId.S10_WRITE, "slicer_failed",
                              {"gate": "estimate"})) == "estimate"
    assert review_kind(paused(StageId.S05_MESH, "budget_exceeded", {})) is None
    assert review_kind(paused(StageId.S09_PAINT, "validation",
                              {"review": "fidelity"})) == "fidelity"


# ---------------------------------------------------------------- 5. heavy work off the loop
def silhouette_area(n: int, seed: int = 0) -> float:
    """p2p_geometry's projected_silhouette on a triangle soup: manifold3d's CrossSection union,
    the call that held the GIL for 83 s at a time on the live sombrero (1,291,050 faces)."""
    import numpy as np
    from p2p_geometry.meshio import projected_silhouette

    rng = np.random.default_rng(seed)
    tri = rng.uniform(0, 60, (n, 1, 3)) + rng.normal(0, 1.5, (n, 3, 3))
    v = tri.reshape(-1, 3)
    return float(projected_silhouette(v, np.arange(len(v)).reshape(-1, 3)).area())


async def _max_gap_while(coro_fn, tick_s: float = 0.02) -> tuple[float, float, object]:
    import asyncio
    import time

    gaps: list[float] = []
    done = False

    async def tick():
        last = time.monotonic()
        while not done:
            await asyncio.sleep(tick_s)
            now = time.monotonic()
            gaps.append(now - last - tick_s)
            last = now

    t = asyncio.create_task(tick())
    t0 = time.monotonic()
    out = await coro_fn()
    took = time.monotonic() - t0
    done = True
    await t
    return max(gaps or [0.0]), took, out


def test_gil_holding_work_in_a_child_process_leaves_the_loop_free():
    """live2 sombrero: s06 ran in a thread and the loop still stalled for minutes, because the
    geometry's native code holds the GIL (a scratch probe on the sombrero mesh: gaps up to
    138 s). run_cpu runs it in a child process; the loop keeps ticking (heartbeats, Realtime,
    HTTP)."""
    from p2p_worker import offload

    async def go():
        await offload.run_cpu(silhouette_area, 50)  # the child is up (spawn plus imports)
        return await _max_gap_while(lambda: offload.run_cpu(silhouette_area, 16000))

    try:
        gap, took, area = run(go())
    finally:
        offload.shutdown()
    assert area > 1000  # the real work ran and came back
    assert took > 0.5, took  # long enough that a stalled loop would show it
    assert gap < 0.25, (gap, took)


def test_the_child_log_reaches_the_worker_log(tmp_path):
    import asyncio

    from p2p_worker.offload import FileLog, tail_log

    path = tmp_path / "paint.log"
    got: list[str] = []

    async def go():
        tail = asyncio.create_task(tail_log(path, got.append, every_s=0.01))
        log = FileLog(str(path))
        log("opened job a1: 36 regions")
        await asyncio.sleep(0.05)
        log("iteration 1: 4 tool calls")
        tail.cancel()
        try:
            await tail
        except asyncio.CancelledError:
            pass

    run(go())
    assert got == ["opened job a1: 36 regions", "iteration 1: 4 tool calls"]


def test_geometry_builds_in_a_child_process_and_its_decisions_land(settings, photos):
    """s06 on a real job with offload=process: build_product runs in the child, the geometry is
    written, and the decisions it made there are on the parent's manifest."""
    from p2p_worker import offload

    rt = make_rt(settings.with_(cost_profile="dev"))
    job_id, status, _ = run(_local(rt, photos[:1], "keychain of my dog",
                                   product_type="keychain"))
    assert status == JobStatus.DONE
    rt2 = make_rt(settings.with_(cost_profile="dev", offload="process"), store=rt.store)
    try:
        out = run(rt2.orchestrator.run_stages(job_id, [StageId.S06_PRINTABILITY]))
    finally:
        offload.shutdown()
    assert out == {"s06_printability": "succeeded"}
    m6 = manifest(rt2, job_id, StageId.S06_PRINTABILITY)
    assert m6.attempt == 2 and m6.data["geometry"] == "p2p_geometry"
    assert any(d.key == "geometry.build_product" and d.value == "ok" for d in m6.decisions)
    assert any(o.role == "geometry" for o in m6.outputs)


def test_the_paint_agent_runs_in_a_child_process_when_offload_is_process(settings, photos,
                                                                         monkeypatch):
    """s09 with the real driver goes to offload (the whole paint run: agent turns, simulated
    prints, renders, the final write and gate); a scripted driver stays in process."""
    from p2p_worker.stages import s09_paint

    calls = []

    async def fake_child(ctx, sam3, max_iter):
        calls.append((ctx.stage.value, ctx.attempt, max_iter))
        raise RuntimeError("stop here")

    monkeypatch.setattr(s09_paint, "paint_in_child", fake_child)
    monkeypatch.delenv("P2P_PAINT_DRIVER", raising=False)
    rt = make_rt(settings.with_(cost_profile="dev"))
    job_id, status, _ = run(_local(rt, photos[:1], "keychain of my dog",
                                   product_type="keychain", profile="full"))
    assert status == JobStatus.DONE
    rt2 = make_rt(settings.with_(cost_profile="dev", offload="process",
                                 paint_agent="agent"), store=rt.store)
    out = run(rt2.orchestrator.run_stages(job_id, [StageId.S09_PAINT]))
    assert out == {"s09_paint": "failed"} and calls == [("s09_paint", 2, 8)]


def test_every_offloaded_build_crosses_a_process_boundary(settings, photos, monkeypatch):
    """With offload=process, s06, s07, s08 and s10 hand their builds to run_cpu; each call and
    its arguments survive pickling (what a spawn child needs) and the job finishes the same.
    run_cpu is swapped for a pickle round trip in a thread, so the slicer stub still applies."""
    import asyncio
    import pickle

    from p2p_worker import offload

    seen: list[str] = []

    async def pickled(fn, /, *args, **kw):
        fn2, args2, kw2 = pickle.loads(pickle.dumps((fn, args, kw)))
        seen.append(getattr(args2[0], "__qualname__", "?") if args else "?")
        return pickle.loads(pickle.dumps(await asyncio.to_thread(fn2, *args2, **kw2)))

    monkeypatch.setattr(offload, "run_cpu", pickled)
    rt = make_rt(settings.with_(cost_profile="dev", offload="process"))
    job_id, status, _ = run(_local(rt, photos[:1], "keychain of my dog", product_type="keychain",
                                   profile="full"))
    assert status == JobStatus.DONE
    assert seen == ["GeometryStage._build", "BaselineStage._build", "PaletteStage._build",
                    "WriteStage._build"], seen
    assert rt.store.load_job(job_id).result["slice_ok"] is True
