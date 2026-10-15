"""Opt-in (RUN_SUPABASE=1): one TEST job through `serve` on the real Supabase project.

Meshy stays on the fake server and Claude on the mock; every row and object the run creates is
deleted at the end. Same code as `uv run python -m p2p_worker smoke-supabase`.
"""

from __future__ import annotations

import pytest
from p2p_worker.config import Settings
from p2p_worker.smoke import run_smoke


@pytest.mark.supabase
def test_serve_against_real_supabase():
    settings = Settings.load()
    if not settings.use_supabase:
        pytest.skip("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY not set")
    rep = run_smoke(settings, timeout_s=240)
    failed = {k: rep.details.get(k) for k, v in rep.checks.items() if not v}
    assert rep.ok, (failed, rep.details.get("serve_tail"))
    assert rep.cleanup.get("clean"), rep.cleanup
