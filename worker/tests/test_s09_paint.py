"""s09 paint stage on a thin-slice job (fake Meshy, fake slicer), with a stub paint agent."""

from __future__ import annotations

import pytest
from conftest import make_rt, run
from p2p_contracts import JobStatus, Manifest, StageId, StageStatus
from p2p_mcp_paint.agent import StubDriver
from p2p_mcp_paint.session import PaintSession
from p2p_worker.errors import NeedsReview
from p2p_worker.stages import RUNNERS
from p2p_worker.stages.base import StageContext
from p2p_worker.stages.s09_paint import PaintStage
from p2p_worker.util import utcnow
from test_pipeline import _local


def test_s09_is_registered():
    from p2p_worker.stages.s09_stub import PaintSwitch, agent_stage

    assert isinstance(RUNNERS[StageId.S09_PAINT], PaintSwitch)  # PAINT_AGENT picks stub or agent
    assert isinstance(agent_stage(), PaintStage)


def test_s09_paint_stage_with_stub_agent(settings, photos, monkeypatch, fake_slicer):
    monkeypatch.setenv("P2P_PAINT_SAM3", "mock")
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos, product_type="keychain"))
    assert status == JobStatus.DONE
    job = rt.store.load_job(job_id)
    job.attempts[StageId.S09_PAINT] = 2
    m = Manifest(job_id=job_id, stage=StageId.S09_PAINT, attempt=2, status=StageStatus.RUNNING)
    rt.store.write_manifest(m)
    stage = PaintStage(driver_factory=lambda s: StubDriver(
        lambda: PaintSession.resume(s.inp, s.dir, s.attempt)), max_iterations=2)
    ctx = StageContext(rt.orchestrator.svc, job, StageId.S09_PAINT, 2, m, utcnow())
    try:
        run(stage.run(ctx))
        reviewed = False
    except NeedsReview:
        reviewed = True
    roles = {o.role for o in m.outputs}
    assert {"paint_state", "region_map", "face_regions", "checklist", "slice_report",
            "preview"} <= roles
    assert ("paint_3mf" in roles) or ("debug_3mf" in roles)
    assert {i.from_stage for i in m.inputs} >= {StageId.S06_PRINTABILITY, StageId.S07_BASELINE}
    assert m.confidence is not None and 0 <= m.confidence <= 1
    assert m.data["iterations"] == 2
    assert fake_slicer.calls, "the gate ran"
    if reviewed:
        kinds = [x.kind for x in rt.store.list_messages(job_id, role="assistant")]
        assert "review_request" in kinds
    run(rt.aclose())


@pytest.mark.parametrize("ok", [False])
def test_s09_failed_gate_goes_to_review(settings, photos, monkeypatch, fake_slicer, ok):
    monkeypatch.setenv("P2P_PAINT_SAM3", "mock")
    fake_slicer.fail_with = ["stub gate failure"]
    rt = make_rt(settings)
    fake_slicer.fail_with = None
    job_id, status, _ = run(_local(rt, photos, product_type="keychain"))
    assert status == JobStatus.DONE
    fake_slicer.fail_with = ["stub gate failure"]
    job = rt.store.load_job(job_id)
    job.attempts[StageId.S09_PAINT] = 2
    m = Manifest(job_id=job_id, stage=StageId.S09_PAINT, attempt=2, status=StageStatus.RUNNING)
    rt.store.write_manifest(m)
    stage = PaintStage(driver_factory=lambda s: StubDriver(
        lambda: PaintSession.resume(s.inp, s.dir, s.attempt)), max_iterations=1)
    ctx = StageContext(rt.orchestrator.svc, job, StageId.S09_PAINT, 2, m, utcnow())
    with pytest.raises(NeedsReview) as e:
        run(stage.run(ctx))
    assert "slicer gate failed" in e.value.reason
    assert m.confidence == 0.0
    assert "debug_3mf" in {o.role for o in m.outputs}
    run(rt.aclose())


def test_inputs_reach_disk_before_a_long_step_so_a_restart_sees_them_go_stale(settings, photos):
    """live2 run: the worker restarted while the cat charm's s09 paint ran; its manifest had no
    inputs on disk (they were saved only after the 20-minute step), so when s06 and s08 got new
    attempts the orchestrator could not tell and resumed the old paint session."""
    from conftest import SimulatedCrash

    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos, product_type="keychain"))
    assert status == JobStatus.DONE
    job = rt.store.load_job(job_id)
    job.attempts[StageId.S09_PAINT] = 2
    m = Manifest(job_id=job_id, stage=StageId.S09_PAINT, attempt=2, status=StageStatus.RUNNING)
    rt.store.write_manifest(m)
    rt.store.save_job(job)
    ctx = StageContext(rt.orchestrator.svc, job, StageId.S09_PAINT, 2, m, utcnow())
    ctx.use_output(StageId.S06_PRINTABILITY, "geometry")

    async def long_step():
        raise SimulatedCrash()  # the process dies inside the step

    with pytest.raises(SimulatedCrash):
        run(ctx.step("paint", long_step))
    on_disk = rt.store.load_manifest(job_id, StageId.S09_PAINT, 2)
    assert [i.from_stage for i in on_disk.inputs] == [StageId.S06_PRINTABILITY]
    # s06 moves to a new attempt: the half-run s09 attempt is stale, not resumed
    job = rt.store.load_job(job_id)
    job.attempts[StageId.S06_PRINTABILITY] += 1
    assert "moved to attempt" in rt.orchestrator._stale_reason(job, on_disk)
    run(rt.aclose())


def test_a_paint_run_whose_claude_call_never_answers_fails_retryable(settings, photos,
                                                                      monkeypatch, fake_slicer):
    """live2 catfish: s09 went 9 h without a log line (the Mac was asleep). A Claude call that
    really hangs now fails the attempt as vendor_timeout, retryable, instead of holding s09 for
    as long as the worker lives."""
    import asyncio

    from p2p_mcp_paint import agent
    from p2p_worker.errors import VendorError

    class Hung(StubDriver):
        async def turn(self, content, effort):
            await asyncio.Event().wait()

    monkeypatch.setenv("P2P_PAINT_SAM3", "mock")
    monkeypatch.setattr(agent, "TURN_TIMEOUT_S", 0.3)
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos, product_type="keychain"))
    assert status == JobStatus.DONE
    job = rt.store.load_job(job_id)
    job.attempts[StageId.S09_PAINT] = 2
    m = Manifest(job_id=job_id, stage=StageId.S09_PAINT, attempt=2, status=StageStatus.RUNNING)
    rt.store.write_manifest(m)
    stage = PaintStage(driver_factory=lambda s: Hung(
        lambda: PaintSession.resume(s.inp, s.dir, s.attempt)), max_iterations=2)
    ctx = StageContext(rt.orchestrator.svc, job, StageId.S09_PAINT, 2, m, utcnow())
    with pytest.raises(VendorError) as e:
        run(stage.run(ctx))
    assert e.value.code == "vendor_timeout" and e.value.retryable
    assert "paint iteration 1" in str(e.value)
    assert "paint" not in ctx.steps  # nothing cached: the retry attempt paints again
    run(rt.aclose())
