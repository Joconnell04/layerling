"""Final print vs approved concept, from every side.

After the paint agent signs off, Claude Opus 5.5 at effort high (CLAUDE.md: high for sign-off)
compares the simulated print, rendered from all eight proof views, with the approved concept
and the customer's photos. Each discrepancy names a route:

- paint_region: a region prints the wrong color. With region ids and a color it is applied
  directly; otherwise the paint agent gets the list for one more edit iteration.
- addon_color: an add-on part (plate, text, loop, back, base) has the wrong color; applied
  directly (part_filaments), within the palette source and the AMS slots.
- text, plate_shape, loop: geometry the job specified (s06). Paint cannot change them; they go
  to review with the side-by-side renders.
- other: anything else, to review.

The loop runs at most MAX_ROUNDS judge calls (MAX_ROUNDS - 1 fix rounds). A major discrepancy
left at the end sends the job to review with the concept next to the print sheets.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import numpy as np
from p2p_contracts.manifest import ModelCall
from pydantic import BaseModel, Field, ValidationError

MODEL = "claude-opus-5-5"
MAX_ROUNDS = 3  # ASSUMED: judge, fix, judge, fix, judge
Route = Literal["paint_region", "addon_color", "text", "plate_shape", "loop", "other"]
PART_WORDS = {"loop": ("keyring", "hanging_loop"), "keyring": ("keyring",),
              "ring": ("keyring", "hanging_loop"), "plate": ("plate",), "text": ("text",),
              "name": ("text",), "back": ("back",), "base": ("base",)}

SYSTEM = """You are the final quality check for layerling, a studio that 3D prints pets and
objects in several filament colors on a Bambu X2D. The customer approved a concept image. You
see the approved concept, the customer's photos, the 3D model with its own texture from eight
sides, and the SIMULATED PRINT from the same eight sides (filament colors, what the slicer will
really print). Judge only the simulated print against what the customer approved.

Rules
- The core idea must stay the same: same subject, pose, form (3D figure or flat charm), markings,
  face (eyes, nose, mouth) and colors, from every side. The concept shows one side; for the
  others, the 3D model's texture shows what was intended.
- A plate, name, loop, back or base may be missing from the concept image; then the job facts
  say what was specified. Judge those against the job facts and whether they fit the figure.
- Fine texture (speckle, fur strokes, gradients) cannot print at this size; do not flag its
  absence. Flag color areas, markings, face parts, add-on colors and form.
- A discrepancy is major when the customer would say "that is not what I approved" (wrong
  color on a marking, the ears or the face, a missing marking, the wrong form, an add-on color
  that clashes). Minor: small shade differences, tiny details.
- The palette is a costed decision (job facts): shades within the same color family were
  merged on purpose to save purge and print time (a light gray coat printed white, a
  near-black saddle printed black). That is minor unless it changes a marking's color family.
- Route each discrepancy: paint_region (a figure color area is wrong; give region_ids from the
  region table when you can tell, and the color it should be as target_hex or filament_id),
  addon_color (part: plate, text, loop, back or base, and target_hex or filament_id),
  text (the name is wrong), plate_shape, loop (position or size), other.
- Answer with one line of JSON and nothing after it:
  {"matches": <true|false>, "confidence": <0..1>, "summary": "<one or two sentences>",
   "discrepancies": [{"view": "<view>", "what": "<short>", "route": "<route>",
     "severity": "major|minor", "region_ids": [<id>...], "part": "<part or null>",
     "target_hex": "<#RRGGBB or null>", "filament_id": "<id or null>"}]}
  matches is true only when there is no major discrepancy."""


class Discrepancy(BaseModel):
    view: str = ""
    what: str
    route: Route = "other"
    severity: Literal["major", "minor"] = "major"
    region_ids: list[int] = Field(default_factory=list)
    part: str | None = None
    target_hex: str | None = None
    filament_id: str | None = None


class Verdict(BaseModel):
    matches: bool = False
    confidence: float = Field(0.0, ge=0, le=1)
    summary: str = ""
    discrepancies: list[Discrepancy] = Field(default_factory=list)

    @property
    def majors(self) -> list[Discrepancy]:
        return [d for d in self.discrepancies if d.severity == "major"]

    @property
    def passed(self) -> bool:
        return self.matches and not self.majors


@dataclass
class CheckResult:
    rounds: list[dict] = field(default_factory=list)  # verdict, applied, sent_to_agent, blocked
    final: Verdict | None = None
    images: list[str] = field(default_factory=list)  # job-relative side-by-sides
    model_calls: list[ModelCall] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.final is not None and self.final.passed

    def to_json(self) -> dict:
        return {"rounds": self.rounds, "passed": self.passed,
                "final": self.final.model_dump() if self.final else None,
                "images": self.images}


# ---------------------------------------------------------------- renders


def proofs(s, out_dir: Path, kinds=("model", "paint", "print"), size_px: int = 768,
           title: str = "") -> list[dict]:
    """The session's product from the eight proof views: model (texture), paint, print (sim)."""
    from p2p_sim.proof import proof_set

    sim = s.run_sim() if "print" in kinds else None
    model = paint = None
    if "paint" in kinds:
        paint = s.figure_rgb()
    if "model" in kinds and s.inp.tex_rgb is not None:
        model = paint.copy() if paint is not None else s.figure_rgb()
        model[: s.inp.figure.n_faces] = s.inp.tex_rgb
    return proof_set(s.product, s.inp.orientation, out_dir, model_rgb=model, paint_rgb=paint,
                     sim=sim, size_px=size_px, title=title or s.inp.job_id[:8])


