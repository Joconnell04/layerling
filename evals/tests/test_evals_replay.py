"""Fixture store, replay Meshy backend, recorded Claude and the replay suite, on a job recorded
against the fake Meshy server (the real store is too big for CI and lives outside git)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from p2p_evals.fixtures import verify
from p2p_evals.jobs import StoredJob
from p2p_evals.replay import Options, run_job
from p2p_worker.clients.meshy import MeshyClient
from p2p_worker.errors import VendorError
from p2p_worker.fakes.replay import ReplayMeshy, ReplayMiss


def _manifest(fixtures_dir: Path) -> dict:
    return json.loads((fixtures_dir / "manifest.json").read_text())


def test_store_keeps_every_paid_output(recorded_job, fixtures_dir):
    jobs_dir, job_id = recorded_job
    job = _manifest(fixtures_dir)["jobs"][0]
    ledger = [json.loads(x) for x in (jobs_dir / job_id / "ledger.jsonl").read_text().splitlines()]
    assert job["credits_live"] == sum(e["credits"] for e in ledger if e["vendor"] == "meshy")
    by_ep = {}
    for t in job["meshy"]:
        by_ep.setdefault(t["endpoint"], []).append(t)
        assert t["key"] and t["request_hash"] and t["status"] == "SUCCEEDED"
    # 2 figures, 2 add-on edits, views, the 3D model, Analyze (free, no file), Multi-Color
    assert len(by_ep["image-to-image"]) == 5 and all(t["assets"] for t in by_ep["image-to-image"])
    assert {a["field"] for a in by_ep["multi-image-to-3d"][0]["assets"]} >= {"model_urls.glb"}
    assert by_ep["print-multi-color"][0]["assets"][0]["field"] == "model_urls.3mf"
    assert by_ep["print-analyze"][0]["assets"] == []
    assert verify(fixtures_dir) == []
    # the job folder was only read
    assert not (jobs_dir / job_id / "evals").exists()


def test_store_detects_a_changed_file(fixtures_dir):
    job = _manifest(fixtures_dir)["jobs"][0]
    asset = job["meshy"][0]["assets"][0]
    p = fixtures_dir / "store" / job["job_id"] / "files" / asset["path"]
    p.write_bytes(p.read_bytes() + b"x")
    assert any("changed" in x for x in verify(fixtures_dir))


def _run(opt: Options, job_id: str) -> dict:
    return asyncio.run(run_job(StoredJob("fake", job_id, "fake"), opt))


def test_replay_from_s01_answers_every_meshy_call_by_input_hash(recorded_job, fixtures_dir,
                                                               tmp_path):
    _, job_id = recorded_job
    r = _run(Options(start="s01", claude="mock", paint="stub", work=tmp_path / "work",
                     fixtures=fixtures_dir), job_id)
    assert r["status"] == "done", r
    assert r["meshy"]["misses"] == 0
    assert r["meshy"]["by_match"] == {"hash": r["meshy"]["answered"]} and \
        r["meshy"]["answered"] == 8
    assert r["meshy_credits_booked"] == 0 and r["meshy"]["credits_charged"] == 0
    assert r["gate"]["ok"] is True and r["gate"]["stored_ok"] is True
    assert r["slots"]["rerun"] == r["slots"]["stored"]
    assert r["grams"]["delta_pct"] == 0.0
    assert r["form"]["consistent"] is True
    assert r["concept_check"]["passed"] is True and r["concept_check"]["rerun"] is True
    assert r["geometry_vs_stored"]["ok"] is True
    assert not r["gates"]["diverged"] and r["gates"]["stuck"] is None
    assert len(r["gates"]["replies"]) == 3  # answers, concept pick, mesh approve


def test_replay_from_s06_reruns_geometry_onward(recorded_job, fixtures_dir, tmp_path):
    _, job_id = recorded_job
    r = _run(Options(start="s06", claude="mock", paint="replay", work=tmp_path / "work",
                     fixtures=fixtures_dir), job_id)
    assert r["status"] == "done", r
    for s in ("s06_printability", "s07_baseline", "s08_palette", "s09_paint", "s10_write"):
        assert r["stages"][s]["status"] == "succeeded", s
    assert r["paint_replay"]["used"] is True and r["paint_replay"]["same_mesh"] is True
    assert r["meshy"]["answered"] == 2 and r["meshy"]["misses"] == 0  # Analyze, Multi-Color
    geo = r["geometry_vs_stored"]
    assert geo["ok"] and all(m["identical"] for m in geo["meshes"].values())
    assert r["gate"]["summary"] == "Bambu pass"
    assert r["stages"]["s03_concept"]["status"] == "stored"  # copied, not rerun
    import time

    from p2p_evals.score import markdown

    md = markdown([r], Options(start="s06", claude="mock", paint="replay"), time.time())
    assert "| fake | done | Bambu pass |" in md and "| identical |" in md


def _client(replay: ReplayMeshy) -> MeshyClient:
    return MeshyClient(api_key="msy_replay_no_network", test_mode=True,
                       transport=replay.transport())


def test_a_call_with_no_recording_fails_loudly(fixtures_dir):
    replay = ReplayMeshy(fixtures_dir)

    async def go():
        c = _client(replay)
        try:
            await c.create("image-to-image", {"ai_model": "nano-banana", "prompt": "a cat",
                                              "reference_image_urls": ["data:image/png;base64,AA"]},
                           "unrecorded")
        finally:
            await c.aclose()

    with pytest.raises(ReplayMiss) as e:
        asyncio.run(go())
    assert isinstance(e.value, VendorError) and e.value.retryable is False
    assert e.value.code == "validation" and "no stored image-to-image task" in str(e.value)
    assert len(replay.misses) == 1


def test_key_match_serves_the_stage_recording_and_names_what_changed(fixtures_dir,
                                                                     recorded_job):
    from p2p_worker import trace

    jobs_dir, job_id = recorded_job
    job = _manifest(fixtures_dir)["jobs"][0]
    rec = next(t for t in job["meshy"] if t["endpoint"] == "print-multi-color")
    replay = ReplayMeshy(fixtures_dir)
    events = []
    replay.on_event = events.append
    ctx = SimpleNamespace(job=SimpleNamespace(id=job_id), stage=SimpleNamespace(value=rec["stage"]),
                          job_dir=jobs_dir / job_id,
                          m=SimpleNamespace(data={"meshy_pending": {rec["key"]: {
                              "at": "x", "endpoint": "print-multi-color"}}}))
    body = {**rec["request"], "style": "realistic"}  # the recording was cartoon

    async def go():
        c = _client(replay)
        try:
            with trace.attempt(ctx):
                task = await c.create("print-multi-color", body, "baseline")
            await replay.drain()
            got = await c.get_task("print-multi-color", task.task_id)
            url = got["model_urls"]["3mf"]
            dest = jobs_dir.parent / "replayed.3mf"
            await c.download(url, dest)
            return task, got, dest
        finally:
            await c.aclose()

    task, got, dest = asyncio.run(go())
    assert task.task_id == rec["task_id"]
    ans = replay.answers[-1]
    assert ans.match == "key" and ans.diff == ["style"]
    assert got["status"] == "SUCCEEDED" and got["consumed_credits"] == 0
    assert events and events[-1]["status"] == "SUCCEEDED"
    stored = fixtures_dir / "store" / job_id / "files" / rec["assets"][0]["path"]
    assert dest.read_bytes() == stored.read_bytes()


def test_meshy_mode_replay_wires_the_backend(fixtures_dir, monkeypatch, tmp_path):
    from p2p_worker.config import Settings
    from p2p_worker.runtime import build_runtime
    from p2p_worker.store import LocalStore

    env = tmp_path / "empty.env"
    env.write_text("")
    monkeypatch.setenv("P2P_ENV_FILE", str(env))
    monkeypatch.setenv("MESHY_MODE", "replay")
    monkeypatch.setenv("P2P_REPLAY_STORE", str(fixtures_dir))
    s = Settings.load(jobs_dir=tmp_path / "jobs")
    assert s.meshy_mode == "replay"
    rt = build_runtime(s, store=LocalStore(s.jobs_dir), claude=object())
    assert isinstance(rt.fake, ReplayMeshy) and rt.meshy.test_mode is True
    asyncio.run(rt.aclose())


def test_recorded_claude_replays_the_stored_reply_then_misses(tmp_path):
    from p2p_evals.recorded_claude import ClaudeReplayMiss, RecordedCall, RecordedClaude
    from p2p_worker.concept_read import ConceptRead

    reply = {"form": "flat", "figure_colors": [{"area": "coat", "hex": "#FFFFFF", "share": 1}]}
    calls = [RecordedCall(job_id="j", stage="s06_printability", attempt=1, seq=2,
                          purpose="concept_read", model="claude-sonnet-5",
                          text="Read this approved product concept.",
                          images=("approved_concept.png",), reply_text=json.dumps(reply),
                          reply_json=reply)]
    claude = RecordedClaude({"j": calls})
    img = tmp_path / "approved_concept.png"
    from PIL import Image

    Image.new("RGB", (8, 8)).save(img)

    async def ask():
        return await claude.ask(purpose="concept_read", system="s",
                                text="Read this approved product concept.", images=[img],
                                output=ConceptRead)

    out, call = asyncio.run(ask())
    assert out.form == "flat" and call.usd == 0
    assert claude.summary()["by_match"] == {"text": 1}
    with pytest.raises(ClaudeReplayMiss):
        asyncio.run(ask())


def test_one_file_hashes_the_same_as_a_data_uri_a_storage_ref_or_a_signed_url(tmp_path):
    from p2p_worker.fakes.replay import canonical, request_hash
    from p2p_worker.util import data_uri, downscale_image
    from PIL import Image

    job_id = "12345678-1234-4234-8234-123456789abc"
    jd = tmp_path / job_id
    (jd / "input").mkdir(parents=True)
    photo = jd / "input" / "photo_1.jpg"
    Image.new("RGB", (2400, 1600), (180, 120, 60)).save(photo)  # downscaled when sent
    data, mime = downscale_image(photo, max_px=1536)
    refs = [data_uri(data, mime), f"storage:inputs/{job_id}/input/photo_1.jpg",
            (f"https://p.supabase.co/storage/v1/object/sign/inputs/{job_id}/input/photo_1.jpg"
             "?token=abc")]
    canon = [canonical({"prompt": "a dog", "reference_image_urls": [r]}, jd, job_id)
             for r in refs]
    assert canon[0] == canon[1] == canon[2]
    assert canon[0]["reference_image_urls"][0].startswith("sha256:")
    assert len({request_hash("image-to-image", c) for c in canon}) == 1
    other = canonical({"prompt": "a cat", "reference_image_urls": [refs[1]]}, jd, job_id)
    assert request_hash("image-to-image", other) != request_hash("image-to-image", canon[0])
