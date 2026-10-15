"""Credit guard: no Meshy credit is spent twice for the same thing, and the cheapest path that
passes the checks is tried first. Every test counts paid calls (POSTs) on the fake Meshy
server; MockClaude plays the checks. Fake Meshy and MockClaude only, 0 credits."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from conftest import SimulatedCrash, make_rt, run
from p2p_contracts import JobRecord, JobStatus, SpendEvent, StageId, StageStatus
from p2p_contracts.payloads import ConceptCandidate
from p2p_worker.budget import BudgetGuard
from p2p_worker.clients.claude import MockClaude
from p2p_worker.credit_ledger import CreditLedger, input_hash
from p2p_worker.errors import BudgetExceeded, VendorError
from p2p_worker.fakes.meshy import FakeMeshy
from p2p_worker.intent import color_only_fix
from p2p_worker.local import AutoResponder, run_job, run_local
from p2p_worker.stages import s05_mesh
from p2p_worker.stages.base import StageContext
from p2p_worker.store import LocalStore
from p2p_worker.util import read_jsonl, utcnow

PAID = ("image-to-image", "multi-image-to-3d", "image-to-3d", "retexture", "print-multi-color",
        "keychain-prototype", "keychain-build")


def posts(fake: FakeMeshy) -> dict[str, int]:
    return {ep: fake.count("POST", ep) for ep in PAID if fake.count("POST", ep)}


def manifest(rt, job_id, stage, attempt=None):
    job = rt.store.load_job(job_id)
    return rt.store.load_manifest(job_id, stage, attempt or job.attempts[stage])


async def _local(rt, photos, prompt="figurine of my dog", **kw):
    try:
        return await run_local(rt, photos, prompt, auto=True, **kw)
    finally:
        await rt.fake.drain()


async def _advance(rt, job_id, responder=None):
    try:
        return await run_job(rt, job_id, responder or AutoResponder())
    finally:
        await rt.fake.drain()


def spend_rows(rt, job_id) -> list[dict]:
    return read_jsonl(rt.store.job_dir(job_id) / "ledger.jsonl")


def report(rt, job_id) -> dict:
    return json.loads((rt.store.job_dir(job_id) / "credits.json").read_text())


# ---------------------------------------------------------------- 1. ledger and idempotency
def test_every_paid_call_is_an_intent_first_then_a_task_id(settings, photos):
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos[:1]))
    assert status == JobStatus.DONE
    events = read_jsonl(rt.store.job_dir(job_id) / "paid_calls.jsonl")
    intents = [e for e in events if e["event"] == "intent"]
    created = {e["id"]: e for e in events if e["event"] == "created"}
    # figurine: 2 figures, views, 3D, analyze (free), Multi-Color; the base drawn in code
    assert len(intents) == len(created) == len(rt.fake.tasks) == 6
    for i in intents:
        c = created[i["id"]]
        assert events.index(i) < events.index(c)  # the intent is on disk before the POST
        assert {"stage", "attempt", "purpose", "hash", "expected", "why"} <= set(i)
        assert c["task"]["task_id"] in rt.fake.tasks
    # the spend table has the intent too: 0 credits, ref = hash|attempt|key|expected
    rows = spend_rows(rt, job_id)
    intent_rows = [r for r in rows if r["kind"].startswith("intent:")]
    assert len(intent_rows) == 6 and all(r["credits"] == 0 for r in intent_rows)
    assert {r["ref"].split("|")[0] for r in intent_rows} == {i["hash"][:16] for i in intents}
    # the report adds up to the spend rows and the job budget
    rep = report(rt, job_id)
    meshy = sum(r["credits"] for r in rows if r["vendor"] == "meshy")
    job = rt.store.load_job(job_id)
    assert rep["credits"]["paid"] == rep["credits"]["spend_table"] == meshy \
        == job.budget.spent_credits == 12 + 9 + 9 + 35 + 10
    assert {c["how"] for c in rep["calls"]} == {"new"} and all(c["why"] for c in rep["calls"])
    # the numbers reach the result: the message body, s07's copy of the report, manifest data
    result = next(m for m in rt.store.list_messages(job_id, role="assistant")
                  if m.kind == "result")
    assert result.body == "Meshy credits: 75 paid"
    m7 = manifest(rt, job_id, StageId.S07_BASELINE)
    assert any(o.role == "credit_report" for o in m7.outputs)
    assert m7.data["credits"]["paid"] == 75


def test_a_failed_attempt_after_paying_is_not_paid_again(settings, photos, monkeypatch):
    """Any failure after a create (not only a network blip): the next attempt takes the task
    the failed attempt paid for. live2 Bronco would have bought a second 3D model."""
    real = s05_mesh.MeshStage._fetch
    broken = []

    async def flaky(self, ctx, tag, key, result):
        if not broken:
            broken.append(key)
            raise VendorError("the GLB could not be read", retryable=True)
        return await real(self, ctx, tag, key, result)

    monkeypatch.setattr(s05_mesh.MeshStage, "_fetch", flaky)
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos[:1]))
    assert status == JobStatus.DONE and broken
    assert rt.fake.count("POST", "multi-image-to-3d") == 1  # paid once
    a1 = rt.store.load_manifest(job_id, StageId.S05_MESH, 1)
    a2 = rt.store.load_manifest(job_id, StageId.S05_MESH, 2)
    assert a1.status == StageStatus.FAILED and a2.status == StageStatus.SUCCEEDED
    tid = a1.meshy_tasks[0].task_id
    assert a2.meshy_tasks[0].task_id == tid and a2.meshy_tasks[0].credits == 0
    assert a2.data["meshy_reused"][tid]["how"] == "reused" and a2.credits == 0
    assert any(d.key == "meshy.reused.m1.gen" for d in a2.decisions)
    assert sum(1 for r in spend_rows(rt, job_id) if r.get("ref") == tid) == 1  # booked once
    calls = report(rt, job_id)["calls"]
    reuse = next(c for c in calls if c["how"] == "reused")
    assert reuse["stage"] == "s05_mesh" and reuse["attempt"] == 2 and reuse["credits"] == 0
    assert reuse["saved"] == 35 and reuse["from"] == "s05_mesh/a1 m1.gen"


def test_rerunning_a_stage_with_the_same_inputs_spends_nothing(settings, photos):
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos[:1]))
    assert status == JobStatus.DONE
    before, spent = posts(rt.fake), rt.store.load_job(job_id).budget.spent_credits
    rt.orchestrator.rerun_stage(job_id, StageId.S04_MULTIVIEW)  # s04 to s07 run again
    assert run(_advance(rt, job_id)) == JobStatus.DONE
    job = rt.store.load_job(job_id)
    assert job.attempts[StageId.S04_MULTIVIEW] == job.attempts[StageId.S05_MESH] == 2
    assert posts(rt.fake) == before  # no new paid call
    assert job.budget.spent_credits == spent == 75
    rep = report(rt, job_id)["credits"]
    assert rep["paid"] == 75 and rep["reused_calls"] == 4 and rep["saved"] == 9 + 35 + 0 + 10
    # the reused 3D model came from disk: no download and no extra GET for its assets
    m5 = manifest(rt, job_id, StageId.S05_MESH)
    assert all(o.path.startswith("s05_mesh/a2/") for o in m5.outputs)


def test_a_crash_after_the_intent_and_before_the_post_creates_once(settings, photos):
    store = LocalStore(settings.jobs_dir)
    fake = FakeMeshy(delay_s=0.005, fixtures_dir=None,
                     on_event=lambda ev: store.insert_meshy_event(ev))

    def crash(point: str) -> None:
        if point == "s05_mesh:meshy_pending:m1.gen":  # the intent is written, no POST yet
            raise SimulatedCrash(point)

    rt = make_rt(settings, store=store, fake=fake, on_checkpoint=crash)
    with pytest.raises(SimulatedCrash):
        run(run_local(rt, photos[:1], "figurine of my dog", auto=True))
    job_id = store.list_jobs()[0]
    assert fake.count("POST", "multi-image-to-3d") == 0
    store2 = LocalStore(settings.jobs_dir)  # a fresh process
    fake.on_event = store2.insert_meshy_event
    rt2 = make_rt(settings, store=store2, fake=fake)
    assert run(_advance(rt2, job_id)) == JobStatus.DONE
    assert fake.count("POST", "multi-image-to-3d") == 1
    events = read_jsonl(store2.job_dir(job_id) / "paid_calls.jsonl")
    lost = [e for e in events if e["event"] == "abandoned"]
    assert len(lost) == 1 and "restart" in lost[0]["why"]  # looked for it once, then made it
    m5 = store2.load_manifest(job_id, StageId.S05_MESH, 1)
    assert any(d.key == "meshy.recover.m1.gen" for d in m5.decisions)


def test_a_create_whose_answer_was_lost_is_looked_up_not_bought_again(settings, photos):
    """A read timeout after the POST: Meshy made (and charged) the task but the worker never
    saw its id. The intent stays open and the next attempt finds the task with one list
    call."""
    fake = FakeMeshy(delay_s=0.005)
    real = fake.handle
    lost: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        resp = await real(request)
        if request.method == "POST" and request.url.path.endswith("multi-image-to-3d") \
                and not lost:
            lost.append(json.loads(resp.content)["result"])
            raise httpx.ReadTimeout("read timed out", request=request)
        return resp

    fake.handle = handle
    rt = make_rt(settings, fake=fake)
    job_id, status, _ = run(_local(rt, photos[:1]))
    assert status == JobStatus.DONE and lost
    assert fake.count("POST", "multi-image-to-3d") == 1  # bought once
    a1 = rt.store.load_manifest(job_id, StageId.S05_MESH, 1)
    assert any(d.key == "meshy.create_unknown.m1.gen" for d in a1.decisions)
    a2 = rt.store.load_manifest(job_id, StageId.S05_MESH, 2)
    assert [t.task_id for t in a2.meshy_tasks] == lost
    assert any(d.key == "meshy.recover.m1.gen" and d.value == lost for d in a2.decisions)
    assert sum(1 for r in spend_rows(rt, job_id) if r.get("ref") == lost[0]) == 1
    assert rt.store.load_job(job_id).budget.spent_credits == 75


def test_a_task_whose_spend_row_failed_is_booked_once_by_the_next_attempt(settings, photos):
    rt = make_rt(settings)
    real = rt.store.record_spend
    failed: list[str] = []

    def record_spend(ev, **kw):
        if ev.kind == "multi-image-to-3d" and not failed:
            failed.append(ev.ref)
            raise RuntimeError("spend table write failed")
        return real(ev, **kw)

    rt.store.record_spend = record_spend
    job_id, status, _ = run(_local(rt, photos[:1]))
    assert status == JobStatus.DONE and failed
    assert rt.fake.count("POST", "multi-image-to-3d") == 1  # adopted, not bought again
    rows = [r for r in spend_rows(rt, job_id) if r.get("ref") == failed[0]]
    assert [r["kind"] for r in rows] == ["multi-image-to-3d"] and rows[0]["credits"] == 35
    a2 = rt.store.load_manifest(job_id, StageId.S05_MESH, 2)
    assert any(d.key == f"meshy.booked_late.{failed[0]}" for d in a2.decisions)
    assert rt.store.load_job(job_id).budget.spent_credits == 75 \
        == report(rt, job_id)["credits"]["paid"]


def test_a_regenerate_skips_a_variant_an_earlier_attempt_bought(settings, photos):
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos[:1], mesh_actions=["regenerate"]))
    assert status == JobStatus.DONE
    rt.orchestrator.rerun_stage(job_id, StageId.S05_MESH)
    assert run(_advance(rt, job_id, AutoResponder(mesh_actions=["regenerate"]))) == \
        JobStatus.DONE
    # m1 is reused; the new regenerate is a variant the job has not seen (Meshy has no seed)
    assert rt.fake.count("POST", "multi-image-to-3d") == 3
    a2 = manifest(rt, job_id, StageId.S05_MESH, 2)
    assert a2.data["steps"]["m2.regen_variant"] == 2
    bodies = [t.request for a in (1, 2)
              for t in manifest(rt, job_id, StageId.S05_MESH, a).meshy_tasks]
    assert bodies[1]["image_enhancement"] is False  # a1 m2: variant 1
    assert len(bodies[3]["image_urls"]) == len(bodies[0]["image_urls"]) - 1  # a2 m2: variant 2


# ---------------------------------------------------------------- 2. input-hash cache
def _ctx(rt, job, stage=StageId.S03_CONCEPT):
    m = rt.orchestrator._new_attempt(job, stage)
    return StageContext(rt.orchestrator.svc, job, stage, m.attempt, m, utcnow())


def test_same_inputs_reuse_and_a_fresh_variation_must_change_one(settings, photos):
    rt = make_rt(settings)
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    ctx = _ctx(rt, job)
    body = {"ai_model": "nano-banana", "prompt": "a dog",
            "reference_image_urls": ["data:image/png;base64,AAAA"]}

    async def go():
        a = await ctx.meshy_create("a", "image-to-image", body, "first")
        b = await ctx.meshy_create("b", "image-to-image", dict(body), "again")
        assert b.task_id == a.task_id  # same inputs: the task already paid for
        with pytest.raises(VendorError, match="fresh variation must change an input"):
            await ctx.meshy_create("c", "image-to-image", dict(body), "new sample", fresh=True)
        d = await ctx.meshy_create("d", "image-to-image", {**body, "prompt": "a dog, v2"},
                                   "changed", fresh=True)
        assert d.task_id != a.task_id
        await rt.fake.drain()

    run(go())
    assert rt.fake.count("POST", "image-to-image") == 2
    assert rt.store.load_job(job.id).budget.spent_credits == 6  # nano-banana 3, twice


def test_a_refunded_failure_is_never_reused(settings, photos):
    rt = make_rt(settings)
    rt.fake.fail_next["image-to-image"] = 1
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    ctx = _ctx(rt, job)
    body = {"ai_model": "nano-banana", "prompt": "a dog",
            "reference_image_urls": ["data:image/png;base64,AAAA"]}

    async def go():
        with pytest.raises(VendorError):
            await ctx.meshy_run("a", "image-to-image", body, "first")
        await ctx.meshy_run("a.retry", "image-to-image", body, "retry", allow_same=True)
        await rt.fake.drain()

    run(go())
    assert rt.fake.count("POST", "image-to-image") == 2
    assert rt.store.load_job(job.id).budget.spent_credits == 3  # the failure was refunded
    rep = report(rt, job.id)["credits"]
    assert rep["paid"] == 3 and rep["refunded"] == 3


def test_the_hash_is_the_content_not_the_job_or_the_signature(tmp_path):
    jobs = tmp_path / "jobs"
    for j in ("j1", "j2"):
        (jobs / j / "input").mkdir(parents=True)
        (jobs / j / "input" / "photo_1.jpg").write_bytes(b"same photo")
    a = {"prompt": "x", "image_url": "https://p.supabase.co/storage/v1/object/sign/inputs/j1/"
         "input/photo_1.jpg?token=abc"}
    b = {"prompt": "x", "image_url": "https://p.supabase.co/storage/v1/object/sign/inputs/j2/"
         "input/photo_1.jpg?token=xyz"}
    assert input_hash("image-to-image", a, jobs) == input_hash("image-to-image", b, jobs)
    (jobs / "j2" / "input" / "photo_1.jpg").write_bytes(b"another photo")
    assert input_hash("image-to-image", a, jobs) != input_hash("image-to-image", b, jobs)
    assert input_hash("image-to-image", a, jobs) != input_hash("image-to-3d", a, jobs)


def test_identical_jobs_reuse_each_others_outputs_at_zero_credits(settings, photos):
    rt = make_rt(settings.with_(meshy_reuse_across_jobs=True))
    first, status, _ = run(_local(rt, photos[:1]))
    assert status == JobStatus.DONE
    before = posts(rt.fake)
    second, status, _ = run(_local(rt, photos[:1]))
    assert status == JobStatus.DONE and second != first
    assert posts(rt.fake) == before  # the second job bought nothing
    job2 = rt.store.load_job(second)
    assert job2.budget.spent_credits == 0
    rep = report(rt, second)
    assert rep["credits"]["paid"] == 0 and rep["credits"]["saved"] == 75
    assert all(c["how"] == "reused" and c["from"].startswith(f"job {first[:8]}")
               for c in rep["calls"])
    # the outputs are the first job's files, copied (no Meshy asset request)
    j1, j2 = rt.store.job_dir(first), rt.store.job_dir(second)
    assert (j1 / "s05_mesh/a1/mesh_m1.glb").read_bytes() == \
        (j2 / "s05_mesh/a1/mesh_m1.glb").read_bytes()


def test_cross_job_reuse_is_off_for_the_fake_by_default(settings, photos):
    rt = make_rt(settings)
    run(_local(rt, photos[:1]))
    before = posts(rt.fake)
    run(_local(rt, photos[:1]))
    assert sum(posts(rt.fake).values()) == 2 * sum(before.values())


# ---------------------------------------------------------------- 3. cheapest first
def test_addons_are_drawn_in_code_first_and_no_edit_is_bought(settings, photos):
    """live2 Bronco: 60 credits of plate edits, and the approved plate was the code
    composite. Now the composite goes first and passes."""
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos[:1], "nameplate for my dog named Bronco",
                                   product_type="nameplate"))
    assert status == JobStatus.DONE
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    assert not [t for t in m3.meshy_tasks if t.purpose.startswith("product")]
    assert rt.fake.count("POST", "image-to-image") == 2 + 1  # two figures, the views
    c = json.loads((rt.store.job_dir(job_id) / "s03_concept/a1/concept.json").read_text())
    approved = ConceptCandidate.model_validate(c["approved"])
    assert approved.product_source == "code" and approved.check.passed
    assert approved.check.text_read == "Bronco"
    assert all(d.value == "code" for d in m3.decisions if d.key.startswith("concept.cheapest"))


def test_a_failed_composite_buys_one_edit_then_switches_model_within_the_cap(settings,
                                                                            photos):
    claude = MockClaude()
    # round 1: composite fails the plate, the first edit fails, the second model passes;
    # round 2 (a tweak): its composite fails, and the stage has used its 2 paid edits
    claude.check_script = [{"part"}, {"part"}, set(), {"part"}]
    rt = make_rt(settings.with_(cost_profile="dev"), claude=claude)
    job_id, status, _ = run(_local(rt, photos[:1], "nameplate for my dog named Bronco",
                                   product_type="nameplate", tweaks=["make the ears floppier"]))
    assert status == JobStatus.DONE
    m3 = manifest(rt, job_id, StageId.S03_CONCEPT)
    edits = [t for t in m3.meshy_tasks if t.purpose.startswith("product")]
    assert [t.request["ai_model"] for t in edits] == ["nano-banana-2", "nano-banana-pro"]
    fixes = {d.key: d.value for d in m3.decisions if d.key.startswith("concept.fix.")}
    assert fixes["concept.fix.r1c1.1"]["action"] == "edit"
    assert fixes["concept.fix.r1c1.1"]["reason"] == "the code composite failed the check"
    assert fixes["concept.fix.r1c1.2"]["reason"] == "nano-banana-2 failed; switch model"
    assert fixes["concept.fix.r2c1.1"]["action"] == "stop"
    assert fixes["concept.fix.r2c1.1"]["reason"] == "paid edit cap reached (2 per stage)"
    c = json.loads((rt.store.job_dir(job_id) / "s03_concept/a1/concept.json").read_text())
    shown = {x["id"]: x for x in c["candidates"]}
    assert shown["r1c1"]["product_source"] == "edit" and shown["r2c1"]["product_source"] == "code"
    assert CreditLedger(rt.store, job_id).paid_edits("s03_concept", "product ") == 2


# ---------------------------------------------------------------- 4. pre-flight
def test_low_meshy_balance_pauses_before_the_post_then_resumes(settings, photos):
    rt = make_rt(settings)
    rt.fake.balance = 50  # s03 21 and s04 9 fit; the 3D model (35) does not
    job_id, status, _ = run(_local(rt, photos[:1]))
    assert status == JobStatus.PAUSED
    m5 = manifest(rt, job_id, StageId.S05_MESH)
    assert m5.status == StageStatus.PAUSED and m5.error.code == "budget_exceeded"
    assert m5.pause_reason == "Meshy balance 20 credits: 3D model round 1 (generate) needs 35"
    assert rt.fake.count("POST", "multi-image-to-3d") == 0
    refused = [c for c in report(rt, job_id)["calls"] if c["how"] == "refused"]
    assert len(refused) == 1 and refused[0]["expected"] == 35
    rt.fake.balance = 500  # credits bought
    rt.orchestrator.resume_job(job_id)
    assert run(_advance(rt, job_id)) == JobStatus.DONE
    assert manifest(rt, job_id, StageId.S05_MESH).attempt == 1
    assert rt.fake.count("POST", "multi-image-to-3d") == 1


def test_a_402_from_meshy_pauses_instead_of_failing(settings, photos):
    rt = make_rt(settings.with_(meshy_balance_check=False))
    rt.fake.balance = 50
    job_id, status, _ = run(_local(rt, photos[:1]))
    assert status == JobStatus.PAUSED
    m5 = manifest(rt, job_id, StageId.S05_MESH)
    assert m5.error.code == "budget_exceeded" and "not enough credits" in m5.pause_reason
    events = read_jsonl(rt.store.job_dir(job_id) / "paid_calls.jsonl")
    assert [e for e in events if e["event"] == "abandoned"]
    assert rt.store.load_job(job_id).budget.spent_credits == 30  # nothing booked for s05


def _job(job_id="j1") -> JobRecord:
    return JobRecord(id=job_id, created_at=datetime.now(UTC), prompt="x")


def test_job_cap_counts_what_the_spend_rows_say(settings):
    store = LocalStore(settings.jobs_dir)
    guard = BudgetGuard(store, settings)
    job = _job()
    store.save_job(job)
    for credits in (300, 90):  # rows the job record missed (a save that did not land)
        store.record_spend(SpendEvent(at=datetime.now(UTC), job_id="j1", vendor="meshy",
                                      kind="image-to-image", credits=credits,
                                      usd=credits * 0.02, test_mode=True))
    assert job.budget.spent_credits == 0
    assert guard.sync_job(job) == {"spent_credits": 390, "spent_usd": 7.8}
    assert store.load_job("j1").budget.spent_credits == 390
    with pytest.raises(BudgetExceeded, match="Job cap 400 credits reached: spent 390"):
        guard.check(job, vendor="meshy", credits=35, usd=0.7, live=False, what="3D model")
    assert guard.sync_job(job) is None  # agrees now


def test_build_cap_takes_the_larger_of_the_spend_sources(settings):
    """live2: the spend table read 491 live credits, this machine's spend.jsonl 463; the 28
    missing were the fixture captures in data/ledger.jsonl (SOURCE: both read after the run)."""
    store = LocalStore(settings.jobs_dir)
    guard = BudgetGuard(store, settings.with_(meshy_build_credit_cap=500))
    store.record_spend(SpendEvent(at=datetime.now(UTC), vendor="meshy", kind="x", credits=463,
                                  usd=9.26, test_mode=False))
    (settings.repo_root / "data").mkdir(exist_ok=True)
    (settings.repo_root / "data" / "ledger.jsonl").write_text("".join(
        json.dumps({"vendor": "meshy", "kind": k, "credits": c, "usd": c * 0.02,
                    "test_mode": False}) + "\n"
        for k, c in (("image-to-image", 3), ("image-to-3d", 15), ("print/multi-color", 10))))
    assert guard.build_used() == {"usd": pytest.approx(9.82), "meshy_credits": 491}
    guard.check(_job(), vendor="meshy", credits=9, usd=0.18, live=True, what="fits")
    with pytest.raises(BudgetExceeded, match="Meshy build cap 500 credits reached: 491 used"):
        guard.check(_job(), vendor="meshy", credits=10, usd=0.2, live=True, what="Multi-Color")


def test_spend_table_is_read_in_pages_and_an_unreadable_one_stops_the_call(settings,
                                                                          monkeypatch):
    from p2p_worker.clients import supabase as sb

    monkeypatch.setattr(sb, "GET_BACKOFF_S", (0.0, 0.0))
    monkeypatch.setattr(sb, "SPEND_PAGE", 3)
    rows = [{"id": i, "vendor": "meshy", "usd": 0.2, "credits": 10} for i in range(7)]
    state = {"fail": False, "pages": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(503, text="unavailable")
        off, lim = int(req.url.params["offset"]), int(req.url.params["limit"])
        state["pages"] += 1
        return httpx.Response(200, json=rows[off:off + lim])

    s = settings.with_(supabase_url="https://proj.supabase.co", supabase_service_key="k")
    store = sb.SupabaseStore(s, transport=httpx.MockTransport(handler))
    assert store.build_totals() == {"usd": 1.4, "meshy_credits": 70}
    assert state["pages"] == 3  # 3 + 3 + 1
    state["fail"] = True
    guard = BudgetGuard(store, s)
    with pytest.raises(BudgetExceeded, match="Spend table unreadable"):
        guard.check(_job(), vendor="meshy", credits=10, usd=0.2, live=True, what="concept")


# ---------------------------------------------------------------- 5. color-only fixes
@pytest.mark.parametrize("text,color", [
    ("make the saddle darker", True), ("the ears should be black", True),
    ("make the tail tip white", True), ("more brown on the back", True),
    ("make the ears bigger", False), ("she should be sitting", False),
    ("add a red collar", False), ("the back looks wrong", False), (None, False),
])
def test_color_only_fix(text, color):
    assert color_only_fix(text) is color


def test_a_color_only_regenerate_is_done_as_a_retexture(settings, photos):
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos[:1],
                                   mesh_actions=["regenerate:make the saddle darker"]))
    assert status == JobStatus.DONE
    assert rt.fake.count("POST", "multi-image-to-3d") == 1  # no second model (35)
    assert rt.fake.count("POST", "retexture") == 1  # 10 instead
    m5 = manifest(rt, job_id, StageId.S05_MESH)
    assert any(d.key == "mesh.cheaper.m1" and d.value == "retexture" for d in m5.decisions)
    retex = next(t for t in m5.meshy_tasks if t.endpoint.endswith("retexture"))
    assert retex.request["text_style_prompt"] == "make the saddle darker"
    notes = [m.payload["text"] for m in rt.store.list_messages(job_id, role="assistant")
             if m.kind == "note"]
    assert notes.count("Color change: retexture (10 credits), not a new model (35)") == 1
    assert rt.store.load_job(job_id).budget.spent_credits == 75 + 10


def test_a_shape_change_still_regenerates(settings, photos):
    rt = make_rt(settings)
    _, status, _ = run(_local(rt, photos[:1], mesh_actions=["regenerate:bigger ears"]))
    assert status == JobStatus.DONE
    assert rt.fake.count("POST", "multi-image-to-3d") == 2
    assert rt.fake.count("POST", "retexture") == 0


def test_the_same_retexture_on_the_same_model_changes_an_input(settings, photos):
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos[:1],
                                   mesh_actions=["retexture:warmer brown coat"]))
    assert status == JobStatus.DONE
    rt.orchestrator.rerun_stage(job_id, StageId.S05_MESH)  # the same model comes back, reused
    responder = AutoResponder(mesh_actions=["retexture:warmer brown coat"])
    assert run(_advance(rt, job_id, responder)) == JobStatus.DONE
    assert rt.fake.count("POST", "multi-image-to-3d") == 1
    texts = [t.request.get("text_style_prompt") for a in (1, 2)
             for t in manifest(rt, job_id, StageId.S05_MESH, a).meshy_tasks
             if t.endpoint.endswith("retexture")]
    # Meshy has no seed: the second ask for the same retexture changes its text
    assert texts == ["warmer brown coat", "warmer brown coat Variation 2."]
    assert rt.fake.count("POST", "retexture") == 2


# ---------------------------------------------------------------- 6. the report
def test_credit_report_lists_every_call_why_and_how(settings, photos):
    rt = make_rt(settings)
    job_id, _, _ = run(_local(rt, photos[:1]))
    rep = report(rt, job_id)
    rows = {(c["stage"], c["key"]): c for c in rep["calls"]}
    assert rows[("s05_mesh", "m1.gen")]["credits"] == 35
    assert rows[("s05_mesh", "m1.gen")]["why"] == "3D model from the approved concept"
    assert rows[("s06_printability", "analyze")]["credits"] == 0
    assert rows[("s03_concept", "r1c1")]["why"] == "concept render, round 1"
    assert set(rep["credits"]) >= {"paid", "reused_calls", "saved", "refunded", "pending",
                                   "refused_calls", "spend_table"}
    ledger = CreditLedger(rt.store, job_id)
    assert ledger.summary_line() == "Meshy credits: 75 paid"
