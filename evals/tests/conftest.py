"""Evals fixtures: a job recorded against the fake Meshy server (no network, no real
slicer), which the replay tests then store and replay."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[2]


class FakeSlicer:
    """Stands in for p2p_threemf.slice_check: passes every file, fixed numbers."""

    def __init__(self):
        self.calls = 0

    def available(self, name: str) -> bool:
        return name == "bambu_studio"

    def __call__(self, path, settings, expected_slots, filament_ids=None, keep_gcode=False,
                 **kw):
        from p2p_contracts import SliceReport
        from p2p_contracts.reports import SliceFilament
        from p2p_threemf import read_3mf_full

        self.calls += 1
        used = sorted({int(s) for s in read_3mf_full(path).face_slots.tolist()})
        fils = [SliceFilament(slot=s, grams=round(3.0 + s, 2),
                              filament_id=(filament_ids[s - 1] if filament_ids else None))
                for s in used]
        return SliceReport(ok=True, slicer="bambu_studio", slicer_version="fake",
                           printer_profile="Bambu Lab X2D 0.4 nozzle", process_profile="fake",
                           print_time_s=4321.0, filaments=fils,
                           total_grams=sum(f.grams for f in fils), slots_used=len(used),
                           expected_slots=expected_slots)


@pytest.fixture(autouse=True)
def fake_slicer(monkeypatch):
    """Evals tests never launch Bambu Studio."""
    import p2p_threemf
    import p2p_threemf.slicer

    fake = FakeSlicer()
    monkeypatch.setattr(p2p_threemf, "slice_check", fake)
    monkeypatch.setattr(p2p_threemf.slicer, "backend_available", fake.available)
    return fake


def _photo(path: Path, color) -> Path:
    im = Image.new("RGB", (320, 240), (230, 230, 230))
    ImageDraw.Draw(im).ellipse((100, 60, 220, 180), fill=color)
    im.save(path)
    return path


@pytest.fixture(scope="session")
def recorded_job(tmp_path_factory):
    """(jobs dir, job id): a full-profile keychain run end to end on the fake Meshy server with
    MockClaude, gates answered by the run-local auto-responder. Recorded once per session."""
    import p2p_threemf
    import p2p_threemf.slicer
    from p2p_worker.clients.claude import MockClaude
    from p2p_worker.config import Settings
    from p2p_worker.fakes.meshy import FakeMeshy
    from p2p_worker.local import run_local
    from p2p_worker.runtime import build_runtime
    from p2p_worker.store import LocalStore

    root = tmp_path_factory.mktemp("recorded")
    fake_slicer = FakeSlicer()
    saved = (p2p_threemf.slice_check, p2p_threemf.slicer.backend_available)
    p2p_threemf.slice_check = fake_slicer
    p2p_threemf.slicer.backend_available = fake_slicer.available
    try:
        settings = Settings(repo_root=REPO, jobs_dir=root / "jobs", mock_claude=True,
                            claude_backend="mock", vendor_wait_s=5.0, fake_meshy_delay_s=0.005)
        store = LocalStore(settings.jobs_dir)
        fake = FakeMeshy(delay_s=0.005)
        fake.on_event = lambda ev: store.insert_meshy_event(ev)
        rt = build_runtime(settings, store=store, fake=fake, claude=MockClaude())
        photos = [_photo(root / "dog.jpg", (200, 150, 90)), _photo(root / "cat.jpg", (60, 60, 60))]

        async def go():
            try:
                return await run_local(rt, photos, "keychain of my dog and cat hugging",
                                       auto=True, product_type="keychain", profile="full")
            finally:
                await rt.aclose()

        job_id, status, _ = asyncio.run(go())
    finally:
        p2p_threemf.slice_check, p2p_threemf.slicer.backend_available = saved
    assert status.value == "done", status
    return settings.jobs_dir, job_id


@pytest.fixture
def fixtures_dir(recorded_job, tmp_path):
    """A fixture store built from the recorded job (the builder under test)."""
    from p2p_evals.fixtures import build
    from p2p_evals.jobs import StoredJob

    jobs_dir, job_id = recorded_job
    out = tmp_path / "fixtures"
    build([StoredJob("fake", job_id, "recorded on the fake Meshy server")], jobs_dir, out,
          log=lambda m: None)
    return out

