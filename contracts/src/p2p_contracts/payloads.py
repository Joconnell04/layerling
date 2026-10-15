"""Stage output payloads and Supabase `messages` payloads.

The worker and the web app talk only through Supabase rows:
- The web inserts `jobs` rows and user messages; the worker inserts assistant messages.
- messages.kind picks the payload model below (MESSAGE_PAYLOADS). payload is validated on both sides.
- The worker sets messages.consumed_at when it has acted on a user message.
- Concept loop: concept_pick with approve=false only marks the base candidate. The worker acts on the
  next `tweak` (its candidate_id, else the last picked). concept_pick with approve=true ends s03.
- Review: job.status = review, s09 stage status = waiting_user with gate kind "review", and a
  review_request message. A review_decision message resolves it.
- preview_glb carries per-vertex COLOR_0 in linear RGB (glTF spec); convert sRGB hex before writing.
Image and model paths in payloads are job-relative; the web turns them into signed Storage URLs.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from .job import AddOn, CustomAsk, ProductForm, ProductType
from .manifest import Strict
from .reports import SliceFilament

Lab = tuple[float, float, float]

# ---------------------------------------------------------------- s01 intake


class Option(Strict):
    id: str
    label: str = Field(max_length=40)


class Question(Strict):
    id: str
    text: str = Field(max_length=120)
    options: list[Option] = Field(min_length=2, max_length=6)
    multi: bool = False
    allow_text: bool = True


class Subject(Strict):
    id: str = Field(description="s1, s2, ...")
    name: str = Field(description="Pet name if given, else a short description")
    kind: str = Field(description="dog, cat, rabbit, object, ...")
    description: str
    photo_indices: list[int] = Field(description="0-based indices into job.photos")


class IntakeResult(Strict):
    subjects: list[Subject]
    questions: list[Question] = Field(min_length=0, max_length=4)
    product_type: str
    target_size_mm: float | None = None
    addons: list[AddOn] = Field(default_factory=list, description="e.g. nameplate text from answers")
    notes: str | None = None
    form: ProductForm | None = Field(
        None, description="Keychain or magnet form the request or the order form already set, "
        "so the form question is not asked. None: asked, or not a choice for this product")
    custom_asks: list[CustomAsk] = Field(
        default_factory=list, description="Props, outfits, poses, text the request asks for")


class Answer(Strict):
    question_id: str
    option_ids: list[str] = Field(default_factory=list)
    text: str | None = None


class AnswersPayload(Strict):
    answers: list[Answer]


class ProductSpec(Strict):
    """s01 output product.json (role product_spec): the one product decision per job that every
    later stage reads instead of picking its own default (docs/runs/fidelity/DEEP-DIVE.md,
    consistency rule 1)."""

    product_type: ProductType
    form: ProductForm
    target_size_mm: float | None = None
    addons: list[AddOn] = Field(default_factory=list)
    custom_asks: list[CustomAsk] = Field(default_factory=list)
    picks: dict[str, list[str]] = Field(
        default_factory=dict, description="Question id -> picked option ids (pose, style, ...)")
    decided_by: dict[str, Literal["user", "form", "request", "default"]] = Field(
        default_factory=dict, description="Who set form and size")


# ---------------------------------------------------------------- s02 color spec

PART_VOCAB: tuple[str, ...] = (
    "body", "head", "face", "muzzle", "eye", "pupil", "iris", "eye_highlight", "eyebrow",
    "nose", "mouth", "tongue", "teeth", "ear", "inner_ear", "marking", "paw", "paw_pad",
    "tail", "collar", "tag", "accessory", "base", "keyring", "text", "other",
)


class PartSpec(Strict):
    part: str = Field(description="One of PART_VOCAB")
    label: str = Field(description="Unique within subject, e.g. left_eye, back_patch")
    color_name: str
    hex: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    lab: Lab
    required: bool = True
    notes: str | None = None
    crop: str | None = Field(None, description="Job-relative path to the photo crop of this part")
    photo_index: int | None = None


class SubjectSpec(Strict):
    subject_id: str
    parts: list[PartSpec]


class ColorSpec(Strict):
    subjects: list[SubjectSpec]
    notes: str | None = None


# ---------------------------------------------------------------- s03 concept


class TweakRoute(StrEnum):
    RERENDER_CONCEPT = "rerender_concept"
    RETEXTURE = "retexture"
    REGENERATE_MESH = "regenerate_mesh"
    PAINT_EDIT = "paint_edit"
    GEOMETRY_OP = "geometry_op"
    ASK_FOLLOWUP = "ask_followup"


CheckKind = Literal["part", "text", "ask", "markings", "no_extra_base", "figure_identical",
                    "view"]


class CheckItem(Strict):
    """One line of a concept or view check."""

    id: str = Field(description="e.g. part.keyring_loop, text, ask.1, markings, no_extra_base")
    kind: CheckKind
    label: str = Field(max_length=160, description="What must be true, e.g. Name reads Bronco")
    on: Literal["figure", "product", "view"] = Field(
        description="figure: the figure-only image; product: the product concept; view: s04")
    passed: bool
    note: str | None = Field(None, max_length=300)
    by: Literal["model", "code"] = "model"


class ConceptCheck(Strict):
    """Claude vision check of one candidate against the photos and the request, plus code checks
    (figure pixels identical, exact name). A failed check starts a regeneration with changed
    inputs, capped per job (job.MAX_CONCEPT_FIXES)."""

    passed: bool
    items: list[CheckItem] = Field(default_factory=list)
    text_read: str | None = Field(None, description="The name as the model read it")
    model: str | None = None
    figure_identical: float | None = Field(
        None, description="Share of the figure's opaque pixels copied unchanged into the product "
        "concept (1.0 = identical; loops keep their attachment band)")


class ConceptCandidate(Strict):
    """One concept. `image` is the product concept the customer approves (the figure with the
    plate and name, the loop, or the base; the charm for a flat form). `figure_image` is the
    figure alone: the 3D input for a figure-form job and the source of a flat charm."""

    id: str = Field(description="r<round>c<n>, e.g. r1c2")
    round: int
    image_model: str = Field(description="gpt-image-2, nano-banana-pro, ...")
    image: str
    meshy_task_id: str | None = Field(
        None, description="The figure render task: tweaks chain from it, and s04 and s05 build a "
        "figure-form job from it")
    prompt: str
    figure_image: str | None = Field(None, description="None on jobs from before the split: "
                                     "the figure is `image`")
    form: ProductForm | None = None
    product_task_id: str | None = Field(
        None, description="Task that drew the product: the add-on edit (figure form) or the "
        "Creative Lab prototype that s05 builds (flat form)")
    product_source: Literal["figure", "edit", "code", "prototype"] = Field(
        "figure", description="figure: no visible add-on, the product is the figure; edit: an "
        "image edit added the add-ons and the figure pixels were pasted back; code: add-ons "
        "drawn in code; prototype: a Creative Lab charm with the loop drawn in code")
    check: ConceptCheck | None = None


class ConceptsPayload(Strict):
    round: int
    candidates: list[ConceptCandidate]
    rounds_left: int
    tweaks_exhausted: bool = Field(False, description="True once MAX_TWEAK_ROUNDS are used: pick only")
    form: ProductForm | None = None


class Resume(Strict):
    """User clears a pause (budget cap, retry limit). Optional new caps for this job."""

    cap_usd: float | None = None
    cap_credits: int | None = None
    note: str | None = None


class ConceptPick(Strict):
    candidate_id: str
    approve: bool = Field(description="True: approve and continue. False: pick as base to tweak.")


class Tweak(Strict):
    text: str
    candidate_id: str | None = None


class TweakDecision(Strict):
    route: TweakRoute
    instruction: str = Field(description="Tweak rewritten for the executor stage")
    followup_question: str | None = None
    model: str


# ---------------------------------------------------------------- s04 multi-view

ViewAngle = Literal["concept", "front", "left", "right", "back", "three_quarter", "top", "other"]


class View(Strict):
    angle: ViewAngle = Field(description="concept: the approved figure itself (usually 3/4)")
    image: str = Field(description="Job-relative PNG on the shared flat background")
    source: Literal["concept", "multiview"]
    meshy_task_id: str | None = None
    passed: bool = True
    note: str | None = None


class ViewSet(Strict):
    """s04 output views.json (role views). `views` is in the order s05 sends them: the approved
    figure first, then back, side, front (at most 4). Empty when skipped or when every view
    failed twice; s05 then builds from the figure alone."""

    views: list[View] = Field(default_factory=list)
    skipped: bool = False
    reason: str | None = None
    dropped: list[View] = Field(default_factory=list, description="Views that failed the check")
    checks: list[CheckItem] = Field(default_factory=list)


# ---------------------------------------------------------------- s05 mesh


class MeshReview(Strict):
    glb: str
    thumbnails: list[str] = Field(default_factory=list)
    meshy_task_id: str
    face_count: int | None = None
    actions: list[Literal["approve", "retexture", "regenerate"]] = Field(
        default_factory=lambda: ["approve", "retexture", "regenerate"]
    )
    retexture_credits: int = 10
    regenerate_credits: int | None = None


class MeshDecision(Strict):
    action: Literal["approve", "retexture", "regenerate"]
    text: str | None = None


# ---------------------------------------------------------------- result and review


class FilamentUsage(Strict):
    slot: int
    filament_id: str | None = None
    grams: float
    meters: float | None = None


class ProofImage(Strict):
    """One proof render: the product from one of regions.PROOF_VIEWS."""

    kind: Literal["model", "paint", "print"] = Field(
        description="model: the 3D model with its own texture; paint: filament colors as "
        "painted; print: the simulated print (slicer limits applied)")
    view: str = Field(description="A key of regions.PROOF_VIEWS")
    path: str = Field(description="Job-relative PNG")


class JobResult(Strict):
    threemf: str
    render_sheet: str | None = None
    preview_glb: str | None = Field(None, description="GLB with per-face filament colors")
    print_time_s: float | None = None
    filaments: list[FilamentUsage] = Field(default_factory=list)
    baseline_only: bool = Field(False, description="True for the M1 Meshy baseline 3MF")
    slice_ok: bool | None = None
    proof_images: list[ProofImage] = Field(
        default_factory=list, description="The shipped product from all 8 proof views, per kind")


class ReviewRequest(Strict):
    reason: str
    confidence: float | None = None
    checklist: str | None = Field(None, description="Job-relative path to checklist.json")
    images: list[str] = Field(default_factory=list)


class ReviewDecision(Strict):
    action: Literal["approve", "reject", "rerun_paint"]
    notes: str | None = None


class Note(Strict):
    text: str


class SpoolSuggestion(Strict):
    """A color the loaded spools cannot match; shown as "Add gray PLA, dE00 26.9 -> 3.1"."""

    suggestion: str = Field(description="Plain name, e.g. gray PLA")
    target_hex: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    target_lab: Lab
    delta_e_now: float = Field(description="CIEDE2000 to the best loaded spool")
    delta_e_with: float | None = Field(None, description="CIEDE2000 with the suggested spool")
    regions: list[str] = Field(default_factory=list, description="Region labels affected")
    area_pct: float | None = None
    filament_id: str | None = Field(
        None, description="Catalog filament id to apply in one click (palette_edit), when one matches"
    )


class PaletteEdit(Strict):
    """User changes a finished job's filaments. The worker re-runs the palette stage, then write
    and slice, as new attempts; the slicer gate still applies."""

    slots: list[str] = Field(
        default_factory=list, description="Filament ids in slot order; empty means auto-pick"
    )
    mode: Literal["recolor", "refit"] = Field(
        description="recolor: same slot count, regions painted with slot i take slots[i]. "
        "refit: re-match the model colors to the new set."
    )
    source: Literal["owned", "any"] = Field(
        "owned", description="any: may add unowned catalog colors"
    )
    max_colors: int | None = None
    note: str | None = None


# ---------------------------------------------------------------- paint edits

# Face sets travel as ranges of figure face ids (the order of the region map's mesh, the same
# order as face_regions.npy): "0-9,15,20-31". Ascending, no overlaps, decimal, no spaces.
FACE_RANGES_PATTERN = r"^(\d+(-\d+)?)(,\d+(-\d+)?)*$"
# Inline `faces` up to this many characters; a longer stroke goes to a file (`faces_path`).
FACES_INLINE_MAX = 200_000


class PaintOp(Strict):
    """One paint edit on a finished job, by region id, by face set, or by palette slot.

    The filament is `filament_id` (an owned spool or a catalog color) or `slot` (1-based in the
    palette as it stands when this op runs); exactly one of the two.
    - fill_region: the regions in `region_ids` take the filament.
    - brush_faces: the faces in `faces` (or the file at `faces_path`) become one new region in
      the filament, id = the region map's next_region_id at that point (ids are never reused).
    - replace_slot: everything painted with slot `from_slot` (regions, the default filament,
      add-on parts) takes the filament; the slot drops out when the filament is already in the
      palette.
    """

    op: Literal["fill_region", "brush_faces", "replace_slot"]
    filament_id: str | None = None
    slot: int | None = Field(None, ge=1)
    region_ids: list[int] = Field(default_factory=list, description="fill_region")
    faces: str | None = Field(
        None, pattern=FACE_RANGES_PATTERN, max_length=FACES_INLINE_MAX,
        description="brush_faces: figure face ids as ranges, e.g. 0-9,15,20-31")
    faces_path: str | None = Field(
        None, description="brush_faces, large strokes: job-relative text file holding the same "
        "ranges (the web uploads input/paint/<name>.txt to bucket inputs)")
    from_slot: int | None = Field(None, ge=1, description="replace_slot: the slot to replace")
    label: str | None = Field(None, max_length=60, description="brush_faces: new region label")

    @model_validator(mode="after")
    def _check(self) -> PaintOp:
        if (self.filament_id is None) == (self.slot is None):
            raise ValueError("give exactly one of filament_id and slot")
        if self.op == "fill_region" and not self.region_ids:
            raise ValueError("fill_region needs region_ids")
        if self.op == "brush_faces" and (self.faces is None) == (self.faces_path is None):
            raise ValueError("brush_faces needs exactly one of faces and faces_path")
        if self.op == "replace_slot" and self.from_slot is None:
            raise ValueError("replace_slot needs from_slot")
        return self


class PaintEdit(Strict):
    """User (or Claude) repaints a finished job. The worker applies `ops` in order to the paint
    in `base_attempt` as a new s09 attempt, re-runs the simulated print, writes the 3MF, slices
    it with p2p_slicer for the preview (reports.PaintPreview, role paint_preview), then runs s10
    as a new attempt: the same paint through the slicer gate (Bambu Studio where installed) and
    a new `result`."""

    ops: list[PaintOp] = Field(
        default_factory=list, max_length=500,
        description="Applied in order. Empty: preview the base paint as it is")
    base_attempt: str | None = Field(
        None, pattern=r"^s0[89]_[a-z]+/a[1-9][0-9]*$",
        description="Attempt folder of the paint the ops were drawn on, e.g. s09_paint/a2. "
        "Refused when the job's paint moved on since. None: the paint behind the current result")
    source: Literal["owned", "any"] = Field(
        "owned", description="any: ops may add unowned catalog colors")
    note: str | None = Field(None, max_length=500)


class PrintNumbers(Strict):
    """Time, grams and purge of one slice."""

    slicer: Literal["bambu_studio", "orcaslicer", "p2p_slicer"]
    print_time_s: float | None = None
    total_grams: float | None = None
    purge_grams: float | None = Field(None, description="Flush and prime tower")
    filament_changes: int | None = None
    layers: int | None = None
    filaments: list[SliceFilament] = Field(default_factory=list)


class SimSummary(Strict):
    """simulate_print in numbers."""

    changed_pct: float = Field(description="Share of the figure area the print repaints, percent")
    regions_lost: list[int] = Field(default_factory=list, description="Regions under 5% kept")
    bleed_flags: int = 0
    purge_g_est: float
    model_g_est: float
    swaps_total: int


class PaintPreview(Strict):
    """What a paint edit prints like: s09 paint_edit attempts write it as paint_preview.json
    (role paint_preview); the paint MCP tool preview_print returns it. `before` and `after` are
    both p2p_slicer numbers, so their difference is the edit's. The Bambu gate runs after (s10).

    Toolpaths (`toolpaths`, version 1): a JSON index next to a little-endian binary file.
      {"version": 1, "bin": "toolpaths.bin", "scale_mm": 0.01, "origin_mm": [x, y],
       "bbox_mm": [x0, y0, x1, y1], "slots": [{"slot", "filament_id", "hex"}], "roles": [...],
       "points": {"offset", "count"}, "chains": {"offset", "count"},
       "layers": [{"z", "height", "chains": [first, count], "time_s", "slots", "changes"}]}
    points: uint16 (x, y) pairs, mm = origin_mm + value * scale_mm. chains: uint32 triples
    (first point, point count, slot << 8 | role), one per continuous extrusion; layers list
    their chains in print order. Travel is left out; the prime tower is in (role Prime tower).
    """

    base_attempt: str | None = Field(None, description="Attempt folder of the paint before")
    ops: int = 0
    changed_faces: int = 0
    changed_regions: list[int] = Field(default_factory=list)
    palette: list[str]
    threemf: str = Field(description="Job-relative 3MF the after numbers are for")
    before: PrintNumbers | None = None
    after: PrintNumbers | None = Field(None, description="None when p2p_slicer could not slice")
    sim: SimSummary | None = None
    proof_images: list[ProofImage] = Field(
        default_factory=list, description="paint and print (simulated) from the 8 proof views")
    toolpaths: str | None = Field(None, description="Job-relative toolpaths.json")
    warnings: list[str] = Field(default_factory=list)
    timings_s: dict[str, float] = Field(default_factory=dict)


# ---------------------------------------------------------------- message registry

MessageRole = Literal["user", "assistant", "system"]

MESSAGE_PAYLOADS: dict[str, tuple[MessageRole, type[Strict] | None]] = {
    "prompt": ("user", None),  # body holds the request text
    "questions": ("assistant", IntakeResult),
    "answers": ("user", AnswersPayload),
    "concepts": ("assistant", ConceptsPayload),
    "concept_pick": ("user", ConceptPick),
    "tweak": ("user", Tweak),
    "tweak_decision": ("assistant", TweakDecision),
    "mesh_review": ("assistant", MeshReview),
    "mesh_decision": ("user", MeshDecision),
    "result": ("assistant", JobResult),
    "review_request": ("assistant", ReviewRequest),
    "review_decision": ("user", ReviewDecision),
    "note": ("assistant", Note),
    "resume": ("user", Resume),
    "spool_suggestion": ("assistant", SpoolSuggestion),
    "palette_edit": ("user", PaletteEdit),
    "paint_edit": ("user", PaintEdit),
}
