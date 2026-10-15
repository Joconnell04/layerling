"""Wiring: build the store, Meshy client (fake or live), Claude client, and orchestrator."""

from __future__ import annotations

from dataclasses import dataclass

from .clients.claude import ClaudeClient, make_claude
from .clients.meshy import MeshyClient
from .config import Settings
from .fakes.meshy import FakeMeshy
from .orchestrator import Orchestrator
from .stages.base import Services
from .store import JobStore, LocalStore


@dataclass
class Runtime:
    settings: Settings
    store: JobStore
    meshy: MeshyClient
    claude: ClaudeClient
    orchestrator: Orchestrator
    fake: FakeMeshy | None = None

    async def aclose(self) -> None:
        if self.fake is not None:
            await self.fake.drain()
        await self.meshy.aclose()
        closer = getattr(self.store, "aclose", None)
        if closer:
            await closer()


def make_store(settings: Settings) -> JobStore:
    if settings.use_supabase:
        from .clients.supabase import SupabaseStore

        return SupabaseStore(settings)
    return LocalStore(settings.jobs_dir)


def build_runtime(
    settings: Settings,
    *,
    store: JobStore | None = None,
    claude: ClaudeClient | None = None,
    fake: FakeMeshy | None = None,
    on_checkpoint=None,
) -> Runtime:
    """MESHY_MODE=test wires the fake Meshy server; live wires api.meshy.ai; replay wires the
    fixture store of paid outputs (fakes/replay.py): test-mode spend, no network."""
    store = store or make_store(settings)
    if settings.meshy_mode == "replay":
        from .fakes.replay import ReplayMeshy

        fake = fake or ReplayMeshy.from_settings(settings)
        if fake.on_event is None:
            fake.on_event = lambda ev: store.insert_meshy_event(ev)
        meshy = MeshyClient(api_key="msy_replay_no_network", test_mode=True,
                            transport=fake.transport())
    elif settings.meshy_mode == "test":
        fake = fake or FakeMeshy(delay_s=settings.fake_meshy_delay_s,
                                 webhooks=settings.fake_meshy_webhooks)
        if fake.on_event is None:
            fake.on_event = lambda ev: store.insert_meshy_event(ev)
        meshy = MeshyClient.from_settings(settings, transport=fake.transport())
    else:
        fake = None
        meshy = MeshyClient.from_settings(settings)
    claude = claude or make_claude(settings)
    svc = Services(store=store, meshy=meshy, claude=claude, settings=settings,
                   on_checkpoint=on_checkpoint)
    return Runtime(settings=settings, store=store, meshy=meshy, claude=claude,
                   orchestrator=Orchestrator(svc), fake=fake)