def _png_block(path_or_bytes, max_px: int = 1024) -> dict:
    from PIL import Image

    im = (Image.open(io.BytesIO(path_or_bytes)) if isinstance(path_or_bytes, bytes)
          else Image.open(path_or_bytes)).convert("RGB")
    im.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(buf.getvalue()).decode()}}


def job_facts(s) -> str:
    inp = s.inp
    geo = inp.geometry
    spec = inp.spec or {}
    parts = []
    for p in inp.parts:
        fid = s.part_filaments.get(p.name) or p.filament_id
        f = inp.filament(fid) if fid else None
        parts.append(f"{p.name} ({p.role}) {fid} {f.hex if f else ''}")
    notes = [n for n in (geo.notes if geo else []) if any(
        w in n for w in ("Form", "Plate", "Name", "keyring", "hanging_loop", "loop"))]
    pal = ", ".join(f"{fid} {s.filament(fid).hex}" for fid in s.palette)
    area = {}
    for rid, r in s.regions.items():
        area[rid] = r.area_mm2
    rows = sorted(s.regions.values(), key=lambda r: -r.area_mm2)[:60]
    table = "\n".join(
        f"{r.id} {r.label} {s.assignment.get(r.id, s.default_filament)} {r.area_mm2:.0f} mm2"
        for r in rows)
    return (f"Job facts: product {inp.product_type}, form {spec.get('form') or 'default'} "
            f"(orientation {inp.orientation}), palette source {inp.palette_source}.\n"
            f"Add-on parts: {'; '.join(parts) or 'none'}.\n"
            f"Geometry decisions: {' | '.join(notes) or 'none'}.\n"
            f"Palette: {pal}.\n{_plan_text(s)}"
            f"Regions (id label filament area), largest first:\n{table}")


def _plan_text(s) -> str:
    """The cost-aware palette decision, for the judge (merged shades are on purpose)."""
    plan = getattr(s, "plan", None)
    if not plan:
        return ""
    from .palette_plan import summary_line

    return f"Palette decision (code, cost-aware): {summary_line(plan)}.\n"


# ---------------------------------------------------------------- judges


def parse_verdict(text: str) -> Verdict:
    from .agent import last_json

    obj = last_json(text)
    if not obj:
        return Verdict(matches=False, summary="no readable verdict", discrepancies=[
            Discrepancy(what="the check gave no readable verdict", route="other")])
    try:
        return Verdict.model_validate(obj)
    except ValidationError:
        with contextlib.suppress(ValidationError):
            ds = [Discrepancy.model_validate(d) for d in obj.get("discrepancies", [])
                  if isinstance(d, dict) and d.get("what")]
            return Verdict(matches=bool(obj.get("matches")), summary=str(obj.get("summary", "")),
                           confidence=float(obj.get("confidence") or 0.0), discrepancies=ds)
    return Verdict(matches=False, summary="no readable verdict", discrepancies=[
        Discrepancy(what="the check gave no readable verdict", route="other")])


