"""Stage and job state enums, and the stage graph.

Stage ids double as folder names under jobs/<job_id>/ and as the Postgres enum values.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class StageId(StrEnum):
    S01_INTAKE = "s01_intake"
    S02_COLOR_SPEC = "s02_color_spec"
    S03_CONCEPT = "s03_concept"
    S04_MULTIVIEW = "s04_multiview"
    S05_MESH = "s05_mesh"
    S06_PRINTABILITY = "s06_printability"
    S07_BASELINE = "s07_baseline"
    S08_PALETTE = "s08_palette"
    S09_PAINT = "s09_paint"
    S10_WRITE = "s10_write"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_USER = "waiting_user"  # a user gate is open (questions, concept pick, mesh approval)
    WAITING_VENDOR = "waiting_vendor"  # a Meshy task is in flight; resumes on webhook
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PAUSED = "paused"  # budget cap or retry limit hit; needs Jackson
    SKIPPED = "skipped"  # not part of the current pipeline profile (e.g. M1 thin slice)
    STALE = "stale"  # an upstream input hash changed; must re-run


class JobStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    PAUSED = "paused"
    REVIEW = "review"  # low confidence; in Jackson's review queue
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Gate(StrEnum):
    NONE = "none"
    USER = "user"
    AUTO = "auto"  # automatic check with retry, no user action


class StageInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: StageId
    number: int
    name: str
    gate: Gate
    requires: tuple[StageId, ...] = ()
    uses_if_present: tuple[StageId, ...] = ()
    milestone: str


S = StageId
STAGES: tuple[StageInfo, ...] = (
    StageInfo(id=S.S01_INTAKE, number=1, name="Intake", gate=Gate.USER, milestone="M1"),
    StageInfo(
        id=S.S02_COLOR_SPEC, number=2, name="Color spec", gate=Gate.NONE,
        requires=(S.S01_INTAKE,), milestone="M3",
    ),
    StageInfo(
        id=S.S03_CONCEPT, number=3, name="Concept", gate=Gate.USER,
        requires=(S.S01_INTAKE,), uses_if_present=(S.S02_COLOR_SPEC,), milestone="M1",
    ),
    StageInfo(
        id=S.S04_MULTIVIEW, number=4, name="Multi-view", gate=Gate.AUTO,
        requires=(S.S03_CONCEPT,), milestone="M3",
    ),
    StageInfo(
        id=S.S05_MESH, number=5, name="3D model", gate=Gate.USER,
        requires=(S.S03_CONCEPT,), uses_if_present=(S.S04_MULTIVIEW,), milestone="M1",
    ),
    StageInfo(
        id=S.S06_PRINTABILITY, number=6, name="Printability", gate=Gate.AUTO,
        requires=(S.S05_MESH,), milestone="M3",
    ),
    StageInfo(
        id=S.S07_BASELINE, number=7, name="Baseline + labels", gate=Gate.AUTO,
        requires=(S.S05_MESH,), uses_if_present=(S.S06_PRINTABILITY,), milestone="M1",
    ),
    StageInfo(
        id=S.S08_PALETTE, number=8, name="Palette", gate=Gate.AUTO,
        requires=(S.S07_BASELINE,), uses_if_present=(S.S02_COLOR_SPEC,), milestone="M3",
    ),
    StageInfo(
        id=S.S09_PAINT, number=9, name="Paint agent", gate=Gate.AUTO,
        requires=(S.S08_PALETTE,), milestone="M3",
    ),
    StageInfo(
        id=S.S10_WRITE, number=10, name="Write + slice", gate=Gate.AUTO,
        requires=(S.S09_PAINT,), milestone="M3",
    ),
)
STAGE_BY_ID: dict[StageId, StageInfo] = {s.id: s for s in STAGES}

# Pipeline profiles: which stages run. Stages not in the profile are SKIPPED.
# s04 runs in the thin slice too: a 3D figure is built from real views, never one image
# (docs/research/meshy-iteration.md section 3). s04 skips itself where that spec allows.
PROFILE_THIN_SLICE: tuple[StageId, ...] = (
    S.S01_INTAKE, S.S03_CONCEPT, S.S04_MULTIVIEW, S.S05_MESH, S.S06_PRINTABILITY,
    S.S07_BASELINE,
)
PROFILE_FULL: tuple[StageId, ...] = tuple(s.id for s in STAGES)
