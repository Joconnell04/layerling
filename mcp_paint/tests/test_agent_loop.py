"""The s09 agent loop without Claude (stub driver), and one opt-in live Opus iteration."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from p2p_contracts import SliceReport
from p2p_contracts.reports import Checklist
from p2p_mcp_paint.agent import StubDriver, last_json, run_paint, scrubbed_env
from p2p_mcp_paint.session import PaintSession

FRENCHIE = "819cc491-c341-470a-bb97-63a194fa8dca"


def fake_slice(ok=True):
    def fn(s):
        return SliceReport(ok=ok, slicer="bambu_studio", slicer_version="stub",
                           printer_profile="x", process_profile="y",
                           errors=[] if ok else ["stub failure"], print_time_s=60.0,
                           total_grams=5.0)
    return fn


def _run(session, driver, **kw):
    return asyncio.run(run_paint(session.inp.job_id, session=session, driver=driver,
                                 slice_fn=kw.pop("slice_fn", fake_slice()), log=lambda m: None,
                                 **kw))


def test_last_json_reads_the_verdict_line():
    assert last_json('notes\n{"iteration": 2, "ready_for_signoff": true}') == {
        "iteration": 2, "ready_for_signoff": True}
    assert last_json("no json here") == {}


def test_scrubbed_env_removes_api_keys():
    os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-real"
    with scrubbed_env():
        assert "ANTHROPIC_API_KEY" not in os.environ
    assert os.environ.pop("ANTHROPIC_API_KEY") == "sk-test-not-real"


def test_stub_loop_writes_contract_outputs(session):
    driver = StubDriver(lambda: PaintSession.resume(session.inp, session.dir, session.attempt))
    run = _run(session, driver)
    d = session.dir
    assert run.iterations == 2  # stub says ready after iteration 2
    assert [e for e, _ in driver.calls] == ["medium", "medium", "high"]
    cl = Checklist.model_validate_json((d / "checklist.json").read_text())
    assert [i.id for i in cl.items][-2:] == ["sim_matches_intent", "purge_under_40pct"]
    assert next(i for i in cl.items if i.id == "sim_matches_intent").passed is True
    assert 0 <= run.confidence <= 1
    st = json.loads((d / "session.json").read_text())
    # c000 the SAM 3 split, c001 the palette decision (palette_plan), then one per iteration
    assert [c["id"] for c in st["checkpoints"]] == ["c000", "c001", "c002", "c003"]
    assert st["checkpoints"][1]["note"].startswith("palette plan")
    assert (d / "palette_plan.json").is_file()
    assert (d / "final.3mf").is_file() and (d / "paint_state.json").is_file()
    assert (d / "previews" / "before_print.png").is_file()
    assert (d / "previews" / "after_print.png").is_file()
    m = json.loads((d / "manifest.json").read_text())
    assert m["stage"] == "s09_paint" and m["confidence"] == run.confidence
    # the gray specks planted in the fixture are no longer brown
    after = PaintSession.resume(session.inp, d, session.attempt)
    fil = after.face_filament()
    assert not (fil[session._speck] == "generic-pla-brown").any()


def test_stub_loop_nudges_a_missing_checkpoint(session):
    def plan(s, k):
        from p2p_contracts.mcp_tools import DespeckleIn

        return [("despeckle", DespeckleIn(min_area_mm2=1.0))]  # forgets to checkpoint

    driver = StubDriver(lambda: PaintSession.resume(session.inp, session.dir, session.attempt),
                        plan=plan)
    run = _run(session, driver, max_iterations=2)
    assert any(t.startswith("You did not checkpoint") for _, t in driver.calls)
    st = json.loads((session.dir / "session.json").read_text())
    assert len(st["checkpoints"]) == 2 + run.iterations  # c000 split, c001 palette plan


def test_failed_gate_or_low_confidence_goes_to_review(session):
    driver = StubDriver(lambda: PaintSession.resume(session.inp, session.dir, session.attempt),
                        confidence=0.3)
    run = _run(session, driver, slice_fn=fake_slice(ok=False))
    assert run.review and run.confidence == 0.0 and run.threemf is None
    assert "slicer gate failed" in run.review_reason


@pytest.mark.live_claude
def test_live_opus_one_iteration_on_the_frenchie(tmp_path):
    """One real Opus 5.5 iteration through Claude Code on the Frenchie keychain (0 Meshy)."""
    root = Path(os.environ.get("P2P_JOBS_DIR", Path(__file__).resolve().parents[2] / "jobs"))
    if not (root / FRENCHIE / "s06_printability").is_dir():
        pytest.skip("Frenchie job folder not on this machine")
    from p2p_mcp_paint.proof import concept_png

    run = asyncio.run(run_paint(FRENCHIE, jobs_dir=root, max_iterations=1,
                                sam3_backend=os.environ.get("P2P_PAINT_SAM3", "local"),
                                concept_png=concept_png(root / FRENCHIE),
                                log=lambda m: print(m, flush=True)))
    assert run.iterations == 1
    assert run.model_calls and all(c.usd == 0 and c.billing == "subscription"
                                   for c in run.model_calls)
    assert (run.dir / "checklist.json").is_file()


def test_driver_passes_an_absolute_jobs_dir_to_the_paint_server():
    """The server starts with cwd = jobs_dir; a relative --jobs-dir found no job and the agent
    ran eight iterations without tools (fidelity rerun2, first try)."""
    from p2p_mcp_paint.agent import ClaudeDriver

    d = ClaudeDriver("j", 3, Path("geometry/_out/fidelity/jobs"))
    assert d.jobs_dir.is_absolute()
    i = d._options("medium").mcp_servers["paint"]["args"].index("--jobs-dir")
    assert Path(d._options("medium").mcp_servers["paint"]["args"][i + 1]).is_absolute()


class _HungDriver(StubDriver):
    """A paint agent whose Claude call never answers (the turn waits forever)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.closed = False

    async def turn(self, content, effort: str):
        self.calls.append((effort, "hung"))
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


