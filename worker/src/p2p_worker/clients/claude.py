"""Claude client: intake and tweak routing (Haiku 4.5), color spec (Sonnet 5), token and USD
accounting.

Three implementations share one interface (`make_claude` picks one; there is no fallback):
- ClaudeCodeClaude (CLAUDE_BACKEND=claude_code, default): Jackson's logged-in Claude Code via
  `claude -p`, with every ANTHROPIC_* key removed from the child env so nothing bills an API key.
  Records usd=0, usd_list=total_cost_usd, billing="subscription".
- AnthropicClaude (CLAUDE_BACKEND=api, explicit only): the `anthropic` SDK with
  ANTHROPIC_API_KEY_TURNKEY (messages.parse with a Pydantic output_format, cached system prompt).
- MockClaude (MOCK_CLAUDE=1 or CLAUDE_BACKEND=mock): deterministic, no network. Test default.

All return validated contract payloads plus a p2p_contracts.ModelCall with tokens and USD.
Model ids follow CLAUDE.md. List prices, USD per million tokens (SOURCE: claude-api skill model
table, cached 2026-06-24): Haiku 4.5 $1 in / $5 out, Sonnet 5 $2 / $10, Opus 5.5 $4 / $20.
Cache writes (5 minute TTL) bill at 1.25x input, cache reads at 0.1x input.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from p2p_contracts import ModelCall
from p2p_contracts.job import FORM_CHOICE_PRODUCTS, AddOn, AddOnKind, ProductForm, TextSpec
from p2p_contracts.payloads import (
    PART_VOCAB,
    CheckItem,
    ColorSpec,
    ConceptCheck,
    IntakeResult,
    Option,
    PartSpec,
    Question,
    Subject,
    SubjectSpec,
    TweakDecision,
    TweakRoute,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import trace
from ..errors import VendorError
from ..intent import asks_from_text, form_from_text, merge_asks, parse_ask_lines
from ..util import downscale_image, utcnow

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5"
OPUS = "claude-opus-5-5"

# model -> (input, output, cache_write_5m, cache_read), USD per million tokens
PRICES_PER_MTOK: dict[str, tuple[float, float, float, float]] = {
    HAIKU: (1.0, 5.0, 1.25, 0.10),
    SONNET: (2.0, 10.0, 2.50, 0.20),
    OPUS: (4.0, 20.0, 5.00, 0.20),
}
IMAGE_TOKENS_EST = 1600  # a photo downscaled to 1024 px is about 1,400 tokens


def usd_for(model: str, inp: int, out: int, cache_write: int = 0, cache_read: int = 0) -> float:
    pi, po, pw, pr = PRICES_PER_MTOK[model]
    return round((inp * pi + out * po + cache_write * pw + cache_read * pr) / 1e6, 6)


def estimate_usd(model: str, *, n_images: int, text_chars: int, max_tokens: int) -> float:
    """Upper-bound cost of one call, used by the budget guard before the call."""
    inp = n_images * IMAGE_TOKENS_EST + text_chars // 3 + 200
    return usd_for(model, inp, max_tokens)


@dataclass
class IntakeRequest:
    prompt: str
    product_type: str
    photos: list[Path]
    question_bank: list[dict[str, Any]]
    min_questions: int = 2
    max_questions: int = 4
    # Already set on the new-job form. Never asked again; kept unless an answer changes them.
    form_target_size_mm: float | None = None
    form_addons: list[AddOn] = field(default_factory=list)
    skip_questions: set[str] = field(default_factory=set)
    form_form: ProductForm | None = None  # flat or figure, when the order form set it

    def form_lines(self) -> str:
        parts = []
        if self.form_form:
            parts.append("a flat charm" if self.form_form == ProductForm.FLAT else "a 3D figure")
        if self.form_target_size_mm:
            parts.append(f"size {self.form_target_size_mm:g} mm")
        for a in self.form_addons:
            if a.text:
                parts.append(f"{a.kind.value.replace('_', ' ')} text \"{a.text.text}\"")
            elif a.params:
                parts.append(f"{a.kind.value.replace('_', ' ')} "
                             + ", ".join(f"{k} {v}" for k, v in a.params.items()))
            else:
                parts.append(a.kind.value.replace("_", " "))
        skip = ", ".join(sorted(self.skip_questions))
        if not parts and not skip:
            return ""
        return (f"Already set on the order form, do not ask about it again: {'; '.join(parts)}."
                + (f" Question ids you must not ask: {skip}." if skip else ""))


class ColorRegion(BaseModel):
    """One visible color area of a subject. Intake lists every one, markings included."""

    model_config = ConfigDict(extra="forbid")
    subject_id: str
    region: str = Field(description="e.g. head and ears, saddle on the back, left front sock")
    color: str = Field(description="Plain color name, e.g. tan, black, white")
    kind: Literal["base", "marking", "feature"]
    hex: str | None = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")


class DraftSubject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    kind: str
    desc: str = Field(max_length=300)
    photos: list[int]


class IntakeDraft(BaseModel):
    """What intake asks Claude for. Compact on purpose (proof run 1: 13.7k output tokens and
    131 s with full question objects): bank question ids only, one pipe-separated line per
    color area, short descriptions."""

    model_config = ConfigDict(extra="forbid")
    subjects: list[DraftSubject]
    colors: list[str] = Field(max_length=24)
    questions: list[str] = Field(default_factory=list, max_length=4)
    product_type: str
    target_size_mm: float | None = None
    addons: list[AddOn] = Field(default_factory=list)
    notes: str | None = Field(None, max_length=300)
    asks: list[str] = Field(default_factory=list, max_length=12)


KINDS = {"b": "base", "m": "marking", "f": "feature"}
HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")


def _hex(s: str) -> str | None:
    m = HEX_RE.match(s.strip())
    return f"#{m.group(1).upper()}" if m else None


def parse_colors(lines: list[str]) -> list[ColorRegion]:
    """"s1|saddle on the back|black|m|#1A1A1A" lines -> ColorRegion. Bad lines are dropped."""
    out = []
    for line in lines:
        f = [x.strip() for x in str(line).split("|")]
        if len(f) < 4 or not f[0] or not f[1] or not f[2]:
            continue
        kind = KINDS.get(f[3][:1].lower())
        if kind is None:
            continue
        out.append(ColorRegion(subject_id=f[0], region=f[1][:80], color=f[2][:40], kind=kind,
                               hex=_hex(f[4]) if len(f) > 4 else None))
    return out


def color_line(r: ColorRegion) -> str:
    return "|".join([r.subject_id, r.region, r.color, r.kind[0], r.hex or ""])


@dataclass
class TweakRequest:
    tweak: str
    concept_prompt: str
    image: Path | None = None
    rounds_left: int = 0
    history: list[str] = field(default_factory=list)


@dataclass
class CheckSpec:
    """One checklist line for the concept check (payloads.CheckItem without the verdict)."""

    id: str
    kind: str
    label: str
    on: str  # figure | product


@dataclass
class ConceptCheckRequest:
    """Check one concept against the photos and the request before any 3D credits are spent."""

    request: str
    photos: list[Path]
    figure: Path
    product: Path | None
    items: list[CheckSpec]
    expected_text: str | None = None

    def images(self) -> list[Path]:
        return [*self.photos[:3], self.figure, *([self.product] if self.product else [])]

    def text(self) -> str:
        n = len(self.photos[:3])
        order = (f"Images, in order: {n} customer photo{'s' if n != 1 else ''}, then FIGURE"
                 + (", then PRODUCT." if self.product else " (it is also the product)."))
        items = "\n".join(f"- {i.id} [{i.on.upper()}]: {i.label}" for i in self.items)
        name = (f"\nThe name must read exactly: {self.expected_text}" if self.expected_text
                else "")
        return f"Customer's request: {self.request}\n{order}\nChecklist:\n{items}{name}"


@dataclass
class ViewCheckRequest:
    """Check s04 views against the approved figure (angle, same figure, nothing added)."""

    concept: Path
    views: list[Path]
    regions: list[str] = field(default_factory=list)
    asks: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)

    def images(self) -> list[Path]:
        return [self.concept, *self.views]

    def text(self) -> str:
        lines = [f"Images, in order: APPROVED, then views 1 to {len(self.views)}."]
        if self.regions:
            lines.append("Colors and markings to keep: " + "; ".join(self.regions))
        if self.asks:
            lines.append("The customer asked for: " + "; ".join(self.asks))
        if self.metrics:
            lines.append("Color measurements (code):\n" + "\n".join(self.metrics))
        return "\n".join(lines)


@dataclass
class ColorSpecRequest:
    photos: list[Path]
    subjects: list[Subject]
    regions: list[ColorRegion] = field(default_factory=list)  # intake's list, as hints
    product_type: str = "figurine"

    def text(self) -> str:
        subj = "\n".join(f"{s.id}: {s.name}, {s.kind}, photos {s.photo_indices}. "
                         f"{s.description[:400]}" for s in self.subjects)
        hints = "\n".join(color_line(r) for r in self.regions) or "none"
        return (f"Product: {self.product_type}\nSubjects:\n{subj}\n"
                f"Intake's color areas (check them against the photos; add what it missed):\n"
                f"{hints}\nThere are {len(self.photos)} photos, indices 0 to "
                f"{len(self.photos) - 1}.")


class SpecDraft(BaseModel):
    """Compact color spec: one pipe-separated line per part."""

    model_config = ConfigDict(extra="forbid")
    parts: list[str] = Field(max_length=48)
    notes: str | None = Field(None, max_length=200)


def hex_lab(hx: str) -> tuple[float, float, float]:
    """sRGB hex -> CIELAB D65 (p2p_threemf's conversion, the one the filament library uses)."""
    from p2p_threemf.colour import hex_to_lab

    return tuple(round(float(x), 2) for x in hex_to_lab(hx))  # type: ignore[return-value]


def parse_spec(draft: SpecDraft, subjects: list[Subject], n_photos: int) -> ColorSpec:
    """SpecDraft lines -> ColorSpec with LAB computed here, labels unique per subject."""
    ids = [s.id for s in subjects]
    by_subject: dict[str, list[PartSpec]] = {i: [] for i in ids}
    for line in draft.parts:
        f = [x.strip() for x in str(line).split("|")]
        if len(f) < 5:
            continue
        sid = f[0] if f[0] in by_subject else (ids[0] if len(ids) == 1 else None)
        hx = _hex(f[4])
        if sid is None or hx is None:
            continue
        part = f[1].lower().replace(" ", "_")
        part = part if part in PART_VOCAB else "other"
        label = re.sub(r"[^a-z0-9_]+", "_", (f[2] or part).lower()).strip("_")[:40] or part
        taken = {p.label for p in by_subject[sid]}
        base, k = label, 2
        while label in taken:
            label, k = f"{base}_{k}", k + 1
        photo = None
        if len(f) > 6 and f[6].isdigit() and int(f[6]) < n_photos:
            photo = int(f[6])
        by_subject[sid].append(PartSpec(
            part=part, label=label, color_name=(f[3] or "unknown")[:40], hex=hx, lab=hex_lab(hx),
            required=not (len(f) > 5 and f[5] in ("0", "false", "no")), photo_index=photo))
    return ColorSpec(subjects=[SubjectSpec(subject_id=i, parts=p) for i, p in by_subject.items()],
                     notes=draft.notes)


Billing = Literal["api", "subscription", "free"]


class ClaudeClient(Protocol):
    mock: bool
    billing: Billing

    def estimate_intake(self, req: IntakeRequest) -> float: ...
    def estimate_route(self, req: TweakRequest) -> float: ...
    def estimate_color_spec(self, req: ColorSpecRequest) -> float: ...
    async def intake(
        self, req: IntakeRequest) -> tuple[IntakeResult, list[ColorRegion], ModelCall]: ...
    async def route_tweak(self, req: TweakRequest) -> tuple[TweakDecision, ModelCall]: ...
    async def color_spec(self, req: ColorSpecRequest) -> tuple[ColorSpec, ModelCall]: ...
    def estimate_check(self, n_images: int) -> float: ...
    async def check_concept(self, req: ConceptCheckRequest) -> tuple[ConceptCheck, ModelCall]: ...
    async def check_views(
        self, req: ViewCheckRequest) -> tuple[list[ViewAnswer], ModelCall]: ...


INTAKE_SYSTEM = """You run intake for a service that turns pet and object photos into small \
multicolor 3D prints (figurines, keychains, nameplates, ornaments, magnets) on a Bambu X2D with AMS.
Text, keyring loops, hanging loops, plates, bases and magnet pockets are added later as exact
geometry, never sculpted by the image or 3D model.

From the photos and the request, produce one short JSON object, no prose:
- subjects: one per distinct pet, person, or object to be sculpted. id s1, s2, ... in the order the
  request names them. name: the pet's name if given, else a short description. kind: dog, cat,
  rabbit, bird, person, object, ... desc: breed, build, eye color, anything a sculptor needs, in
  under 25 words (colors go in `colors`). photos: 0-based indices of the photos showing it.
- colors: every visible color area of every subject, one line each, fields joined by "|":
  subject|region|color|kind|hex, kind b (base coat), m (marking) or f (feature), e.g.
  "s1|saddle on the back|black|m|#1A1A1A". First the base coat, then EVERY marking (saddles,
  patches, spots, blazes, masks, socks, stripes, tail tips, chest bibs), then features (eyes,
  nose, inner ears, tongue, collar). A missed marking is the most costly mistake here.
- questions: 2 to 4 question ids from the bank below, only where the photos and request leave
  the answer open.
- product_type: figurine, keychain, nameplate, ornament, magnet, or other.
- target_size_mm: longest print dimension if the request states or implies it, else null.
- addons: only what the request states outright, e.g. a nameplate with the name text it gives
  ({"kind": "nameplate", "text": {"text": "Rex"}}) or a keyring loop position
  ({"kind": "keyring_loop", "params": {"position": "top"}}). Else [].
- asks: every specific thing the customer asked to see beyond the subjects themselves: props,
  outfits and accessories, poses, expressions, and any name or text to show. One line each,
  fields joined by "|": kind|subject|what. kind: prop, outfit, pose, expression, text or other.
  subject: s1, s2, ... or empty for the whole piece. what: the customer's meaning in a few words,
  e.g. "outfit|s1|wearing a sombrero", "prop|s1|holding a tennis ball", "pose||hugging",
  "text||Otis". Only what the request says; never invent. [] when there are none.
- notes: anything else the concept artist must know, under 25 words, else null. Never text,
  loops, plates or bases.
"""

COLOR_SPEC_SYSTEM = """You write the color spec for a small multicolor 3D print of the subjects \
in the photos, printed with a few filament colors. List every part that needs its own color,
one line each, fields joined by "|":
  subject|part|label|color|hex|required|photo
- part: one of """ + ", ".join(PART_VOCAB) + """.
- label: unique within the subject, snake_case, the subject's own left and right
  (left_eye, back_patch, chest_blaze, left_inner_ear).
- color: plain name (black, cream, fawn, pink). hex: the color under neutral light.
- required: 1 if a viewer would miss it on the print (base coat, every marking, eyes, nose),
  0 for subtle shading.
- photo: 0-based index of the photo that shows the part best.
Order: base coat (part body), then every marking, then eyes, nose, inner ears, tongue, paw pads,
collar. Do not repeat a color area twice. At most 16 lines per subject. No prose."""

ROUTE_SYSTEM = """You route a user's tweak on a concept render for a 3D-printed figurine.
Pick exactly one route:
- rerender_concept: change what the figurine looks like (pose, expression, proportions, colors of
  fur or markings, props) before any 3D model exists.
- retexture: keep the 3D shape, change surface colors or texture only.
- regenerate_mesh: the 3D model's shape is wrong and needs a new generation.
- paint_edit: change which filament color goes on one region (eyes, nose, a patch).
- geometry_op: move or add the keyring loop, change the base, resize.
- ask_followup: the tweak is too vague to act on; ask one short question.
instruction: the tweak rewritten as a direct instruction for the stage that executes it.
followup_question: only for ask_followup, under 120 characters."""


class TweakRouting(BaseModel):
    route: TweakRoute
    instruction: str
    followup_question: str | None = Field(None, max_length=120)


# Concept and view checks. CLAUDE.md names Haiku for the s01 and s03 helper calls (intake, tweak
# routing) and PLAN.md stage 4 names a Haiku consistency check, so both checks run on Haiku.
CHECK_MODEL = HAIKU
CHECK_MAX_TOKENS = 700

CONCEPT_CHECK_SYSTEM = """You check a concept image for a small 3D-printed product before any \
3D model is paid for. The customer's photos come first, then FIGURE (the figure alone, the 3D \
model's input), then PRODUCT (the figure with the product's parts, what the customer approves) \
when there is one. Answer every checklist item by its id. ok is true only when you clearly see
it in the image the item names ([FIGURE] or [PRODUCT]):
- part: the named part is there, in the shape and at the spot the item says.
- text: read the name on PRODUCT letter by letter, exactly as printed, case included, into
  text_read; ok only when it matches the item exactly.
- ask: what the customer asked for or picked is visible and recognisable. A pose must match:
  sitting is haunches on the ground, standing is on four straight legs, lying is belly down.
  A form must match: a 3D figure has depth; a flat relief does not.
- markings: every coat color and marking in the photos is on the figure in the same place
  (saddles, patches, masks, blazes, socks, tail tips, ear colors). Ignore style and pose.
- no_extra_base: nothing under the figure (no base, plinth, stand, pedestal, ground, floor or
  slab) beyond the parts the checklist names. Items with id no_extras: nothing anywhere beyond
  the subjects, what the customer asked for and the parts the checklist names (no frame,
  ring, border, badge rim, backdrop, scenery, blocks, extra props or decorations).
why: under 20 words, only when ok is false; name the wrong pose or the extra thing."""

VIEW_CHECK_SYSTEM = """You check generated views of a figure before they go to a 3D model \
generator. The first image is APPROVED; the others are views 1..N of it. For each view give
angle as the viewer sees the figure (front, left, right, back, three_quarter, top, other) and ok:
true only when it is the same figure: same subject count, pose and proportions, same ears and
tail, the same colors and markings where they belong for that angle, the customer's asks still
there, and nothing added (no base, text, loop, plate or extra prop). why: under 20 words, only
when ok is false."""


class CheckAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    ok: bool
    why: str | None = Field(None, max_length=200)


class CheckDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[CheckAnswer]
    text_read: str | None = Field(None, max_length=60)


class ViewAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(description="1-based view number")
    angle: Literal["front", "left", "right", "back", "three_quarter", "top", "other"]
    ok: bool
    why: str | None = Field(None, max_length=200)


class ViewDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    views: list[ViewAnswer]


def same_text(a: str | None, b: str | None) -> bool:
    """Exact, case included; runs of spaces count as one."""
    return " ".join((a or "").split()) == " ".join((b or "").split())


def finalize_check(draft: CheckDraft, req: ConceptCheckRequest, model: str) -> ConceptCheck:
    """Model answers -> ConceptCheck. The name passes only on an exact read."""
    got = {a.id: a for a in draft.items}
    items = []
    for spec in req.items:
        a = got.get(spec.id)
        ok, note = bool(a and a.ok), (a.why if a else "not answered")
        if spec.kind == "text" and req.expected_text is not None \
                and not same_text(draft.text_read, req.expected_text):
            ok = False
            note = (f"reads {draft.text_read!r}, must read {req.expected_text!r}"
                    if draft.text_read else (note or f"must read {req.expected_text!r}"))
        items.append(CheckItem(id=spec.id, kind=spec.kind, label=spec.label[:160],  # type: ignore[arg-type]
                               on=spec.on, passed=ok,  # type: ignore[arg-type]
                               note=None if ok else (note or "failed")[:300], by="model"))
    return ConceptCheck(passed=all(i.passed for i in items), items=items,
                        text_read=draft.text_read, model=model)


def views_by_index(draft: ViewDraft, n: int) -> list[ViewAnswer]:
    """One answer per view in order; a view the model skipped fails."""
    got = {v.index: v for v in draft.views}
    return [got.get(i) or ViewAnswer(index=i, angle="other", ok=False, why="not answered")
            for i in range(1, n + 1)]


# Output caps. The compact formats need a few hundred tokens; the caps stop a runaway answer.
# Claude Code: CLAUDE_CODE_MAX_OUTPUT_TOKENS per response, thinking off (MAX_THINKING_TOKENS=0),
# --effort low. Measured with `claude -p` 2.1.281: all three are accepted by Haiku and Sonnet.
INTAKE_MAX_TOKENS = 1200
ROUTE_MAX_TOKENS = 600
SPEC_MAX_TOKENS = 1600


def bank_lines(bank: list[dict[str, Any]]) -> str:
    """The question bank as one line per question: id, text, option labels."""
    out = []
    for q in bank:
        opts = ", ".join(o["label"] for o in q.get("options", []))
        when = f" [only {q['when'].replace('_', ' ')}]" if q.get("when") else ""
        out.append(f"- {q['id']}: {q['text']} ({opts}){when}")
    return "\n".join(out)


def intake_system(req: IntakeRequest) -> str:
    return INTAKE_SYSTEM + "\nQuestion bank (pick ids):\n" + bank_lines(req.question_bank)


def intake_text(req: IntakeRequest) -> str:
    return (f"Request: {req.prompt}\nProduct type chosen in the app: {req.product_type}\n"
            f"{req.form_lines()}\n"
            f"There are {len(req.photos)} photos, indices 0 to {len(req.photos) - 1}.")


def _image_block(path: Path) -> dict[str, Any]:
    data, mime = downscale_image(path, max_px=1024)
    return {"type": "image",
            "source": {"type": "base64", "media_type": mime,
                       "data": base64.standard_b64encode(data).decode()}}


def finalize_intake(draft: IntakeDraft, req: IntakeRequest
                    ) -> tuple[IntakeResult, list[ColorRegion]]:
    """IntakeDraft -> (IntakeResult, color regions). Questions come from the bank by id. Each
    subject's description ends with its color regions, so every stage that reads the contract
    sees the markings."""
    ids = {s.id for s in draft.subjects}
    regions = [r for r in parse_colors(draft.colors) if r.subject_id in ids]
    subjects = []
    for s in draft.subjects:
        mine = [r for r in regions if r.subject_id == s.id]
        desc = s.desc.rstrip(". ")
        if mine:
            desc += ". Colors: " + "; ".join(f"{r.color} {r.region}" for r in mine)
        subjects.append(Subject(id=s.id, name=s.name[:80], kind=s.kind[:40],
                                description=desc[:2000], photo_indices=s.photos))
    bank = {bq["id"]: bq for bq in req.question_bank}
    questions = [bank_question(bank[q]) for q in dict.fromkeys(draft.questions) if q in bank]
    words = {w for s in draft.subjects for w in re.findall(r"[a-z]+", f"{s.name} {s.kind}".lower())}
    asks = merge_asks(parse_ask_lines(draft.asks, ids), asks_from_text(req.prompt), words)
    res = IntakeResult(subjects=subjects, questions=questions[:4],
                       product_type=draft.product_type, target_size_mm=draft.target_size_mm,
                       addons=draft.addons, notes=clean_notes(draft.notes), custom_asks=asks)
    return normalize_intake(res, req), regions


def clean_notes(notes: str | None) -> str | None:
    """Intake notes go into the concept prompt verbatim. Haiku sometimes closes the string with
    the JSON it was writing (live2 run: 'from photos."}]'); drop trailing quotes and brackets."""
    if notes is None:
        return None
    s = re.sub(r'[\s"\'\]\}\[\{,\\]+$', "", notes).strip()
    return s or None


def normalize_intake(res: IntakeResult, req: IntakeRequest) -> IntakeResult:
    """2 to 4 questions with unique ids. Questions the form or the request already answered are
    dropped; bank questions marked `required` come first otherwise; the bank tops up if the model
    gave too few."""
    known = {a.kind.value for a in [*res.addons, *req.form_addons] if a.text or a.params}
    if req.form_target_size_mm:
        known.add("target_size_mm")
    # The form is a choice only for keychains and magnets, and only the order form or the
    # request's own words ("3d", "flat") settle it without asking.
    form = None
    if req.product_type in {p.value for p in FORM_CHOICE_PRODUCTS}:
        form = req.form_form or form_from_text(req.prompt)
    if form:
        known.add("form")
    covered = {bq["id"] for bq in req.question_bank if bq.get("covers") in known}
    covered |= set(req.skip_questions)
    seen: set[str] = set(covered)  # never asked
    qs: list[Question] = []
    for bq in req.question_bank:
        if bq.get("required") and bq["id"] not in covered:
            got = next((q for q in res.questions if q.id == bq["id"]), None)
            qs.append(got or bank_question(bq))
            seen.add(bq["id"])
    for q in res.questions:
        if q.id not in seen:
            qs.append(q)
            seen.add(q.id)
    for bq in req.question_bank:
        if len(qs) >= req.min_questions:
            break
        if bq["id"] not in seen and _applies(bq, len(res.subjects)):
            qs.append(bank_question(bq))
            seen.add(bq["id"])
    n_photos = len(req.photos)
    subjects = [
        s.model_copy(update={"photo_indices": [i for i in s.photo_indices if 0 <= i < n_photos]})
        for s in res.subjects
    ]
    return res.model_copy(update={"questions": qs[: req.max_questions], "subjects": subjects,
                                  "form": form})


def _applies(bq: dict[str, Any], n_subjects: int) -> bool:
    when = bq.get("when")
    return (not when or (when == "multi_subject" and n_subjects > 1)
            or (when == "single_subject" and n_subjects == 1))


def bank_question(bq: dict[str, Any]) -> Question:
    return Question(
        id=bq["id"], text=bq["text"][:120],
        options=[Option(id=o["id"], label=o["label"][:40]) for o in bq["options"]],
        multi=bool(bq.get("multi", False)), allow_text=bool(bq.get("allow_text", True)),
    )


def _call(model: str, purpose: str, started, t0: float, inp: int, out: int, cw: int, cr: int,
          request_id: str | None, *, billing: Billing = "api",
          usd_list: float | None = None, effort: str | None = None) -> ModelCall:
    listed = usd_for(model, inp, out, cw, cr) if usd_list is None else round(usd_list, 6)
    return ModelCall(
        vendor="anthropic", model=model, purpose=purpose, started_at=started,
        duration_s=round(time.monotonic() - t0, 3), input_tokens=inp, output_tokens=out,
        cache_read_tokens=cr, cache_write_tokens=cw,
        usd=listed if billing == "api" else 0.0, usd_list=listed, billing=billing,
        request_id=request_id, effort=effort,
    )


class AnthropicClaude:
    """Anthropic API with the turnkey key. Only when CLAUDE_BACKEND=api is set on purpose."""

    mock = False
    billing: Billing = "api"

    def __init__(self, api_key: str | None = None, client: Any = None):
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)
        self.client = client

    def estimate_intake(self, req: IntakeRequest) -> float:
        return estimate_usd(HAIKU, n_images=len(req.photos), text_chars=len(req.prompt) + 3000,
                            max_tokens=INTAKE_MAX_TOKENS)

    def estimate_route(self, req: TweakRequest) -> float:
        return estimate_usd(HAIKU, n_images=1 if req.image else 0,
                            text_chars=len(req.tweak) + len(req.concept_prompt) + 2000,
                            max_tokens=ROUTE_MAX_TOKENS)

    def estimate_color_spec(self, req: ColorSpecRequest) -> float:
        return estimate_usd(SONNET, n_images=len(req.photos), text_chars=len(req.text()) + 2000,
                            max_tokens=SPEC_MAX_TOKENS)

    async def _parse(self, *, purpose: str, system: str, content: list[dict], output: type,
                     max_tokens: int, model: str = HAIKU):
        started, t0 = utcnow(), time.monotonic()
        resp = await self.client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            output_format=output,
        )
        if getattr(resp, "stop_reason", None) == "refusal" or resp.parsed_output is None:
            raise VendorError(f"Claude {purpose} returned no structured output "
                              f"(stop_reason={getattr(resp, 'stop_reason', None)})",
                              code="validation", retryable=True)
        u = resp.usage
        call = _call(model, purpose, started, t0, u.input_tokens, u.output_tokens,
                     getattr(u, "cache_creation_input_tokens", 0) or 0,
                     getattr(u, "cache_read_input_tokens", 0) or 0,
                     getattr(resp, "_request_id", None))
        trace.record("claude", vendor="anthropic", model=HAIKU, purpose=purpose,
                     ref=call.request_id, started_at=started, duration_s=call.duration_s,
                     usd=call.usd, usd_list=call.usd_list, billing="api",
                     tokens={"in": call.input_tokens, "out": call.output_tokens,
                             "cache_read": call.cache_read_tokens,
                             "cache_write": call.cache_write_tokens},
                     input={"system": system,
                            "images": [b for b in content if b.get("type") == "image"],
                            "text": "\n".join(b.get("text", "") for b in content
                                              if b.get("type") == "text")},
                     output={"json": resp.parsed_output})
        return resp.parsed_output, call

    async def intake(self, req: IntakeRequest
                     ) -> tuple[IntakeResult, list[ColorRegion], ModelCall]:
        content = [_image_block(p) for p in req.photos]
        content.append({"type": "text", "text": intake_text(req)})
        draft, call = await self._parse(purpose="intake", system=intake_system(req),
                                        content=content, output=IntakeDraft,
                                        max_tokens=INTAKE_MAX_TOKENS)
        res, regions = finalize_intake(draft, req)
        return res, regions, call

    async def color_spec(self, req: ColorSpecRequest) -> tuple[ColorSpec, ModelCall]:
        content = [_image_block(p) for p in req.photos]
        content.append({"type": "text", "text": req.text()})
        draft, call = await self._parse(purpose="color_spec", system=COLOR_SPEC_SYSTEM,
                                        content=content, output=SpecDraft,
                                        max_tokens=SPEC_MAX_TOKENS, model=SONNET)
        return parse_spec(draft, req.subjects, len(req.photos)), call

    def estimate_check(self, n_images: int) -> float:
        return estimate_usd(CHECK_MODEL, n_images=n_images, text_chars=3000,
                            max_tokens=CHECK_MAX_TOKENS)

    async def check_concept(self, req: ConceptCheckRequest) -> tuple[ConceptCheck, ModelCall]:
        content = [_image_block(p) for p in req.images()]
        content.append({"type": "text", "text": req.text()})
        draft, call = await self._parse(purpose="check.concept", system=CONCEPT_CHECK_SYSTEM,
                                        content=content, output=CheckDraft,
                                        max_tokens=CHECK_MAX_TOKENS, model=CHECK_MODEL)
        return finalize_check(draft, req, CHECK_MODEL), call

    async def check_views(self, req: ViewCheckRequest) -> tuple[list[ViewAnswer], ModelCall]:
        content = [_image_block(p) for p in req.images()]
        content.append({"type": "text", "text": req.text()})
        draft, call = await self._parse(purpose="check.views", system=VIEW_CHECK_SYSTEM,
                                        content=content, output=ViewDraft,
                                        max_tokens=CHECK_MAX_TOKENS, model=CHECK_MODEL)
        return views_by_index(draft, len(req.views)), call

    async def route_tweak(self, req: TweakRequest) -> tuple[TweakDecision, ModelCall]:
        content: list[dict] = [_image_block(req.image)] if req.image else []
        hist = "\n".join(f"- {h}" for h in req.history) or "none"
        content.append({"type": "text", "text": (
            f"Current concept prompt: {req.concept_prompt}\nEarlier tweaks:\n{hist}\n"
            f"Concept re-render rounds left: {req.rounds_left}\nTweak: {req.tweak}"
        )})
        r, call = await self._parse(purpose="route.tweak", system=ROUTE_SYSTEM, content=content,
                                    output=TweakRouting, max_tokens=ROUTE_MAX_TOKENS)
        return TweakDecision(route=r.route, instruction=r.instruction,
                             followup_question=r.followup_question, model=HAIKU), call


