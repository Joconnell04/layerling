"""Shared fixtures. Tests never read the repo .env: Settings are built directly."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from p2p_worker.clients.claude import MockClaude
from p2p_worker.config import Settings
from p2p_worker.fakes.meshy import FakeMeshy
from p2p_worker.runtime import Runtime, build_runtime
from p2p_worker.store import LocalStore
from PIL import Image, ImageDraw

OPT_IN = {
    "live": ("RUN_LIVE_CLAUDE", "one real Haiku call through the Claude Code login"),
    "supabase": ("RUN_SUPABASE", "creates and deletes TEST rows in the real Supabase project"),
    "real_slicer": ("RUN_SLICER", "slices with the installed Bambu Studio CLI"),
}


class FakeSlicer:
    """Stands in for p2p_threemf.slice_check: reads the written 3MF, passes by default.
    `installed` is which slicers this worker has (p2p_threemf.slicer.backend_available): Bambu
    Studio by default, so tests behave the same on a Mac, in CI and once p2p_slicer lands."""

    def __init__(self):
        self.calls: list[dict] = []
        self.fail_with: list[str] | None = None
        self.installed: set[str] = {"bambu_studio"}

    def available(self, name: str) -> bool:
        return name in self.installed

    def __call__(self, path, settings, expected_slots, filament_ids=None, keep_gcode=False,
                 **kw):
        from p2p_contracts import SliceReport
        from p2p_contracts.reports import SliceFilament
        from p2p_threemf import read_3mf_full

        src = read_3mf_full(path)
        used = sorted({int(s) for s in src.face_slots.tolist()})
        self.calls.append({"path": str(path), "expected_slots": expected_slots,
                           "filament_ids": filament_ids, "used": used,
                           "overrides": dict(settings.process_overrides),
                           "slicer": settings.slicer})
        if settings.slicer not in self.installed:  # what slice_check says without the binary
            return SliceReport(ok=False, slicer=settings.slicer, slicer_version="unknown",
                               printer_profile="Bambu Lab X2D 0.4 nozzle", process_profile="",
                               errors=[f"{settings.slicer} is not installed"],
                               expected_slots=expected_slots)
        fils = [SliceFilament(slot=s, grams=round(3.0 + s, 2),
                              filament_id=(filament_ids[s - 1] if filament_ids else None))
                for s in used]
        ok = self.fail_with is None
        return SliceReport(ok=ok, slicer=settings.slicer, slicer_version="fake",
                           printer_profile="Bambu Lab X2D 0.4 nozzle", process_profile="fake",
                           errors=list(self.fail_with or []), print_time_s=4321.0,
                           filaments=fils, total_grams=sum(f.grams for f in fils),
                           slots_used=len(used), expected_slots=expected_slots)


@pytest.fixture(autouse=True)
def fake_slicer(request, monkeypatch):
    """Tests never launch Bambu Studio unless marked real_slicer (RUN_SLICER=1)."""
    if "real_slicer" in request.keywords:
        yield None
        return
    import p2p_threemf
    import p2p_threemf.slicer

    fake = FakeSlicer()
    monkeypatch.setattr(p2p_threemf, "slice_check", fake)
    monkeypatch.setattr(p2p_threemf.slicer, "backend_available", fake.available)
    yield fake


def pytest_configure(config):
    for mark, (env, what) in OPT_IN.items():
        config.addinivalue_line("markers", f"{mark}: {what}; opt in with {env}=1")


def pytest_collection_modifyitems(config, items):
    for mark, (env, _) in OPT_IN.items():
        if os.environ.get(env) == "1":
            continue
        skip = pytest.mark.skip(reason=f"opt-in test; set {env}=1 to run")
        for item in items:
            if mark in item.keywords:
                item.add_marker(skip)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    import shutil

    shutil.copytree(REPO / "filaments", tmp_path / "filaments")  # the real library
    return Settings(repo_root=tmp_path, jobs_dir=tmp_path / "jobs", mock_claude=True,
                    claude_backend="mock", vendor_wait_s=5.0, missed_webhook_s=600.0,
                    fake_meshy_delay_s=0.005)


def make_photo(path: Path, color=(200, 150, 90)) -> Path:
    im = Image.new("RGB", (320, 240), (230, 230, 230))
    ImageDraw.Draw(im).ellipse((100, 60, 220, 180), fill=color)
    im.save(path)
    return path


@pytest.fixture
def photos(tmp_path: Path) -> list[Path]:
    return [make_photo(tmp_path / "dog.jpg"), make_photo(tmp_path / "cat.jpg", (60, 60, 60))]


REPO = Path(__file__).resolve().parents[2]


def make_rt(settings: Settings, *, store=None, fake: FakeMeshy | None = None, claude=None,
            on_checkpoint=None) -> Runtime:
    store = store or LocalStore(settings.jobs_dir)
    fake = fake or FakeMeshy(delay_s=settings.fake_meshy_delay_s)  # replays recorded fixtures
    if fake.on_event is None:
        fake.on_event = lambda ev: store.insert_meshy_event(ev)
    return build_runtime(settings, store=store, fake=fake, claude=claude or MockClaude(),
                         on_checkpoint=on_checkpoint)


def run(coro):
    return asyncio.run(coro)


class SimulatedCrash(BaseException):
    """Like a killed process: not caught by stage error handling."""


class FailFirstComposite(MockClaude):
    """MockClaude that fails the product check of every candidate's first product, the free
    code composite s03 draws first (concept_<cid>.png), so the paid edit path runs."""

    async def check_concept(self, req):
        import re

        if req.product is not None and re.fullmatch(r"concept_r\d+c\d+\.png",
                                                    Path(req.product).name):
            self.check_script.insert(0, {"part"})
        return await super().check_concept(req)