def test_a_paint_turn_that_never_answers_times_out(session, monkeypatch):
    """live2 catfish: s09 had no log line for 9 h. That was the Mac asleep, but nothing bounded
    a Claude call, so a hung one would have held the stage for as long as the worker lived."""
    import time

    from p2p_mcp_paint import agent

    monkeypatch.setattr(agent, "TURN_TIMEOUT_S", 0.3)
    driver = _HungDriver(lambda: PaintSession.resume(session.inp, session.dir, session.attempt))
    t = time.monotonic()
    with pytest.raises(agent.ClaudeTimeout, match="paint iteration 1"):
        _run(session, driver, max_iterations=2)
    assert time.monotonic() - t < 30
    assert driver.closed and len(driver.calls) == 1
    assert isinstance(agent.ClaudeTimeout("x"), TimeoutError)


def test_a_concept_check_that_never_answers_times_out(session, monkeypatch):
    import io

    from p2p_mcp_paint import agent
    from PIL import Image

    async def hung_judge(content):
        await asyncio.Event().wait()

    monkeypatch.setattr(agent, "JUDGE_TIMEOUT_S", 0.3)
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 200, 200)).save(buf, format="PNG")
    driver = StubDriver(lambda: PaintSession.resume(session.inp, session.dir, session.attempt))
    with pytest.raises(agent.ClaudeTimeout, match="concept check 1"):
        _run(session, driver, judge=hung_judge, concept_png=buf.getvalue(), photos=[])


def test_claude_timeout_crosses_the_process_boundary():
    """The paint run is a child process (worker offload.py): its exception is pickled back."""
    import pickle

    from p2p_mcp_paint.agent import ClaudeTimeout

    e = pickle.loads(pickle.dumps(ClaudeTimeout("paint iteration 3: no answer")))
    assert isinstance(e, TimeoutError) and "paint iteration 3" in str(e)
