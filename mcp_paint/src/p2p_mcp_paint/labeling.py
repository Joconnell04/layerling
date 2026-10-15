"""Initial region map: SAM 3 part labels plus texture markings, split by the baseline paint.

1. Render the figure lit, in its per-face texture colors, from the views that show it (a flat,
   face-up product is seen from the top and the front three-quarter view; an upright one from
   the four sides and the three-quarter view).
2. p2p_sam3.label_regions: SAM 3 text prompts, back-projection through the face-ID buffers,
   multi-view vote, then texture color clusters split body and head into markings.
3. Each labeled region is split by the filament the baseline (Meshy Multi-Color retargeted to
   the X2D) gave its faces: the dominant filament keeps the region id and label, every other
   filament becomes its own region "<label>_<color>" (source meshy_color). This is where Meshy's
   color mistakes become addressable by id (the Frenchie's brown flecks are the brown pieces of
   head, body and markings).
4. Faces no prompt claimed become "<subject>.other_<color>" regions.

Region color_lab is the area-weighted mean texture color of its faces.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from p2p_contracts.mcp_tools import LabelRegionsIn
from p2p_contracts.regions import Region
from p2p_sim.mesh import Mesh
from p2p_sim.render import render_views

from .names import filament_word

VIEWS_FLAT = ["top", "front_3q"]
VIEWS_UPRIGHT = ["front", "back", "left", "right", "front_3q"]
DEFAULT_PROMPTS = ["eye", "nose", "mouth", "ear", "paw", "tail", "head", "dog"]


def sam3_backend() -> str:
    return os.environ.get("P2P_PAINT_SAM3", os.environ.get("SAM3_BACKEND", "local"))


@dataclass
class LabelOut:
    face_regions: np.ndarray
    regions: list[Region]
    next_region_id: int
    stats: dict


def views_for(orientation: str) -> list[str]:
    return VIEWS_FLAT if orientation == "flat_face_up" else VIEWS_UPRIGHT


def run_sam3(mesh: Mesh, tex_rgb: np.ndarray, req: LabelRegionsIn, orientation: str,
             subject_id: str = "s1", subject_kind: str = "dog", backend: str | None = None,
             tex_lab: np.ndarray | None = None, size_px: int = 1024,
             first_region_id: int = 1) -> LabelOut:
    from p2p_sam3.client import get_segmenter
    from p2p_sam3.labeler import RenderedView, label_regions

    names = list(req.views or views_for(orientation))
    rr = render_views(mesh, kinds=("shaded",), views=names, face_colors=tex_rgb,
                      size_px=size_px)
    views = [RenderedView(r.view, r.image, r.face_id.astype(np.int32),
                          camera_pos_mm=tuple(float(x) for x in r.camera.eye)) for r in rr]
    prompts = list(req.prompts or DEFAULT_PROMPTS)
    pmap = {subject_kind: (subject_id, "body"), "dog": (subject_id, "body"),
            "cat": (subject_id, "body"), "base": (None, "base")}
    req2 = req.model_copy(update={"prompts": prompts, "views": None})
    seg = get_segmenter(backend or sam3_backend())
    res = label_regions(req2, mesh.vertices, mesh.faces, views, subject_id=subject_id,
                        prompt_map=pmap,
                        face_colors_lab=tex_lab if (req.markings and tex_lab is not None) else None,
                        segmenter=seg, first_region_id=first_region_id)
    return LabelOut(np.asarray(res.face_regions, np.int32), list(res.regions),
                    int(res.next_region_id),
                    {"views": names, "prompts": prompts, "backend": backend or sam3_backend(),
                     "n_regions": len(res.regions)})


def region_color_lab(mesh: Mesh, fr: np.ndarray, tex_lab: np.ndarray | None) -> dict[int, tuple]:
    if tex_lab is None:
        return {}
    n = int(fr.max()) + 1
    w = mesh.face_areas
    tot = np.bincount(fr, weights=w, minlength=n)
    out = {}
    means = np.stack([np.bincount(fr, weights=w * tex_lab[:, k], minlength=n) for k in range(3)], 1)
    for rid in np.nonzero(tot > 0)[0]:
        out[int(rid)] = tuple(round(float(x), 2) for x in means[rid] / tot[rid])
    return out


def split_by_filament(mesh: Mesh, fr: np.ndarray, regions: list[Region], face_fil: np.ndarray,
                      next_id: int, filament_names: dict[str, str], subject_id: str = "s1"):
    """Split every region (and the unlabeled faces) by baseline filament.

    Returns (face_regions, regions, next_id, assignment {rid: filament_id})."""
    fr = np.asarray(fr, np.int64).copy()
    area = mesh.face_areas
    fil = np.asarray(face_fil, dtype=object)
    ids = sorted({str(x) for x in fil})
    code = {f: i for i, f in enumerate(ids)}
    fcode = np.array([code[str(x)] for x in fil], np.int64)
    by_id = {r.id: r for r in regions}
    out_regions: list[Region] = []
    assignment: dict[int, str] = {}
    labels = np.unique(fr)
    for rid in labels:
        rid = int(rid)
        sel = fr == rid
        a = np.bincount(fcode[sel], weights=area[sel], minlength=len(ids))
        order = [int(i) for i in np.argsort(-a) if a[i] > 0]
        base = by_id.get(rid)
        if base is None:  # unlabeled faces: one region per filament
            for i in order:
                part_sel = sel & (fcode == i)
                name = filament_names.get(ids[i], ids[i])
                r = Region(id=next_id, label=f"{subject_id}.other_{name}", subject_id=subject_id,
                           part="other", source="meshy_color", n_faces=int(part_sel.sum()),
                           area_mm2=0.0, centroid_mm=(0, 0, 0), bbox_min_mm=(0, 0, 0),
                           bbox_max_mm=(0, 0, 0))
                fr[part_sel] = next_id
                assignment[next_id] = ids[i]
                out_regions.append(r)
                next_id += 1
            continue
        assignment[rid] = ids[order[0]]
        out_regions.append(base)
        for i in order[1:]:
            part_sel = sel & (fcode == i)
            name = filament_names.get(ids[i], ids[i])
            r = Region(id=next_id, label=f"{base.label}_{name}", subject_id=base.subject_id,
                       part=base.part, source="meshy_color", confidence=base.confidence,
                       n_faces=int(part_sel.sum()), area_mm2=0.0, centroid_mm=(0, 0, 0),
                       bbox_min_mm=(0, 0, 0), bbox_max_mm=(0, 0, 0), parent_ids=[rid])
            fr[part_sel] = next_id
            assignment[next_id] = ids[i]
            out_regions.append(r)
            next_id += 1
    return fr.astype(np.int32), out_regions, next_id, assignment


def save_labels(d: Path, out: LabelOut) -> None:
    d.mkdir(parents=True, exist_ok=True)
    np.save(d / "face_regions_sam.npy", out.face_regions)
    (d / "regions_sam.json").write_text(json.dumps({
        "regions": [r.model_dump(mode="json") for r in out.regions],
        "next_region_id": out.next_region_id, "stats": out.stats}, indent=1))


def load_labels(d: Path, n_faces: int) -> LabelOut | None:
    p, q = d / "face_regions_sam.npy", d / "regions_sam.json"
    if not (p.is_file() and q.is_file()):
        return None
    fr = np.load(p)
    if fr.shape != (n_faces,):
        return None
    j = json.loads(q.read_text())
    return LabelOut(fr.astype(np.int32), [Region.model_validate(r) for r in j["regions"]],
                    int(j["next_region_id"]), j.get("stats", {}))


def filament_short_names(filaments) -> dict[str, str]:
    """filament id -> short color word for labels (black, brown, white, ...)."""
    out = {}
    for f in filaments:
        out[f.id] = filament_word(f)
    return out
