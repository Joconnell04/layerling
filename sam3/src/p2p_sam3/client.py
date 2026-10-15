"""SAM 3 client: `segment(images, prompts) -> SegmentResult`.

Backends, picked by SAM3_BACKEND (default "mock", so nothing spends by accident):
- local   LocalSegmenter: SAM 3 in-process (Apple MPS, CUDA or CPU), `local` extra, $0.
- mock    MockSegmenter: color and position heuristics or fixture masks, no GPU, $0.
- runpod  RunpodSegmenter: the serverless endpoint in sam3/runpod/. Needs RUNPOD_API_KEY and
          RUNPOD_SAM3_ENDPOINT_ID.

Every request first calls `budget_guard(estimated_usd)`; the guard raises (or returns False) to
stop it. Each finished request becomes a p2p_contracts.SpendEvent, returned in the result and
passed to `on_spend`. USD is an estimate from worker seconds (execution + model load on a cold
start + idle timeout) times the pool's hourly price; reconcile against Runpod billing.
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
import numpy as np
from p2p_contracts.job import SpendEvent
from p2p_contracts.stages import StageId
from PIL import Image

from .types import ImageSegments, RequestRecord, SegmentResult

ImageInput = np.ndarray | bytes | str | Path | Image.Image
BudgetGuard = Callable[[float], object]
SpendSink = Callable[[SpendEvent], object]

RUNPOD_BASE_URL = "https://api.runpod.ai/v2"
# Runpod serverless flex price for the 16 GB pool (A4000 class), USD per hour. See
# docs/research/sam3-runpod.md. Override with SAM3_GPU_USD_PER_HOUR for another pool.
DEFAULT_USD_PER_HOUR = 0.58
DEFAULT_IDLE_TIMEOUT_S = 5.0  # Runpod default idle timeout; billed after the last job
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}


class Sam3Error(RuntimeError):
    """The endpoint failed or returned something unusable."""


class BudgetExceeded(RuntimeError):
    """The budget guard refused a request."""


class Segmenter(Protocol):
    backend: str

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
    ) -> SegmentResult: ...


# ---------------------------------------------------------------- helpers


def to_png_bytes(image: ImageInput) -> bytes:
    """PNG bytes for an array (H, W[, 3|4]) uint8, PIL image, image path, or PNG bytes."""
    if isinstance(image, bytes):
        if not image.startswith(b"\x89PNG"):
            raise ValueError("image bytes must be PNG")
        return image
    if isinstance(image, (str, Path)):
        data = Path(image).read_bytes()
        if data.startswith(b"\x89PNG"):
            return data
        with Image.open(io.BytesIO(data)) as im:  # JPEG etc.: re-encode
            return to_png_bytes(im.convert("RGB"))
    if isinstance(image, np.ndarray):
        if image.dtype != np.uint8:
            raise ValueError(f"image array must be uint8, got {image.dtype}")
        image = Image.fromarray(image)
    if isinstance(image, Image.Image):
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()
    raise TypeError(f"unsupported image type {type(image).__name__}")


def to_rgb_array(image: ImageInput) -> np.ndarray:
    if isinstance(image, np.ndarray) and image.ndim == 3 and image.shape[2] == 3:
        return image
    if isinstance(image, np.ndarray) and image.ndim == 2:
        return np.repeat(image[:, :, None], 3, axis=2)
    with Image.open(io.BytesIO(to_png_bytes(image))) as im:
        if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
            # Same compositing as the worker, so overlays match what the model saw.
            bg = tuple(int(v) for v in os.environ.get("SAM3_BACKGROUND", "235,235,235").split(","))
            rgba = im.convert("RGBA")
            flat = Image.new("RGBA", rgba.size, (*bg[:3], 255))
            return np.asarray(Image.alpha_composite(flat, rgba).convert("RGB"))
        return np.asarray(im.convert("RGB"))


def check_budget(guard: BudgetGuard | None, usd: float) -> None:
    if guard is not None and guard(usd) is False:
        raise BudgetExceeded(f"budget guard refused a request estimated at ${usd:.4f}")


def spend_event(
    usd: float, ref: str | None, job_id: str | None, stage: StageId | None, test_mode: bool
) -> SpendEvent:
    return SpendEvent(
        at=datetime.now(UTC),
        job_id=job_id,
        stage=stage,
        vendor="runpod",
        kind="sam3",
        usd=round(usd, 6),
        ref=ref,
        test_mode=test_mode,
    )


def _normalize_prompts(prompts: Sequence[str]) -> list[str]:
    out: list[str] = []
    for p in prompts:
        p = " ".join(str(p).split())
        if not p:
            raise ValueError("empty prompt")
        if p not in out:
            out.append(p)
    if not out:
        raise ValueError("at least one prompt is required")
    return out


# ---------------------------------------------------------------- Runpod


class RunpodSegmenter:
    """Calls the SAM 3 serverless endpoint. Stateless apart from the HTTP client."""

    backend = "runpod"

    def __init__(
        self,
        endpoint_id: str,
        api_key: str,
        *,
        base_url: str = RUNPOD_BASE_URL,
        usd_per_hour: float = DEFAULT_USD_PER_HOUR,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
        http: httpx.Client | None = None,
        timeout_s: float = 600.0,
        runsync_wait_ms: int = 90_000,
        runsync_max_est_s: float = 60.0,
        max_request_bytes: int = 9_000_000,
        max_images_per_request: int = 16,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        est_cold_s: float = 45.0,
        est_image_s: float = 0.5,
        est_prompt_s: float = 0.08,
    ) -> None:
        if not endpoint_id or not api_key:
            raise ValueError("endpoint_id and api_key are required")
        self.endpoint_id = endpoint_id
        self.base = f"{base_url.rstrip('/')}/{endpoint_id}"
        self.usd_per_hour = usd_per_hour
        self.idle_timeout_s = idle_timeout_s
        self.http = http or httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self.timeout_s = timeout_s
        self.runsync_wait_ms = runsync_wait_ms
        self.runsync_max_est_s = runsync_max_est_s
        self.max_request_bytes = max_request_bytes
        self.max_images_per_request = max_images_per_request
        self.max_retries = max_retries
        self.sleep = sleep
        self.est_cold_s = est_cold_s
        self.est_image_s = est_image_s
        self.est_prompt_s = est_prompt_s

    @classmethod
    def from_env(cls, **kwargs: Any) -> RunpodSegmenter:
        endpoint = os.environ.get("RUNPOD_SAM3_ENDPOINT_ID", "")
        key = os.environ.get("RUNPOD_API_KEY", "")
        if not endpoint or not key:
            raise Sam3Error("SAM3_BACKEND=runpod needs RUNPOD_API_KEY and RUNPOD_SAM3_ENDPOINT_ID")
        kwargs.setdefault(
            "usd_per_hour", float(os.environ.get("SAM3_GPU_USD_PER_HOUR", DEFAULT_USD_PER_HOUR))
        )
        kwargs.setdefault(
            "idle_timeout_s", float(os.environ.get("SAM3_IDLE_TIMEOUT_S", DEFAULT_IDLE_TIMEOUT_S))
        )
        return cls(endpoint, key, **kwargs)

    # -- cost model

    def estimate_seconds(self, n_images: int, n_prompts: int) -> float:
        """Worst case: assumes a cold start."""
        return (
            self.est_cold_s
            + n_images * self.est_image_s
            + n_images * n_prompts * self.est_prompt_s
            + self.idle_timeout_s
        )

    def usd(self, seconds: float) -> float:
        return seconds * self.usd_per_hour / 3600.0

    # -- HTTP

    def _request(self, method: str, url: str, body: dict | None = None) -> dict:
        delay = 1.0
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.http.request(method, url, headers=self._headers, json=body)
            except httpx.TransportError as e:
                last = e
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last = Sam3Error(f"{method} {url} -> HTTP {resp.status_code}")
                elif resp.status_code >= 400:
                    raise Sam3Error(f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
                else:
                    return resp.json()
            if attempt < self.max_retries:
                self.sleep(delay)
                delay = min(delay * 2, 20.0)
        raise Sam3Error(f"{method} {url} failed after {self.max_retries + 1} tries: {last}")

    def _wait(self, job: dict, deadline: float) -> dict:
        interval = 1.0
        while job.get("status") not in TERMINAL:
            if time.monotonic() > deadline:
                job_id = job.get("id")
                if job_id:
                    try:
                        self._request("POST", f"{self.base}/cancel/{job_id}")
                    except Sam3Error:
                        pass
                raise Sam3Error(f"SAM 3 job {job_id} timed out after {self.timeout_s:.0f} s")
            self.sleep(interval)
            interval = min(interval * 1.5, 5.0)
            job = self._request("GET", f"{self.base}/status/{job['id']}")
        return job

    def _chunks(self, pngs: list[str], n_prompts_bytes: int) -> list[list[int]]:
        chunks: list[list[int]] = []
        cur: list[int] = []
        size = n_prompts_bytes + 200
        for i, b in enumerate(pngs):
            n = len(b) + 4
            if n + n_prompts_bytes + 200 > self.max_request_bytes:
                raise Sam3Error(f"image {i} alone is over the {self.max_request_bytes} byte limit")
            if cur and (
                size + n > self.max_request_bytes or len(cur) >= self.max_images_per_request
            ):
                chunks.append(cur)
                cur, size = [], n_prompts_bytes + 200
            cur.append(i)
            size += n
        if cur:
            chunks.append(cur)
        return chunks

    # -- public

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
        prompts = _normalize_prompts(prompts)
        if not images:
            raise ValueError("at least one image is required")
        pngs = [base64.b64encode(to_png_bytes(im)).decode("ascii") for im in images]
        params: dict[str, Any] = {
            "prompts": prompts,
            "score_threshold": score_threshold,
            "max_masks_per_prompt": max_masks_per_prompt,
        }
        if mask_threshold is not None:
            params["mask_threshold"] = mask_threshold
        chunks = self._chunks(pngs, len(json.dumps(params)))

        out_images: list[ImageSegments] = []
        records: list[RequestRecord] = []
        spend: list[SpendEvent] = []
        for ci, idxs in enumerate(chunks):
            est_s = self.estimate_seconds(len(idxs), len(prompts))
            check_budget(budget_guard, self.usd(est_s))
            body = {"input": {"images": [pngs[i] for i in idxs], **params}}
            started = datetime.now(UTC)
            t0 = time.monotonic()
            deadline = t0 + self.timeout_s
            if est_s - self.est_cold_s <= self.runsync_max_est_s:
                mode = "runsync"
                job = self._request(
                    "POST", f"{self.base}/runsync?wait={self.runsync_wait_ms}", body
                )
            else:
                mode = "run"
                job = self._request("POST", f"{self.base}/run", body)
            job = self._wait(job, deadline)
            wall = time.monotonic() - t0

            status = job.get("status")
            output = job.get("output")
            if status != "COMPLETED" or not isinstance(output, dict) or "error" in output:
                err = job.get("error")
                if not err and isinstance(output, dict):
                    err = output.get("error")
                raise Sam3Error(f"SAM 3 job {job.get('id')} {status}: {err or output}")

            timing = {k: float(v) for k, v in (output.get("timing") or {}).items()}
            cold = bool(output.get("cold_start"))
            exec_ms = int(job.get("executionTime") or 0)
            gpu_s = exec_ms / 1000.0
            if cold:
                gpu_s += timing.get("model_load_s", 0.0)
            if ci == len(chunks) - 1:
                gpu_s += self.idle_timeout_s
            usd = self.usd(gpu_s)

            for img in output.get("results", []):
                img = dict(img)
                img["image_index"] = idxs[int(img["image_index"])]
                out_images.append(ImageSegments.model_validate(img))
            ev = spend_event(usd, job.get("id"), job_id, stage, test_mode=False)
            spend.append(ev)
            if on_spend is not None:
                on_spend(ev)
            records.append(
                RequestRecord(
                    ref=job.get("id"),
                    image_indices=idxs,
                    mode=mode,
                    started_at=started,
                    wall_s=round(wall, 3),
                    delay_ms=int(job.get("delayTime") or 0),
                    execution_ms=exec_ms,
                    cold_start=cold,
                    gpu_seconds=round(gpu_s, 3),
                    usd=round(usd, 6),
                    worker_timing=timing,
                    worker_model={
                        k: (None if v is None else str(v))
                        for k, v in (output.get("model") or {}).items()
                    },
                )
            )

        out_images.sort(key=lambda im: im.image_index)
        if [im.image_index for im in out_images] != list(range(len(images))):
            raise Sam3Error("endpoint returned results for the wrong images")
        return SegmentResult(
            backend="runpod",
            prompts=prompts,
            images=out_images,
            requests=records,
            spend=spend,
            gpu_seconds=round(sum(r.gpu_seconds for r in records), 3),
            usd=round(sum(r.usd for r in records), 6),
        )


# ---------------------------------------------------------------- entry points


def get_segmenter(backend: str | None = None, **kwargs: Any) -> Segmenter:
    """Backend from the argument or SAM3_BACKEND (mock | local | runpod); default mock.

    local returns one shared, warm LocalSegmenter per process.
    """
    name = (backend or os.environ.get("SAM3_BACKEND") or "mock").strip().lower()
    if name == "mock":
        from .mock import MockSegmenter

        return MockSegmenter(**kwargs)
    if name == "local":
        from .local import shared_local_segmenter

        return shared_local_segmenter(**kwargs)
    if name == "runpod":
        return RunpodSegmenter.from_env(**kwargs)
    raise ValueError(f"SAM3_BACKEND must be mock, local or runpod, got {name!r}")


def segment(
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
    segmenter: Segmenter | None = None,
) -> SegmentResult:
    """Segment every image with every prompt. See module docstring for backends and spend."""
    seg = segmenter or get_segmenter()
    return seg.segment(
        images,
        prompts,
        score_threshold=score_threshold,
        max_masks_per_prompt=max_masks_per_prompt,
        mask_threshold=mask_threshold,
        budget_guard=budget_guard,
        on_spend=on_spend,
        job_id=job_id,
        stage=stage,
    )
