"""Control-flow exceptions a stage raises; the orchestrator maps each to a stage status."""

from __future__ import annotations

from typing import Any, Literal


class WorkerError(Exception):
    pass


class BudgetExceeded(WorkerError):
    """A paid call would cross a job cap or the build ceiling. Stage -> PAUSED."""

    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


class StagePaused(WorkerError):
    """A non-budget cap was hit (tweak rounds, mesh rounds, unknown vendor state). Stage -> PAUSED."""

    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


class GateWaiting(WorkerError):
    """A user gate is open and no reply has arrived. Stage -> WAITING_USER."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class VendorWaiting(WorkerError):
    """A Meshy task is in flight and its webhook has not arrived. Stage -> WAITING_VENDOR."""

    def __init__(self, task_ids: list[str]):
        super().__init__(", ".join(task_ids))
        self.task_ids = task_ids


class VendorError(WorkerError):
    """A vendor call or task failed. Stage -> FAILED (retryable or not)."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "vendor_failed",
        retryable: bool = True,
        status_code: int | None = None,
        detail: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.detail = detail or {}


class ForbiddenRequest(WorkerError):
    """A request the worker must never send (DELETE to Meshy, live key without approval)."""


ReviewKind = Literal["gate", "estimate", "fidelity"]
# StageError.code per review kind. A fidelity review is not a slicer failure (live2 sombrero:
# the paint check failed, the Bambu gate passed, and the review said "failed the slicer gate").
# The contract has no review code yet (worker README contract change request 8); "validation"
# stands for "the output failed a check", and detail.review carries the kind.
REVIEW_CODES: dict[str, str] = {"gate": "slicer_failed", "estimate": "slicer_failed",
                                "fidelity": "validation"}


class NeedsReview(WorkerError):
    """Something only Jackson can clear. Stage -> PAUSED, job -> REVIEW. kind:
    - gate: the slicer gate failed. Nothing ships; approve is refused.
    - estimate: only p2p_slicer checked the file; approve is refused until the Bambu gate runs.
    - fidelity: the gate passed (or runs later, at s10) but a fidelity check failed or the
      confidence is low. Approve accepts the stage as it is and the job goes on; the file
      still ships only through the s10 gate."""

    def __init__(self, reason: str, detail: dict[str, Any] | None = None, *,
                 kind: ReviewKind = "gate"):
        super().__init__(reason)
        self.reason = reason
        self.kind = kind
        self.detail = {**(detail or {}), "review": kind}


# Network blips between the worker and Supabase or a vendor (not a vendor verdict). A stage that
# fails on one must not start over and pay again for tasks it already created.
TRANSIENT_NAMES = ("RemoteProtocolError", "ConnectError", "ConnectTimeout", "ReadTimeout",
                   "WriteTimeout", "PoolTimeout", "ReadError", "WriteError", "NetworkError",
                   "ProtocolError", "TimeoutException")


def is_transient(e: BaseException | None) -> bool:
    """An httpx transport error, directly or as the cause of a store error, or a 502/503/504."""
    seen = 0
    while e is not None and seen < 5:
        if type(e).__name__ in TRANSIENT_NAMES:
            return True
        if any(f"returned {c}" in str(e) for c in (502, 503, 504)):
            return True
        e, seen = (e.__cause__ or e.__context__), seen + 1
    return False


def review_kind(m) -> str | None:
    """What a PAUSED stage manifest waits on in REVIEW: gate, estimate or fidelity; None for a
    pause that is not a review (budget, retry limit). Manifests written before detail.review
    existed: an s09 paint review whose gate passed is fidelity (the live2 sombrero), a
    p2p_slicer-only pass is estimate, any other slicer_failed is gate."""
    err = getattr(m, "error", None)
    if err is None:
        return None
    detail = err.detail or {}
    if detail.get("review") in ("gate", "estimate", "fidelity"):
        return detail["review"]
    if err.code != "slicer_failed":
        return None
    if detail.get("gate") == "estimate":
        return "estimate"
    stage = getattr(getattr(m, "stage", None), "value", getattr(m, "stage", None))
    if stage == "s09_paint" and detail.get("slice_ok") is True:
        return "fidelity"
    return "gate"


def is_transient_message(message: str | None) -> bool:
    """The same test on a StageError message written before `detail.transient` existed
    ("SupabaseError: Supabase GET /rest/v1/meshy_events: RemoteProtocolError")."""
    m = message or ""
    return any(n in m for n in TRANSIENT_NAMES) or any(f"returned {c}" in m
                                                       for c in (502, 503, 504))
