"""Slicer gate and review, form values, signed URLs, spend columns, heartbeat, logs."""

from __future__ import annotations

import json
import logging

import pytest
from conftest import make_rt, run
from p2p_contracts import JobStatus, SliceReport, StageId, StageStatus
from p2p_worker import logs
from p2p_worker.local import run_job, run_local
from p2p_worker.store import LocalStore


def _job(rt, photos, prompt="keychain of my dog", **kw):
    async def go():
        try:
            return await run_local(rt, photos, prompt, auto=True, **kw)
        finally:
            await rt.fake.drain()

    return run(go())


def test_slicer_gate_failure_goes_to_review_and_never_ships(settings, photos, fake_slicer):
    fake_slicer.fail_with = ["slice failed: exit -11", "Invalid T command (T65279)"]
    rt = make_rt(settings)
    job_id, status, _ = _job(rt, photos[:1], product_type="keychain")
    assert status == JobStatus.REVIEW
    job = rt.store.load_job(job_id)
    assert job.result is None and job.status == JobStatus.REVIEW
    row = rt.store.job_row(job_id)
    assert row["review_reason"].startswith("slicer gate failed: slice failed: exit -11")
    kinds = [m.kind for m in rt.store.list_messages(job_id, role="assistant")]
    assert "review_request" in kinds and "result" not in kinds
    req = next(m for m in rt.store.list_messages(job_id) if m.kind == "review_request")
    assert "exit -11" in req.payload["reason"] and req.payload["images"]
    m7 = rt.store.load_manifest(job_id, StageId.S07_BASELINE, 1)
    assert m7.status == StageStatus.PAUSED and m7.error.code == "slicer_failed"
    roles = {o.role: o.path for o in m7.outputs}
    assert "baseline_3mf" not in roles and "job_result" not in roles
    assert roles["debug_3mf"].endswith("debug_x2d.3mf")
    assert roles["slice_report"] and roles["baseline_3mf_meshy"]
    report = SliceReport.model_validate_json((rt.store.job_dir(job_id) / roles["slice_report"])
                                             .read_text())
    assert report.ok is False and "exit -11" in report.errors[0]

    # approve is refused: a file that failed the gate never ships
    rt.store.post_message(job_id, "user", "review_decision", {"action": "approve"})
    assert run(run_job(rt, job_id, None)) == JobStatus.REVIEW
    # rerun: a fresh s07 attempt reuses Meshy's 3MF (0 credits) and passes this time
    fake_slicer.fail_with = None
    before = rt.fake.count("POST", "print-multi-color")
    rt.store.post_message(job_id, "user", "review_decision", {"action": "rerun_paint"})
    assert run(run_job(rt, job_id, None)) == JobStatus.DONE
    assert rt.fake.count("POST", "print-multi-color") == before
    m7b = rt.store.load_manifest(job_id, StageId.S07_BASELINE, 2)
    assert any(d.key == "baseline.reused.cartoon" for d in m7b.decisions)
    job = rt.store.load_job(job_id)
    assert job.result["threemf"] == "s07_baseline/a2/baseline_x2d.3mf"
    notes = [m.payload["text"] for m in rt.store.list_messages(job_id) if m.kind == "note"]
    assert any(n.startswith("Cannot approve") for n in notes)


def test_review_reject_fails_the_job(settings, photos, fake_slicer):
    fake_slicer.fail_with = ["no print time"]
    rt = make_rt(settings)
    job_id, status, _ = _job(rt, photos[:1], product_type="magnet", prompt="3d magnet of my dog")
    assert status == JobStatus.REVIEW
    rt.store.post_message(job_id, "user", "review_decision", {"action": "reject"})
    assert run(run_job(rt, job_id, None)) == JobStatus.FAILED


