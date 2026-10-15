"""Cloud fallback: a worker without Bambu Studio slices with p2p_slicer, the report says it is not
ship-gated, and the job waits in REVIEW ("Bambu gate pending") until a Mac worker reruns it."""

from __future__ import annotations

import json

import pytest
from conftest import make_rt, run
from p2p_contracts import JobStatus, PrintSettings, SliceReport, StageId, StageStatus
from p2p_contracts.payloads import JobResult
from p2p_mcp_paint.agent import StubDriver
from p2p_mcp_paint.session import PaintSession
from p2p_worker import printing as pr
from p2p_worker.errors import NeedsReview
from p2p_worker.local import run_job
from p2p_worker.stages.base import StageContext
from p2p_worker.stages.s09_paint import PaintStage
from p2p_worker.util import utcnow
from test_m3 import _job, manifest


def _report(slicer: str) -> SliceReport:
    return SliceReport(ok=True, slicer=slicer, slicer_version="0.1.0", printer_profile="p",
                       process_profile="q", warnings=["It seems object x has floating regions"])


def test_gate_settings_pick_the_backend(fake_slicer):
    s = PrintSettings()
    assert pr.gate_settings(s) is s  # Bambu Studio here: unchanged
    fake_slicer.installed = {"p2p_slicer"}
    assert pr.gate_settings(s).slicer == "p2p_slicer"
    assert s.slicer == "bambu_studio"  # a copy, the job's settings stay
    fake_slicer.installed = set()
    assert pr.gate_settings(s).slicer == "bambu_studio"  # nothing can slice: fail as before
    orca = s.model_copy(update={"slicer": "orcaslicer"})
    assert pr.gate_settings(orca) is orca  # only the Bambu gate falls back
    assert pr.paint_slice_fn(s) is None


def test_estimate_warning_goes_first_once():
    rep = pr.mark_estimate(_report("p2p_slicer"), fallback=True)
    assert rep.warnings[0].startswith("not ship-gated: sliced with p2p_slicer (Bambu Studio is "
                                      "not installed here)")
    pr.mark_estimate(rep, fallback=True)
    assert sum(w.startswith("not ship-gated") for w in rep.warnings) == 1
    assert not pr.ship_gated(rep) and not pr.ship_gated(None)
    bambu = pr.mark_estimate(_report("bambu_studio"), fallback=False)
    assert pr.ship_gated(bambu) and not any("ship-gated" in w for w in bambu.warnings)


def test_cloud_thin_slice_reaches_review_then_the_mac_ships_it(settings, photos, fake_slicer):
    fake_slicer.installed = {"p2p_slicer"}  # a cloud VM once the slicer core has landed
    rt = make_rt(settings)
    job_id, status, _ = _job(rt, photos[:1], product_type="keychain")
    assert status == JobStatus.REVIEW
    assert fake_slicer.calls[-1]["slicer"] == "p2p_slicer"
    row = rt.store.job_row(job_id)
    assert row["review_reason"].startswith("Bambu gate pending: only p2p_slicer fake")
    job = rt.store.load_job(job_id)
    result = JobResult.model_validate(job.result)
    assert result.slice_ok is None and result.print_time_s == 4321.0 and result.filaments
    m7 = manifest(rt, job_id, StageId.S07_BASELINE)
    assert m7.status == StageStatus.PAUSED and m7.error.code == "slicer_failed"
    assert m7.error.detail["gate"] == "estimate"
    roles = {o.role: o.path for o in m7.outputs}
    assert roles["baseline_3mf"].endswith("baseline_x2d.3mf") and "debug_3mf" not in roles
    gate = next(d for d in m7.decisions if d.key == "baseline.slicer_gate")
    assert gate.value == "estimate" and "Bambu gate pending" in gate.reason
    assert m7.data["slice_ok"] is None
    rep = SliceReport.model_validate_json(
        (rt.store.job_dir(job_id) / roles["slice_report"]).read_text())
    assert rep.slicer == "p2p_slicer" and rep.warnings[0].startswith("not ship-gated")
    kinds = [m.kind for m in rt.store.list_messages(job_id, role="assistant")]
    assert kinds.count("result") == 1 and "review_request" in kinds

    # approve is refused: a native slice never ships on its own
    rt.store.post_message(job_id, "user", "review_decision", {"action": "approve"})
    assert run(run_job(rt, job_id, None)) == JobStatus.REVIEW
    notes = [m.payload["text"] for m in rt.store.list_messages(job_id) if m.kind == "note"]
    assert any(n.startswith("Cannot approve: only p2p_slicer checked this file") for n in notes)

    # the Mac worker (Bambu Studio installed) reruns the stage and ships
    fake_slicer.installed = {"bambu_studio", "p2p_slicer"}
    rt.store.post_message(job_id, "user", "review_decision", {"action": "rerun_paint"})
    assert run(run_job(rt, job_id, None)) == JobStatus.DONE
    assert fake_slicer.calls[-1]["slicer"] == "bambu_studio"
    assert JobResult.model_validate(rt.store.load_job(job_id).result).slice_ok is True


