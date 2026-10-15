"""The product a job builds, as s03 and s04 need it: form, visible add-ons, the name, the
customer's asks, and the checklist every concept must pass.

One place decides what the concept shows, so the concept, the 3D model and the print keep the
same idea (FROM JACKSON): a nameplate concept shows the figure on the plate with the name; a
keychain concept shows the loop on the head; a flat charm is a relief with its loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from p2p_contracts.job import (
    FORM_CHOICE_PRODUCTS,
    FORM_DEFAULTS,
    AddOn,
    AddOnKind,
    CustomAsk,
    JobRecord,
    ProductForm,
    ProductType,
    product_form,
)
from p2p_contracts.payloads import AnswersPayload

from .. import imaging
from ..clients.claude import CheckSpec
from .prompts import LOOP_SPOTS, PLATE_SHAPES, answer_checks, edit_prompt

# Add-ons the product concept shows. Flat backs and magnet pockets are behind the figure.
VISIBLE = (AddOnKind.KEYRING_LOOP, AddOnKind.HANGING_LOOP, AddOnKind.ROUND_BASE,
           AddOnKind.OVAL_BASE, AddOnKind.NAMEPLATE)
LOOPS = (AddOnKind.KEYRING_LOOP, AddOnKind.HANGING_LOOP)
FLAT_NAME_MAX = 10  # Creative Lab Keychain name_text limit (SOURCE: docs.meshy.ai)
# Nothing unrequested gets added (FROM JACKSON). Checked on the figure (the 3D input) and on the
# product; the vision check names the extra thing in its note.
NO_EXTRAS = ("Nothing the customer did not ask for: no frame, ring, border, backdrop, scenery, "
             "blocks or extra objects")
NO_EXTRAS_PRODUCT = ("Nothing added beyond the listed parts: no frame, ring, border, backdrop, "
                     "blocks or extra objects")
NO_EXTRAS_FLAT = ("Nothing the customer did not ask for around the subject: no decorative "
                  "frame, ring, border, pattern or extra objects")


@dataclass
class ProductPlan:
    product_type: str
    form: ProductForm
    size_mm: float | None
    addons: list[AddOn] = field(default_factory=list)
    asks: list[CustomAsk] = field(default_factory=list)  # buildable ones only
    regions: list[dict] = field(default_factory=list)
    # intake picks the image can show, (question id, what it must show): prompts.answer_checks
    picks: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def from_job(cls, job: JobRecord, regions: list[dict] | None = None,
                 answers: AnswersPayload | None = None) -> ProductPlan:
        form = product_form(job.product_type, job.form) or FORM_DEFAULTS.get(
            job.product_type, ProductForm.FIGURE)
        return cls(product_type=job.product_type.value, form=form,
                   size_mm=job.target_size_mm, addons=list(job.addons),
                   asks=[a for a in job.custom_asks if a.buildable],
                   regions=list(regions or []),
                   picks=answer_checks(job.product_type.value, answers))

    # ------------------------------------------------------------ what the concept shows
    @property
    def flat(self) -> bool:
        return self.form == ProductForm.FLAT

    @property
    def creative_lab(self) -> str | None:
        """The Creative Lab product a flat charm is built with (meshy-iteration.md section 4)."""
        if not self.flat:
            return None
        return "magnet" if self.product_type == ProductType.MAGNET.value else "keychain"

    def visible(self) -> list[AddOn]:
        """Add-ons drawn on the product concept."""
        if self.flat:
            return [a for a in self.addons if a.kind in LOOPS]
        return [a for a in self.addons if a.kind in VISIBLE]

    def loop(self) -> AddOn | None:
        return next((a for a in self.visible() if a.kind in LOOPS), None)

    @property
    def name_text(self) -> str | None:
        """The name the product shows: the nameplate's text, or a flat keychain's engraving."""
        plate = next((a for a in self.addons if a.kind == AddOnKind.NAMEPLATE), None)
        if plate is not None and not self.flat:
            return plate.text.text if plate.text else None
        if self.creative_lab == "keychain":
            t = next((a.text for a in self.asks if a.kind == "text"), None)
            return t if t and len(t) <= FLAT_NAME_MAX else None
        return None

    def edit_kind(self) -> str | None:
        """None: the figure is the product (no edit). "text": a name to draw. "plain": a loop
        or base."""
        if self.flat or not self.visible():
            return None
        return "text" if any(a.kind == AddOnKind.NAMEPLATE for a in self.visible()) else "plain"

    def paste_mode(self) -> str:
        return "loop" if self.loop() is not None else "full"

    def edit(self, fix: list[str] | None = None) -> tuple[str, set[str]]:
        return edit_prompt(self.visible(), fix)

    # ------------------------------------------------------------ checklist
    def part_label(self, a: AddOn) -> str:
        if a.kind in LOOPS:
            spot = LOOP_SPOTS.get(str(a.params.get("position", "top")), LOOP_SPOTS["top"])
            if self.flat:
                spot = "at the charm's top edge" if str(a.params.get("position", "top")) \
                    in ("top", "back") else f"on the charm's {a.params.get('position')} edge"
            what = "keyring loop" if a.kind == AddOnKind.KEYRING_LOOP else "hanging loop"
            return f"A {what} {spot}"
        if a.kind in (AddOnKind.ROUND_BASE, AddOnKind.OVAL_BASE):
            return f"A {'round' if a.kind == AddOnKind.ROUND_BASE else 'oval'} base under the figure"
        shape = PLATE_SHAPES.get(str(a.params.get("shape", "rounded")), "rectangular")
        art = "An" if shape[0] in "aeiou" else "A"
        return f"{art} {shape} name plate with the figure standing on one end"

    def check_items(self) -> list[CheckSpec]:
        """What every concept of this job must show. Figure items judge the figure render (the
        3D input); product items judge what the customer approves. A flat charm is judged on
        the charm itself, which is what gets built.

        Every intake answer the image can show is on the list (FROM JACKSON: custom asks and
        picks survive from the request to the print): the form (part.relief for a flat charm,
        answer.form for a 3D keychain or magnet), the plate shape and the loop spot (in the part
        labels), the name (text), the pose, look and framing (answer.<question id>), and every
        custom ask (ask.<n>). no_extras flags what nobody asked for (live2 catfish: a decorative
        ring frame; Bronco r1c2: a stray wedge block in front of the dog). Answers ride on the
        existing check kinds (ask, no_extra_base); see worker README contract change request 7."""
        fig_on = "product" if self.flat else "figure"
        items: list[CheckSpec] = []
        if self.flat:
            items.append(CheckSpec("part.relief", "part",
                                   "A flat relief charm seen from the front", "product"))
        for a in self.visible():
            items.append(CheckSpec(f"part.{a.kind.value}", "part", self.part_label(a), "product"))
        if self.name_text:
            items.append(CheckSpec("text", "text", f"The name reads exactly {self.name_text}",
                                   "product"))
        for i, a in enumerate(a for a in self.asks if a.kind != "text"):
            items.append(CheckSpec(f"ask.{i + 1}", "ask", a.text, fig_on))
        if not self.flat and self.product_type in {p.value for p in FORM_CHOICE_PRODUCTS}:
            items.append(CheckSpec("answer.form", "ask",
                                   "Form: a 3D sculpted figure with depth, not a flat relief, "
                                   "badge or cutout", "figure"))
        seen: dict[str, int] = {}
        for qid, what in self.picks:
            seen[qid] = seen.get(qid, 0) + 1
            key = f"answer.{qid}" if seen[qid] == 1 else f"answer.{qid}.{seen[qid]}"
            items.append(CheckSpec(key, "ask", what, fig_on))
        if self.regions:
            colors = "; ".join(f"{r.get('color')} {r.get('region')}" for r in self.regions
                               if r.get("kind") in ("base", "marking"))[:120]
            items.append(CheckSpec("markings", "markings",
                                   f"Coat colors and markings true to the photos: {colors}",
                                   fig_on))
        items.append(CheckSpec("no_extra_base", "no_extra_base",
                               "No base, plinth, stand or ground under the figure", fig_on))
        if self.visible() and not self.flat:
            items.append(CheckSpec("no_extra_base.product", "no_extra_base",
                                   "Nothing under the figure except the parts listed here",
                                   "product"))
        items.append(CheckSpec("no_extras", "no_extra_base", NO_EXTRAS_FLAT if self.flat
                               else NO_EXTRAS, fig_on))
        if self.visible() and not self.flat:
            items.append(CheckSpec("no_extras.product", "no_extra_base", NO_EXTRAS_PRODUCT,
                                   "product"))
        return items

    # ------------------------------------------------------------ free fallback
    def composite(self, figure: Path, out: Path) -> dict:
        """Draw the visible add-ons in code around the unchanged figure."""
        vis = self.visible()
        plate = next((a for a in vis if a.kind == AddOnKind.NAMEPLATE), None)
        if plate is not None:
            return imaging.composite_nameplate(
                figure, out, self.name_text or "", shape=str(plate.params.get("shape", "rounded")),
                side="right" if str(plate.params.get("position", "left")) == "right" else "left")
        base = next((a for a in vis if a.kind in (AddOnKind.ROUND_BASE, AddOnKind.OVAL_BASE)),
                    None)
        loop = self.loop()
        info: dict = {}
        src = figure
        if base is not None:
            info = imaging.composite_base(src, out, base.kind.value)
            src = out
        if loop is not None:
            pos = str(loop.params.get("position", "top"))
            info = {**info, **imaging.composite_loop(src, out,
                                                     "top" if pos == "back" else pos)}
        return info
