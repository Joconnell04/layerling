"""End-to-end thin slice against the fake Meshy server and MockClaude."""

from __future__ import annotations

import json

import pytest
from conftest import SimulatedCrash, make_rt, run
from p2p_contracts import JobStatus, StageId, StageStatus
from p2p_contracts.geometry import GeometryResult
from p2p_contracts.payloads import JobResult
from p2p_worker.fakes.meshy import FakeMeshy
from p2p_worker.local import AutoResponder, run_job, run_local
from p2p_worker.stages.prompts import GPT_CONSTRAINT, NANO_CLOSING, NANO_OPENING
from p2p_worker.store import LocalStore

THIN = [StageId.S01_INTAKE, StageId.S03_CONCEPT, StageId.S04_MULTIVIEW, StageId.S05_MESH,
        StageId.S06_PRINTABILITY, StageId.S07_BASELINE]


def manifest(rt, job_id, stage, attempt=None):
    job = rt.store.load_job(job_id)
    return rt.store.load_manifest(job_id, stage, attempt or job.attempts[stage])


async def _local(rt, photos, prompt="figurine of my dog and cat hugging", **kw):
    try:
        return await run_local(rt, photos, prompt, auto=True, **kw)
    finally:
        await rt.fake.drain()


def test_thin_slice_end_to_end(settings, photos):
    rt = make_rt(settings)
    job_id, status, _responder = run(_local(rt, photos, product_type="keychain"))
    assert status == JobStatus.DONE, rt.store.job_row(job_id)
    job = rt.store.load_job(job_id)
    for s in THIN:
        assert manifest(rt, job_id, s).status == StageStatus.SUCCEEDED
    for s in (StageId.S02_COLOR_SPEC, StageId.S08_PALETTE, StageId.S09_PAINT,
              StageId.S10_WRITE):
        assert manifest(rt, job_id, s).status == StageStatus.SKIPPED

    jdir = rt.store.job_dir(job_id)
    row = rt.store.job_row(job_id)
    result = JobResult.model_validate(row["result"])
    assert result.baseline_only is True
    assert (jdir / result.threemf).is_file() and result.threemf.endswith(".3mf")
    assert (jdir / result.preview_glb).read_bytes()[:4] == b"glTF"
    # every paid call is on the ledger and the job budget: figures 12 + 9, the loops drawn in
    # code (0; they pass the check, so no paid edit), views 9 (the approved nano-banana-pro,
    # billed once), 3D 35, Multi-Color 10
    ledger = [json.loads(line) for line in (jdir / "ledger.jsonl").read_text().splitlines()]
    assert sum(e["credits"] for e in ledger if e["vendor"] == "meshy") == 75 \
        == job.budget.spent_credits
    assert all(e["test_mode"] for e in ledger)
    # messages: questions, concepts, mesh_review, result from the worker
    kinds = [m.kind for m in rt.store.list_messages(job_id, role="assistant")]
    for k in ("questions", "concepts", "mesh_review", "result"):
        assert k in kinds
    assert not rt.store.list_messages(job_id, role="user", unconsumed=True,
                                      kinds={"answers", "concept_pick", "mesh_decision"})
    # webhooks drove everything: exactly one confirming GET per task, never a DELETE
    fake = rt.fake
    n_tasks = len(fake.tasks)
    assert n_tasks == 6  # 2 figures, views, 3D model, analyze (free), multi-color
    for tid, ep in fake.endpoint_of.items():
        assert fake.count("GET", ep, task_id=tid) == 1
    assert fake.deletes == 0
    assert fake.asset_requests_with_auth == 0
    # manifests record the Meshy tasks and the round-1 models
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    figures = [t for t in m3.meshy_tasks if t.purpose.startswith("concept r1")]
    assert sorted(t.request["ai_model"] for t in figures) == ["gpt-image-2", "nano-banana-pro"]
    assert all(t.request["remove_background"] for t in m3.meshy_tasks)
    assert all(str(u).startswith("data:image/jpeg;sha256=")
               for t in figures for u in t.request["reference_image_urls"])
    m5 = manifest(rt, job_id, StageId.S05_MESH)
    body = m5.meshy_tasks[0].request
    assert body["ai_model"] == "latest" and body["geometry_resolution"] == "2k"
    assert body["multi_view_thumbnails"] is True and len(body["texture_image_urls"]) == 4
    assert body["texture_image_urls"] == body["image_urls"] and "input_task_id" not in body
    assert len([o for o in m5.outputs if o.role == "approved_thumbnail"]) == 4
    m7 = manifest(rt, job_id, StageId.S07_BASELINE)
    assert [t.request["style"] for t in m7.meshy_tasks] == ["cartoon"]  # one style by default
    assert all(t.request["max_colors"] == 5 for t in m7.meshy_tasks)  # 5 AMS slots
    assert {o.role for o in m7.outputs} >= {"baseline_3mf", "baseline_3mf_meshy",
                                            "slice_report", "job_result"}
    assert any(d.key == "baseline.slicer_gate" and d.value == "pass" for d in m7.decisions)
    # the shipped file is the X2D project that passed the gate, never Meshy's own 3MF
    assert result.threemf == "s07_baseline/a1/baseline_x2d.3mf" and result.slice_ok is True
    assert result.print_time_s == 4321.0 and result.filaments
    assert all(f.filament_id for f in result.filaments)
    # no signed URL, token or data URI is persisted anywhere in the job folder
    for f in jdir.rglob("*.json*"):
        text = f.read_text()
        for bad in ("Signature=", "Expires=", "token=", ";base64,"):
            assert bad not in text, (f.name, bad)
    assert fake.unsigned_asset_requests == 0
    # recorded Meshy responses shaped the results (replayed fixtures)
    assert "preview_front.png" in " ".join(m5.meshy_tasks[0].result["thumbnail_urls"].values())


