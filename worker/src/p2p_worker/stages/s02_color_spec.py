"""Stage 2, color spec: a structured ColorSpec per subject (claude-sonnet-5) and a photo crop of
each part (SAM 3 on the customer's photos).

1. Sonnet 5 through the Claude client (Claude Code OAuth by default) reads the photos, the
   intake subjects and intake's color areas, and lists every part that needs its own color:
   base coat, every marking, eyes, nose, inner ears, tongue, paw pads, collar. The reply is
   compact on purpose (one pipe-separated line per part, output capped, thinking off, effort
   low); LAB is computed here from the hex, not by the model.
2. SAM 3 (p2p_sam3, backend SAM3_BACKEND: mock in tests, local on the Mac, runpod) segments each
   part's best photo with "<kind> <part>" prompts. The top mask's box, padded, becomes
   crop_<subject>_<label>.png; left/right labels take the mask on that side of the subject
   (the subject's left is the photo's right in a front view). A part SAM 3 cannot find keeps
   crop null. SAM3_BACKEND=off skips crops.

Outputs: color_spec.json (ColorSpec, role color_spec), crop_*.png (role part_crop).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import numpy as np
from p2p_contracts import ModelCall, StageId
from p2p_contracts.payloads import ColorSpec, IntakeResult, Subject

from ..clients.claude import ColorRegion, ColorSpecRequest
from ..logs import log_event
from ..util import read_json
from .base import Stage, StageContext
from .common import optional

NOUNS = {"eye": "eye", "pupil": "eye", "iris": "eye", "eye_highlight": "eye",
         "eyebrow": "eyebrow", "nose": "nose", "mouth": "mouth", "tongue": "tongue",
         "teeth": "teeth", "ear": "ear", "inner_ear": "ear", "paw": "paw", "paw_pad": "paw",
         "tail": "tail", "collar": "collar", "tag": "tag", "head": "head", "face": "face",
         "muzzle": "muzzle", "accessory": "accessory"}
CROP_PX = 256
PAD = 0.2


def prompt_for(subject: Subject, part: str, label: str, color: str) -> str:
    """SAM 3 text prompt for a part: "dog nose", "dog", "white chest blaze"."""
    kind = subject.kind or "animal"
    if part == "body":
        return kind
    if part in NOUNS:
        return f"{kind} {NOUNS[part]}"
    words = [w for w in label.split("_") if w not in ("left", "right")][:3]
    return " ".join([color, *words]) if words else f"{color} {kind}"


def pick_box(boxes: list, scores: list, label: str) -> tuple[float, ...] | None:
    """The best box; for left_/right_ labels the one on the subject's side."""
    if not boxes:
        return None
    order = sorted(range(len(boxes)), key=lambda i: -scores[i])[:4]
    if label.startswith(("left", "right")) and len(order) > 1:
        cx = {i: (boxes[i][0] + boxes[i][2]) / 2 for i in order}
        # subject's left = image right in a front view
        pick = max(order, key=cx.get) if label.startswith("left") else min(order, key=cx.get)
        return tuple(boxes[pick])
    return tuple(boxes[order[0]])


def save_crop(photo: np.ndarray, box, out: Path) -> None:
    from PIL import Image

    h, w = photo.shape[:2]
    x0, y0, x1, y1 = box
    px, py = (x1 - x0) * PAD, (y1 - y0) * PAD
    x0, y0 = max(0, int(x0 - px)), max(0, int(y0 - py))
    x1, y1 = min(w, int(x1 + px) + 1), min(h, int(y1 + py) + 1)
    im = Image.fromarray(photo[y0:y1, x0:x1])
    im.thumbnail((CROP_PX, CROP_PX))
    im.save(out)


