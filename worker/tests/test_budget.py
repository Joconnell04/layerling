"""Budget guard: job caps in every mode, build caps on live spend only."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from p2p_contracts import JobRecord, SpendEvent
from p2p_worker.budget import BudgetGuard
from p2p_worker.errors import BudgetExceeded
from p2p_worker.store import LocalStore


def job() -> JobRecord:
    return JobRecord(id="j1", created_at=datetime.now(UTC), prompt="x")


def live_spend(store, credits: int, usd: float) -> None:
    store.record_spend(SpendEvent(at=datetime.now(UTC), vendor="meshy", kind="image-to-image",
                                  credits=credits, usd=usd, test_mode=False))


def test_job_caps(settings):
    store = LocalStore(settings.jobs_dir)
    g = BudgetGuard(store, settings)
    j = job()
    j.budget.spent_credits = 380
    g.check(j, vendor="meshy", credits=20, usd=0.4, live=False, what="ok")
    with pytest.raises(BudgetExceeded, match="Job cap 400 credits"):
        g.check(j, vendor="meshy", credits=21, usd=0.42, live=False, what="concept")
    j.budget.spent_usd = 9.9
    with pytest.raises(BudgetExceeded, match=r"Job cap \$10.00"):
        g.check(j, vendor="anthropic", credits=0, usd=0.2, live=False, what="intake")


def test_meshy_build_credit_cap_reads_the_ledger(settings):
    store = LocalStore(settings.jobs_dir)
    g = BudgetGuard(store, settings.with_(meshy_build_credit_cap=500))
    live_spend(store, 480, 9.6)
    store.record_spend(SpendEvent(at=datetime.now(UTC), vendor="meshy", kind="x",
                                  credits=999, usd=20, test_mode=True))  # fake runs don't count
    assert store.build_totals() == {"usd": 9.6, "meshy_credits": 480}
    g.check(job(), vendor="meshy", credits=35, usd=0.7, live=False, what="test mode")
    g.check(job(), vendor="meshy", credits=20, usd=0.4, live=True, what="fits")
    with pytest.raises(BudgetExceeded, match="Meshy build cap 500"):
        g.check(job(), vendor="meshy", credits=35, usd=0.7, live=True, what="3D model")


def test_build_usd_ceiling(settings):
    store = LocalStore(settings.jobs_dir)
    g = BudgetGuard(store, settings.with_(build_ceiling_usd=10.0))
    live_spend(store, 0, 9.95)
    with pytest.raises(BudgetExceeded, match="Build ceiling"):
        g.check(job(), vendor="anthropic", credits=0, usd=0.1, live=True, what="api call")


def test_record_updates_job_ledger_and_row(settings):
    store = LocalStore(settings.jobs_dir)
    g = BudgetGuard(store, settings)
    j = job()
    store.save_job(j)
    g.record(j, stage=None, vendor="meshy", kind="image-to-image", credits=12, usd=0.24,
             ref="t1", test_mode=True)
    g.record(j, stage=None, vendor="meshy", kind="refund:image-to-image", credits=-12,
             usd=-0.24, ref="t1", test_mode=True)
    assert store.load_job("j1").budget.spent_credits == 0
    assert (store.job_dir("j1") / "ledger.jsonl").read_text().count("\n") == 2
    assert store.job_row("j1")["spent_credits"] == 0