PARTS = {"keychain": {"keyring"}, "nameplate": {"plate", "text"}, "figurine": {"base"},
         "ornament": {"hanging_loop"}, "magnet": {"base"}}  # a 3D magnet stands on a base


@pytest.mark.parametrize("product,prompt", [
    ("keychain", "keychain of my dog and cat hugging"),
    ("nameplate", "nameplate for my dog and cat"),
    ("figurine", "figurine of my dog"),
    ("ornament", "ornament of my dog"),
    ("magnet", "3d magnet of my dog"),  # flat magnets: test_front_fidelity (Creative Lab)
])
def test_products_get_their_addons_prompts_and_geometry(settings, photos, product, prompt,
                                                        fake_slicer):
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos, prompt=prompt, product_type=product))
    assert status == JobStatus.DONE, rt.store.job_row(job_id)
    job = rt.store.load_job(job_id)
    kinds = {a.kind.value: a for a in job.addons}
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    prompts = {t.request["ai_model"]: t.request["prompt"] for t in m3.meshy_tasks
               if t.purpose.startswith("concept r1")}
    # the no-base constraint in each model's own form (Meshy has no negative-prompt field)
    assert prompts["gpt-image-2"].endswith(GPT_CONSTRAINT)
    assert prompts["nano-banana-pro"].startswith(NANO_OPENING)
    assert prompts["nano-banana-pro"].endswith(NANO_CLOSING)
    concept = prompts["gpt-image-2"]
    # every color region, markings included, and never the question text
    assert "black saddle on the back" in concept
    q = next(m for m in rt.store.list_messages(job_id) if m.kind == "questions")
    for question in q.payload["questions"]:
        assert question["text"] not in concept
    if product == "keychain":
        assert kinds["keyring_loop"].params == {"position": "top"}
        assert job.target_size_mm == 35 and "chunky and compact" in concept
        assert "key ring" not in concept.lower()
        assert "The subjects hugging each other." in concept  # the option's meaning
    if product == "nameplate":
        assert kinds["nameplate"].text.text == "Dog & Cat"
        assert kinds["nameplate"].params["shape"] == "rectangle"
        assert "flat, wide underside" in concept and "Dog & Cat" not in concept
    if product == "figurine":
        assert "round_base" in kinds and job.target_size_mm == 50
    row = rt.store.job_row(job_id)
    assert [a["kind"] for a in row["addons"]] == list(kinds)
    # s06 runs the real p2p_geometry.build_product on the fake server's figure
    m6 = manifest(rt, job_id, StageId.S06_PRINTABILITY)
    assert m6.data["geometry"] == "p2p_geometry", m6.decisions
    assert m6.data["printability"] == "warning"  # recorded Meshy analyze response
    geo_ref = next(o for o in m6.outputs if o.role == "geometry")
    geo = GeometryResult.model_validate_json(
        (rt.store.job_dir(job_id) / geo_ref.path).read_text())
    assert PARTS[product] <= {p.role for p in geo.parts}
    # s07 builds the X2D project from the geometry figure and gates it
    m7 = manifest(rt, job_id, StageId.S07_BASELINE)
    assert any(i.from_stage == StageId.S06_PRINTABILITY for i in m7.inputs)
    gate = m7.data["steps"]["gate"]["retarget"]
    assert gate["figure_source"] == "geometry"
    support = {"none": "0", "tree": "1", "normal": "1"}[geo.supports]
    assert gate["process_overrides"]["enable_support"] == support
    call = fake_slicer.calls[-1]
    assert call["path"].endswith("baseline_x2d.3mf")
    assert call["expected_slots"] == len(gate["slots_used"])
    assert call["overrides"]["enable_support"] == support
    assert job.result["threemf"].endswith("baseline_x2d.3mf") and job.result["slice_ok"]


