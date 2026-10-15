"""Stage 9 for a paint edit (message kind paint_edit, contracts payloads.PaintEdit): the studio
editor's ops, or Claude's, applied to a finished paint as a new s09 attempt, then the preview.

p2p_worker.paint_edit starts the attempt with manifest data `paint_edit` (the payload, the
message id, `base`: the attempt folder of the paint the ops apply to). Then:

1. `edit`: a PaintSession in this attempt folder seeded from the base paint (region map, face
   regions, paint state; p2p_mcp_paint.edits.open_edit_session), or on a thin-slice job with no
   paint map yet, from the s07 baseline split by filament. The ops go through the session's
   region ops (edits.apply_ops): fill by region id, brush a face set into a new region, replace
   a slot. A refused op fails the attempt (validation); the orchestrator rolls the job back.
2. `before`: the base's own preview numbers when it was an edit, else p2p_slicer on the
   result's 3MF, so before and after come from the same slicer.
3. `preview`: simulate_print, final.3mf written exactly as s10 writes it (despeckle, then
   printing.write_print), p2p_slicer on it (time, grams, purge, per-layer toolpaths) and the
   eight proof views, paint and simulated print (edits.preview).
4. Outputs: paint_state.json, region_map.json, face_regions.npy (s10 reads these), final.3mf
   (role preview_3mf), preview/ (toolpaths, slice report, proof views), paint_preview.json
   (role paint_preview). The orchestrator then runs s10: the same paint through the slicer gate
   (Bambu Studio where installed) and a new result.

Manifest data `progress` is edit, preview, then done, for the web's status line.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import numpy as np
from p2p_contracts import StageId
from p2p_contracts.payloads import PaintEdit, PaintPreview, PrintNumbers

from .. import printing as pr
from ..errors import VendorError
from ..logs import log_event
from ..util import read_json
from .base import Stage, StageContext
from .common import geometry_of, jsonable, optional
from .s10_write import WriteStage

STATE_ROLES = {"paint_state.json": "paint_state", "region_map.json": "region_map",
               "face_regions.npy": "face_regions"}


def payload_of(data: dict[str, Any]) -> PaintEdit:
    return PaintEdit.model_validate({k: v for k, v in data.items() if k in PaintEdit.model_fields})


class PaintEditStage(Stage):
    id = StageId.S09_PAINT

    def __init__(self, sam3_backend: str | None = "off"):
        self.sam3_backend = sam3_backend

    # ------------------------------------------------------------------ helpers
    def _progress(self, ctx: StageContext, what: str) -> None:
        ctx.m.data["progress"] = what
        ctx.save(f"paint_edit:{what}")

    def _reader(self, ctx: StageContext, used: list[str]):
        def read(rel: str) -> str:
            from p2p_mcp_paint.session import ToolError

            if not rel.startswith("input/paint/") or ".." in rel.split("/"):
                raise ToolError(f"faces_path must be input/paint/<name>, got {rel!r}")
            try:
                p = ctx.store.fetch_input(ctx.job.id, rel)
            except FileNotFoundError as e:
                raise ToolError(f"stroke file {rel} not found") from e
            used.append(rel)
            return p.read_text()
        return read

    def _open(self, ctx: StageContext, base: str | None, fresh: bool):
        from p2p_mcp_paint.edits import open_edit_session
        from p2p_mcp_paint.job import load_job
        from p2p_mcp_paint.session import PaintSession

        if fresh:
            return open_edit_session(ctx.job.id, base=base, root=ctx.job_dir.parent,
                                     attempt_dir=ctx.dir, sam3_backend=self.sam3_backend)
        inp = load_job(ctx.job.id, ctx.dir / "cache", ctx.job_dir.parent)
        return PaintSession.resume(inp, ctx.dir, ctx.attempt, self.sam3_backend)

    def _writer(self, ctx: StageContext, geometry, palette):
        """final.3mf the way s10 writes it: despeckle, then printing.write_print."""
        settings = ctx.job.settings

        def write(s, _out_dir: Path) -> dict:
            v = np.asarray(s.inp.figure.vertices, float)
            f = np.asarray(s.inp.figure.faces, np.int64)
            clean, changed = pr.despeckle(v, f, s.face_slots(), settings)
            out = ctx.path("final.3mf")
            pr.write_print(out, v, f, clean, [palette[i] for i in s.palette], geometry, settings,
                           ctx.job_dir, part_filaments=s.part_filaments,
                           sidecar={"worker_paint": {"paint_edit_attempt": ctx.attempt,
                                                     "despeckle_faces": changed}})
            used = set(np.unique(clean).tolist()) | {s.part_slot(p) for p in s.inp.parts}
            return {"path": out, "slots_used": len(used), "despeckle_faces": changed}
        return write

    def _before(self, ctx: StageContext, base: str | None) -> PrintNumbers | None:
        """The base's preview numbers when it was an edit, else p2p_slicer on the result's
        3MF (the file the job ships now)."""
        from p2p_contracts.payloads import JobResult
        from p2p_mcp_paint.edits import slice_numbers

        if base:
            prev = read_json(ctx.abs(f"{base}/paint_preview.json"))
            if prev and prev.get("after"):
                return PaintPreview.model_validate(prev).after
        if not ctx.job.result:
            return None
        res = JobResult.model_validate(ctx.job.result)
        path = ctx.abs(res.threemf)
        if not path.is_file():
            return None
        ids: list[str] = []
        for u in sorted(res.filaments, key=lambda u: u.slot):
            ids += [""] * (u.slot - 1 - len(ids)) + [u.filament_id or ""]
        nums, _, _ = slice_numbers(path, ids)
        return nums

    # ------------------------------------------------------------------ run
    async def run(self, ctx: StageContext) -> None:
        from p2p_mcp_paint.edits import EditResult, apply_ops, preview
        from p2p_mcp_paint.session import ToolError

        data = ctx.m.data.get("paint_edit")
        if not data:
            raise VendorError("s09 paint edit without manifest data paint_edit",
                              code="internal", retryable=False)
        edit = payload_of(data)
        base = data.get("base")
        geometry = geometry_of(ctx)
        if base and ctx.abs(f"{base}/paint_state.json").is_file():
            ctx.add_input(f"{base}/paint_state.json", role="base_paint_state")
        session: list[Any] = []
        strokes: list[str] = []

        async def do_edit() -> dict:
            def work() -> dict:
                try:
                    s = self._open(ctx, base, fresh=True)
                    res = apply_ops(s, list(edit.ops), source=edit.source,
                                    read_faces=self._reader(ctx, strokes))
                except FileNotFoundError as e:
                    raise VendorError(f"paint edit needs the s06 figure: {e}",
                                      code="validation", retryable=False) from e
                except ToolError as e:
                    raise VendorError(str(e), code="validation", retryable=False) from e
                session.append(s)
                return res.__dict__

            return await asyncio.to_thread(work)

        self._progress(ctx, "edit")
        res = await ctx.step("edit", do_edit)
        for rel in dict.fromkeys(strokes):
            ctx.add_input(rel, role="paint_stroke")
        for name, role in STATE_ROLES.items():
            ctx.add_output(name, role=role)
        self._progress(ctx, "preview")

        async def do_before() -> dict | None:
            try:
                nums = await asyncio.to_thread(self._before, ctx, base)
            except Exception as e:  # noqa: BLE001 before numbers never block the edit
                log_event("paint_edit_before_failed", error=f"{type(e).__name__}: {e}"[:300])
                return None
            return nums.model_dump(mode="json") if nums else None

        before = await ctx.step("before", do_before)

        async def do_preview() -> dict:
            s = session[0] if session else await asyncio.to_thread(self._open, ctx, base, False)
            ids = list(s.palette)
            palette = dict(zip(ids, WriteStage()._palette(ctx, ids), strict=True))
            write = self._writer(ctx, geometry, palette)
            out = await asyncio.to_thread(
                preview, s, ctx.path("preview"), edit=EditResult(**res),
                before=PrintNumbers.model_validate(before) if before else None,
                base_attempt=base, write=write,
                log=lambda msg: log_event("paint_edit", msg=msg))
            return out.model_dump(mode="json")

        out = PaintPreview.model_validate(await ctx.step("preview", do_preview))
        ctx.write_json_output("paint_preview.json", out, role="paint_preview")
        if ctx.path("final.3mf").is_file():
            ctx.add_output("final.3mf", role="preview_3mf", kind="3mf")
        for name, role in (("preview/toolpaths.json", "toolpaths"),
                           ("preview/toolpaths.bin", "toolpaths_bin"),
                           ("preview/slice_report.json", "preview_slice_report")):
            if ctx.path(name).is_file():
                ctx.add_output(name, role=role)
        for p in out.proof_images:
            name = p.path.removeprefix(ctx.rel_dir + "/")
            if ctx.path(name).is_file():
                ctx.add_output(name, role=f"edit_proof_{p.kind}", kind=name.rsplit(".", 1)[-1])
        ctx.decide("paint.edit", f"{out.ops} ops, {out.changed_faces} faces", by="user",
                   reason=edit.note)
        ctx.m.data.update(jsonable({
            "agent": "paint_edit", "iteration": None, "ops": out.ops,
            "changed_faces": out.changed_faces, "palette": out.palette,
            "preview": {"after": out.after.model_dump(mode="json") if out.after else None,
                        "before": out.before.model_dump(mode="json") if out.before else None,
                        "warnings": out.warnings[:8], "timings_s": out.timings_s},
            "progress": "done"}))
        if optional(ctx, StageId.S08_PALETTE) is None:
            ctx.m.data["bootstrapped"] = base is None
        log_event("paint_edit_preview", ops=out.ops, changed_faces=out.changed_faces,
                  print_time_s=out.after.print_time_s if out.after else None,
                  warnings=out.warnings[:3] or None)
