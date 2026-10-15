"""Claude stub for CI replays: every Claude call answered with the stored run's own reply.

RecordedClaude is the real Claude Code client (ClaudeCodeClaude) with the `claude -p`
subprocess swapped for a lookup in the stored job's trace bodies, so everything around the
model call (request building, schema validation, finalize_intake, finalize_check,
views_by_index) runs for real. A call is matched within the same job and stage by purpose:
the recorded call with the same prompt text first, then the one with the same images
(candidate r1c2's check reads figure_r1c2.png and concept_r1c2.png), then the next unused one
in recorded order. A call with no recording fails the stage loudly (not retryable), like a
Meshy replay miss. A concept checklist item the recording never answered (the checklist grew
after the run) passes and is counted as filled. Nothing is sent anywhere; no login is used.
"""

from __future__ import annotations

import contextvars
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from p2p_worker.clients.claude import INTAKE_MAX_TOKENS, ClaudeCodeClaude
from p2p_worker.errors import VendorError

_CALL: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "p2p_recorded_claude_call", default=None)


class ClaudeReplayMiss(VendorError):
    def __init__(self, message: str):
        super().__init__(f"Claude replay miss: {message}", code="validation", retryable=False)


@dataclass
class RecordedCall:
    job_id: str
    stage: str
    attempt: int
    seq: int
    purpose: str
    model: str | None
    text: str
    images: tuple[str, ...]
    reply_text: str
    reply_json: Any


@dataclass
class ClaudeAnswer:
    stage: str | None
    purpose: str
    match: str  # text, images, order, miss
    recorded: str | None = None  # stage/aN/seq of the recording used

    def to_json(self) -> dict[str, Any]:
        return {"stage": self.stage, "purpose": self.purpose, "match": self.match,
                "recorded": self.recorded}


def load_calls(files_dir: Path, job_id: str) -> list[RecordedCall]:
    """Claude calls with a parsed reply, from a stored job's trace bodies."""
    out = []
    for p in sorted(files_dir.glob("s*/a*/trace/*-claude.json")):
        try:
            b = json.loads(p.read_text())
        except ValueError:
            continue
        o = b.get("output") or {}
        if o.get("json") is None:
            continue  # a reconstructed paint turn: nothing to replay
        inp = b.get("input") or {}
        out.append(RecordedCall(
            job_id=job_id, stage=b.get("stage") or p.parts[-4], attempt=int(b.get("attempt") or 0),
            seq=int(b.get("seq") or 0), purpose=b.get("purpose") or "", model=b.get("model"),
            text=inp.get("text") or "", images=tuple(Path(i).name for i in inp.get("images") or []),
            reply_text=o.get("text") or "", reply_json=o["json"]))
    out.sort(key=lambda c: (c.stage, c.attempt, c.seq))
    return out


class RecordedClaude(ClaudeCodeClaude):
    """ClaudeCodeClaude whose subprocess is the stored run. calls: {source job id: calls}."""

    billing = "free"

    def __init__(self, calls: dict[str, list[RecordedCall]],
                 job_map: dict[str, str] | None = None):
        super().__init__(cli="recorded-claude", runner=self._runner)
        self.calls = calls
        self.job_map = dict(job_map or {})
        self.answers: list[ClaudeAnswer] = []
        self.filled = 0  # concept check items with no recorded answer (passed, marked)
        self._used: set[tuple[str, str, int, int]] = set()

    @classmethod
    def from_store(cls, store: Path, job_ids: list[str],
                   job_map: dict[str, str] | None = None) -> RecordedClaude:
        return cls({j: load_calls(store / j / "files", j) for j in job_ids}, job_map)

    async def _ask(self, *, purpose: str, system: str, text: str, images: list[Path],
                   output, model: str | None = None, max_tokens: int = INTAKE_MAX_TOKENS):
        token = _CALL.set({"purpose": purpose, "text": text, "model": model,
                           "images": tuple(Path(i).name for i in images)})
        try:
            return await super()._ask(purpose=purpose, system=system, text=text, images=images,
                                      output=output, model=model, max_tokens=max_tokens)
        finally:
            _CALL.reset(token)

    async def check_concept(self, req):
        """The recorded verdicts. A checklist item the stored run was never asked (the checklist
        grew after it ran: pose, no extras) has no recorded answer; it passes here, marked
        `stub: no recorded answer`, and is counted in the summary as filled."""
        check, call = await super().check_concept(req)
        items, filled = [], 0
        for i in check.items:
            if not i.passed and i.note == "not answered":
                i = i.model_copy(update={"passed": True, "note": "stub: no recorded answer"})
                filled += 1
            items.append(i)
        if filled:
            self.filled += filled
            check = check.model_copy(update={"items": items,
                                             "passed": all(i.passed for i in items)})
        return check, call

    def pick(self, job_id: str | None, stage: str | None, call: dict[str, Any]
             ) -> tuple[RecordedCall, str]:
        if job_id:  # outside a stage (no job bound) any stored job's calls may answer
            recs = self.calls.get(self.job_map.get(job_id, job_id), [])
        else:
            recs = [c for cs in self.calls.values() for c in cs]
        pool = [c for c in recs
                if c.purpose == call["purpose"] and (stage is None or c.stage == stage)
                and (c.stage, c.purpose, c.attempt, c.seq) not in self._used]
        for how, ok in (("text", lambda c: c.text == call["text"]),
                        ("images", lambda c: call["images"] and c.images == call["images"]),
                        ("order", lambda c: True)):
            hit = next((c for c in pool if ok(c)), None)
            if hit is not None:
                self._used.add((hit.stage, hit.purpose, hit.attempt, hit.seq))
                return hit, how
        self.answers.append(ClaudeAnswer(stage, call["purpose"], "miss"))
        raise ClaudeReplayMiss(f"no recorded {call['purpose']} call left for job "
                               f"{(job_id or '-')[:8]} stage {stage or '-'}")

    async def _runner(self, args: list[str], stdin: bytes, env: dict[str, str],
                      timeout: float) -> tuple[int, bytes, bytes]:
        from p2p_worker import trace

        call = _CALL.get()
        if call is None:
            raise ClaudeReplayMiss("a Claude call outside RecordedClaude._ask")
        if "picked" in call:  # the repair round of the same call gets the same reply
            rec, how = call["picked"]
        else:
            ctx = trace.current()
            rec, how = self.pick(ctx.job.id if ctx else None,
                                 ctx.stage.value if ctx else None, call)
            call["picked"] = (rec, how)
            self.answers.append(ClaudeAnswer(rec.stage, rec.purpose, how,
                                             f"{rec.stage}/a{rec.attempt}/{rec.seq:02d}"))
        result = {"type": "result", "subtype": "success", "is_error": False,
                  "result": rec.reply_text or json.dumps(rec.reply_json),
                  "structured_output": rec.reply_json, "total_cost_usd": 0.0,
                  "session_id": f"recorded-{rec.stage}-{rec.attempt}-{rec.seq}",
                  "usage": {"input_tokens": 0, "output_tokens": 0}}
        return 0, (json.dumps(result) + "\n").encode(), b""

    def summary(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for a in self.answers:
            kinds[a.match] = kinds.get(a.match, 0) + 1
        return {"answered": sum(v for k, v in kinds.items() if k != "miss"),
                "by_match": kinds, "check_items_filled": self.filled,
                "calls": [a.to_json() for a in self.answers]}
