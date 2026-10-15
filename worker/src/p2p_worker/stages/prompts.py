"""Concept prompt rules, the question bank, and add-on resolution.

Text, loops, plates and bases are exact geometry built in s06 (p2p_geometry). The 3D input is
the figure alone (figure_prompt / concept_prompt); the customer sees the product: an edit adds
the loop, base or plate and name the way s06 builds them (edit_prompt), and the figure's own
pixels are pasted back over it (imaging.paste_back). A flat charm's art is a front view
(FLAT_LINES) that the Creative Lab prototype turns into the relief (meshy-iteration.md
section 4).
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from p2p_contracts.job import PRODUCT_DEFAULTS, AddOn, AddOnKind, CustomAsk, ProductType, TextSpec
from p2p_contracts.payloads import AnswersPayload, IntakeResult

BANK_PATH = Path(__file__).with_name("question_bank.yaml")

# PLAN.md prompt rules for stage 3 and 4, minus the base (now exact geometry, see module doc).
PROMPT_RULES = (
    "One connected sculpted figure: every subject touching the others so it prints as one solid "
    "piece, with a flat underside. Chunky proportions for a small print. No loose whiskers, no "
    "thin tails, no gaps between subjects. Neutral plain background. Flat even lighting. "
    "3/4 hero view."
)
NO_GEOMETRY = "No text, letters, keyring, loop, hole, plate, stand or base in the image."
FORBIDDEN = ("base", "plinth", "pedestal", "plate", "stand", "podium", "ground or floor",
             "text or letters", "keyring", "loop or hole", "frame, ring or border",
             "extra objects the request does not name")

# Meshy Image to Image has no negative-prompt field (only the deprecated, inert negative_prompt on
# 3D task objects), so the constraint goes in the prompt, phrased the way each model family
# follows best:
# - GPT Image: an explicit closing list of prohibitions.
# - Nano Banana (Gemini): a positive description of the empty space first (semantic negative
#   prompt), then a short "Avoid:" list at the end.
GPT_CONSTRAINT = "Hard constraints, do not draw any of these: " + ", ".join(FORBIDDEN) + "."
NANO_OPENING = ("The figure stands on its own feet with nothing under it, cut out and isolated "
                "on a plain light background like a product photo.")
NANO_CLOSING = "Avoid: " + ", ".join(FORBIDDEN) + "."


def for_model(prompt: str, model: str) -> str:
    """Add the no-base, no-plate, no-stand constraint in the model's preferred form."""
    if model.startswith("gpt-image"):
        return f"{prompt} {GPT_CONSTRAINT}"
    if model.startswith("nano-banana"):
        return f"{NANO_OPENING} {prompt} {NANO_CLOSING}"
    return f"{prompt} {GPT_CONSTRAINT}"

PRODUCT_LINES = {
    "figurine": "A small collectible figurine.",
    "keychain": (
        "A keychain charm figure: chunky and compact, head and body close together, no thin "
        "limbs, ears or tails sticking out."
    ),
    "nameplate": (
        "A figure that will sit on a flat name plate: a flat, wide underside so it stands "
        "steadily, compact pose."
    ),
    "ornament": "A hanging ornament figure: compact, rounded silhouette, no thin parts.",
    "magnet": (
        "A fridge magnet figure: facing forward, shallow depth, flat back, bold simple shapes."
    ),
    "other": "A small 3D-printed object.",
}
# Flat charm art: the Creative Lab prototype turns this front view into a depth relief.
FLAT_LINES = {
    "keychain": ("Art for a flat relief keychain charm: the subject facing straight forward, "
                 "front view, bold simple shapes and clear color areas, compact silhouette."),
    "magnet": ("Art for a flat relief fridge magnet: the subject facing straight forward, front "
               "view, bold simple shapes and clear color areas."),
}
# live2 catfish: both charms came back inside a decorative ring frame nobody asked for (the art
# had a sticker-card outline, "compact rounded outline"). The art is the subject alone.
FLAT_RULES = ("One connected subject, front view, facing the viewer. The subject alone: no "
              "frame, ring, border, card or badge around it. Neutral plain background. Flat "
              "even lighting.")
