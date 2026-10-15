"""label_regions: one call from rendered views to a region map, driven by LabelRegionsIn.

The caller (paint MCP server, s07 stage) renders the views; this module segments them with SAM 3,
back-projects the masks, and optionally splits the body and head into coat-marking regions from
texture color clusters (markings.py).

LabelRegionsIn fields used here:
- prompts            SAM 3 text prompts (default DEFAULT_PROMPTS)
- views              keep only rendered views with these names (default all given)
- min_vote           back-projection vote share
- score_threshold    passed to the segmenter; masks scoring at or below it are dropped
- min_component_frac drops 2D mask components under this share of their mask's pixels, and
                     face-label islands under this share of the label's largest component
Not handled here: close_ups (the caller renders close-ups and passes them as extra views) and
replace (merging into an existing region map is the paint server's job).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from p2p_contracts.mcp_tools import LabelRegionsIn
from p2p_contracts.regions import MeshRef, Region, RegionMap
from p2p_contracts.stages import StageId
from scipy import ndimage

from .backproject import (
    BackprojectResult,
    LabelMask,
    ViewLabels,
    backproject,
    masks_from_result,
    save_region_map,
)
from .client import BudgetGuard, ImageInput, Segmenter, SpendSink, segment
from .types import SegmentResult

DEFAULT_PROMPTS: tuple[str, ...] = (
    "eye", "nose", "mouth", "ear", "paw", "tail", "head", "collar", "tag", "body", "base",
)  # fmt: skip
_EIGHT = np.ones((3, 3), bool)


@dataclass
class RenderedView:
    """One render and its face-ID buffer (same size), from the sim renderer."""

    name: str
    image: ImageInput
    face_ids: np.ndarray  # (H, W) int32, -1 = background
    camera_pos_mm: Sequence[float] | None = None
    view_dir: Sequence[float] | None = None


@dataclass
class LabelResult:
    face_regions: np.ndarray  # int32 (F,), 0 = unlabeled
    regions: list[Region]
    next_region_id: int
    backprojection: BackprojectResult
    segments: SegmentResult
    view_names: list[str]
    markings: object | None = None  # markings.MarkingResult when texture colors were given
    stats: dict = field(default_factory=dict)

    def region_map(self, mesh: MeshRef, face_regions_ref) -> RegionMap:
        return RegionMap(
            mesh=mesh,
            face_regions=face_regions_ref,
            regions=self.regions,
            next_region_id=self.next_region_id,
        )

    def save(self, out_dir: Path, rel_dir: str, mesh: MeshRef) -> RegionMap:
        """Write face_regions.npy and region_map.json (see backproject.save_region_map)."""
        return save_region_map(
            self.face_regions, self.regions, self.next_region_id, out_dir, rel_dir, mesh
        )


def drop_small_components(mask: np.ndarray, min_frac: float) -> np.ndarray:
    """mask without 8-connected pixel components under min_frac of the mask's pixel count."""
    mask = np.asarray(mask, bool)
    if min_frac <= 0 or not mask.any():
        return mask
    lab, n = ndimage.label(mask, structure=_EIGHT)
    if n <= 1:
        return mask
    sizes = np.bincount(lab.ravel(), minlength=n + 1)
    keep = sizes >= min_frac * mask.sum()
    keep[0] = False
    return keep[lab]


def clean_masks(masks: Sequence[LabelMask], min_frac: float) -> list[LabelMask]:
    out = []
    for m in masks:
        cleaned = drop_small_components(m.mask, min_frac)
        if cleaned.any():
            out.append(LabelMask(m.part, cleaned, m.score, m.subject_id))
    return out


def label_regions(
    req: LabelRegionsIn,
    vertices: np.ndarray,
    faces: np.ndarray,
    views: Sequence[RenderedView],
    *,
    subject_id: str | None = None,
    prompt_map: Mapping[str, tuple[str | None, str]] | None = None,
    face_colors_lab: np.ndarray | None = None,
    marking_hosts: Sequence[str] = ("body", "head"),
    marking_options: Mapping | None = None,
    segmenter: Segmenter | None = None,
    max_masks_per_prompt: int = 10,
    budget_guard: BudgetGuard | None = None,
    on_spend: SpendSink | None = None,
    job_id: str | None = None,
    stage: StageId | None = None,
    first_region_id: int = 1,
) -> LabelResult:
    """Segment the views, back-project, and (with face_colors_lab) add texture markings.

    prompt_map: prompt -> (subject_id, part); prompts not in it map through
    parts.part_for_prompt with subject_id. face_colors_lab: (F, 3) CIELAB per face, from
    markings.face_colors_from_texture; None skips markings.
    """
    from .parts import part_for_prompt

    prompts = list(req.prompts or DEFAULT_PROMPTS)
    if req.views is not None:
        wanted = set(req.views)
        views = [v for v in views if v.name in wanted]
    if not views:
        raise ValueError("no rendered views to label")
    pmap = {p: (subject_id, part_for_prompt(p)) for p in prompts}
    pmap.update(prompt_map or {})

    seg = segment(
        [v.image for v in views],
        prompts,
        score_threshold=req.score_threshold,
        max_masks_per_prompt=max_masks_per_prompt,
        budget_guard=budget_guard,
        on_spend=on_spend,
        job_id=job_id,
        stage=stage,
        segmenter=segmenter,
    )
    view_labels = [
        ViewLabels(
            face_ids=v.face_ids,
            masks=clean_masks(masks_from_result(seg, i, pmap), req.min_component_frac),
            camera_pos_mm=v.camera_pos_mm,
            view_dir=v.view_dir,
            name=v.name,
        )
        for i, v in enumerate(views)
    ]
    bp = backproject(
        vertices,
        faces,
        view_labels,
        min_vote=req.min_vote,
        min_component_frac=req.min_component_frac,
        first_region_id=first_region_id,
    )
    face_regions, regions, next_id = bp.face_regions, list(bp.regions), bp.next_region_id
    marks = None
    if face_colors_lab is not None:
        from .markings import split_markings

        marks = split_markings(
            vertices,
            faces,
            face_regions,
            regions,
            face_colors_lab,
            hosts=marking_hosts,
            next_region_id=next_id,
            **dict(marking_options or {}),
        )
        face_regions, regions, next_id = marks.face_regions, marks.regions, marks.next_region_id
    return LabelResult(
        face_regions=face_regions,
        regions=regions,
        next_region_id=next_id,
        backprojection=bp,
        segments=seg,
        view_names=[v.name for v in views],
        markings=marks,
        stats={**bp.stats, "prompts": prompts, "score_threshold": req.score_threshold},
    )
