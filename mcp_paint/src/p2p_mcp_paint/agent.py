"""The s09 paint agent loop: Opus 5.5 through the Claude Agent SDK with the paint MCP server.

run_paint(job_id) does, in order:
1. Opens a new s09 attempt in-process (labels, split, initial checkpoint c000), simulates the
   baseline and runs the code checklist on it. Before-previews go to previews/.
2. Iterations (at most 8, effort medium): the agent edits by region id through the paint MCP
   server (a separate stdio process on the same attempt folder), judges simulate_print, and
   checkpoints once per iteration. The runner checks the checkpoint landed and nudges if not.
3. Sign-off (effort high, same session resumed): the agent judges the final simulated print and
   returns sim_matches_intent, confidence and a summary. A band-like region still printing as
   scattered pieces is then made one strip or given its surroundings' color
   (PaintSession.consolidate_bands), here and again before the write.
4. Final vs concept (concept_check): a fresh Opus 5.5 session at effort high compares the
   simulated print from all eight sides with the approved concept and the photos; add-on
   colors and region colors it names are fixed in code, other paint issues go back to the
   agent, and it judges again (at most concept_check.MAX_ROUNDS). Geometry issues (text, plate
   shape, loop) and whatever is still wrong go to review with the side-by-side renders.
5. The runner reloads the final state from disk, writes final.3mf through write_3mf, runs
   slice_check (the gate), renders the proof set (model, paint, print from the eight proof
   views, proof/final/), completes checklist.json (contracts Checklist) and manifest.json.
   Confidence = min(agent, code checklist, concept check); a failed gate, a failed concept
   check or confidence under REVIEW_BELOW sends the job to review.

Claude runs only through the Claude Code login: ANTHROPIC_API_KEY and friends are removed from
the environment the CLI inherits. Calls are recorded as usd=0 (subscription) with the CLI's
list-price estimate in usd_list.

Every paint turn and every concept check has a time limit (TURN_TIMEOUT_S, JUDGE_TIMEOUT_S):
a Claude call that never answers raises ClaudeTimeout and the run stops, instead of holding the
stage with no log line for as long as the worker lives.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from p2p_contracts.manifest import Decision, FileRef, Manifest, ModelCall
from p2p_contracts.mcp_tools import TOOLS, OpenJobIn, SimulatePrintIn, SliceCheckIn, Write3mfIn
from p2p_contracts.reports import Checklist, ChecklistItem

from . import prompts
from .checklist import summarize
from .session import PaintSession

MODEL = "claude-opus-5-5"
MAX_ITERATIONS = 8
REVIEW_BELOW = 0.7
SCRUB_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_TURNKEY", "ANTHROPIC_AUTH_TOKEN",
             "DISABLE_PROMPT_CACHING")
AGENT_TOOLS = [t for t in TOOLS if t != "open_job"]
# Tool results carry render sheets; the SDK's 1 MB default message buffer is too small for a
# textured eight-view sheet (the first 3D Frenchie run failed on it).
MAX_BUFFER = 64 * 1024 * 1024
# Time limits per Claude call. Nothing bounded them before (live2 catfish: s09 had no log line
# for 9 h; that was the Mac asleep, but a hung call would have looked the same and never ended).
# The longest paint turn on record is 965 s, catfish iteration 1 (SOURCE: live2 worker log);
# 45 min is ASSUMED headroom. A concept check takes about 2 min (SOURCE: same log); 20 min is
# ASSUMED. asyncio measures these on the monotonic clock, which on macOS stops while the
# machine sleeps, so a closed lid does not trip them.
TURN_TIMEOUT_S = 45 * 60.0
JUDGE_TIMEOUT_S = 20 * 60.0


class ClaudeTimeout(TimeoutError):
    """A Claude call in the paint run (a turn or a concept check) did not answer in time."""


async def bounded(aw, seconds: float, what: str):
    """await aw, or raise ClaudeTimeout after `seconds` (aw is cancelled)."""
    try:
        return await asyncio.wait_for(aw, seconds)
    except TimeoutError as e:
        raise ClaudeTimeout(f"{what}: no answer from Claude in {seconds:.0f} s") from e


@dataclass
class TurnResult:
    text: str
    tool_calls: list[str] = field(default_factory=list)
    model_call: ModelCall | None = None
    session_id: str | None = None


def last_json(text: str) -> dict:
    """The last {...} line of a reply (the protocol's verdict line)."""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip().strip("`")
        if line.startswith("{") and line.endswith("}"):
            with contextlib.suppress(json.JSONDecodeError):
                return json.loads(line)
    m = re.findall(r"\{[^{}]*\}", text or "")
    for chunk in reversed(m):
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(chunk)
    return {}


@contextlib.contextmanager
def scrubbed_env():
    """Remove API keys from os.environ while the Claude Code CLI is spawned (OAuth only)."""
    saved = {k: os.environ.pop(k) for k in SCRUB_ENV if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def _image_block(png: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(png).decode()}}


# ================================================================ drivers


class ClaudeDriver:
    """Opus through the Agent SDK; the paint server runs as a stdio MCP subprocess."""

    def __init__(self, job_id: str, attempt: int, jobs_dir: Path, *, model: str = MODEL,
                 sam3_backend: str | None = None, cwd: Path | None = None,
                 max_turns: int = 80, log: Callable[[str], None] = print):
        # absolute: the MCP server starts with cwd = jobs_dir, so a relative --jobs-dir (the
        # worker's P2P_JOBS_DIR=geometry/_out/...) would not find the job and give no tools
        self.job_id, self.attempt, self.jobs_dir = job_id, attempt, Path(jobs_dir).resolve()
        self.model, self.sam3, self.cwd = model, sam3_backend, cwd
        self.max_turns = max_turns
        self.log = log
        self.session_id: str | None = None
        self._client = None
        self._effort: str | None = None
        self._cost_seen = 0.0  # the CLI reports the session's cumulative cost on each result

    def _options(self, effort: str):
        from claude_agent_sdk import ClaudeAgentOptions

        args = ["-m", "p2p_mcp_paint", "--job", self.job_id, "--attempt", str(self.attempt),
                "--jobs-dir", str(self.jobs_dir)]
        if self.sam3:
            args += ["--sam3", self.sam3]
        server = {"type": "stdio", "command": sys.executable, "args": args,
                  "env": {"PYTHONUNBUFFERED": "1"}}
        return ClaudeAgentOptions(
            model=self.model, system_prompt=prompts.SYSTEM, effort=effort,
            mcp_servers={"paint": server}, strict_mcp_config=True, tools=[],
            allowed_tools=[f"mcp__paint__{t}" for t in AGENT_TOOLS],
            disallowed_tools=["mcp__paint__open_job"], setting_sources=[],
            permission_mode="default", max_turns=self.max_turns, resume=self.session_id,
            cwd=str(self.cwd or self.jobs_dir), env={"MCP_TOOL_TIMEOUT": "900000", "MCP_TIMEOUT": "240000"},
            stderr=lambda line: None, max_buffer_size=MAX_BUFFER,
        )

    async def _ensure(self, effort: str):
        from claude_agent_sdk import ClaudeSDKClient

        if self._client is not None and self._effort == effort:
            return self._client
        await self.close()
        with scrubbed_env():
            self._client = ClaudeSDKClient(self._options(effort))
            await self._client.connect()
        self._effort = effort
        return self._client

    async def turn(self, content: str | list, effort: str) -> TurnResult:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

        client = await self._ensure(effort)
        t0 = time.time()
        started = datetime.now(UTC)
        if isinstance(content, str):
            await client.query(content)
        else:
            async def stream():
                yield {"type": "user", "message": {"role": "user", "content": content},
                       "parent_tool_use_id": None, "session_id": self.session_id or "default"}
            await client.query(stream())
        texts, tools, result = [], [], None
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        texts.append(b.text)
                    elif isinstance(b, ToolUseBlock):
                        tools.append(b.name.removeprefix("mcp__paint__"))
                        self.log(f"  tool {tools[-1]} {json.dumps(b.input)[:160]}")
            elif isinstance(msg, ResultMessage):
                result = msg
        text = "\n".join(texts)
        mc = None
        if result is not None:
            self.session_id = result.session_id
            u = result.usage or {}
            mc = ModelCall(vendor="anthropic", model=self.model, purpose="paint", started_at=started,
                           duration_s=round(time.time() - t0, 2), effort=effort,
                           input_tokens=int(u.get("input_tokens") or 0),
                           output_tokens=int(u.get("output_tokens") or 0),
                           cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
                           cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0),
                           usd=0.0, usd_list=round(self._turn_cost(result.total_cost_usd), 4),
                           billing="subscription", request_id=result.session_id)
            if result.is_error:
                text += f"\n[agent error: {result.subtype} {result.errors}]"
        return TurnResult(text, tools, mc, self.session_id)

    def _turn_cost(self, total: float | None) -> float:
        total = float(total or 0.0)
        turn = total - self._cost_seen if total >= self._cost_seen else total
        self._cost_seen = total
        return max(turn, 0.0)

    async def close(self):
        if self._client is not None:
            with contextlib.suppress(Exception):  # after a timed-out turn too: never wait long
                await asyncio.wait_for(self._client.disconnect(), 60)
        self._client = None
        self._effort = None