class ColorSpecStage(Stage):
    id = StageId.S02_COLOR_SPEC

    def _crops(self, ctx: StageContext, spec: ColorSpec, subjects: list[Subject],
               photos: list[Path]) -> dict[str, Any]:
        from p2p_sam3.client import get_segmenter, segment, to_rgb_array

        by_id = {s.id: s for s in subjects}
        jobs: dict[int, list[tuple[str, str, str]]] = {}  # photo -> (key, prompt, label)
        for ss in spec.subjects:
            subj = by_id.get(ss.subject_id)
            if subj is None:
                continue
            for p in ss.parts:
                idx = p.photo_index if p.photo_index is not None else (
                    subj.photo_indices[0] if subj.photo_indices else 0)
                if 0 <= idx < len(photos):
                    jobs.setdefault(idx, []).append(
                        (f"{ss.subject_id}.{p.label}",
                         prompt_for(subj, p.part, p.label, p.color_name), p.label))
        seg = get_segmenter(ctx.settings.sam3_backend)
        crops: dict[str, Any] = {}
        calls, usd = [], 0.0
        for idx, items in sorted(jobs.items()):
            rgb = to_rgb_array(photos[idx])
            prompts = sorted({p for _, p, _ in items})
            res = segment([rgb], prompts, segmenter=seg, score_threshold=0.3,
                          max_masks_per_prompt=4, job_id=ctx.job.id, stage=self.id)
            billing = "gpu_seconds" if res.backend == "runpod" else "free"
            calls += [c.model_copy(update={"billing": billing}).model_dump(mode="json")
                      for c in res.model_calls("sam3 part crops")]
            usd += res.usd
            img = res.images[0]
            for key, prompt, label in items:
                pm = img.get(prompt)
                box = pick_box(pm.boxes, pm.scores, label) if pm else None
                if box is None:
                    crops[key] = None
                    continue
                name = f"crop_{key.replace('.', '_')}.png"
                save_crop(rgb, box, ctx.path(name))
                crops[key] = {"crop": name, "photo_index": idx,
                              "box": [round(float(x), 1) for x in box]}
        return {"crops": crops, "calls": calls, "usd": usd, "backend": seg.backend}

    async def run(self, ctx: StageContext) -> None:
        job = ctx.job
        intake_path, _ = ctx.use_output(StageId.S01_INTAKE, "intake_result")
        intake = IntakeResult.model_validate(read_json(intake_path))
        regions: list[ColorRegion] = []
        m1 = optional(ctx, StageId.S01_INTAKE)
        if m1 is not None and any(o.role == "color_regions" for o in m1.outputs):
            rp, _ = ctx.use_output(StageId.S01_INTAKE, "color_regions")
            regions = [ColorRegion.model_validate(r) for r in read_json(rp, [])]
        photos = []
        for p in job.photos:
            ctx.add_input(p.path, role="photo")
            photos.append(ctx.abs(p.path))
        req = ColorSpecRequest(photos=photos, subjects=intake.subjects, regions=regions,
                               product_type=job.product_type.value)
        claude = ctx.svc.claude

        async def call_spec():
            async def call():
                spec, mc = await claude.color_spec(req)
                return spec, mc

            return await ctx.claude("color_spec", claude.estimate_color_spec(req), call)

        spec = ColorSpec.model_validate(await ctx.step("color_spec", call_spec))
        n_parts = sum(len(s.parts) for s in spec.subjects)
        ctx.decide("color_spec.parts", n_parts, by="model",
                   model=ctx.m.model_calls[-1].model if ctx.m.model_calls else None)

        crops: dict[str, Any] = {}
        if ctx.settings.sam3_backend != "off" and n_parts:
            async def run_crops() -> dict[str, Any]:
                return await asyncio.to_thread(self._crops, ctx, spec, intake.subjects, photos)

            try:
                out = await ctx.step("crops", run_crops)
            except Exception as e:  # noqa: BLE001 crops help the paint agent; never block
                log_event("sam3_crops_failed", error=f"{type(e).__name__}: {e}"[:300])
                ctx.decide("color_spec.crops", "failed", by="code",
                           reason=f"{type(e).__name__}: {e}"[:300])
                out = {"crops": {}, "calls": [], "usd": 0.0, "backend": "failed"}
            crops = out["crops"]
            ctx.m.data["sam3_backend"] = out["backend"]
            known = {(c.purpose, c.started_at) for c in ctx.m.model_calls}
            for c in out["calls"]:
                mc = ModelCall.model_validate(c)
                if (mc.purpose, mc.started_at) not in known:
                    ctx.m.model_calls.append(mc)
            if out["usd"]:
                ctx.svc.budget.record(job, stage=self.id, vendor="runpod", kind="sam3",
                                      usd=out["usd"], test_mode=False, billing="gpu_seconds")
        subjects = []
        for ss in spec.subjects:
            parts = []
            for p in ss.parts:
                c = crops.get(f"{ss.subject_id}.{p.label}")
                if c:
                    ref = ctx.add_output(c["crop"], role="part_crop", kind="png")
                    p = p.model_copy(update={"crop": ref.path, "photo_index": c["photo_index"]})
                parts.append(p)
            subjects.append(ss.model_copy(update={"parts": parts}))
        spec = spec.model_copy(update={"subjects": subjects})
        ctx.write_json_output("color_spec.json", spec, role="color_spec")
        ctx.m.data.update({"parts": n_parts,
                           "crops": sum(1 for c in crops.values() if c),
                           "required": sum(p.required for s in spec.subjects for p in s.parts)})
        ctx.m.confidence = None
