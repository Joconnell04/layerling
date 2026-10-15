"""LocalSegmenter: SAM 3 in-process on this machine (Apple MPS, CUDA or CPU).

Uses the same inference code as the Runpod worker (sam3/runpod/handler.py, loaded by path), so
local and Runpod results have the same format and thresholds. Needs the `local` extra:

    uv run --package p2p-sam3 --extra local python ...

Weights: SAM3_LOCAL_PATH (default ~/models/sam3), a transformers-format folder with
model.safetensors, checked against the facebook/sam3 sha256 before loading. The model loads once
per process and stays warm; get_segmenter("local") returns the same instance every time.
"""

from __future__ import annotations

import base64
import importlib.util
import os
import sys
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from p2p_contracts.stages import StageId

from .client import (
    BudgetGuard,
    ImageInput,
    Sam3Error,
    SpendSink,
    _normalize_prompts,
    check_budget,
    to_png_bytes,
)
from .types import ImageSegments, RequestRecord, SegmentResult

DEFAULT_LOCAL_PATH = "~/models/sam3"
HANDLER_PATH = Path(__file__).resolve().parents[2] / "runpod" / "handler.py"


def load_handler_module() -> ModuleType:
    """Import sam3/runpod/handler.py once (it holds the shared SAM 3 inference code)."""
    name = "p2p_sam3_runpod_handler"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(os.environ.get("SAM3_HANDLER_PATH", HANDLER_PATH))
    if not path.is_file():
        raise Sam3Error(f"SAM 3 handler not found at {path}; set SAM3_HANDLER_PATH")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class LocalSegmenter:
    backend = "local"

    def __init__(
        self,
        model_path: str | Path | None = None,
        device: str | None = None,
        dtype: str | None = None,
        model: Any | None = None,
    ) -> None:
        self.model_path = Path(
            model_path or os.environ.get("SAM3_LOCAL_PATH", DEFAULT_LOCAL_PATH)
        ).expanduser()
        self.device = device or os.environ.get("SAM3_DEVICE", "auto")
        # None: float16 on MPS (1.6x faster than float32 here, scores within 0.001), else float32.
        self.dtype = dtype or os.environ.get("SAM3_DTYPE") or None
        self._model = model  # injectable for tests
        self._handler: ModuleType | None = None
        self._lock = threading.Lock()
        self.load_s: float | None = None

    def warm(self) -> dict:
        """Load the model now (about 10 to 30 s the first time). Returns the model info."""
        with self._lock:
            if self._handler is None:
                # Before torch is imported: let MPS fall back to CPU for any missing op.
                os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
                h = load_handler_module()
                t = time.perf_counter()
                if self._model is None:
                    import torch

                    device = h.pick_device(torch, self.device)
                    dtype = self.dtype or ("float16" if device == "mps" else "float32")
                    self._model = h.TorchSam3(model_dir=self.model_path, device=device, dtype=dtype)
                self.load_s = time.perf_counter() - t
                self._state: dict[str, Any] = {
                    "model": self._model,
                    "load_s": self.load_s,
                    "served": 0,
                }
                self._handler = h
            return dict(getattr(self._model, "info", {}))

    def segment(
        self,
        images: Sequence[ImageInput],
        prompts: Sequence[str],
        *,
        score_threshold: float = 0.5,
        max_masks_per_prompt: int = 10,
        mask_threshold: float | None = None,
        budget_guard: BudgetGuard | None = None,
        on_spend: SpendSink | None = None,
        job_id: str | None = None,
        stage: StageId | None = None,
    ) -> SegmentResult:
        del on_spend, job_id, stage  # local compute costs nothing; no SpendEvent
        prompts = _normalize_prompts(prompts)
        if not images:
            raise ValueError("at least one image is required")
        check_budget(budget_guard, 0.0)
        self.warm()
        h = self._handler
        body: dict[str, Any] = {
            "images": [base64.b64encode(to_png_bytes(im)).decode("ascii") for im in images],
            "prompts": prompts,
            "score_threshold": score_threshold,
            "max_masks_per_prompt": max_masks_per_prompt,
        }
        if mask_threshold is not None:
            body["mask_threshold"] = mask_threshold
        try:
            req = h.validate(body)
        except h.ValidationError as e:
            raise Sam3Error(f"validation: {e}") from None
        started = datetime.now(UTC)
        t0 = time.monotonic()
        with self._lock:  # one inference at a time on the shared device
            out = h.run(req, self._state)
        wall = time.monotonic() - t0
        rec = RequestRecord(
            ref=None,
            image_indices=list(range(len(images))),
            mode="local",
            started_at=started,
            wall_s=round(wall, 3),
            cold_start=bool(out.get("cold_start")),
            worker_timing={k: float(v) for k, v in out["timing"].items()},
            worker_model={k: None if v is None else str(v) for k, v in out["model"].items()},
        )
        return SegmentResult(
            backend="local",
            prompts=prompts,
            images=[ImageSegments.model_validate(r) for r in out["results"]],
            requests=[rec],
            spend=[],
            gpu_seconds=0.0,
            usd=0.0,
        )


_SHARED: LocalSegmenter | None = None


def shared_local_segmenter(**kwargs: Any) -> LocalSegmenter:
    """One warm LocalSegmenter per process."""
    global _SHARED
    if _SHARED is None:
        _SHARED = LocalSegmenter(**kwargs)
    return _SHARED