class StubDriver:
    """A scripted agent for tests: no Claude. It edits through the in-process session with the
    same tools the model would call, and answers with the protocol's JSON lines."""

    def __init__(self, session_factory: Callable[[], PaintSession], plan=None,
                 confidence: float = 0.9):
        self.factory = session_factory
        self.plan = plan or self.default_plan
        self.confidence = confidence
        self.calls: list[tuple[str, str]] = []
        self.k = 0

    @staticmethod
    def default_plan(s: PaintSession, k: int) -> list[tuple[str, Any]]:
        from p2p_contracts.mcp_tools import CheckpointIn, DespeckleIn, SuggestPaletteIn

        if k == 1:
            return [("suggest_palette", SuggestPaletteIn(k=s.inp.settings.ams_slots, apply=True)),
                    ("simulate_print", SimulatePrintIn(render=False)),
                    ("checkpoint", CheckpointIn(note="palette with gray guard"))]
        return [("despeckle", DespeckleIn(min_area_mm2=1.0)),
                ("simulate_print", SimulatePrintIn(render=False)),
                ("checkpoint", CheckpointIn(note="despeckle 1 mm2"))]

    async def turn(self, content, effort: str) -> TurnResult:
        text = content if isinstance(content, str) else " ".join(
            b.get("text", "") for b in content if isinstance(b, dict))
        self.calls.append((effort, text[:80]))
        s = self.factory()
        if text.startswith("Final sign-off"):
            s.simulate_print(SimulatePrintIn(render=False))
            v = {"sim_matches_intent": True, "confidence": self.confidence,
                 "evidence": "stub", "summary": "stub agent", "swaps": []}
            return TurnResult(json.dumps(v), ["simulate_print"])
        if text.startswith("You did not checkpoint"):
            from p2p_contracts.mcp_tools import CheckpointIn

            s.checkpoint(CheckpointIn(note="nudged"))
            return TurnResult(json.dumps({"iteration": self.k, "ready_for_signoff": False}))
        self.k += 1
        names = []
        for name, req in self.plan(s, self.k):
            getattr(s, name)(req)
            names.append(name)
        return TurnResult(json.dumps({"iteration": self.k, "ready_for_signoff": self.k >= 2,
                                      "issues": []}), names)

    async def close(self):
        pass