def test_tweak_loop_routes_and_chains(settings, photos):
    rt = make_rt(settings)
    job_id, status, _responder = run(_local(
        rt, photos,
        tweaks=["make the dog's ears floppier and bigger", "hmm?",
                "add a keyring loop on the back"],
    ))
    assert status == JobStatus.DONE
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    routes = {d.key: d.value for d in m3.decisions if d.key.startswith("route.tweak")}
    assert sorted(routes.values()) == ["ask_followup", "geometry_op", "rerender_concept"]
    r2 = [t for t in m3.meshy_tasks if t.purpose.startswith("concept r2c1")]
    assert len(r2) == 1
    first_round = {t.task_id for t in m3.meshy_tasks if t.purpose.startswith("concept r1")}
    assert r2[0].request["input_task_id"] in first_round  # chained, not re-uploaded
    assert m3.data["tweaks_used"] == 1
    assert any(d.key.startswith("tweak.deferred") and d.value["route"] == "geometry_op"
               for d in m3.decisions)
    decisions = [m for m in rt.store.list_messages(job_id, role="assistant")
                 if m.kind == "tweak_decision"]
    assert len(decisions) == 3
    assert any(m.payload.get("followup_question") for m in decisions)


def test_mesh_retexture_uses_model_url(settings, photos):
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos, mesh_actions=["retexture"]))
    assert status == JobStatus.DONE
    m5 = manifest(rt, job_id, StageId.S05_MESH)
    retex = [t for t in m5.meshy_tasks if t.endpoint.endswith("retexture")]
    assert len(retex) == 1
    assert "model_url" in retex[0].request and "input_task_id" not in retex[0].request
    assert m5.data["endpoint"] == "retexture"
    m7 = manifest(rt, job_id, StageId.S07_BASELINE)
    assert m7.meshy_tasks[0].request["input_task_id"] == retex[0].task_id


def test_dev_profile_is_cheap(settings, photos):
    rt = make_rt(settings.with_(cost_profile="dev", baseline_styles=("cartoon",)))
    job_id, status, _ = run(_local(rt, photos))
    assert status == JobStatus.DONE
    job = rt.store.load_job(job_id)
    # nano-banana-2 figure, the figurine's round base drawn in code (0), no views (dev),
    # meshy-6-lite textured, Multi-Color
    assert job.budget.spent_credits == 6 + 15 + 10
    m5 = manifest(rt, job_id, StageId.S05_MESH)
    assert m5.meshy_tasks[0].request["ai_model"] == "meshy-6-lite"
    assert "geometry_resolution" not in m5.meshy_tasks[0].request