def test_form_values_are_never_asked_again(settings, photos):
    rt = make_rt(settings)
    form = [{"kind": "nameplate", "text": {"text": "Bronco"}}]
    job_id, status, _ = _job(rt, photos[:1], prompt="nameplate of my dog",
                             product_type="nameplate", target_size_mm=110, addons=form)
    assert status == JobStatus.DONE
    q = next(m for m in rt.store.list_messages(job_id) if m.kind == "questions")
    asked = [x["id"] for x in q.payload["questions"]]
    assert "name" not in asked and "size" not in asked and 2 <= len(asked) <= 4
    job = rt.store.load_job(job_id)
    plate = next(a for a in job.addons if a.kind.value == "nameplate")
    assert plate.text.text == "Bronco"  # the form wins over intake
    assert plate.params["shape"] == "rectangle"  # the customer's answer added the shape
    assert job.target_size_mm == 110
    m1 = rt.store.load_manifest(job_id, StageId.S01_INTAKE, 1)
    assert any(d.key == "intake.skipped" and set(d.value) == {"name", "size"}
               for d in m1.decisions)
    size = next(d for d in m1.decisions if d.key == "job.target_size_mm")
    assert size.reason == "from form"
    regions = json.loads((rt.store.job_dir(job_id) / "s01_intake/a1/color_regions.json")
                         .read_text())
    assert any(r["kind"] == "marking" for r in regions)


def test_signed_urls_go_to_meshy_but_never_into_manifests(settings, photos):
    class SignedStore(LocalStore):
        def signed_url(self, job_id, rel_path, expires_s=3600):
            bucket = "inputs" if rel_path.startswith("input/") else "artifacts"
            return (f"https://proj.supabase.co/storage/v1/object/sign/{bucket}/{job_id}/"
                    f"{rel_path}?token=secret-token-123")

    store = SignedStore(settings.jobs_dir)
    rt = make_rt(settings, store=store)
    job_id, status, _ = _job(rt, photos[:1], prompt="figurine of my dog")
    assert status == JobStatus.DONE
    sent = [b for tid, b in rt.fake.bodies.items() if rt.fake.endpoint_of[tid] ==
            "image-to-image" and "reference_image_urls" in b]
    assert sent and all(u.startswith("https://proj.supabase.co/storage/v1/object/sign/inputs/")
                        and u.endswith("?token=secret-token-123")
                        for b in sent for u in b["reference_image_urls"])  # URLs, not data
    m3 = rt.store.load_manifest(job_id, StageId.S03_CONCEPT, 1)
    refs = {u for t in m3.meshy_tasks for u in t.request.get("reference_image_urls", [])}
    assert refs == {f"storage:inputs/{job_id}/input/photo_1.jpg"}
    # the s04 views reach Multi-Image to 3D as signed artifact URLs, stored without the token
    m5 = rt.store.load_manifest(job_id, StageId.S05_MESH, 1)
    urls = m5.meshy_tasks[0].request["image_urls"]
    assert urls[0] == f"storage:artifacts/{job_id}/s04_multiview/a1/view_concept.png"
    for f in rt.store.job_dir(job_id).rglob("*.json*"):
        assert "secret-token-123" not in f.read_text(), f
        assert "Signature=" not in f.read_text(), f


def test_spend_rows_carry_usd_list_and_billing(settings, photos):
    rt = make_rt(settings)
    job_id, _status, _ = _job(rt, photos[:1], prompt="figurine of my dog")
    ledger = [json.loads(x) for x in
              (rt.store.job_dir(job_id) / "ledger.jsonl").read_text().splitlines()]
    claude = [e for e in ledger if e["vendor"] == "anthropic"]
    assert claude and claude[0]["kind"] == "intake" and claude[0]["usd"] == 0
    assert claude[0]["billing"] == "free" and claude[0]["usd_list"] > 0
    assert claude[0]["ref"] == "mock-intake"
    meshy = [e for e in ledger if e["vendor"] == "meshy"]
    assert meshy and all(e["billing"] == "credits" and e["usd_list"] == e["usd"]
                         for e in meshy)