# Deep dive item 8: at small sizes specks under 2 x nozzle (0.8 mm) cannot print.
SMALL_PRINT_MM = 60.0  # ASSUMED threshold for the bold-markings rule
SMALL_RULE = ("Bold simple markings, no fine speckle or ticking: details under 1 mm do not print "
              "at this size.")


@cache
def load_question_bank() -> dict[str, list[dict[str, Any]]]:
    return yaml.safe_load(BANK_PATH.read_text())


def bank_for(product_type: str) -> list[dict[str, Any]]:
    bank = load_question_bank()
    return bank.get(product_type) or bank["other"]


def default_addons(product_type: str) -> list[AddOn]:
    pt = ProductType(product_type) if product_type in ProductType._value2member_map_ \
        else ProductType.OTHER
    return [AddOn(kind=k) for k in PRODUCT_DEFAULTS[pt]["addons"]]


def default_size(product_type: str) -> float | None:
    pt = ProductType(product_type) if product_type in ProductType._value2member_map_ \
        else ProductType.OTHER
    return PRODUCT_DEFAULTS[pt].get("target_size_mm")


def subject_names(intake: IntakeResult) -> str:
    names = [re.sub(r"^(the|my|a|an)\s+", "", s.name.strip(), flags=re.IGNORECASE) for s in intake.subjects]
    names = [n[:1].upper() + n[1:] for n in names if n]
    return " & ".join(names)[:24] or "Best Friends"


def answer_lines(intake: IntakeResult, answers: AnswersPayload | None) -> list[str]:
    """What the picked options mean for the image, never the question text.

    Bank options carry a `prompt` phrase (e.g. "the two subjects hugging each other"); an option
    without one uses its label. Loops, plates, names and sizes are exact geometry, so questions
    whose options change add-ons or job fields add nothing here."""
    if not answers:
        return []
    by_id = {q.id: q for q in intake.questions}
    bank = {q["id"]: q for q in load_question_bank_flat()}
    lines = []
    for a in answers.answers:
        q = by_id.get(a.question_id)
        bq = bank.get(a.question_id, {})
        geometry_only = bq.get("text_addon") or any(
            o.get("addon") or o.get("remove") or o.get("sets") for o in bq.get("options", []))
        if not q or geometry_only:
            continue
        meanings = {o["id"]: o.get("prompt", o["label"].lower()) for o in bq.get("options", [])
                    if not o.get("ask")}  # asks go in ask_line, once
        for o in q.options:
            if o.id in a.option_ids:
                phrase = meanings.get(o.id, "" if o.id in bank_ask_ids(bq) else o.label.lower())
                if phrase:
                    lines.append(phrase[0].upper() + phrase[1:] + ".")
    return lines


def bank_ask_ids(bq: dict[str, Any]) -> set[str]:
    return {o["id"] for o in bq.get("options", []) if o.get("ask")}


# What a picked option's meaning is called on the checklist. Other questions use their text.
ANSWER_TOPICS = {"pose": "Pose", "pose_group": "Pose", "style": "Look", "framing": "Shows"}


def answer_checks(product_type: str, answers: AnswersPayload | None) -> list[tuple[str, str]]:
    """(question id, what the concept must show) for every intake pick an image can show: the
    option's `prompt` meaning (pose, look, framing), the same phrase the concept prompt got.

    live2 Bronco: the answer was "sitting", the approved concept stood, and nothing checked it.
    Other picks are checked elsewhere: asks (collar, toy) as ask items; the plate shape and the
    loop spot by the part items, whose labels carry the pick; the name by the text item; the
    form by part.relief or answer.form. Size and geometry-only picks are not in the image."""
    out: list[tuple[str, str]] = []
    if not answers:
        return out
    by_id = {q["id"]: q for q in bank_for(product_type)}
    for a in answers.answers:
        q = by_id.get(a.question_id)
        if not q:
            continue
        geometry_only = q.get("text_addon") or any(
            o.get("addon") or o.get("remove") or o.get("sets") for o in q.get("options", []))
        if geometry_only:
            continue
        topic = ANSWER_TOPICS.get(q["id"], str(q.get("text", q["id"])).rstrip("?"))
        for o in q.get("options", []):
            if o["id"] not in a.option_ids or o.get("ask"):
                continue
            meaning = str(o.get("prompt", o["label"].lower())).strip()
            if meaning:
                out.append((q["id"], f"{topic}: {meaning}"[:160]))
    return out