def test_budget_cap_pauses_before_spending_then_resumes(settings, photos):
    rt = make_rt(settings.with_(job_cap_credits=50))  # s03 33 + s04 9 fit; s05 35 does not
    job_id, status, _ = run(_local(rt, photos))
    assert status == JobStatus.PAUSED
    m5 = manifest(rt, job_id, StageId.S05_MESH)
    assert m5.status == StageStatus.PAUSED
    assert "Job cap 50 credits" in m5.pause_reason
    assert m5.error.code == "budget_exceeded"
    assert rt.fake.count("POST", "multi-image-to-3d") == 0  # refused before the call
    assert rt.store.job_row(job_id)["pause_reason"] == m5.pause_reason

    job = rt.store.load_job(job_id)
    job.budget.cap_credits = 400  # Jackson raises the cap
    rt.store.save_job(job)
    rt.orchestrator.resume_job(job_id)
    status = run(run_job(rt, job_id, AutoResponder()))
    assert status == JobStatus.DONE
    assert manifest(rt, job_id, StageId.S05_MESH).attempt == 1  # same attempt resumed


def test_resume_after_crash_mid_stage(settings, photos):
    store = LocalStore(settings.jobs_dir)
    fake = FakeMeshy(delay_s=0.005, fixtures_dir=None,
                     on_event=lambda ev: store.insert_meshy_event(ev))

    def crash(point: str) -> None:
        if point == "s05_mesh:meshy_created:m1.gen":
            raise SimulatedCrash(point)

    rt = make_rt(settings, store=store, fake=fake, on_checkpoint=crash)
    with pytest.raises(SimulatedCrash):
        run(run_local(rt, [p for p in photos], "figurine of my dog", auto=True))
    job_id = store.list_jobs()[0]
    m5 = rt.store.load_manifest(job_id, StageId.S05_MESH, 1)
    assert m5.status == StageStatus.RUNNING and len(m5.meshy_tasks) == 1
    # Meshy finishes while the worker is down; the webhook row lands in meshy_events.
    assert fake.complete_pending()

    store2 = LocalStore(settings.jobs_dir)  # a fresh process
    fake.on_event = store2.insert_meshy_event
    rt2 = make_rt(settings, store=store2, fake=fake)
    status = run(run_job(rt2, job_id, AutoResponder()))
    assert status == JobStatus.DONE
    assert fake.count("POST", "multi-image-to-3d") == 1  # no duplicate spend
    assert rt2.store.load_job(job_id).attempts[StageId.S05_MESH] == 1


def test_crash_between_create_and_record_adopts_task(settings, photos):
    class CrashOnce(LocalStore):
        armed = False

        def index_task(self, task_id, job_id):
            if self.armed:  # runs right after the POST, before the manifest records the task
                self.armed = False
                raise SimulatedCrash("after POST, before record")
            super().index_task(task_id, job_id)

    store = CrashOnce(settings.jobs_dir)
    fake = FakeMeshy(delay_s=0.005, fixtures_dir=None,
                     on_event=lambda ev: store.insert_meshy_event(ev))

    def arm(point: str) -> None:
        if point == "s05_mesh:meshy_pending:m1.gen":
            store.armed = True

    rt = make_rt(settings, store=store, fake=fake, on_checkpoint=arm)
    with pytest.raises(SimulatedCrash):
        run(run_local(rt, photos, "figurine of my dog", auto=True))
    job_id = store.list_jobs()[0]
    m5 = store.load_manifest(job_id, StageId.S05_MESH, 1)
    assert "m1.gen" in m5.data["meshy_pending"] and not m5.meshy_tasks
    assert fake.complete_pending()  # Meshy finishes the task while the worker is down

    store2 = LocalStore(settings.jobs_dir)  # a fresh process
    fake.on_event = store2.insert_meshy_event
    rt2 = make_rt(settings, store=store2, fake=fake)
    status = run(run_job(rt2, job_id, AutoResponder()))
    assert status == JobStatus.DONE
    assert fake.count("POST", "multi-image-to-3d") == 1  # adopted, not created twice
    m5 = rt2.store.load_manifest(job_id, StageId.S05_MESH, 1)
    # the credit ledger had the task id before the crash: adopted and booked once, no list call
    assert any(d.key == "meshy.adopted.m1.gen" for d in m5.decisions)  # gitleaks:allow (case id, not a secret)
    tid = m5.meshy_tasks[0].task_id
    assert any(d.key == f"meshy.booked_late.{tid}" for d in m5.decisions)


