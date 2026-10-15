"""Budget guard: checked before every paid call, recorded after.

Caps, in order:
1. Per job: JobRecord.budget cap_usd ($10) and cap_credits (400). Counted in every mode, so test
   runs exercise the same pauses a live job would hit. Before a paid Meshy call the job's spend
   is read back from its own spend rows (ledger.jsonl, the local mirror of `spend`), and a
   JobRecord that shows less (a save that did not land) is raised to match.
2. Build ceiling (live spend only): BUILD_SPEND_CEILING_USD minus real spend so far.
3. Meshy build credit cap (live Meshy only): MESHY_BUILD_CREDIT_CAP minus live credits spent.
   Jackson's live key also has a Meshy-side 500 credit monthly limit.

"Spent so far" for 2 and 3 is the largest of what the sources say (`build_used`): the spend
table (SupabaseStore), and this machine's jobs/_worker/spend.jsonl plus the fixture capture
ledger data/ledger.jsonl. live2 run: the spend table read 491 live credits and the local file
463; the 28 missing were the fixture captures, which reach only the table and data/ledger.jsonl
(SOURCE: both read after the run). A spend table that cannot be read stops the call
(SupabaseStore.build_totals raises) instead of falling back to the smaller local number.

Crossing any cap raises BudgetExceeded; the stage goes PAUSED with pause_reason.
Every spend lands in ledger.jsonl (job), the spend table (store), and job.budget.
"""

from __future__ import annotations

from p2p_contracts import JobRecord, SpendEvent, StageId
from p2p_contracts.job import USD_PER_MESHY_CREDIT

from .errors import BudgetExceeded
from .logs import log_event
from .store import JobStore, LocalStore
from .util import read_jsonl, utcnow


def meshy_usd(credits: int) -> float:
    return round(credits * USD_PER_MESHY_CREDIT, 6)


def live_totals(rows: list[dict]) -> dict[str, float]:
    """Real (non test-mode) usd and Meshy credits of spend rows; rows with no vendor (the
    fixture capture ledger) are Meshy."""
    usd, credits = 0.0, 0
    for e in rows:
        if e.get("test_mode"):
            continue
        usd += float(e.get("usd") or 0)
        if e.get("vendor", "meshy") == "meshy":
            credits += int(e.get("credits") or 0)
    return {"usd": round(usd, 6), "meshy_credits": credits}