def answer_asks(product_type: str, answers: AnswersPayload | None) -> list[CustomAsk]:
    """Custom asks from the answers: options marked `ask` in the bank, and any text the customer
    typed into a question that is not a name field."""
    out: list[CustomAsk] = []
    if not answers:
        return out
    by_id = {q["id"]: q for q in bank_for(product_type)}
    for a in answers.answers:
        q = by_id.get(a.question_id, {})
        for o in q.get("options", []):
            if o["id"] in a.option_ids and o.get("ask"):
                out.append(CustomAsk(kind=o["ask"], text=o.get("prompt") or o["label"].lower(),
                                     source="answer"))
        geometry_only = q.get("text_addon") or any(
            o.get("addon") or o.get("remove") or o.get("sets") for o in q.get("options", []))
        if a.text and a.text.strip() and not geometry_only:
            out.append(CustomAsk(kind="other", text=a.text.strip().rstrip(".")[:120],
                                 source="answer"))
    return out


def load_question_bank_flat() -> list[dict[str, Any]]:
    return [q for qs in load_question_bank().values() for q in qs]


def option_effects(product_type: str, answers: AnswersPayload | None) -> dict[str, Any]:
    """Job fields set by picked options (e.g. target_size_mm)."""
    out: dict[str, Any] = {}
    if not answers:
        return out
    by_id = {q["id"]: q for q in bank_for(product_type)}
    for a in answers.answers:
        q = by_id.get(a.question_id)
        if not q:
            continue
        for o in q["options"]:
            if o["id"] in a.option_ids and o.get("sets"):
                out.update(o["sets"])
    return out


def resolve_addons(product_type: str, intake: IntakeResult, answers: AnswersPayload | None,
                   form_addons: list[AddOn] | None = None) -> list[AddOn]:
    """Product defaults, then what intake inferred, then the new-job form, then the answers.
    The form wins over intake; only a customer's answer overrides the form."""
    addons: dict[str, AddOn] = {a.kind.value: a for a in default_addons(product_type)}
    for a in intake.addons:
        addons[a.kind.value] = a
    for a in form_addons or []:
        cur = addons.get(a.kind.value)
        if cur is not None:  # keep intake's details the form left empty
            a = a.model_copy(update={"text": a.text or cur.text,
                                     "params": {**cur.params, **a.params}})
        addons[a.kind.value] = a
    by_id = {q["id"]: q for q in bank_for(product_type)}
    for ans in (answers.answers if answers else []):
        q = by_id.get(ans.question_id)
        if not q:
            continue
        for o in q["options"]:
            if o["id"] not in ans.option_ids:
                continue
            if o.get("remove"):
                addons.pop(o["remove"], None)
            if o.get("addon"):
                kind = o["addon"]["kind"]
                cur = addons.get(kind) or AddOn(kind=AddOnKind(kind))
                params = {**cur.params, **(o["addon"].get("params") or {})}
                addons[kind] = cur.model_copy(update={"params": params})
            src = o.get("text_from")
            target = q.get("text_addon")
            if src and target:
                text = (ans.text or "").strip() if src == "answer" else subject_names(intake)
                if not text:
                    text = subject_names(intake)
                cur = addons.get(target) or AddOn(kind=AddOnKind(target))
                spec = (cur.text or TextSpec(text=text[:24])).model_copy(update={"text": text[:24]})
                addons[target] = cur.model_copy(update={"text": spec})
    nameplate = addons.get(AddOnKind.NAMEPLATE.value)
    if nameplate is not None and nameplate.text is None:
        addons[AddOnKind.NAMEPLATE.value] = nameplate.model_copy(
            update={"text": TextSpec(text=subject_names(intake))})
    return list(addons.values())