class ClaudeJudge:
    """Opus 5.5, effort high, a fresh session per check, no tools, through the Claude Code
    login (API keys removed from the environment the CLI starts with)."""

    def __init__(self, model: str = MODEL, cwd: Path | None = None):
        self.model, self.cwd = model, cwd

    async def __call__(self, content: list[dict]) -> tuple[str, ModelCall | None]:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            TextBlock,
        )

        from .agent import MAX_BUFFER, scrubbed_env

        opts = ClaudeAgentOptions(model=self.model, system_prompt=SYSTEM, effort="high",
                                  tools=[], allowed_tools=[], setting_sources=[],
                                  strict_mcp_config=True, permission_mode="default",
                                  max_turns=1, cwd=str(self.cwd or Path.cwd()),
                                  stderr=lambda line: None, max_buffer_size=MAX_BUFFER)
        t0, started = time.time(), datetime.now(UTC)
        with scrubbed_env():
            client = ClaudeSDKClient(opts)
            await client.connect()
        texts, result = [], None
        try:
            async def stream():
                yield {"type": "user", "message": {"role": "user", "content": content},
                       "parent_tool_use_id": None, "session_id": "default"}
            await client.query(stream())
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    texts += [b.text for b in msg.content if isinstance(b, TextBlock)]
                elif isinstance(msg, ResultMessage):
                    result = msg
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()
        mc = None
        if result is not None:
            u = result.usage or {}
            mc = ModelCall(vendor="anthropic", model=self.model, purpose="concept_check",
                           started_at=started, duration_s=round(time.time() - t0, 2),
                           effort="high", input_tokens=int(u.get("input_tokens") or 0),
                           output_tokens=int(u.get("output_tokens") or 0),
                           cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
                           cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0),
                           usd=0.0, usd_list=round(float(result.total_cost_usd or 0.0), 4),
                           billing="subscription", request_id=result.session_id)
        return "\n".join(texts), mc


class StubJudge:
    """Scripted verdicts for tests: a list of Verdict dicts, one per call (the last repeats)."""

    def __init__(self, verdicts: list[dict]):
        self.verdicts = verdicts
        self.calls: list[list[dict]] = []

    async def __call__(self, content: list[dict]) -> tuple[str, ModelCall | None]:
        self.calls.append(content)
        v = self.verdicts[min(len(self.calls) - 1, len(self.verdicts) - 1)]
        return json.dumps(v), None


# ---------------------------------------------------------------- fixes


def _allowed(s) -> list:
    inp = s.inp
    return list(inp.library) + (list(inp.catalog) if inp.allows_unowned else [])


def _pick(s, d: Discrepancy):
    """The filament a discrepancy asks for, within what the job allows, or None."""
    from . import palette

    allowed = _allowed(s)
    ids = {f.id for f in allowed} | set(s.palette)
    if d.filament_id and d.filament_id in ids:
        return s.filament(d.filament_id)
    if d.target_hex:
        try:
            lab = palette.hex_lab(d.target_hex)
        except (ValueError, IndexError):
            return None
        pool = allowed + [s.filament(x) for x in s.palette if x not in {f.id for f in allowed}]
        cost, _ = palette.guarded_de([lab], palette.filament_labs(pool))
        cost = cost[0] + np.array([0.0 if s.inp.owned(f.id) else palette.UNOWNED_PENALTY_DE
                                   for f in pool])
        return pool[int(np.argmin(cost))]
    return None


def _parts_for(s, word: str | None) -> list[str]:
    if not word:
        return []
    w = word.lower().strip()
    names = [p.name for p in s.inp.parts if p.name == w]
    if names:
        return names
    roles = PART_WORDS.get(w, (w,))
    return [p.name for p in s.inp.parts if p.role in roles]


def apply_fixes(s, verdict: Verdict) -> tuple[list[str], list[Discrepancy], list[Discrepancy]]:
    """Apply what code can apply. Returns (applied notes, for the paint agent, blocked)."""
    from p2p_contracts.mcp_tools import PaintRegionIn

    from .session import ToolError

    applied, agent, blocked = [], [], []
    for d in verdict.majors:
        if d.route == "addon_color":
            names, fil = _parts_for(s, d.part), _pick(s, d)
            if not names or fil is None:
                blocked.append(d)
                continue
            if fil.id not in s.palette and len(s.palette) >= s.inp.settings.ams_slots:
                blocked.append(d.model_copy(update={"what": d.what + " (no free AMS slot)"}))
                continue
            for n in names:
                s.part_filaments[n] = fil.id
            if fil.id not in s.palette:
                s.palette.append(fil.id)
            s._prune_palette()
            s.dirty, s.sim_key = True, None
            s._save()
            applied.append(f"{', '.join(names)} -> {fil.id} ({d.what})")
        elif d.route == "paint_region":
            fil = _pick(s, d)
            rids = [r for r in d.region_ids if r in s.regions]
            painted = False
            if rids and fil is not None:
                try:
                    s.paint_region(PaintRegionIn(region_ids=rids, filament_id=fil.id,
                                                 add_to_palette=True))
                    applied.append(f"regions {rids} -> {fil.id} ({d.what})")
                    painted = True
                except ToolError as e:
                    d = d.model_copy(update={"what": f"{d.what} (direct fix failed: {e})"})
            # live2 sombrero: recoloring the trim left it as red spots. A band the check names
            # becomes one strip (or its pieces take the surrounding color), whatever its color.
            bands = s.consolidate_bands(rids, {r: d.what for r in rids},
                                        why="final check") if rids else []
            if bands:
                applied.append(f"{s._band_text(bands)} ({d.what})")
            if not painted and not bands:
                agent.append(d)
        else:
            blocked.append(d)
    if applied:
        from p2p_contracts.mcp_tools import CheckpointIn

        s.checkpoint(CheckpointIn(note="concept check: " + "; ".join(applied)[:300]))
    return applied, agent, blocked


