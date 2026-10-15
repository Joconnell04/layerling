"""Regression suite: re-run each stored job at 0 credits and score it against the stored run.

Two starting points:
- `s06` (default): a scratch copy of the job with the stored s01 to s05 attempts; s06 onward
  run fresh (the job's profile decides which: s06, s07 on a thin slice; s06 to s10 on full).
- `s01`: a new job with the same photos, request and order form; every stage runs, gates get
  the stored run's own replies (answers, concept pick, mesh decision).

Meshy is always the replay backend (p2p_worker.fakes.replay). Claude is `recorded` (the stored
run's replies, for CI; no login), `login` (real calls through the Claude Code login; never an
API key) or `mock`. s09 is `replay` (the stored run's shipped paint, see paint_replay.py),
`stub` (s08's first pass) or `agent` (the real paint agent through the login; slow).

Nothing touches the stored job folders: every run works in its own scratch jobs folder.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from p2p_contracts import STAGES, JobStatus, StageId, StageStatus
from p2p_worker.config import Settings
from p2p_worker.fakes.replay import ReplayMeshy
from p2p_worker.runtime import Runtime, build_runtime
from p2p_worker.store import LocalStore
from p2p_worker.util import read_json, write_json

from .fixtures import UPSTREAM, job_entry
from .jobs import FIXTURES, REPO, StoredJob

log = logging.getLogger("p2p_evals")


@dataclass
class Options:
    start: str = "s06"  # s06 | s01
    claude: str = "recorded"  # recorded | login | mock
    paint: str = "replay"  # replay | stub | agent
    work: Path = field(default_factory=lambda: REPO / "evals" / "_work")
    fixtures: Path = FIXTURES
    max_steps: int = 60


# ---------------------------------------------------------------------------- seeding


def _files(opt: Options, sj: StoredJob) -> Path:
    return opt.fixtures / "store" / sj.job_id / "files"


def _copy_tree(src: Path, dst: Path) -> None:
    for p in src.rglob("*"):
        if p.is_file():
            q = dst / p.relative_to(src)
            q.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, q)


def seed_from_s06(files: Path, job_dir: Path) -> None:
    """The stored job up to s05: job.json with s06 onward removed, photos and request, the
    accepted s01 to s05 attempts. The rerun's s06 onward start at attempt 1."""
    job = read_json(files / "job.json")
    for name in ("job_row.json",):
        if (files / name).is_file():
            shutil.copyfile(files / name, job_dir / name)
    _copy_tree(files / "input", job_dir / "input")
    attempts = job.get("attempts") or {}
    for stage in UPSTREAM:
        n = attempts.get(stage)
        if n and (files / stage / f"a{n}").is_dir():
            _copy_tree(files / stage / f"a{n}", job_dir / stage / f"a{n}")
    job["attempts"] = {s: n for s, n in attempts.items() if s in UPSTREAM}
    job.update(status="queued", stage=StageId.S06_PRINTABILITY.value, result=None,
               pause_reason=None)
    write_json(job_dir / "job.json", job)
    row = read_json(job_dir / "job_row.json", {})
    row.update(status="queued", stage=StageId.S06_PRINTABILITY.value, result=None,
               pause_reason=None, review_reason=None)
    write_json(job_dir / "job_row.json", row)


def seed_from_s01(rt: Runtime, files: Path, job_id: str) -> None:
    """A new job (same id, scratch folder) from the stored photos, request and order form."""
    from p2p_contracts.job import PrintSettings

    job = read_json(files / "job.json")
    req = read_json(files / "input" / "request.json") or {}
    photos = [files / p["path"] for p in job.get("photos") or []]
    rt.orchestrator.create_job(
        job["prompt"], photos, product_type=job["product_type"],
        target_size_mm=req.get("target_size_mm", job.get("target_size_mm")),
        addons=req.get("addons") or [], profile=job.get("profile") or "full",
        job_id=job_id, form=req.get("form"))
    jdir = rt.store.job_dir(job_id)
    shutil.copyfile(files / "input" / "request.json", jdir / "input" / "request.json")
    rec = rt.store.load_job(job_id)
    rec.settings = PrintSettings.model_validate(job.get("settings") or {})
    rt.store.save_job(rec)


# ---------------------------------------------------------------------------- gates