def color_lines(intake: IntakeResult, regions: list[dict] | None) -> list[str]:
    """Every color region, markings included, as its own clause the image must keep."""
    if not regions:
        return []
    names = {s.id: s.name for s in intake.subjects}
    out = []
    for sid, name in names.items():
        mine = [r for r in regions if r.get("subject_id") == sid]
        if mine:
            out.append(f"{name} colors, keep every one: "
                       + "; ".join(f"{r['color']} {r['region']}" for r in mine) + ".")
    return out


def ask_line(asks) -> str:
    """The customer's custom asks as one clause the image must keep."""
    items = [a.text for a in asks or [] if a.buildable and a.kind != "text"]
    return ("Must show: " + "; ".join(items) + ".") if items else ""


def concept_prompt(request: str, product_type: str, intake: IntakeResult,
                   answers: AnswersPayload | None, size_mm: float | None,
                   regions: list[dict] | None = None, *, form: str | None = None,
                   asks: list | None = None) -> str:
    """Model-agnostic prompt for the figure-only image; for_model() adds the per-model
    constraint. A flat form asks for front-view charm art instead of a 3/4 figure."""
    subjects = "; ".join(f"{s.name} ({s.kind}): {s.description.split('. Colors:')[0]}"
                         for s in intake.subjects)
    flat = form == "flat"
    lines = FLAT_LINES if flat else PRODUCT_LINES
    parts = [
        lines.get(product_type, lines.get("keychain") if flat else PRODUCT_LINES["other"]),
        f"Request: {request.strip()}",
        f"Subjects, matching the reference photos: {subjects}." if subjects else "",
        *(color_lines(intake, regions) or [f"Colors: {s.description}" for s in intake.subjects
                                           if ". Colors:" in s.description]),
        *answer_lines(intake, answers),
        ask_line(asks),
        f"Finished size about {size_mm:.0f} mm." if size_mm else "",
        SMALL_RULE if size_mm and size_mm < SMALL_PRINT_MM else "",
        f"Notes: {intake.notes}" if intake.notes else "",
        FLAT_RULES if flat else PROMPT_RULES,
    ]
    return " ".join(p for p in parts if p)


def fix_prompt(base_prompt: str, issues: list[str]) -> str:
    """The same figure prompt with what the concept check found wrong, so a regeneration never
    resends identical inputs (Meshy has no seed)."""
    return f"{base_prompt} Correct these problems from the last try: {'; '.join(issues)}."


def tweak_prompt(base_prompt: str, instruction: str, asks: list | None = None,
                 form: str | None = None) -> str:
    keep = ask_line(asks)
    return (
        f"Edit this figure concept: {instruction.strip()}. Keep everything else the same, "
        f"including the subjects' likeness and every color marking. "
        f"{(keep.replace('Must show:', 'Keep showing:') + ' ') if keep else ''}"
        f"{FLAT_RULES if form == 'flat' else PROMPT_RULES}"
    )


# ---------------------------------------------------------------- product edit (add-ons)
LOOP_SPOTS = {"top": "on top of the head", "back": "on the middle of the back",
              "left": "on the left side of the head", "right": "on the right side of the head",
              "any": "on top of the figure, right above its middle, where it balances"}
# How p2p_geometry lays out a nameplate (product.py _layout_nameplate): the figure stands on
# one end, about 0.42 of the plate width tall; the name fills the rest of the top.
NAMEPLATE_FIGURE_FRAC = 0.42
PLATE_SHAPES = {"rectangle": "rectangular", "rounded": "rounded-corner rectangular",
                "oval": "oval"}
KEEP_FIGURE = ("Keep the figure itself exactly as it is: same pose, proportions, colors and "
               "markings; do not redraw it. Only its size and place in the frame may change, to "
               "make room for what is added.")