# ================================================================ runner


@dataclass
class PaintRun:
    job_id: str
    attempt: int
    dir: Path
    iterations: int
    checklist: Checklist
    confidence: float
    review: bool
    review_reason: str | None
    threemf: str | None
    slice_ok: bool | None
    model_calls: list[ModelCall]
    duration_s: float
    signoff: dict
    baseline: dict
    final_sim: dict
    swaps: list
    concept_check: dict = field(default_factory=dict)
    proof: list = field(default_factory=list)  # [{kind, view, path}] job-relative
    review_images: list = field(default_factory=list)


def _previews(s: PaintSession, tag: str) -> list[str]:
    """Flat (ideal) and simulated-print sheets of the current paint into previews/."""
    from p2p_sim.render import render_sheet

    d = s.dir / "previews"
    d.mkdir(exist_ok=True)
    sim = s.run_sim()
    out = []
    p1 = render_sheet(s.product, s.figure_rgb(), d / f"{tag}_paint.png", kind="shaded",
                      title=f"{tag}: paint (filament colors)")
    p2 = render_sheet(s.product, None, d / f"{tag}_print.png", kind="sim", sim=sim,
                      title=f"{tag}: simulated print")
    for p in (p1, p2):
        out.append(s.rel(Path(p)))
    # one large view of the face: top for flat products, front three-quarter for upright ones
    from p2p_sim.render import render

    hero = "top" if s.inp.orientation == "flat_face_up" else "front_3q"
    for kind, name in (("sim", "print"), ("shaded", "paint")):
        r = render(s.product, hero, kind, face_colors=None if kind == "sim" else s.figure_rgb(),
                   size_px=1024, sim=sim if kind == "sim" else None)
        p = d / f"{tag}_{name}_{hero}.png"
        r.save(p)
        out.append(s.rel(p))
    return out