def test_changed_photo_marks_stages_stale(settings, photos):
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos))
    assert status == JobStatus.DONE
    photo = rt.store.job_dir(job_id) / "input" / "photo_1.jpg"
    photo.write_bytes(photo.read_bytes() + b"\x00changed")

    status = run(run_job(rt, job_id, None))
    assert status == JobStatus.WAITING_USER
    old = rt.store.load_manifest(job_id, StageId.S01_INTAKE, 1)
    assert old.status == StageStatus.STALE
    assert "input changed: input/photo_1.jpg" in old.data["stale_reason"]
    assert rt.store.load_job(job_id).attempts[StageId.S01_INTAKE] == 2

    status = run(run_job(rt, job_id, AutoResponder()))
    assert status == JobStatus.DONE
    job = rt.store.load_job(job_id)
    for s in THIN:
        assert job.attempts[s] == 2
        assert rt.store.load_manifest(job_id, s, 1).status == StageStatus.STALE
    m3 = rt.store.load_manifest(job_id, StageId.S03_CONCEPT, 1)
    assert "moved to attempt 2" in m3.data["stale_reason"]


def test_failed_meshy_task_retries_within_and_across_attempts(settings, photos):
    rt = make_rt(settings.with_(baseline_styles=("cartoon", "realistic")))
    rt.fake.fail_next["image-to-image"] = 1  # one concept fails: retried inside the stage
    rt.fake.fail_next["print-multi-color"] = 2  # both styles fail once: stage retries
    job_id, status, _ = run(_local(rt, photos))
    assert status == JobStatus.DONE
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    assert m3.attempt == 1 and any(d.key.startswith("concept.retry") for d in m3.decisions)
    assert rt.store.load_manifest(job_id, StageId.S07_BASELINE, 1).status == StageStatus.FAILED
    assert manifest(rt, job_id, StageId.S07_BASELINE).attempt == 2
    ledger = (rt.store.job_dir(job_id) / "ledger.jsonl").read_text()
    assert '"kind": "refund:print-multi-color"' in ledger
    job = rt.store.load_job(job_id)
    # refunds cancel the failed tasks: 21 concepts (bases in code) + 9 views + 35 + 10 + 10
    # (two styles); the retry attempt of s07 takes nothing twice
    assert job.budget.spent_credits == 85


def test_retry_limit_pauses(settings, photos):
    rt = make_rt(settings.with_(baseline_styles=("cartoon",)))
    rt.fake.fail_next["print-multi-color"] = 10
    job_id, status, _ = run(_local(rt, photos))
    assert status == JobStatus.PAUSED
    job = rt.store.load_job(job_id)
    assert job.attempts[StageId.S07_BASELINE] == 3  # first try plus 2 retries
    assert "failed 3 times" in rt.store.job_row(job_id)["pause_reason"]


def test_waits_on_webhooks_without_polling(settings, photos):
    s = settings.with_(vendor_wait_s=0.2)
    store = LocalStore(s.jobs_dir)
    fake = FakeMeshy(auto_complete=False, fixtures_dir=None,
                     on_event=lambda ev: store.insert_meshy_event(ev))
    rt = make_rt(s, store=store, fake=fake)
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    responder = AutoResponder()
    status = run(run_job(rt, job.id, responder, max_steps=3))
    assert status == JobStatus.RUNNING
    m3 = rt.store.load_manifest(job.id, StageId.S03_CONCEPT, 1)
    assert m3.status == StageStatus.WAITING_VENDOR
    assert fake.count("GET", "image-to-image") == 0  # no polling while waiting

    # A forged "SUCCEEDED" event is checked with one GET and ignored.
    tid = m3.meshy_tasks[0].task_id
    store.insert_meshy_event({"id": tid, "status": "SUCCEEDED", "image_urls": ["https://x"]})
    status = run(rt.orchestrator.advance(job.id))
    assert status == JobStatus.RUNNING
    assert fake.count("GET", "image-to-image", task_id=tid) == 1
    m3 = rt.store.load_manifest(job.id, StageId.S03_CONCEPT, 1)
    assert any(d.key.startswith("meshy.event_mismatch") for d in m3.decisions)

    # Resume after a long gap: exactly one status GET per in-flight task.
    rt2 = make_rt(s.with_(missed_webhook_s=0.0), store=store, fake=fake)
    before = fake.count("GET", "image-to-image")
    status = run(rt2.orchestrator.advance(job.id))
    assert status == JobStatus.RUNNING
    assert fake.count("GET", "image-to-image") - before == 2

    for t in list(fake.tasks):
        fake.complete(t)
    fake.auto_complete = True
    status = run(run_job(rt, job.id, responder, max_steps=50))
    assert status == JobStatus.DONE