# A plate is wide: its edit gets a wide frame so the figure stays large (Meshy aspect ratios:
# nano-banana 16:9, gpt-image 3:2; SOURCE: docs.meshy.ai/api/image-to-image).
WIDE_ASPECT = {"nano-banana": "16:9", "gpt-image": "3:2"}


def edit_aspect(addons: list[AddOn], model: str) -> str:
    if any(a.kind == AddOnKind.NAMEPLATE for a in addons):
        return next((v for k, v in WIDE_ASPECT.items() if model.startswith(k)), "1:1")
    return "1:1"


def addon_phrases(addons: list[AddOn]) -> tuple[list[str], set[str]]:
    """What the edit adds, phrased the way s06 builds it, and the FORBIDDEN words it may draw."""
    out, allowed = [], set()
    for a in addons:
        k = a.kind
        if k in (AddOnKind.KEYRING_LOOP, AddOnKind.HANGING_LOOP):
            spot = LOOP_SPOTS.get(str(a.params.get("position", "top")), LOOP_SPOTS["top"])
            what = "keyring loop" if k == AddOnKind.KEYRING_LOOP else "hanging loop"
            out.append(f"Add a small round {what} tab {spot}, attached to the figure and the same "
                       f"color as the fur it touches, with a round hole through it.")
            allowed |= {"keyring", "loop or hole"}
        elif k in (AddOnKind.ROUND_BASE, AddOnKind.OVAL_BASE):
            shape = "round" if k == AddOnKind.ROUND_BASE else "oval"
            out.append(f"Stand the figure on a low, plain {shape} base, a smooth disc a little "
                       f"wider than the figure, in one solid color.")
            allowed |= {"base"}
        elif k == AddOnKind.NAMEPLATE:
            shape = PLATE_SHAPES.get(str(a.params.get("shape", "rounded")), "rectangular")
            side = "right" if str(a.params.get("position", "left")) == "right" else "left"
            other = "left" if side == "right" else "right"
            text = a.text.text if a.text else ""
            out.append(
                f"Stand the figure on the {side} end of a long, low, flat {shape} name plate in "
                f"one solid color, about {1 / NAMEPLATE_FIGURE_FRAC:.1f} times as wide as the "
                f"figure is tall. On the plate's top surface, to the {other} of the figure, "
                f"the name \"{text}\" in raised rounded bold letters, spelled exactly "
                f"\"{text}\" and nothing else.")
            allowed |= {"plate", "text or letters", "base"}
    return out, allowed


def edit_prompt(addons: list[AddOn], fix: list[str] | None = None) -> tuple[str, set[str]]:
    lines, allowed = addon_phrases(addons)
    parts = [*lines, KEEP_FIGURE, "Plain light background, same lighting."]
    if fix:
        parts.append(f"Correct these problems from the last try: {'; '.join(fix)}.")
    return " ".join(parts), allowed


def for_model_edit(prompt: str, model: str, allowed: set[str]) -> str:
    """The no-extra-parts constraint for an add-on edit: FORBIDDEN minus what the edit adds."""
    banned = [w for w in FORBIDDEN if w not in allowed]
    if not banned:
        return prompt
    if model.startswith("nano-banana"):
        return f"{prompt} Avoid: {', '.join(banned)}."
    return f"{prompt} Hard constraints, do not draw any of these: {', '.join(banned)}."


VIEW_PROMPT = (
    "The same figure seen from the front, the left side and the back. Keep proportions, pose, "
    "every color and every marking identical. Same plain light background, same lighting, same "
    "scale, centered with margin. No base, plate, stand, text, keyring or loop."
)


def view_prompt(asks: list | None = None, fix: list[str] | None = None) -> str:
    keep = ask_line(asks)
    parts = [VIEW_PROMPT, keep.replace("Must show:", "Keep showing:") if keep else ""]
    if fix:
        parts.append(f"Correct these problems from the last try: {'; '.join(fix)}.")
    return " ".join(p for p in parts if p)
