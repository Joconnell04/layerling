"""Stage manifest: the record every stage attempt writes to jobs/<job_id>/<stage>/a<N>/manifest.json.

Rules
- Paths are POSIX, relative to the job folder (jobs/<job_id>/). The same relative path is the
  Supabase Storage key under bucket `artifacts`, prefixed by the job id.
- A stage never writes outside its own attempt folder and never mutates another stage's files.
- `inputs` carry sha256 hashes; if an upstream file's hash no longer matches, this attempt is STALE.
- Money: `usd` includes Claude and Runpod spend plus Meshy credits converted at the plan rate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .stages import StageId, StageStatus

SCHEMA_VERSION = "1"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileRef(Strict):
    path: str = Field(description="POSIX path relative to jobs/<job_id>/")
    sha256: str
    bytes: int | None = None
    kind: str | None = Field(None, description="png, jpg, glb, 3mf, json, npy, ply, gcode.3mf, ...")
    role: str | None = Field(None, description="Semantic role, e.g. concept_candidate, baseline_3mf")


class InputRef(FileRef):
    from_stage: StageId | None = None
    from_attempt: int | None = None


class ModelCall(Strict):
    vendor: Literal["anthropic", "meshy", "runpod", "local"]
    model: str = Field(description="claude-sonnet-5, meshy:image-to-image/nano-banana-pro, sam3, ...")
    purpose: str
    started_at: datetime
    duration_s: float
    effort: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    credits: int = 0
    usd: float = Field(0.0, description="Real money spent. 0 for subscription (Claude Code OAuth) calls")
    usd_list: float = Field(0.0, description="List-price equivalent, for comparing backends")
    billing: Literal["api", "subscription", "credits", "gpu_seconds", "free"] = "api"
    request_id: str | None = None


MeshyStatus = Literal["PENDING", "IN_PROGRESS", "SUCCEEDED", "FAILED", "CANCELED", "EXPIRED"]


class MeshyTask(Strict):
    task_id: str
    endpoint: str = Field(description="API path, e.g. openapi/v1/image-to-image")
    purpose: str
    status: MeshyStatus
    test_mode: bool
    credits: int = 0
    refunded: bool = False
    created_at: datetime
    finished_at: datetime | None = None
    request: dict[str, Any] = Field(
        default_factory=dict, description="Request body with secrets and data URIs stripped"
    )
    result: dict[str, Any] | None = Field(None, description="Trimmed vendor result (urls, ids)")


class Decision(Strict):
    key: str = Field(description="Dotted key, e.g. concept.pick, palette.k, route.tweak")
    value: Any
    reason: str | None = None
    by: Literal["user", "model", "code"]
    model: str | None = None
    at: datetime


class StageError(Strict):
    code: Literal[
        "budget_exceeded", "vendor_failed", "vendor_timeout", "validation", "slicer_failed",
        "retry_limit", "internal", "cancelled",
    ]
    message: str
    retryable: bool
    detail: dict[str, Any] = Field(default_factory=dict)


GateKind = Literal["answer_questions", "pick_concept", "approve_mesh", "review"]


class GateRequest(Strict):
    """What a WAITING_USER stage is waiting on. Mirrored as an assistant message in Supabase."""

    kind: GateKind
    message_id: str | None = Field(None, description="Supabase messages.id of the prompt message")
    opened_at: datetime


class Manifest(Strict):
    schema_version: Literal["1"] = SCHEMA_VERSION
    job_id: str
    stage: StageId
    attempt: int = Field(ge=1)
    status: StageStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float | None = None
    inputs: list[InputRef] = Field(default_factory=list)
    outputs: list[FileRef] = Field(default_factory=list)
    model_calls: list[ModelCall] = Field(default_factory=list)
    meshy_tasks: list[MeshyTask] = Field(default_factory=list)
    credits: int = 0
    usd: float = 0.0
    decisions: list[Decision] = Field(default_factory=list)
    confidence: float | None = Field(None, ge=0, le=1)
    gate: GateRequest | None = None
    pause_reason: str | None = None
    error: StageError | None = None
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Small stage-specific summary. Large payloads go in output files.",
    )