def _consolidate_bands(s: PaintSession, log, why: str) -> list[dict]:
    """Safety net: a band-like region that still prints as scattered pieces becomes one strip
    or takes its surroundings' color (PaintSession.consolidate_bands), checkpointed."""
    done = s.consolidate_bands(why=why)
    if done:
        text = s._band_text(done)
        s._checkpoint(f"bands ({why}): {text}"[:300], bump=False)
        log(f"bands ({why}): {text}")
    return done


def _checkpoints_on_disk(d: Path) -> int:
    sj = d / "session.json"
    if not sj.is_file():
        return 0
    return len(json.loads(sj.read_text()).get("checkpoints", []))


async def run_paint(
    job_id: str,
    *,
    jobs_dir: Path | None = None,
    attempt: int | None = None,
    session: PaintSession | None = None,
    driver=None,
    max_iterations: int = MAX_ITERATIONS,
    sam3_backend: str | None = None,
    slice_fn: Callable | None = None,
    concept_png: bytes | None = None,
    write_manifest: bool = True,
    log: Callable[[str], None] = print,
    judge=None,
    photos: list[Path] | None = None,
    concept_check: bool = True,
    palette_plan: bool | None = None,
) -> PaintRun:
    from .job import jobs_root

    t_start = time.time()
    started = datetime.now(UTC)
    root = Path(jobs_dir or jobs_root()).resolve()
    if palette_plan is None:  # the cost-aware palette decision (palette_plan.py); 0 turns it off
        palette_plan = os.environ.get("P2P_PALETTE_PLAN", "1") != "0"
    if session is None:
        session = PaintSession.open(OpenJobIn(job_id=job_id, attempt=attempt), root, sam3_backend)
    s = session
    log(f"opened {job_id} a{s.attempt}: {len(s.regions)} regions, palette {s.palette} "
        f"({time.time() - t_start:.0f} s)")
    baseline = s.sim_summary()
    s.baseline_sim = baseline
    s._save()
    cl0 = s.run_checklist().out
    (s.dir / "checklist_baseline.json").write_text(cl0.model_dump_json(indent=1))
    before = _previews(s, "before")
    log(f"baseline sim {baseline}")
    if palette_plan and s.inp.tex_rgb is not None:
        from .palette_plan import summary_line

        t_plan = time.time()
        s.apply_palette_plan()
        s.timings["palette_plan_s"] = round(time.time() - t_plan, 2)
        log(f"palette plan: {summary_line(s.plan)}")

    def reload() -> PaintSession:
        return PaintSession.resume(s.inp, s.dir, s.attempt, sam3_backend)

    if driver is None:
        driver = ClaudeDriver(job_id, s.attempt, root, sam3_backend=sam3_backend, log=log)
    from .concept_check import ClaudeJudge, StubJudge, concept_loop, proofs

    if judge is None:  # a scripted paint driver never reaches Claude through the judge
        judge = (ClaudeJudge(cwd=root) if isinstance(driver, ClaudeDriver)
                 else StubJudge([{"matches": True, "confidence": 1.0, "summary": "stub"}]))
    if photos is None:
        photos = sorted(p for p in (s.job_dir / "input").glob("photo_*")
                        if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    check = None
    calls: list[ModelCall] = []
    first = prompts.first_message(s, baseline, prompts.checklist_text(cl0), max_iterations)
    content: str | list = first
    if concept_png:
        content = [{"type": "text", "text": first}, _image_block(concept_png)]
    iterations, issues, verdict = 0, [], {}
    try:
        for k in range(1, max_iterations + 1):
            n0 = _checkpoints_on_disk(s.dir)
            t = time.time()
            res = await bounded(driver.turn(content, "medium"), TURN_TIMEOUT_S,
                                f"paint iteration {k}")
            if res.model_call:
                calls.append(res.model_call)
            verdict = last_json(res.text)
            iterations = k
            if _checkpoints_on_disk(s.dir) <= n0:
                nud = await bounded(driver.turn(
                    "You did not checkpoint this iteration. Call checkpoint(note) now, then "
                    "repeat the JSON line.", "medium"), TURN_TIMEOUT_S, f"paint nudge {k}")
                if nud.model_call:
                    calls.append(nud.model_call)
                verdict = last_json(nud.text) or verdict
            issues = verdict.get("issues", issues)
            log(f"iteration {k}: {len(res.tool_calls)} tool calls, {time.time() - t:.0f} s, "
                f"verdict {verdict}")
            if k == 1 and not res.tool_calls and isinstance(driver, ClaudeDriver) \
                    and _checkpoints_on_disk(s.dir) <= n0:
                raise RuntimeError("the paint agent had no tools in iteration 1 (the paint "
                                   "MCP server did not start); stopped before spending more "
                                   "turns")
            if verdict.get("ready_for_signoff"):
                break
            cur = reload()
            content = prompts.next_message(k + 1, max_iterations,
                                           prompts.checklist_text(cur.run_checklist().out))
        cur = reload()
        sign = await bounded(driver.turn(prompts.signoff_message(
            prompts.checklist_text(cur.run_checklist().out), cur.sim_summary()), "high"),
            TURN_TIMEOUT_S, "paint sign-off")
        if sign.model_call:
            calls.append(sign.model_call)
        signoff = last_json(sign.text)
        log(f"sign-off: {signoff}")
        _consolidate_bands(reload(), log, "before the final check")
        if concept_check and concept_png is not None:
            check = await concept_loop(reload, judge, driver, concept_png, photos,
                                       s.dir / "proof" / "check", lambda p: s.rel(Path(p)), log)
            calls.extend(check.model_calls)
    finally:
        await driver.close()

    # final: reload from disk, write, gate, checklist
    f = reload()
    _consolidate_bands(f, log, "before the write")
    after = _previews(f, "after")
    final_sim = f.sim_summary()
    w = f.write_3mf(Write3mfIn(name="final")).out
    if slice_fn is not None:
        rep = slice_fn(f)
    else:
        rep = f.slice_check(SliceCheckIn()).out
    cl = f.run_checklist().out
    items = []
    for it in cl.items:
        if it.id == "sim_matches_intent":
            ok = signoff.get("sim_matches_intent")
            it = ChecklistItem(id=it.id, by="model", passed=ok if isinstance(ok, bool) else None,
                               evidence=(str(signoff.get("evidence") or "no sign-off") + " | "
                                         + it.evidence)[:1500])
        items.append(it)
    passed_all, code_conf = summarize(items)
    agent_conf = signoff.get("confidence")
    agent_conf = float(agent_conf) if isinstance(agent_conf, int | float) else 0.0
    confidence = round(min(code_conf, agent_conf), 2)
    reasons = []
    if not rep.ok:
        confidence = 0.0
        reasons.append("slicer gate failed: " + "; ".join(rep.errors[:3]))
    if confidence < REVIEW_BELOW:
        fails = [i.id for i in items if i.passed is False]
        reasons.append(f"confidence {confidence} (failed: {', '.join(fails) or 'none'})")
    spec = next((i for i in items if i.id == "spec_coverage"), None)
    if spec is not None and spec.passed is False and confidence >= REVIEW_BELOW:
        reasons.append("color spec not met with the loaded spools: "
                       + spec.evidence.split(". ")[0])
    if check is not None and check.final is not None:
        confidence = round(min(confidence, check.final.confidence), 2)
        if not check.passed:
            majors = check.final.majors or check.final.discrepancies
            reasons.append("final print differs from the approved concept: " + "; ".join(
                f"{d.what} ({d.route})" for d in majors)[:600])
    checklist = Checklist(items=items, passed_all=passed_all and rep.ok, confidence=confidence)
    (f.dir / "checklist.json").write_text(checklist.model_dump_json(indent=1))
    swaps = signoff.get("swaps") or [x for x in issues if "swap" in str(x).lower()]
    t_proof = time.time()
    proof_rows = [{"kind": r["kind"], "view": r["view"], "path": f.rel(Path(r["path"]))}
                  for r in proofs(f, f.dir / "proof" / "final")]
    (f.dir / "proof" / "proof.json").write_text(json.dumps(proof_rows, indent=1))
    log(f"proof renders: {len(proof_rows)} in {time.time() - t_proof:.0f} s")
    run = PaintRun(job_id=job_id, attempt=f.attempt, dir=f.dir, iterations=iterations,
                   checklist=checklist, confidence=confidence, review=bool(reasons),
                   review_reason="; ".join(reasons) or None,
                   threemf=w.path if rep.ok else None, slice_ok=rep.ok, model_calls=calls,
                   duration_s=round(time.time() - t_start, 1), signoff=signoff,
                   baseline=baseline, final_sim=final_sim, swaps=swaps,
                   concept_check=check.to_json() if check is not None else {},
                   proof=proof_rows, review_images=check.images if check is not None else [])
    _write_outputs(f, run, started, before, after, rep, write_manifest)
    log(f"done: {iterations} iterations, confidence {confidence}, gate {rep.ok}, "
        f"{run.duration_s:.0f} s, list ${sum(c.usd_list for c in calls):.2f}")
    return run


def _write_outputs(s: PaintSession, run: PaintRun, started: datetime, before, after, rep,
                   write_manifest: bool = True) -> None:
    d = s.dir
    outs = []
    for name, role in (("paint_state.json", "paint_state"), ("region_map.json", "region_map"),
                       ("face_regions.npy", "face_regions"), ("checklist.json", "checklist"),
                       ("slice_report.json", "slice_report"),
                       ("palette_plan.json", "palette_plan")):
        if (d / name).is_file():
            outs.append(s._fileref(d / name, role=role))
    if (d / "final.3mf").is_file():
        outs.append(s._fileref(d / "final.3mf", "3mf", "paint_3mf" if rep.ok else "debug_3mf"))
    for rel in before + after:
        outs.append(s._fileref(s.job_dir / rel, "png", "preview"))
    now = datetime.now(UTC)
    summary = {"iterations": run.iterations, "confidence": run.confidence,
               "review": run.review, "review_reason": run.review_reason,
               "baseline_sim": run.baseline, "final_sim": run.final_sim,
               "signoff": run.signoff, "swaps": run.swaps, "slice_ok": rep.ok,
               "concept_check": run.concept_check, "proof": run.proof,
               "print_time_s": rep.print_time_s, "total_grams": rep.total_grams,
               "purge_grams": rep.purge_grams, "palette": list(s.palette),
               "palette_plan": s.plan,
               "claude_usd_list": round(sum(c.usd_list for c in run.model_calls), 4),
               "timings": s.timings}
    (d / "paint_run.json").write_text(json.dumps(summary, indent=1, default=str))
    if not write_manifest:  # the worker owns manifest.json when it runs this stage
        return
    try:
        m = Manifest(job_id=s.inp.job_id, stage="s09_paint", attempt=s.attempt,
                     status="succeeded" if not run.review else "waiting_user",
                     started_at=started, finished_at=now,
                     duration_s=(now - started).total_seconds(), outputs=outs,
                     model_calls=run.model_calls, confidence=run.confidence,
                     decisions=[Decision(key="paint.confidence", value=run.confidence, by="model",
                                         model=MODEL, at=now,
                                         reason=run.review_reason)],
                     pause_reason=run.review_reason, data=summary)
        (d / "manifest.json").write_text(m.model_dump_json(indent=1))
    except Exception as e:  # noqa: BLE001 - manifest shape is the worker's; keep the run
        (d / "manifest_error.txt").write_text(repr(e))
    _ = FileRef


def run_paint_sync(*args, **kwargs) -> PaintRun:
    return asyncio.run(run_paint(*args, **kwargs))