# Env vars removed from the `claude` child so it can only use the OAuth login, never an API key.
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_TURNKEY", "ANTHROPIC_AUTH_TOKEN",
                "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")

Runner = Callable[[list[str], bytes, dict[str, str], float], Awaitable[tuple[int, bytes, bytes]]]


def child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}


async def run_subprocess(args: list[str], stdin: bytes, env: dict[str, str],
                         timeout: float) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise VendorError(f"claude -p timed out after {timeout:.0f}s", code="vendor_timeout",
                          retryable=True) from None
    except BaseException:  # cancelled (the stage watchdog, a shutdown): no orphan claude -p
        if proc.returncode is None:
            proc.kill()
        raise
    return proc.returncode or 0, out, err


def extract_json(text: str) -> Any:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in reply")
    return json.loads(t[start : end + 1])


class ClaudeCodeClaude:
    """Claude through Jackson's Claude Code OAuth login (`claude -p`). No API billing.

    Flags keep the child lean (about 21k tokens of Claude Code context otherwise): a custom
    --system-prompt, --setting-sources "", --strict-mcp-config, and no tools. Images go in as
    base64 content blocks over --input-format stream-json (vision="stream"), or as file paths
    read with the Read tool (vision="read"). Output is checked against the contract model, with
    one repair round on failure. Errors raise VendorError; nothing falls back to the API.
    """

    mock = False
    billing: Billing = "subscription"

    def __init__(self, cli: str = "claude", vision: str = "stream", timeout_s: float = 300.0,
                 runner: Runner | None = None, model: str = HAIKU):
        self.cli = cli
        self.vision = vision
        self.timeout_s = timeout_s
        self.runner = runner or run_subprocess
        self.model = model
        self.invocations: list[list[str]] = []  # args only; never env

    def estimate_intake(self, req: IntakeRequest) -> float:
        return 0.0  # subscription: no USD spend

    def estimate_route(self, req: TweakRequest) -> float:
        return 0.0

    def estimate_color_spec(self, req: ColorSpecRequest) -> float:
        return 0.0

    def _base_args(self, system: str, schema: dict[str, Any], max_turns: int,
                   model: str | None = None) -> list[str]:
        return [
            self.cli, "-p", "--model", model or self.model, "--system-prompt", system,
            "--setting-sources", "", "--strict-mcp-config",
            "--json-schema", json.dumps(schema, separators=(",", ":")),
            "--max-turns", str(max_turns), "--effort", "low",
        ]

    async def _invoke(self, system: str, text: str, images: list[Path],
                      schema: dict[str, Any], *, model: str | None = None,
                      max_tokens: int = INTAKE_MAX_TOKENS) -> dict[str, Any]:
        env = child_env()
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
        env["MAX_THINKING_TOKENS"] = "0"
        with tempfile.TemporaryDirectory(prefix="p2p_claude_") as tmp:
            if images and self.vision == "read":
                paths = []
                for i, p in enumerate(images):
                    data, mime = downscale_image(p, max_px=1024)
                    dst = Path(tmp) / f"photo_{i}.{'png' if mime == 'image/png' else 'jpg'}"
                    dst.write_bytes(data)
                    paths.append(str(dst))
                listing = "\n".join(f"{i}: {p}" for i, p in enumerate(paths))
                args = self._base_args(system, schema, len(paths) + 3, model) + [
                    "--output-format", "json", "--tools", "Read", "--allowedTools", "Read",
                    "--add-dir", tmp,
                ]
                stdin = (f"Read each photo file first (0-based index: path):\n{listing}\n\n"
                         f"{text}").encode()
            else:
                args = self._base_args(system, schema, 3, model) + [
                    "--tools", "", "--input-format", "stream-json",
                    "--output-format", "stream-json", "--verbose",
                ]
                content = [_image_block(p) for p in images] + [{"type": "text", "text": text}]
                stdin = (json.dumps({"type": "user",
                                     "message": {"role": "user", "content": content}}) + "\n"
                         ).encode()
            self.invocations.append([a if len(a) < 200 else a[:60] + "..." for a in args])
            rc, out, err = await self.runner(args, stdin, env, self.timeout_s)
        result = None
        for line in out.decode(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "result":
                result = obj
        if result is None:
            try:
                result = json.loads(out.decode(errors="replace"))
            except json.JSONDecodeError:
                result = None
        if rc != 0 or not isinstance(result, dict) or result.get("is_error"):
            detail = (err.decode(errors="replace")[-400:] if err else "") or str(
                (result or {}).get("result", ""))[:400]
            raise VendorError(f"claude -p failed (exit {rc}): {detail}", code="vendor_failed",
                              retryable=True)
        return result

    async def _ask(self, *, purpose: str, system: str, text: str, images: list[Path],
                   output: type[BaseModel], model: str | None = None,
                   max_tokens: int = INTAKE_MAX_TOKENS) -> tuple[BaseModel, ModelCall]:
        model = model or self.model
        started, t0 = utcnow(), time.monotonic()
        schema = output.model_json_schema()
        usage = {"in": 0, "out": 0, "cw": 0, "cr": 0, "usd": 0.0}
        session = None
        raw_text = ""
        payload = None
        error = ""
        for attempt in range(2):  # the first answer, then one repair round
            if attempt == 0:
                res = await self._invoke(system, text, images, schema, model=model,
                                         max_tokens=max_tokens)
            else:
                res = await self._invoke(system, (
                    f"Your previous answer did not validate: {error}\n"
                    f"Previous answer:\n{raw_text[:4000]}\n"
                    "Reply with the corrected JSON object only."), [], schema, model=model,
                    max_tokens=max_tokens)
            u = res.get("usage") or {}
            usage["in"] += int(u.get("input_tokens", 0))
            usage["out"] += int(u.get("output_tokens", 0))
            usage["cw"] += int(u.get("cache_creation_input_tokens", 0))
            usage["cr"] += int(u.get("cache_read_input_tokens", 0))
            usage["usd"] += float(res.get("total_cost_usd") or 0.0)
            session = res.get("session_id") or session
            raw_text = str(res.get("result") or "")
            try:
                obj = res.get("structured_output")
                if obj is None:
                    obj = extract_json(raw_text)
                payload = output.model_validate(obj)
                break
            except (ValueError, ValidationError) as e:
                error = str(e)[:1500]
        trace.record("claude", vendor="anthropic", model=model, purpose=purpose, ref=session,
                     started_at=started, duration_s=round(time.monotonic() - t0, 3),
                     status="ok" if payload is not None else "error",
                     error=None if payload is not None else error, usd_list=usage["usd"],
                     billing="subscription",
                     tokens={"in": usage["in"], "out": usage["out"], "cache_read": usage["cr"],
                             "cache_write": usage["cw"]},
                     input={"system": system, "text": text, "images": images},
                     output={"text": raw_text, "json": payload},
                     rounds=[{"round": 2, "kind": "repair",
                              "after_error": error if payload is not None else None}]
                     if attempt else None)
        if payload is None:
            raise VendorError(f"Claude {purpose} output failed validation twice: {error[:300]}",
                              code="validation", retryable=True)
        call = _call(model, purpose, started, t0, usage["in"], usage["out"], usage["cw"],
                     usage["cr"], session, billing="subscription", usd_list=usage["usd"],
                     effort="low")
        return payload, call

    async def ask(self, *, purpose: str, system: str, text: str, images: list[Path],
                  output: type[BaseModel], model: str | None = None,
                  max_tokens: int = INTAKE_MAX_TOKENS) -> tuple[BaseModel, ModelCall]:
        """One structured vision call (used by concept_read); same rules as intake."""
        return await self._ask(purpose=purpose, system=system, text=text, images=images,
                               output=output, model=model, max_tokens=max_tokens)

    async def intake(self, req: IntakeRequest
                     ) -> tuple[IntakeResult, list[ColorRegion], ModelCall]:
        system = intake_system(req) + "\nReply with one JSON object matching the schema only."
        draft, call = await self._ask(purpose="intake", system=system, text=intake_text(req),
                                      images=req.photos, output=IntakeDraft,
                                      max_tokens=INTAKE_MAX_TOKENS)
        res, regions = finalize_intake(draft, req)  # type: ignore[arg-type]
        return res, regions, call

    async def color_spec(self, req: ColorSpecRequest) -> tuple[ColorSpec, ModelCall]:
        system = COLOR_SPEC_SYSTEM + "\nReply with one JSON object matching the schema only."
        draft, call = await self._ask(purpose="color_spec", system=system, text=req.text(),
                                      images=req.photos, output=SpecDraft, model=SONNET,
                                      max_tokens=SPEC_MAX_TOKENS)
        return parse_spec(draft, req.subjects, len(req.photos)), call  # type: ignore[arg-type]

    def estimate_check(self, n_images: int) -> float:
        return 0.0

    async def check_concept(self, req: ConceptCheckRequest) -> tuple[ConceptCheck, ModelCall]:
        system = CONCEPT_CHECK_SYSTEM + "\nReply with one JSON object matching the schema only."
        draft, call = await self._ask(purpose="check.concept", system=system, text=req.text(),
                                      images=req.images(), output=CheckDraft, model=CHECK_MODEL,
                                      max_tokens=CHECK_MAX_TOKENS)
        return finalize_check(draft, req, CHECK_MODEL), call  # type: ignore[arg-type]

    async def check_views(self, req: ViewCheckRequest) -> tuple[list[ViewAnswer], ModelCall]:
        system = VIEW_CHECK_SYSTEM + "\nReply with one JSON object matching the schema only."
        draft, call = await self._ask(purpose="check.views", system=system, text=req.text(),
                                      images=req.images(), output=ViewDraft, model=CHECK_MODEL,
                                      max_tokens=CHECK_MAX_TOKENS)
        return views_by_index(draft, len(req.views)), call  # type: ignore[arg-type]

    async def route_tweak(self, req: TweakRequest) -> tuple[TweakDecision, ModelCall]:
        hist = "\n".join(f"- {h}" for h in req.history) or "none"
        text = (f"Current concept prompt: {req.concept_prompt}\nEarlier tweaks:\n{hist}\n"
                f"Concept re-render rounds left: {req.rounds_left}\nTweak: {req.tweak}")
        system = ROUTE_SYSTEM + "\nReply with one JSON object matching the given schema only."
        r, call = await self._ask(purpose="route.tweak", system=system, text=text,
                                  images=[req.image] if req.image else [], output=TweakRouting,
                                  max_tokens=ROUTE_MAX_TOKENS)
        return TweakDecision(route=r.route, instruction=r.instruction,  # type: ignore[attr-defined]
                             followup_question=r.followup_question,  # type: ignore[attr-defined]
                             model=self.model), call


MOCK_REGIONS: dict[str, list[tuple[str, str, str, str]]] = {
    "dog": [("head and ears", "tan", "base", "#C8955A"),
            ("muzzle, blaze and chest", "white", "marking", "#F2EEE8"),
            ("saddle on the back", "black", "marking", "#1E1C1A"),
            ("nose", "black", "feature", "#141414"), ("eyes", "dark brown", "feature", "#3A2616")],
    "cat": [("body", "gray tabby", "base", "#8A8A88"), ("chest and paws", "white", "marking",
                                                        "#F0F0EE"),
            ("nose", "pink", "feature", "#D99A9E"), ("eyes", "green", "feature", "#6E8B3D")],
    "object": [("body", "white", "base", "#F4F4F4")],
}
# region words -> PART_VOCAB part and label, for the mock color spec
MOCK_PARTS = (("eye", "eye", "eyes"), ("nose", "nose", "nose"), ("saddle", "marking", "saddle"),
              ("muzzle", "marking", "muzzle_blaze_chest"), ("chest", "marking", "chest_paws"),
              ("head", "body", "coat"), ("body", "body", "coat"))

# The order generate_multi_view returns its views in, for the mock view check.
# SOURCE: the multi-view probe in docs/research/meshy-iteration.md (section 3, "Probe result").
MOCK_VIEW_ANGLES: tuple[str, ...] = ("three_quarter", "front", "back")

ANIMALS = ("dog", "puppy", "cat", "kitten", "rabbit", "bunny", "bird", "parrot", "horse",
           "hamster", "guinea pig", "ferret", "turtle", "lizard", "person", "baby")


class MockClaude:
    """Deterministic stand-in. Same inputs, same outputs; token counts are fixed estimates."""

    mock = True
    billing: Billing = "free"

    def __init__(self) -> None:
        self.calls: list[str] = []
        # Tests script check failures: each concept check pops one set of item ids or kinds to
        # fail ("text" also misspells the name); each view check pops one set of 1-based view
        # numbers to fail. Empty: everything passes.
        self.check_script: list[set[str]] = []
        self.view_script: list[set[int]] = []
        self.view_angles: tuple[str, ...] = MOCK_VIEW_ANGLES
        self.check_requests: list[ConceptCheckRequest] = []

    def estimate_intake(self, req: IntakeRequest) -> float:
        return AnthropicClaude.estimate_intake(self, req)  # type: ignore[arg-type]

    def estimate_check(self, n_images: int) -> float:
        return AnthropicClaude.estimate_check(self, n_images)  # type: ignore[arg-type]

    async def check_concept(self, req: ConceptCheckRequest) -> tuple[ConceptCheck, ModelCall]:
        started, t0 = utcnow(), time.monotonic()
        self.calls.append("check_concept")
        self.check_requests.append(req)
        fail = self.check_script.pop(0) if self.check_script else set()
        answers = [CheckAnswer(id=i.id, ok=not (i.id in fail or i.kind in fail),
                               why="mock: not visible" if (i.id in fail or i.kind in fail)
                               else None) for i in req.items]
        read = req.expected_text
        if read and "text" in fail:
            read = read[:-1] + ("x" if read[-1:] != "x" else "y")
        draft = CheckDraft(items=answers, text_read=read)
        call = _call(CHECK_MODEL, "check.concept (mock)", started, t0,
                     len(req.images()) * IMAGE_TOKENS_EST + 900, 150, 0, 0,
                     f"mock-check-{len(self.check_requests)}", billing="free")
        trace.record("claude", vendor="anthropic", model=CHECK_MODEL, purpose=call.purpose,
                     ref=call.request_id, started_at=started, duration_s=call.duration_s,
                     usd_list=call.usd_list, billing="free",
                     tokens={"in": call.input_tokens, "out": call.output_tokens},
                     input={"system": CONCEPT_CHECK_SYSTEM, "text": req.text(),
                            "images": req.images()},
                     output={"json": draft})
        return finalize_check(draft, req, CHECK_MODEL), call

    async def check_views(self, req: ViewCheckRequest) -> tuple[list[ViewAnswer], ModelCall]:
        started, t0 = utcnow(), time.monotonic()
        self.calls.append("check_views")
        n_calls = self.calls.count("check_views")
        fail = self.view_script.pop(0) if self.view_script else set()
        n = len(req.views)
        answers = [ViewAnswer(index=i, angle=self.view_angles[(i - 1) % len(self.view_angles)],
                              ok=i not in fail, why="mock: markings moved" if i in fail else None)
                   for i in range(1, n + 1)]
        call = _call(CHECK_MODEL, "check.views (mock)", started, t0,
                     (n + 1) * IMAGE_TOKENS_EST + 700, 40 * n, 0, 0, f"mock-views-{n_calls}",
                     billing="free")
        trace.record("claude", vendor="anthropic", model=CHECK_MODEL, purpose=call.purpose,
                     ref=call.request_id, started_at=started, duration_s=call.duration_s,
                     usd_list=call.usd_list, billing="free",
                     tokens={"in": call.input_tokens, "out": call.output_tokens},
                     input={"system": VIEW_CHECK_SYSTEM, "text": req.text(),
                            "images": req.images()},
                     output={"json": ViewDraft(views=answers)})
        return answers, call

    def estimate_route(self, req: TweakRequest) -> float:
        return AnthropicClaude.estimate_route(self, req)  # type: ignore[arg-type]

    def estimate_color_spec(self, req: ColorSpecRequest) -> float:
        return AnthropicClaude.estimate_color_spec(self, req)  # type: ignore[arg-type]

    async def color_spec(self, req: ColorSpecRequest) -> tuple[ColorSpec, ModelCall]:
        """One part per intake color region (hex from the region, else a fixed gray)."""
        started, t0 = utcnow(), time.monotonic()
        self.calls.append("color_spec")
        lines = []
        for r in req.regions:
            part, label = next(((p, lab) for w, p, lab in MOCK_PARTS if w in r.region),
                               ("other", r.region))
            photo = next((s.photo_indices[0] for s in req.subjects
                          if s.id == r.subject_id and s.photo_indices), 0)
            lines.append(f"{r.subject_id}|{part}|{label}|{r.color}|{r.hex or '#808080'}|1|"
                         f"{photo}")
        spec = parse_spec(SpecDraft(parts=lines), req.subjects, len(req.photos))
        call = _call(SONNET, "color_spec (mock)", started, t0,
                     len(req.photos) * IMAGE_TOKENS_EST + 1200, 30 * len(lines), 0, 0,
                     "mock-color-spec", billing="free")
        return spec, call

    async def intake(self, req: IntakeRequest
                     ) -> tuple[IntakeResult, list[ColorRegion], ModelCall]:
        started, t0 = utcnow(), time.monotonic()
        self.calls.append("intake")
        text = req.prompt.lower()
        found: list[tuple[int, str]] = []
        for a in ANIMALS:
            for m in re.finditer(rf"\b{re.escape(a)}s?\b", text):
                found.append((m.start(), a))
        kinds = [a for _, a in sorted(found)] or ["object"]
        name = re.search(r"\bnamed?\s+([A-Z][a-zA-Z]+(?:\s+(?:and|&)\s+[A-Z][a-zA-Z]+)?)",
                         req.prompt)
        pet_names = re.split(r"\s+(?:and|&)\s+", name.group(1)) if name else []
        n_photos = max(1, len(req.photos))
        subjects = []
        for i, kind in enumerate(kinds[:4]):
            idx = [j for j in range(len(req.photos)) if j % len(kinds[:4]) == i] or \
                  list(range(len(req.photos)))
            subjects.append(Subject(
                id=f"s{i + 1}", name=pet_names[i] if i < len(pet_names) else f"the {kind}",
                kind=kind,
                description=f"{kind} from photo {idx[0] + 1 if idx else 1}", photo_indices=idx,
            ))
        applicable = [q for q in req.question_bank if _applies(q, len(subjects))]
        questions = [q["id"] for q in applicable[:4]]
        colors = [color_line(ColorRegion(subject_id=s.id, region=region, color=color, kind=kind,
                                         hex=hx))
                  for s in subjects
                  for region, color, kind, hx in MOCK_REGIONS.get(s.kind,
                                                                  MOCK_REGIONS["object"])]
        size = None
        m = re.search(r"(\d+(?:\.\d+)?)\s*(mm|cm|in)\b", text)
        if m:
            size = float(m.group(1)) * {"mm": 1, "cm": 10, "in": 25.4}[m.group(2)]
        addons = []
        if req.product_type == "nameplate" and name:
            addons.append(AddOn(kind=AddOnKind.NAMEPLATE,
                                text=TextSpec(text=name.group(1).replace(" and ", " & ")[:24])))
        draft = IntakeDraft(
            subjects=[DraftSubject(id=s.id, name=s.name, kind=s.kind, desc=s.description,
                                   photos=s.photo_indices) for s in subjects],
            colors=colors, questions=questions, product_type=req.product_type,
            target_size_mm=size, addons=addons, notes=None)
        inp = n_photos * IMAGE_TOKENS_EST + 900
        call = _call(HAIKU, "intake (mock)", started, t0, inp, 420, 0, 0, "mock-intake",
                     billing="free")
        trace.record("claude", vendor="anthropic", model=HAIKU, purpose=call.purpose,
                     ref=call.request_id, started_at=started, duration_s=call.duration_s,
                     usd_list=call.usd_list, billing="free",
                     tokens={"in": call.input_tokens, "out": call.output_tokens},
                     input={"system": INTAKE_SYSTEM, "images": req.photos,
                            "text": f"Request: {req.prompt}\nProduct type chosen in the app: "
                                    f"{req.product_type}\n{req.form_lines()}"},
                     output={"text": draft, "json": draft})
        res, regions = finalize_intake(draft, req)
        return res, regions, call

    async def route_tweak(self, req: TweakRequest) -> tuple[TweakDecision, ModelCall]:
        started, t0 = utcnow(), time.monotonic()
        self.calls.append("route_tweak")
        t = req.tweak.strip()
        low = t.lower()
        words = re.findall(r"[a-z']+", low)
        follow = None
        if t.endswith("?") or len(words) <= 2:
            route, follow = TweakRoute.ASK_FOLLOWUP, "Which part should change, and how?"
        elif re.search(r"\b(keyring|key ring|loop|hole|resize|bigger base|smaller|size)\b", low):
            route = TweakRoute.GEOMETRY_OP
        elif re.search(r"\bretexture|texture\b", low):
            route = TweakRoute.RETEXTURE
        elif re.search(r"\bregenerate|mesh|3d model\b", low):
            route = TweakRoute.REGENERATE_MESH
        elif re.search(r"\bpaint|filament\b", low):
            route = TweakRoute.PAINT_EDIT
        else:
            route = TweakRoute.RERENDER_CONCEPT
        call = _call(HAIKU, "route.tweak (mock)", started, t0,
                     (IMAGE_TOKENS_EST if req.image else 0) + 700, 120, 0, 0, "mock-route",
                     billing="free")
        trace.record("claude", vendor="anthropic", model=HAIKU, purpose=call.purpose,
                     ref=call.request_id, started_at=started, duration_s=call.duration_s,
                     usd_list=call.usd_list, billing="free",
                     tokens={"in": call.input_tokens, "out": call.output_tokens},
                     input={"system": ROUTE_SYSTEM, "text": f"Tweak: {req.tweak}",
                            "images": [req.image] if req.image else []},
                     output={"json": {"route": route.value, "instruction": t,
                                      "followup_question": follow}})
        return TweakDecision(route=route, instruction=t, followup_question=follow,
                             model=HAIKU), call


def make_claude(settings) -> ClaudeClient:
    """Pick the backend CLAUDE_BACKEND names. No automatic fallback between backends."""
    if settings.mock_claude or settings.claude_backend == "mock":
        return MockClaude()
    if settings.claude_backend == "claude_code":
        return ClaudeCodeClaude(cli=settings.claude_cli, vision=settings.claude_vision)
    if settings.claude_backend == "api":
        if not settings.anthropic_turnkey_key:
            raise VendorError("CLAUDE_BACKEND=api needs ANTHROPIC_API_KEY_TURNKEY",
                              code="validation", retryable=False)
        return AnthropicClaude(api_key=settings.anthropic_turnkey_key)
    raise ValueError(f"unknown CLAUDE_BACKEND {settings.claude_backend!r}")
