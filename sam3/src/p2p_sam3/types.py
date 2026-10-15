"""Segmentation results shared by the Runpod and mock backends."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import numpy as np
from p2p_contracts.job import SpendEvent
from p2p_contracts.manifest import ModelCall
from pydantic import BaseModel, ConfigDict, Field

from . import rle


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CocoRLE(_Model):
    size: tuple[int, int] = Field(description="[height, width]")
    counts: str


class PromptMasks(_Model):
    prompt: str
    scores: list[float]
    boxes: list[tuple[float, float, float, float]] = Field(description="xyxy, pixels")
    areas: list[int]
    masks: list[CocoRLE]

    def decoded(self) -> list[np.ndarray]:
        return [rle.decode(m.model_dump()) for m in self.masks]


class ImageSegments(_Model):
    image_index: int
    width: int
    height: int
    prompts: list[PromptMasks]

    def get(self, prompt: str) -> PromptMasks | None:
        return next((p for p in self.prompts if p.prompt == prompt), None)


class RequestRecord(_Model):
    """One call to the backend (one Runpod job)."""

    ref: str | None = Field(None, description="Runpod job id")
    image_indices: list[int]
    mode: Literal["runsync", "run", "mock", "local"]
    started_at: datetime
    wall_s: float
    delay_ms: int = 0
    execution_ms: int = 0
    cold_start: bool = False
    gpu_seconds: float = Field(0.0, description="Estimated billed worker seconds")
    usd: float = 0.0
    worker_timing: dict[str, float] = Field(default_factory=dict)
    worker_model: dict[str, str | None] = Field(default_factory=dict)


class SegmentResult(_Model):
    backend: Literal["mock", "runpod", "local"]
    prompts: list[str]
    images: list[ImageSegments]
    requests: list[RequestRecord]
    spend: list[SpendEvent]
    gpu_seconds: float
    usd: float

    def model_calls(self, purpose: str = "sam3 part labels") -> list[ModelCall]:
        """Manifest entries, one per backend request."""
        names = {"runpod": "sam3", "local": "sam3", "mock": "sam3-mock"}
        return [
            ModelCall(
                vendor="runpod" if self.backend == "runpod" else "local",
                model=names[self.backend],
                purpose=purpose,
                started_at=r.started_at,
                duration_s=r.wall_s,
                usd=r.usd,
                request_id=r.ref,
            )
            for r in self.requests
        ]
