"""s09 for replays: the stored run's final paint, carried onto the rerun's figure.

The paint agent takes 20 minutes to hours of Opus turns, so a replay does not rerun it by
default. ReplayPaint takes the paint the stored run shipped (its accepted s09 attempt's
paint_state.json, region_map.json and face_regions.npy, kept in the fixture store) and, when the
rerun's s06 figure has the same faces (face ids are Meshy's order, so the same count means the
same faces), writes it as this attempt's paint. s10 then writes and gates exactly the paint the
customer approved, so print time and grams compare with the stored run's. When the figure
differs (another face count), the paint cannot carry over: the attempt falls back to the stub
(s08's first pass) and says why in its manifest data (`paint_replay`).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import numpy as np
from p2p_contracts import PaintState, RegionMap, StageId
from p2p_worker.stages.base import Stage, StageContext
from p2p_worker.stages.s09_stub import PaintStub
from p2p_worker.util import read_json


def stored_paint(files: Path) -> dict[str, Any] | None:
    """The stored run's shipped paint: its accepted s09 attempt's manifest and paint state, the
    region map and face regions that state points at (paths in the store), or None."""
    job = read_json(files / "job.json") or {}
    n = (job.get("attempts") or {}).get("s09_paint")
    d = files / "s09_paint" / f"a{n}"
    m = read_json(d / "manifest.json") if n else None
    state = read_json(d / "paint_state.json") if n else None
    if not m or m.get("status") != "succeeded" or not state:
        return None
    rmap_path = files / state["region_map"]["path"]
    rmap = read_json(rmap_path)
    if not rmap or not (files / rmap["face_regions"]["path"]).is_file():
        return None
    return {"dir": d, "manifest": m, "state": state, "region_map": rmap,
            "face_regions": files / rmap["face_regions"]["path"]}


class ReplayPaint(Stage):
    id = StageId.S09_PAINT

    def __init__(self, files: Path):
        self.files = Path(files)

    async def run(self, ctx: StageContext) -> None:
        src = stored_paint(self.files)
        path8, _ = ctx.use_output(StageId.S08_PALETTE, "paint_state")
        state8 = PaintState.model_validate(read_json(path8))
        rmap8 = RegionMap.model_validate(read_json(ctx.abs(state8.region_map.path)))
        why = None
        if src is None:
            why = "the stored run has no accepted paint (s09 skipped or not finished)"
        else:
            n_stored = len(np.load(src["face_regions"], mmap_mode="r"))
            if n_stored != rmap8.mesh.n_faces:
                why = (f"stored paint has {n_stored} faces, the rerun figure "
                       f"{rmap8.mesh.n_faces}")
        if why is not None:
            await PaintStub().run(ctx)
            ctx.m.data["paint_replay"] = {"used": False, "reason": why}
            ctx.decide("paint.replay", "stub", by="code", reason=why)
            return
        m9 = src["manifest"]
        ctx.add_input(state8.region_map.path, from_stage=StageId.S08_PALETTE,
                      from_attempt=ctx.job.attempts.get(StageId.S08_PALETTE), role="region_map")
        shutil.copyfile(src["face_regions"], ctx.path("face_regions.npy"))
        fr = ctx.add_output("face_regions.npy", role="face_regions", kind="npy")
        stored_map = dict(src["region_map"])
        same_mesh = stored_map["mesh"]["sha256"] == rmap8.mesh.sha256
        stored_map["mesh"] = rmap8.mesh.model_dump(mode="json")
        stored_map["face_regions"] = fr.model_dump(mode="json")
        rm = ctx.write_json_output("region_map.json",
                                   RegionMap.model_validate(stored_map), role="region_map")
        state = PaintState.model_validate(src["state"])
        ctx.write_json_output("paint_state.json", state.model_copy(update={"region_map": rm}),
                              role="paint_state")
        data9 = m9.get("data") or {}
        check = data9.get("concept_check") or {}
        info = {"used": True, "from": f"s09_paint/{src['dir'].name}", "same_mesh": same_mesh,
                "iteration": state.iteration, "stored_agent": data9.get("agent"),
                "stored_check_passed": check.get("passed") if check else None,
                "stored_review_approved": bool(data9.get("review_approved"))}
        ctx.m.data.update({"agent": "replay", "paint_replay": info})
        ctx.decide("paint.replay", info["from"], by="code",
                   reason="the stored run's shipped paint; figure "
                          + ("identical" if same_mesh else "same faces, new vertices"))
        if m9.get("confidence") is not None:
            ctx.m.confidence = m9["confidence"]