def recorded_replies(files: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """(stage, gate step key) -> the user reply the stored run consumed there."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for p in sorted(files.glob("s0*/a*/manifest.json")):
        m = read_json(p) or {}
        for k, v in ((m.get("data") or {}).get("steps") or {}).items():
            if k.startswith("gate:") and isinstance(v, dict) and isinstance(v.get("reply"), dict):
                out[(m["stage"], k)] = v["reply"]
    return out


@dataclass
class ReplayResponder:
    """Plays the stored run's customer: the same reply at the same gate."""

    replies: dict[tuple[str, str], dict[str, Any]]
    log: list[str] = field(default_factory=list)
    diverged: list[str] = field(default_factory=list)
    stuck: str | None = None

    def respond(self, rt: Runtime, job_id: str) -> bool:
        store = rt.store
        job = store.load_job(job_id)
        stage = job.stage
        m = store.load_manifest(job_id, stage, job.attempts[stage]) if stage else None
        if m is None or m.status != StageStatus.WAITING_USER or m.gate is None:
            return False
        steps = m.data.get("steps") or {}
        open_keys = [k for k, v in steps.items()
                     if k.startswith("gate:") and isinstance(v, dict) and "reply" not in v]
        if not open_keys:
            return False
        key = open_keys[-1]
        rec = self.replies.get((stage.value, key))
        if rec is None:
            self.stuck = f"{stage.value} {key}: the stored run has no reply for this gate"
            return False
        payload = self._adapt(rt, job_id, rec)
        store.post_message(job_id, "user", rec["kind"], payload, stage=stage,
                           reply_to=steps[key].get("message_id"))
        self.log.append(f"{stage.value} {key}: {rec['kind']} {json.dumps(payload)[:120]}")
        return True

    def _latest(self, rt: Runtime, job_id: str, kind: str) -> dict[str, Any]:
        rows = rt.store.list_messages(job_id, role="assistant", kinds={kind})
        return rows[-1].payload if rows else {}

    def _adapt(self, rt: Runtime, job_id: str, rec: dict[str, Any]) -> dict[str, Any]:
        payload = dict(rec.get("payload") or {})
        if rec["kind"] == "answers":
            qs = {q["id"]: q for q in self._latest(rt, job_id, "questions").get("questions", [])}
            answers = []
            for a in payload.get("answers", []):
                q = qs.get(a["question_id"])
                opts = {o["id"] for o in (q or {}).get("options", [])}
                if q is None or not set(a.get("option_ids") or []) <= opts:
                    self.diverged.append(f"answer {a['question_id']} not asked this time")
                    continue
                answers.append(a)
            for qid, q in qs.items():
                if not any(a["question_id"] == qid for a in answers):
                    self.diverged.append(f"question {qid} is new; first option")
                    answers.append({"question_id": qid, "option_ids": [q["options"][0]["id"]]})
            payload = {"answers": answers}
        elif rec["kind"] == "concept_pick":
            cands = [c["id"] for c in self._latest(rt, job_id, "concepts").get("candidates", [])]
            if payload.get("candidate_id") not in cands and cands:
                self.diverged.append(f"concept {payload.get('candidate_id')} not offered; "
                                     f"picked {cands[-1]}")
                payload["candidate_id"] = cands[-1]
        return payload


# ---------------------------------------------------------------------------- one job


@contextlib.contextmanager
def s09_runner(runner):
    from p2p_worker.stages import RUNNERS

    old = RUNNERS[StageId.S09_PAINT]
    if runner is not None:
        RUNNERS[StageId.S09_PAINT] = runner
    try:
        yield
    finally:
        RUNNERS[StageId.S09_PAINT] = old


def suite_settings(jobs_dir: Path, opt: Options) -> Settings:
    from p2p_worker.config import _default_cli

    return Settings(
        repo_root=REPO, jobs_dir=jobs_dir, meshy_mode="replay",
        claude_backend="claude_code" if opt.claude == "login" else "mock",
        mock_claude=opt.claude == "mock", claude_cli=_default_cli(),
        paint_agent="agent" if opt.paint == "agent" else "stub",
        sam3_backend="mock", offload="thread", vendor_wait_s=120.0,
        fake_meshy_delay_s=0.01, worker_id="replay-suite",
    )  # secrets stay None: no API key, no Supabase, no live Meshy key


def make_claude(opt: Options, sj: StoredJob, settings: Settings):
    from p2p_worker.clients.claude import MockClaude, make_claude

    if opt.claude == "recorded":
        from .recorded_claude import RecordedClaude

        return RecordedClaude.from_store(opt.fixtures / "store", [sj.job_id])
    if opt.claude == "mock":
        return MockClaude()
    return make_claude(settings)  # the Claude Code login; ANTHROPIC_* keys stripped


async def drive(rt: Runtime, job_id: str, responder: ReplayResponder | None,
                max_steps: int) -> JobStatus:
    status = JobStatus.QUEUED
    for _ in range(max_steps):
        status = await rt.orchestrator.advance(job_id)
        if status == JobStatus.WAITING_USER:
            if responder is None or not responder.respond(rt, job_id):
                return status
            continue
        if status == JobStatus.RUNNING:
            continue
        return status
    return status


async def run_from_s06(rt: Runtime, job_id: str) -> JobStatus:
    """Fresh attempts of s06 onward (the job's profile says which), whatever the current
    profile would do upstream: the stored s01 to s05 stand as they ran (an old thin slice has
    s04 skipped; the profile now runs it, and there is no recording for it)."""
    from p2p_contracts.stages import PROFILE_FULL, PROFILE_THIN_SLICE

    job = rt.store.load_job(job_id)
    profile = PROFILE_FULL if job.profile == "full" else PROFILE_THIN_SLICE
    order = [s.id for s in STAGES]
    stages = [s for s in profile if order.index(s) >= order.index(StageId.S06_PRINTABILITY)]
    await rt.orchestrator.run_stages(job_id, stages, allow_meshy=True)
    return rt.store.load_job(job_id).status


async def run_job(sj: StoredJob, opt: Options) -> dict[str, Any]:
    from .paint_replay import ReplayPaint
    from .score import score_job

    job_entry(sj.job_id, opt.fixtures)  # fails loudly when the store lacks the job
    files = _files(opt, sj)
    root = work_root(opt, sj)
    if root.exists():
        shutil.rmtree(root)
    jobs_dir = root / "jobs"
    job_dir = jobs_dir / sj.job_id
    job_dir.mkdir(parents=True)
    settings = suite_settings(jobs_dir, opt)
    replay = ReplayMeshy(opt.fixtures)
    claude = make_claude(opt, sj, settings)
    store = LocalStore(jobs_dir)
    if opt.start == "s06":
        seed_from_s06(files, job_dir)
    rt = build_runtime(settings, store=store, claude=claude, fake=replay)
    if opt.start == "s01":
        seed_from_s01(rt, files, sj.job_id)
    responder = ReplayResponder(recorded_replies(files)) if opt.start == "s01" else None
    runner = {"replay": ReplayPaint(files), "agent": None}.get(opt.paint)
    if opt.paint == "stub":
        from p2p_worker.stages.s09_stub import PaintStub

        runner = PaintStub()
    t0 = time.monotonic()
    error = None
    try:
        with s09_runner(runner):
            if opt.start == "s06":
                status = await run_from_s06(rt, sj.job_id)
            else:
                status = await drive(rt, sj.job_id, responder, opt.max_steps)
    except Exception as e:
        log.exception("replay of %s crashed", sj.label)
        status, error = None, f"{type(e).__name__}: {e}"
    wall = time.monotonic() - t0
    await rt.aclose()
    return score_job(sj, opt, job_dir, files, status, wall, replay, claude, responder, error)


# ---------------------------------------------------------------------------- the suite


def _setup_logs(work: Path) -> Path:
    from p2p_worker.logs import setup_logging

    work.mkdir(parents=True, exist_ok=True)
    s = Settings(repo_root=REPO, jobs_dir=work)
    return setup_logging(s, work / "logs", console=False)


def work_root(opt: Options, sj: StoredJob) -> Path:
    """The scratch folder of one job's replay (kept after the run, for `report`)."""
    return opt.work / f"{sj.label}-{report_name(opt).removeprefix('replay-')}"


def report_name(opt: Options) -> str:
    """replay-from-s06, or with the Claude and paint modes when they are not the defaults."""
    return (f"replay-from-{opt.start}" + ("" if opt.claude == "recorded" else f"-{opt.claude}")
            + ("" if opt.paint == "replay" else f"-paint-{opt.paint}"))


def run_suite(jobs: list[StoredJob], opt: Options, out_dir: Path, say=print) -> dict[str, Any]:
    from .score import write_report

    _setup_logs(opt.work)
    started = time.time()
    results: list[dict[str, Any]] = []
    name = report_name(opt)
    for sj in jobs:
        say(f"{sj.label}: from {opt.start}, claude {opt.claude}, paint {opt.paint} ...")
        res = asyncio.run(run_job(sj, opt))
        results.append(res)
        s06 = res["stages"].get("s06_printability", {}).get("wall_s")
        say(f"{sj.label}: {res['status']} in {res['wall_s']:.0f} s; gate "
            f"{res['gate'].get('summary')}; s06 {s06} s")
        write_report(results, opt, out_dir, name, started, partial=True)  # checkpoint
    return write_report(results, opt, out_dir, name, started, partial=False)