def test_heartbeat_setting(tmp_path, monkeypatch):
    from p2p_worker.config import Settings

    monkeypatch.setenv("P2P_ENV_FILE", str(tmp_path / "none.env"))
    assert Settings.load().heartbeat_s == 15.0
    monkeypatch.setenv("WORKER_HEARTBEAT_S", "7.5")
    assert Settings.load().heartbeat_s == 7.5


@pytest.mark.real_slicer
def test_real_bambu_gate_on_the_fake_figure(settings, photos):
    """Opt-in (RUN_SLICER=1): the installed Bambu Studio CLI slices the X2D keychain."""
    rt = make_rt(settings)
    job_id, status, _ = _job(rt, photos[:1], product_type="keychain")
    job = rt.store.load_job(job_id)
    m7 = rt.store.load_manifest(job_id, StageId.S07_BASELINE, job.attempts[StageId.S07_BASELINE])
    ref = next(o for o in m7.outputs if o.role == "slice_report")
    report = SliceReport.model_validate_json((rt.store.job_dir(job_id) / ref.path).read_text())
    print("real gate:", report.ok, report.print_time_s, report.total_grams, report.errors[:3])
    assert status == JobStatus.DONE, report.errors
    assert job.result["slice_ok"] and job.result["print_time_s"] > 0


def test_json_logs_carry_context_and_redact(settings, photos, tmp_path):
    s = settings.with_(supabase_service_key="eyJhbGciOiJIUzI1NiJ9.c2VydmljZS1yb2xl.c2lnbmF0dXJl",  # gitleaks:allow (test fake)
                       webhook_token="hook-token-123456")
    log_dir = tmp_path / "logs"
    path = logs.setup_logging(s, log_dir, console=False)
    try:
        rt = make_rt(s)

        async def go():
            try:
                return await run_local(rt, photos[:1], "figurine of my dog", auto=True)
            finally:
                await rt.fake.drain()

        job_id, status, _ = run(go())
        assert status == JobStatus.DONE
        logging.getLogger("p2p_worker").warning(
            "leak check data:image/png;base64,QUJDRA== https://assets.meshy.ai/x/model.glb"
            "?Expires=1&Signature=abc token=hook-token-123456 Bearer abc.def "
            "eyJhbGciOiJIUzI1NiJ9.c2VydmljZS1yb2xl.c2lnbmF0dXJl msy_abcdefghijk")
    finally:
        for h in list(logging.getLogger().handlers):
            if getattr(h, "_p2p", False):
                h.close()
                logging.getLogger().removeHandler(h)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert (log_dir / ".gitignore").read_text() == "*\n"
    ends = [x for x in lines if x["event"] == "stage_end"]
    assert {x["stage"] for x in ends} >= {"s01_intake", "s03_concept", "s05_mesh", "s07_baseline"}
    assert all(x["job_id"] == job_id and x["attempt"] >= 1 and "duration_ms" in x for x in ends)
    creates = [x for x in lines if x["event"] == "meshy_create"]
    assert creates and all(x["meshy_task_id"] and x["stage"] for x in creates)
    assert any(x["event"] == "meshy_done" and x["meshy_task_id"] for x in lines)
    assert any(x["event"] == "gate_reply" for x in lines)
    text = path.read_text()
    for secret in ("QUJDRA==", "Signature=abc", "hook-token-123456", "c2lnbmF0dXJl",
                   "msy_abcdefghijk", "abc.def", "Expires=1"):
        assert secret not in text, secret
    leak = next(x for x in lines if x["msg"].startswith("leak check"))
    assert "data:<redacted>" in leak["msg"] and "model.glb?<redacted>" in leak["msg"]


def test_log_rotation(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "MAX_BYTES", 2000)
    path = logs.setup_logging(settings, tmp_path / "logs", console=False)
    try:
        for i in range(100):
            logs.log_event("tick", n=i)
    finally:
        for h in list(logging.getLogger().handlers):
            if getattr(h, "_p2p", False):
                h.close()
                logging.getLogger().removeHandler(h)
    assert path.stat().st_size <= 2000
    assert (tmp_path / "logs" / "worker.jsonl.1").is_file()
