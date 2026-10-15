"""MockSegmenter: SAM 3 stand-in for running the pipeline with no GPU.

It returns the same SegmentResult as the Runpod backend. Masks come from, in order:
1. fixtures: {(image_index, prompt): [(mask, score), ...]} or a callable (index, rgb, prompt).
2. color_rules: {prompt or part: (r, g, b)}; each connected blob of that color is one mask.
3. Heuristics for flat renders on a plain background (the paint agent's flat views):
   eye   = a pair of small dark blobs side by side in the upper part of the figure
   nose  = the small dark blob below and between the eyes
   ear   = blobs of the figure's top band
   head  = top 45 percent of the figure; body = the whole figure; base = bottom 12 percent
Anything else gets no masks. The heuristics exist for plumbing and tests, not accuracy.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

import numpy as np
from p2p_contracts.stages import StageId
from scipy import ndimage

from . import rle
from .client import (
    BudgetGuard,
    ImageInput,
    SpendSink,
    _normalize_prompts,
    check_budget,
    spend_event,
    to_rgb_array,
)
from .parts import part_for_prompt
from .types import ImageSegments, PromptMasks, RequestRecord, SegmentResult

Mask = tuple[np.ndarray, float]
FixtureFn = Callable[[int, np.ndarray, str], Sequence[Mask]]
_EIGHT = np.ones((3, 3), bool)


def _components(mask: np.ndarray, min_px: int = 1) -> list[np.ndarray]:
    lab, n = ndimage.label(mask, structure=_EIGHT)
    if n == 0:
        return []
    sizes = np.bincount(lab.ravel(), minlength=n + 1)
    return [lab == i for i in range(1, n + 1) if sizes[i] >= min_px]


def _centroid(m: np.ndarray) -> tuple[float, float]:
    ys, xs = np.nonzero(m)
    return float(ys.mean()), float(xs.mean())


class FlatRenderHeuristics:
    """Finds eyes, nose, ears, head, body and base on a flat render with a plain background."""

    def __init__(self, bg_tol: int = 24, dark_max: float = 70.0, max_blob_frac: float = 0.03):
        self.bg_tol = bg_tol
        self.dark_max = dark_max
        self.max_blob_frac = max_blob_frac

    def foreground(self, rgb: np.ndarray) -> np.ndarray:
        border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]]).astype(np.int32)
        colors, counts = np.unique(border, axis=0, return_counts=True)
        bg = colors[np.argmax(counts)]
        return np.abs(rgb.astype(np.int32) - bg).max(axis=2) > self.bg_tol

    @staticmethod
    def _ears(fg: np.ndarray, y0: int, h: int, cx: float) -> list[np.ndarray]:
        """Two topmost bumps: rows from the top while the silhouette is split, before it merges."""
        end = y0
        split_seen = False
        for y in range(y0, int(y0 + 0.4 * h)):
            row = fg[y]
            n_runs = int(row[0]) + int(np.count_nonzero(row[1:] & ~row[:-1]))
            if n_runs >= 2:
                split_seen = True
                end = y + 1
            elif split_seen:
                break
        if not split_seen:
            return []
        zone = fg.copy()
        zone[:y0] = False
        zone[end:] = False
        comps = _components(zone, min_px=4)
        comps.sort(key=lambda m: (int(np.nonzero(m.any(axis=1))[0][0]), -int(m.sum())))
        ears = sorted(comps[:2], key=lambda m: _centroid(m)[1])
        if len(ears) == 2 and _centroid(ears[0])[1] < cx < _centroid(ears[1])[1]:
            return ears
        return []

    def analyze(self, rgb: np.ndarray) -> dict[str, list[Mask]]:
        fg = self.foreground(rgb)
        out: dict[str, list[Mask]] = {}
        if fg.sum() < 16:
            return out
        ys, xs = np.nonzero(fg)
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        h, w = y1 - y0, x1 - x0
        rows = np.arange(rgb.shape[0])[:, None]

        out["body"] = [(fg, 0.9)]
        out["head"] = [(fg & (rows < y0 + 0.45 * h), 0.6)]
        out["base"] = [(fg & (rows >= y1 - 0.12 * h), 0.6)]

        ears = self._ears(fg, y0, h, (x0 + x1) / 2)
        if ears:
            out["ear"] = [(m, 0.7) for m in ears]

        lum = rgb[..., :3].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
        fg_lum = float(np.median(lum[fg]))
        dark = fg & (lum < min(self.dark_max, 0.5 * fg_lum))
        max_px = max(4, int(self.max_blob_frac * fg.sum()))
        blobs = [m for m in _components(dark, min_px=4) if m.sum() <= max_px]
        info = [(m, *_centroid(m), int(m.sum())) for m in blobs]  # (mask, cy, cx, area)

        best = None
        for i in range(len(info)):
            for j in range(i + 1, len(info)):
                a, b = info[i], info[j]
                if abs(a[1] - b[1]) > 0.1 * h or abs(a[2] - b[2]) < 0.08 * w:
                    continue
                if max(a[3], b[3]) > 2.5 * min(a[3], b[3]):
                    continue
                if min(a[1], b[1]) > y0 + 0.7 * h:
                    continue
                key = ((a[1] + b[1]) / 2, -(a[3] + b[3]))
                if best is None or key < best[0]:
                    best = (key, i, j)
        if best is not None:
            _, i, j = best
            left, right = sorted((info[i], info[j]), key=lambda t: t[2])
            out["eye"] = [(left[0], 0.85), (right[0], 0.85)]
            eye_y = (left[1] + right[1]) / 2
            below = [
                t
                for k, t in enumerate(info)
                if k not in (i, j) and t[1] > eye_y and left[2] < t[2] < right[2]
            ]
            if below:
                mid_x = (left[2] + right[2]) / 2
                nose = min(below, key=lambda t: (abs(t[2] - mid_x) + abs(t[1] - eye_y), t[1]))
                out["nose"] = [(nose[0], 0.8)]
        return out


class MockSegmenter:
    backend = "mock"

    def __init__(
        self,
        fixtures: Mapping[tuple[int, str], Sequence[Mask]] | FixtureFn | None = None,
        color_rules: Mapping[str, tuple[int, int, int]] | None = None,
        color_tol: int = 12,
        heuristics: FlatRenderHeuristics | None = None,
        use_heuristics: bool = True,
    ) -> None:
        self.fixtures = fixtures
        self.color_rules = dict(color_rules or {})
        self.color_tol = color_tol
        self.heuristics = heuristics or FlatRenderHeuristics()
        self.use_heuristics = use_heuristics

    def _masks(self, idx: int, rgb: np.ndarray, prompt: str, cache: dict) -> list[Mask]:
        if callable(self.fixtures):
            got = self.fixtures(idx, rgb, prompt)
            if got is not None:
                return list(got)
        elif self.fixtures is not None and (idx, prompt) in self.fixtures:
            return list(self.fixtures[(idx, prompt)])
        part = part_for_prompt(prompt)
        color = self.color_rules.get(prompt, self.color_rules.get(part))
        if color is not None:
            near = np.abs(rgb.astype(np.int32) - np.array(color)).max(axis=2) <= self.color_tol
            return [(m, 0.8) for m in _components(near, min_px=4)]
        if not self.use_heuristics:
            return []
        if "flat" not in cache:
            cache["flat"] = self.heuristics.analyze(rgb)
        key = "eye" if part in ("pupil", "iris") else part
        return list(cache["flat"].get(key, []))

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
        del mask_threshold  # masks are already binary
        prompts = _normalize_prompts(prompts)
        if not images:
            raise ValueError("at least one image is required")
        check_budget(budget_guard, 0.0)
        started = datetime.now(UTC)
        t0 = time.monotonic()
        out: list[ImageSegments] = []
        for idx, image in enumerate(images):
            rgb = to_rgb_array(image)
            cache: dict = {}
            per_prompt = []
            for p in prompts:
                masks = [
                    (np.asarray(m, bool), float(s))
                    for m, s in self._masks(idx, rgb, p, cache)
                    if float(s) > score_threshold and np.asarray(m).any()
                ]
                masks.sort(key=lambda t: -t[1])
                masks = masks[:max_masks_per_prompt]
                for m, _ in masks:
                    if m.shape != rgb.shape[:2]:
                        raise ValueError(f"mask shape {m.shape} does not match image {idx}")
                per_prompt.append(
                    PromptMasks(
                        prompt=p,
                        scores=[round(s, 4) for _, s in masks],
                        boxes=[tuple(rle.bbox(m)) for m, _ in masks],
                        areas=[int(m.sum()) for m, _ in masks],
                        masks=[rle.encode(m) for m, _ in masks],
                    )
                )
            h, w = rgb.shape[:2]
            out.append(ImageSegments(image_index=idx, width=w, height=h, prompts=per_prompt))
        ev = spend_event(0.0, "mock", job_id, stage, test_mode=True)
        if on_spend is not None:
            on_spend(ev)
        rec = RequestRecord(
            ref="mock",
            image_indices=list(range(len(images))),
            mode="mock",
            started_at=started,
            wall_s=round(time.monotonic() - t0, 3),
        )
        return SegmentResult(
            backend="mock",
            prompts=prompts,
            images=out,
            requests=[rec],
            spend=[ev],
            gpu_seconds=0.0,
            usd=0.0,
        )