class BudgetGuard:
    def __init__(self, store: JobStore, settings):
        self.store = store
        self.settings = settings

    # ------------------------------------------------------------------ job spend
    def job_spent(self, job_id: str) -> dict[str, float]:
        """The job's spend from its spend rows (every mode: the job caps count test runs)."""
        rows = read_jsonl(self.store.job_dir(job_id) / "ledger.jsonl")
        return {"usd": round(sum(float(r.get("usd") or 0) for r in rows), 6),
                "credits": sum(int(r.get("credits") or 0) for r in rows
                               if r.get("vendor") == "meshy")}

    def sync_job(self, job: JobRecord) -> dict[str, float] | None:
        """Raise job.budget to what its spend rows say when it shows less. Returns the
        correction, or None when they agree."""
        spent = self.job_spent(job.id)
        b = job.budget
        if spent["credits"] <= b.spent_credits and spent["usd"] <= b.spent_usd + 1e-6:
            return None
        fix = {"spent_credits": max(b.spent_credits, int(spent["credits"])),
               "spent_usd": round(max(b.spent_usd, spent["usd"]), 6)}
        log_event("budget_synced", job_id=job.id, before={"spent_credits": b.spent_credits,
                                                          "spent_usd": b.spent_usd}, after=fix)
        b.spent_credits, b.spent_usd = fix["spent_credits"], fix["spent_usd"]
        self.store.save_job(job)
        self.store.update_job_row(job.id, **fix)
        return fix

    # ------------------------------------------------------------------ build spend
    def build_used(self) -> dict[str, float]:
        """Live spend across the build: the larger of the spend table and this machine's
        records (worker spend.jsonl plus the fixture capture ledger)."""
        table = self.store.build_totals()  # SupabaseStore: the spend table; raises if unreadable
        local = (LocalStore.build_totals(self.store) if isinstance(self.store, LocalStore)
                 else table)
        root = getattr(self.settings, "repo_root", None)
        capture = (live_totals(read_jsonl(root / "data" / "ledger.jsonl")) if root
                   else {"usd": 0.0, "meshy_credits": 0})
        mine = {"usd": round(local["usd"] + capture["usd"], 6),
                "meshy_credits": int(local["meshy_credits"] + capture["meshy_credits"])}
        return {"usd": max(float(table["usd"]), mine["usd"]),
                "meshy_credits": max(int(table["meshy_credits"]), mine["meshy_credits"])}

    def check(self, job: JobRecord, *, vendor: str, credits: int, usd: float, live: bool,
              what: str) -> None:
        b = job.budget
        if b.spent_usd + usd > b.cap_usd + 1e-9:
            raise BudgetExceeded(
                f"Job cap ${b.cap_usd:.2f} reached: spent ${b.spent_usd:.2f}, {what} needs "
                f"${usd:.2f}",
                {"cap": "job_usd", "spent_usd": b.spent_usd, "need_usd": usd},
            )
        if b.spent_credits + credits > b.cap_credits:
            raise BudgetExceeded(
                f"Job cap {b.cap_credits} credits reached: spent {b.spent_credits}, {what} needs "
                f"{credits}",
                {"cap": "job_credits", "spent_credits": b.spent_credits, "need_credits": credits},
            )
        if not live:
            return
        totals = self.build_used()
        left = self.settings.build_ceiling_usd - totals["usd"]
        if usd > left + 1e-9:
            raise BudgetExceeded(
                f"Build ceiling ${self.settings.build_ceiling_usd:.2f} reached: "
                f"${totals['usd']:.2f} spent, {what} needs ${usd:.2f}",
                {"cap": "build_usd", "spent_usd": totals["usd"], "need_usd": usd},
            )
        if vendor == "meshy":
            cap = self.settings.meshy_build_credit_cap
            used = int(totals["meshy_credits"])
            if used + credits > cap:
                raise BudgetExceeded(
                    f"Meshy build cap {cap} credits reached: {used} used, {what} needs {credits}",
                    {"cap": "build_meshy_credits", "used": used, "need_credits": credits},
                )

    def record(self, job: JobRecord, *, stage: StageId | None, vendor: str, kind: str,
               credits: int = 0, usd: float = 0.0, ref: str | None = None,
               test_mode: bool, usd_list: float | None = None,
               billing: str | None = None) -> SpendEvent:
        """One spend row. billing: api, subscription, credits, gpu_seconds or free (the spend
        table's check); usd_list is the list-price value (Claude on the subscription bills
        usd 0 with its list price here; Meshy's list value is its credits at the plan rate)."""
        ev = SpendEvent(at=utcnow(), job_id=job.id, stage=stage, vendor=vendor, kind=kind,
                        usd=round(usd, 6), credits=credits, ref=ref, test_mode=test_mode)
        # Booked already when the worker died after booking this task (or its refund) and
        # before the manifest recorded it, and the replay adopted the task: once only.
        if not (vendor == "meshy" and ref and self.store.spend_recorded(job.id, vendor, kind,
                                                                         ref)):
            if billing is None:
                billing = {"meshy": "credits", "runpod": "gpu_seconds"}.get(vendor, "api")
            self.store.record_spend(ev, usd_list=round(usd if usd_list is None else usd_list, 6),
                                    billing=billing)
        # The job's spend is its ledger (store.job_spend), not a running sum, so a death
        # between the ledger row and this save neither loses nor doubles it.
        job.budget.spent_usd, job.budget.spent_credits = self.store.job_spend(job.id)
        self.store.save_job(job)
        self.store.update_job_row(job.id, spent_usd=job.budget.spent_usd,
                                  spent_credits=job.budget.spent_credits)
        return ev