def fix_message(items: list[Discrepancy], rnd: int) -> str:
    lines = "\n".join(
        f"- [{d.view}] {d.what}" + (f" (regions {d.region_ids})" if d.region_ids else "")
        + (f" -> {d.filament_id or d.target_hex}" if (d.filament_id or d.target_hex) else "")
        for d in items)
    return (f"Final check against the approved concept, round {rnd}: the simulated print differs "
            f"from the concept here:\n{lines}\nFix these by region id (look at every side), "
            "simulate_print, judge, then call checkpoint(note) and answer with the iteration "
            "JSON line.")


# ---------------------------------------------------------------- loop


async def concept_loop(reload, judge, driver, concept_png: bytes | None, photos: list[Path],
                       out_dir: Path, rel, log=print, max_rounds: int = MAX_ROUNDS
                       ) -> CheckResult:
    """reload() -> the current PaintSession from disk; judge(content) -> (text, ModelCall);
    driver: the paint agent (for fixes it must make itself); rel(path) -> job-relative."""
    from p2p_sim.proof import side_by_side

    from . import agent  # the time limits per Claude call (lazily, as ClaudeJudge does)

    res = CheckResult()
    if concept_png is None:
        log("concept check skipped: no approved concept")
        return res
    concept_path = out_dir / "concept.png"
    out_dir.mkdir(parents=True, exist_ok=True)
    concept_path.write_bytes(concept_png)
    for rnd in range(1, max_rounds + 1):
        s = reload()
        rows = proofs(s, out_dir / f"round{rnd}", kinds=("model", "print"), size_px=512)
        sheets = {r["kind"]: r["path"] for r in rows if r["view"] == "sheet"}
        content = [{"type": "text", "text": "Approved concept:"}, _png_block(concept_png)]
        for i, p in enumerate(photos[:3]):
            content += [{"type": "text", "text": f"Customer photo {i + 1}:"}, _png_block(p)]
        if "model" in sheets:
            content += [{"type": "text", "text": "3D model with its own texture, eight sides:"},
                        _png_block(sheets["model"], 1600)]
        content += [{"type": "text", "text": "SIMULATED PRINT, eight sides (judge this):"},
                    _png_block(sheets["print"], 1600),
                    {"type": "text", "text": job_facts(s) + "\nGive the verdict JSON line."}]
        t = time.time()
        text, mc = await agent.bounded(judge(content), agent.JUDGE_TIMEOUT_S,
                                       f"concept check {rnd}")
        if mc is not None:
            res.model_calls.append(mc)
        verdict = parse_verdict(text)
        res.final = verdict
        entry = {"round": rnd, "verdict": verdict.model_dump(), "seconds": round(time.time() - t)}
        cmp_png = side_by_side(concept_path, sheets["print"],
                               out_dir / f"round{rnd}" / "compare_print.png",
                               "approved concept", f"simulated print, round {rnd}")
        entry["compare"] = rel(cmp_png)
        log(f"concept check {rnd}: matches {verdict.matches}, {len(verdict.majors)} major, "
            f"{verdict.summary[:160]}")
        if verdict.passed or rnd == max_rounds:
            res.rounds.append(entry)
            res.images = [entry["compare"]]
            break
        applied, agent_items, blocked = apply_fixes(s, verdict)
        entry.update(applied=applied, sent_to_agent=[d.model_dump() for d in agent_items],
                     blocked=[d.model_dump() for d in blocked])
        res.rounds.append(entry)
        if agent_items and driver is not None:
            r = await agent.bounded(driver.turn(fix_message(agent_items, rnd), "medium"),
                                    agent.TURN_TIMEOUT_S, f"concept fix {rnd}")
            if r.model_call:
                res.model_calls.append(r.model_call)
        if not applied and not agent_items:
            res.images = [entry["compare"]]
            break  # only blocked items: nothing paint can change
    return res