def test_cloud_without_any_slicer_fails_the_gate_as_before(settings, photos, fake_slicer):
    fake_slicer.installed = set()
    rt = make_rt(settings)
    job_id, status, _ = _job(rt, photos[:1], product_type="keychain")
    assert status == JobStatus.REVIEW
    assert rt.store.job_row(job_id)["review_reason"] == (
        "slicer gate failed: bambu_studio is not installed")
    assert rt.store.load_job(job_id).result is None


def test_cloud_full_profile_holds_s10_for_bambu(settings, photos, fake_slicer):
    fake_slicer.installed = {"p2p_slicer"}
    rt = make_rt(settings)
    job_id, status, _ = _job(rt, photos[:1], product_type="keychain", profile="full")
    assert status == JobStatus.REVIEW
    m10 = manifest(rt, job_id, StageId.S10_WRITE)
    assert m10.status == StageStatus.PAUSED and m10.error.detail["gate"] == "estimate"
    assert {o.role for o in m10.outputs} >= {"final_3mf", "slice_report", "job_result"}
    gate = next(d for d in m10.decisions if d.key == "write.slicer_gate")
    assert gate.value == "estimate"
    result = JobResult.model_validate(rt.store.load_job(job_id).result)
    assert result.slice_ok is None and result.threemf.endswith("final.3mf")
    assert rt.store.job_row(job_id)["review_reason"].startswith("Bambu gate pending")


@pytest.mark.parametrize("installed", [{"p2p_slicer"}])
def test_s09_final_gate_uses_p2p_slicer_in_the_cloud(settings, photos, monkeypatch, fake_slicer,
                                                     installed):
    from p2p_contracts import Manifest

    monkeypatch.setenv("P2P_PAINT_SAM3", "mock")
    rt = make_rt(settings)
    job_id, status, _ = _job(rt, photos[:1], product_type="keychain")
    assert status == JobStatus.DONE
    fake_slicer.installed = installed
    job = rt.store.load_job(job_id)
    job.attempts[StageId.S09_PAINT] = 2
    m = Manifest(job_id=job_id, stage=StageId.S09_PAINT, attempt=2, status=StageStatus.RUNNING)
    rt.store.write_manifest(m)
    stage = PaintStage(driver_factory=lambda s: StubDriver(
        lambda: PaintSession.resume(s.inp, s.dir, s.attempt)), max_iterations=1)
    ctx = StageContext(rt.orchestrator.svc, job, StageId.S09_PAINT, 2, m, utcnow())
    try:
        run(stage.run(ctx))
    except NeedsReview:
        pass  # the stub's confidence decides; the gate is what this test checks
    assert fake_slicer.calls[-1]["slicer"] == "p2p_slicer"
    rep = json.loads(ctx.path("slice_report.json").read_text())
    assert rep["slicer"] == "p2p_slicer" and rep["warnings"][0].startswith("not ship-gated")
    gate = next(d for d in m.decisions if d.key == "paint.slicer_gate")
    assert gate.value == ("estimate" if rep["ok"] else "fail")
    run(rt.aclose())