def test_invalid_reply_is_rejected_with_a_note(settings, photos):
    rt = make_rt(settings)
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    assert run(rt.orchestrator.advance(job.id)) == JobStatus.WAITING_USER
    # a broken payload written straight into the local inbox, as a bad client might
    from p2p_worker.util import append_jsonl, utcnow

    append_jsonl(rt.store.job_dir(job.id) / "messages.jsonl", {
        "id": "bad-1", "job_id": job.id, "created_at": utcnow().isoformat(), "role": "user",
        "kind": "answers", "payload": {"nope": 1}, "stage": None, "body": None,
        "reply_to": None, "consumed_at": None,
    })
    assert run(rt.orchestrator.advance(job.id)) == JobStatus.WAITING_USER
    notes = [m for m in rt.store.list_messages(job.id, role="assistant") if m.kind == "note"]
    assert notes and "Could not read" in notes[-1].payload["text"]
    run(rt.fake.drain())


def test_name_given_in_the_request_is_not_asked_again(settings, photos):
    rt = make_rt(settings.with_(cost_profile="dev"))
    job_id, status, _ = run(_local(rt, photos[:1], prompt="nameplate for my dog named Biscuit",
                                   product_type="nameplate"))
    assert status == JobStatus.DONE
    job = rt.store.load_job(job_id)
    plate = next(a for a in job.addons if a.kind.value == "nameplate")
    assert plate.text.text == "Biscuit"
    q = next(m for m in rt.store.list_messages(job_id) if m.kind == "questions")
    asked = [x["id"] for x in q.payload["questions"]]
    assert "name" not in asked and 2 <= len(asked) <= 4


def test_resume_message_applies_new_caps(settings, photos):
    rt = make_rt(settings.with_(job_cap_credits=50))  # s03 33 + s04 9 fit; s05 35 does not
    job_id, status, _ = run(_local(rt, photos))
    assert status == JobStatus.PAUSED
    rt.store.post_message(job_id, "user", "resume", {"cap_credits": 200, "note": "go on"})
    status = run(run_job(rt, job_id, AutoResponder()))
    assert status == JobStatus.DONE
    job = rt.store.load_job(job_id)
    assert job.budget.cap_credits == 200 and job.pause_reason is None
    assert rt.store.job_row(job_id)["budget_credits"] == 200
    assert not rt.store.list_messages(job_id, kinds={"resume"}, unconsumed=True)
    notes = [m.payload["text"] for m in rt.store.list_messages(job_id) if m.kind == "note"]
    assert any(n.startswith("Resumed. Caps") for n in notes)
    assert job.result and job.result["threemf"].endswith(".3mf")


def test_tweaks_exhausted_after_five_rounds(settings, photos):
    rt = make_rt(settings.with_(cost_profile="dev"))
    tweaks = [f"make the dog look happier, version {i}" for i in range(6)]
    job_id, status, _ = run(_local(rt, photos[:1], tweaks=tweaks))
    assert status == JobStatus.DONE
    concepts = [m for m in rt.store.list_messages(job_id) if m.kind == "concepts"]
    assert concepts[-1].payload["tweaks_exhausted"] is True
    assert concepts[-1].payload["rounds_left"] == 0
    assert not concepts[0].payload["tweaks_exhausted"]
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    assert m3.data["tweaks_used"] == 5 and m3.data["rounds"] == 6
    notes = [m.payload["text"] for m in rt.store.list_messages(job_id) if m.kind == "note"]
    assert any("All 5 tweak rounds used" in n for n in notes)
