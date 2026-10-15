"""Job record and job-folder layout.

jobs/<job_id>/
  job.json                 JobRecord (worker-owned; mirrored to Supabase `jobs`)
  input/                   uploaded photos (photo_1.jpg ...), never modified
  messages.jsonl           mirror of Supabase `messages` for this job, append-only
  ledger.jsonl             one SpendEvent per line, append-only
  <stage_id>/a<N>/         one folder per stage attempt: artifacts + manifest.json
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field

from .manifest import SCHEMA_VERSION, Strict
from .stages import JobStatus, StageId

# Per-job hard caps (plan). Hitting either pauses the job for Jackson.
JOB_CAP_USD = 10.0
JOB_CAP_CREDITS = 400
MAX_STAGE_RETRIES = 2
MAX_TWEAK_ROUNDS = 5
MAX_PAINT_ITERATIONS = 8
# Paid concept regenerations the s03 concept check may start per job (a new figure render or a
# second add-on edit). ASSUMED: 4 covers one figure fix and one edit retry on each of the two
# round-1 candidates. Code composites after that are free.
MAX_CONCEPT_FIXES = 4
# Credit to USD conversion for budgeting. Pro is about $0.02 per credit (plan); use the worse rate.
USD_PER_MESHY_CREDIT = 0.02
# Worker heartbeat: upsert public.workers every WORKER_HEARTBEAT_S; the web shows online if last_seen
# is under WORKER_ONLINE_S old.
WORKER_HEARTBEAT_S = 15
WORKER_ONLINE_S = 60


class ProductType(StrEnum):
    FIGURINE = "figurine"  # sculpted figure on a base
    KEYCHAIN = "keychain"  # small figure with a keyring loop, usually 35 to 50 mm
    NAMEPLATE = "nameplate"  # plaque with raised name text and a figurine on it
    ORNAMENT = "ornament"  # hanging loop
    MAGNET = "magnet"  # flat back with a magnet pocket
    OTHER = "other"


class ProductForm(StrEnum):
    """What the printed object is, set at intake and kept by every stage (FROM JACKSON).

    flat: a relief charm, a height field of the front view with a flat back.
    figure: a full 3D figure, never flattened later.
    """

    FLAT = "flat"
    FIGURE = "figure"


# Products whose form the customer picks at intake (asked unless the request or the order form
# says). Every other product is a 3D figure.
FORM_CHOICE_PRODUCTS: tuple[ProductType, ...] = (ProductType.KEYCHAIN, ProductType.MAGNET)
# Used only when a keychain or magnet job ends intake without an answer (ASSUMED).
FORM_DEFAULTS: dict[ProductType, ProductForm] = {
    ProductType.KEYCHAIN: ProductForm.FIGURE, ProductType.MAGNET: ProductForm.FLAT,
}


def product_form(product_type: ProductType | str, form: ProductForm | str | None
                 ) -> ProductForm | None:
    """The form a job builds: the customer's pick for keychains and magnets (None until picked),
    a 3D figure for every other product."""
    pt = ProductType(product_type) if product_type in ProductType._value2member_map_ \
        else ProductType.OTHER
    if pt not in FORM_CHOICE_PRODUCTS:
        return ProductForm.FIGURE
    return ProductForm(form) if form else None


class AddOnKind(StrEnum):
    ROUND_BASE = "round_base"
    OVAL_BASE = "oval_base"
    KEYRING_LOOP = "keyring_loop"
    HANGING_LOOP = "hanging_loop"
    NAMEPLATE = "nameplate"  # plate under or in front of the figure, with TextSpec
    FLAT_BACK = "flat_back"
    MAGNET_POCKET = "magnet_pocket"


class TextSpec(Strict):
    text: str = Field(min_length=1, max_length=24)
    font: str = Field("rounded-bold", description="Font key from the geometry package")
    cap_height_mm: float | None = Field(None, description="None = fit to plate")
    relief_mm: float = Field(1.0, description="Raised height (or engrave depth)")
    style: Literal["raised", "engraved"] = "raised"
    filament_id: str | None = Field(None, description="None = highest-contrast palette filament")


class AddOn(Strict):
    """Exact geometry added in s06. Generated, never asked of Meshy (AI meshes garble text)."""

    kind: AddOnKind
    text: TextSpec | None = None
    filament_id: str | None = None
    params: dict[str, float | str] = Field(
        default_factory=dict,
        description="e.g. loop_inner_d_mm, plate_w_mm, plate_d_mm, plate_t_mm, pocket_d_mm, "
        "pocket_depth_mm, position (top|back|left|right)",
    )


class CustomAsk(Strict):
    """Something the customer asked to see beyond the subjects themselves: a prop, an outfit, a
    pose, a name. Carried from intake into the concept prompt, the concept check and the views,
    so it survives into the 3D model."""

    kind: Literal["prop", "outfit", "pose", "expression", "text", "other"]
    text: str = Field(min_length=1, max_length=120,
                      description="In the customer's words, e.g. wearing a sombrero")
    subject_id: str | None = Field(None, description="s1, s2, ... or None for the whole piece")
    source: Literal["request", "answer", "form"] = "request"
    buildable: bool = Field(
        True, description="False when the product has nowhere to put it (a name on a 3D "
        "keychain); never drawn or checked, and the customer gets a note")


class SizeTier(StrEnum):
    XS = "xs"
    S = "s"
    M = "m"
    L = "l"


# ASSUMED product decision (orchestrator), refined by slices: keychain XS raised to 40 mm because
# eyes fall under 2 mm below that (geometry measured 6.3% of figure height). Units:
# the size of the product's longest visible dimension in mm per tier. Nameplate = plate width;
# figurine = height; keychain, ornament, magnet = longest side.
SIZE_TIERS_MM: dict[ProductType, dict[SizeTier, float]] = {
    ProductType.KEYCHAIN: {SizeTier.XS: 40, SizeTier.S: 45, SizeTier.M: 52, SizeTier.L: 60},
    ProductType.NAMEPLATE: {SizeTier.XS: 80, SizeTier.S: 100, SizeTier.M: 120, SizeTier.L: 150},
    ProductType.FIGURINE: {SizeTier.XS: 50, SizeTier.S: 70, SizeTier.M: 90, SizeTier.L: 120},
    ProductType.ORNAMENT: {SizeTier.XS: 50, SizeTier.S: 60, SizeTier.M: 70, SizeTier.L: 85},
    ProductType.MAGNET: {SizeTier.XS: 40, SizeTier.S: 50, SizeTier.M: 60, SizeTier.L: 75},
}
DEFAULT_SIZE_TIER = SizeTier.M

# Sensible defaults per product; intake may override (e.g. asks for the name on a nameplate).
PRODUCT_DEFAULTS: dict[ProductType, dict] = {
    ProductType.FIGURINE: {"target_size_mm": 90.0, "addons": [AddOnKind.ROUND_BASE]},
    ProductType.KEYCHAIN: {"target_size_mm": 52.0, "addons": [AddOnKind.KEYRING_LOOP]},
    ProductType.NAMEPLATE: {"target_size_mm": 120.0, "addons": [AddOnKind.NAMEPLATE]},
    ProductType.ORNAMENT: {"target_size_mm": 70.0, "addons": [AddOnKind.HANGING_LOOP]},
    ProductType.MAGNET: {
        "target_size_mm": 60.0, "addons": [AddOnKind.FLAT_BACK, AddOnKind.MAGNET_POCKET],
    },
    ProductType.OTHER: {"target_size_mm": 90.0, "addons": []},
}


class PrintSettings(Strict):
    printer: str = "Bambu Lab X2D"
    nozzle_mm: float = 0.4
    layer_mm: float = 0.2
    ams_slots: int = Field(5, ge=1, le=16, description="FROM JACKSON: 5 loaded AMS slots")
    slicer: Literal["bambu_studio", "orcaslicer", "p2p_slicer"] = "bambu_studio"
    process_overrides: dict[str, str] = Field(
        default_factory=dict, description="Slicer process keys, e.g. enable_support=1"
    )


class Budget(Strict):
    cap_usd: float = JOB_CAP_USD
    cap_credits: int = JOB_CAP_CREDITS
    spent_usd: float = 0.0
    spent_credits: int = 0


class Photo(Strict):
    path: str = Field(description="input/photo_<n>.<ext>")
    sha256: str
    subject_ids: list[str] = Field(default_factory=list, description="Filled by intake")


class JobRecord(Strict):
    schema_version: Literal["1"] = SCHEMA_VERSION
    id: str = Field(description="UUID, same as Supabase jobs.id")
    created_at: datetime
    prompt: str
    product_type: ProductType = ProductType.FIGURINE
    target_size_mm: float | None = Field(None, description="Longest dimension of the print")
    size_tier: SizeTier | None = Field(None, description="Customer's pick; sets target_size_mm")
    form: ProductForm | None = Field(
        None, description="Set by s01: flat charm or 3D figure (keychains and magnets pick; "
        "every other product is figure). None before intake finishes")
    custom_asks: list[CustomAsk] = Field(default_factory=list, description="Set by s01")
    addons: list[AddOn] = Field(default_factory=list)
    photos: list[Photo] = Field(default_factory=list)
    settings: PrintSettings = Field(default_factory=PrintSettings)
    budget: Budget = Field(default_factory=Budget)
    status: JobStatus = JobStatus.QUEUED
    stage: StageId | None = None
    profile: Literal["thin_slice", "full"] = "thin_slice"
    access: Literal["owner", "code"] = "owner"
    pause_reason: str | None = None
    result: dict | None = Field(None, description="payloads.JobResult as a dict once s07 or s10 ships")
    attempts: dict[StageId, int] = Field(
        default_factory=dict, description="Current (accepted) attempt number per stage"
    )


class SpendEvent(Strict):
    at: datetime
    job_id: str | None = None
    stage: StageId | None = None
    vendor: Literal["anthropic", "meshy", "runpod"]
    kind: str = Field(description="e.g. image-to-image, multi-image-to-3d, messages, sam3")
    usd: float = 0.0
    credits: int = 0
    ref: str | None = Field(None, description="Meshy task id, Anthropic request id, Runpod job id")
    test_mode: bool = False


def stage_dir(stage: StageId, attempt: int) -> PurePosixPath:
    return PurePosixPath(stage.value) / f"a{attempt}"


def manifest_path(stage: StageId, attempt: int) -> PurePosixPath:
    return stage_dir(stage, attempt) / "manifest.json"


def storage_key(job_id: str, rel_path: str) -> str:
    """Storage key for a job-relative path, in every bucket: `<job_id>/<rel_path>`.

    Uploaded photos: bucket `inputs`, key `<job_id>/input/photo_1.jpg`, job.photos[].path
    `input/photo_1.jpg`. Stage outputs: bucket `artifacts`, key `<job_id>/<stage>/a<N>/<file>`.
    """
    return f"{job_id}/{rel_path}"
