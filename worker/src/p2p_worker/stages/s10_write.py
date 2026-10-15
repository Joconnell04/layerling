"""Stage 10, write and slice: the final X2D project from the paint state, slicer gate, render
sheet and preview GLB.

Inputs: s09's paint_state.json (s08's first pass when s09 is skipped), the region map and
face_regions.npy it points at, s06's GeometryResult (add-on parts, supports), and the filament
library snapshot s08 took (filaments.json).

1. Face slots: each face takes its region's filament (unlabeled faces the default filament),
   as a 1-based index into PaintState.palette (AMS order, so slot N = AMS slot N).
2. Despeckle: islands under 2 x nozzle merge into their neighbour before the write.
3. p2p_threemf.write_3mf via p2p_worker.printing.write_print: the figure mesh the region map
   indexes, the add-on parts with PaintState.part_filaments, supports from GeometryResult.
4. Slicer gate (slice_check); floating regions rebuild once with supports on.
   - Pass: final.3mf, render_sheet.png (as it prints, filament colors, as it sits on the
     plate), preview.glb (per-face COLOR_0), the proof set (proof/: the 3D model, the paint
     and the simulated print from the eight proof views, contracts PROOF_VIEWS), and
     JobResult(baseline_only=False, proof_images) as `result`.
   - Fail: debug_x2d.3mf, a `review_request` with the render sheet, job to REVIEW.
   - p2p_slicer pass (no Bambu Studio on this worker): the pass outputs and a JobResult with
     slice_ok None, then a "Bambu gate pending" review; approve is refused until a Mac worker
     reruns the stage with Bambu Studio.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from p2p_contracts import Filament, FilamentLibrary, PaintState, RegionMap, SliceReport, StageId
from p2p_contracts.geometry import GeometryResult
from p2p_contracts.payloads import JobResult, ProofImage

from .. import printing as pr
from ..errors import VendorError
from ..logs import log_event
from ..offload import run_heavy
from ..util import read_json
from .adapters import library_for
from .base import Stage, StageContext
from .common import (
    filament_usage,
    geometry_of,
    jsonable,
    optional,
    pending_review,
    post_result,
    to_review,
)


def face_slots_for(state: PaintState, face_regions: np.ndarray) -> np.ndarray:
    """1-based palette slot per face from region -> filament (0 / unassigned -> default)."""
    idx = {fid: i + 1 for i, fid in enumerate(state.palette)}
    if state.default_filament not in idx:
        raise ValueError(f"default filament {state.default_filament} is not in the palette")
    n = int(face_regions.max(initial=0)) + 1
    lut = np.full(n, idx[state.default_filament], dtype=np.int64)
    for rid, fid in state.assignment.items():
        r = int(rid)
        if fid not in idx:
            raise ValueError(f"region {rid} uses {fid}, which is not in the palette")
        if 0 < r < n:
            lut[r] = idx[fid]
    return lut[np.asarray(face_regions, dtype=np.int64)]


def hang_of(ctx: StageContext) -> dict | None:
    """How the product hangs (s06 geometry_report.json metrics.hang), for the hanging proof
    view; None without a loop."""
    try:
        path, _ = ctx.use_output(StageId.S06_PRINTABILITY, "geometry")
    except Exception:  # noqa: BLE001 no geometry: no loop
        return None
    report = read_json(path.parent / "geometry_report.json") or {}
    hang = (report.get("metrics") or {}).get("hang")
    return hang if isinstance(hang, dict) else None


class WriteStage(Stage):
    id = StageId.S10_WRITE

    def _state(self, ctx: StageContext) -> tuple[PaintState, StageId]:
        src = StageId.S09_PAINT if optional(ctx, StageId.S09_PAINT) else StageId.S08_PALETTE
        path, _ = ctx.use_output(src, "paint_state")
        return PaintState.model_validate(read_json(path)), src

    def _palette(self, ctx: StageContext, ids: list[str]) -> list[Filament]:
        """Filament objects for the palette ids: s08's snapshot, then the live library, then
        the catalog."""
        pool: dict[str, Filament] = {}
        m8 = optional(ctx, StageId.S08_PALETTE)
        if m8 is not None and any(o.role == "filament_library" for o in m8.outputs):
            path, _ = ctx.use_output(StageId.S08_PALETTE, "filament_library")
            pool.update({f.id: f for f in FilamentLibrary.model_validate(
                read_json(path)).filaments})
        lib, _ = library_for(ctx.store, ctx.job.id, ctx.settings.repo_root)
        for f in (lib.filaments if lib else []) + pr.load_catalog(ctx.settings.repo_root):
            pool.setdefault(f.id, f)
        missing = [i for i in ids if i not in pool]
        if missing:
            raise VendorError(f"palette filaments not in the library: {missing}",
                              code="validation", retryable=False)
        return [pool[i] for i in ids]

    def _build(self, ctx: StageContext, state: PaintState, rmap: RegionMap,
               palette: list[Filament], geometry: GeometryResult | None,
               hang: dict | None = None) -> dict[str, Any]:
        from p2p_threemf.ply import read_ply

        settings = ctx.job.settings
        mesh_path = ctx.abs(rmap.mesh.path)
        v, f = read_ply(mesh_path)
        v, f = np.asarray(v, float), np.asarray(f, np.int64)
        face_regions = np.load(ctx.abs(rmap.face_regions.path))
        if len(face_regions) != len(f):
            raise VendorError(f"face_regions has {len(face_regions)} ids for {len(f)} faces",
                              code="validation", retryable=False)
        slots = face_slots_for(state, face_regions)
        before = pr.islands(v, f, slots, settings)
        clean, changed = pr.despeckle(v, f, slots, settings)
        after = pr.islands(v, f, clean, settings)
        despeckle = {"faces_changed": changed, "islands_before": before, "islands_after": after}
        out = ctx.path("final.3mf")
        sidecar = {"worker_paint": {"paint_state_iteration": state.iteration,
                                    "region_map": rmap.face_regions.path,
                                    "despeckle": despeckle}}
        res, rep = pr.build_and_gate(
            out, lambda o: pr.write_print(out, v, f, clean, palette, geometry, settings,
                                          ctx.job_dir, overrides=o,
                                          part_filaments=state.part_filaments, sidecar=sidecar),
            settings, ctx.dir, ctx.job_dir)
        render: dict[str, Any] = {}
        ok = rep is not None and rep.ok
        if out.is_file():
            render = pr.render_outputs(out, ctx.path("render_sheet.png"),
                                       ctx.path("preview.glb") if ok else None,
                                       f"{str(ctx.job.product_type).capitalize()} "
                                       "print preview")
        proof: dict[str, Any] = {}
        try:
            proof = pr.proof_renders(ctx.path("proof"), v, f, clean, palette, geometry, settings,
                                     ctx.job_dir, state.part_filaments,
                                     title=str(ctx.job.product_type).capitalize(),
                                     hang=hang)
            proof["images"] = [{"kind": r["kind"], "view": r["view"], "path": ctx.rel(
                f"proof/{r['path'].name}")} for r in proof["images"]]
        except Exception as e:  # noqa: BLE001 proofs never block the gate result
            proof = {"images": [], "error": f"{type(e).__name__}: {e}"}
        retarget = {k: val for k, val in res.items() if k != "path"}
        retarget["despeckle"] = despeckle
        return jsonable({"retarget": retarget, "render": render, "proof": proof,
                         "report": rep.model_dump(mode="json") if rep else None})

    async def run(self, ctx: StageContext) -> None:
        state, src = self._state(ctx)
        ctx.add_input(state.region_map.path, role="region_map")
        rmap = RegionMap.model_validate(read_json(ctx.abs(state.region_map.path)))
        ctx.add_input(rmap.face_regions.path, role="face_regions")
        geometry = geometry_of(ctx)
        palette = self._palette(ctx, state.palette)

        hang = hang_of(ctx)

        async def gate() -> dict[str, Any]:  # a child process: the loop keeps running
            return await run_heavy(ctx, self._build, state, rmap, palette, geometry, hang)

        out = await ctx.step("gate", gate)
        report = SliceReport.model_validate(out["report"])
        ctx.write_json_output("slice_report.json", report, role="slice_report")
        retarget = out["retarget"]
        log_event("slicer_gate", ok=report.ok, print_time_s=report.print_time_s,
                  slots_used=report.slots_used, expected_slots=report.expected_slots,
                  errors=report.errors[:5] or None)
        gated = pr.ship_gated(report)
        ctx.decide("write.slicer_gate", ("pass" if gated else "estimate") if report.ok else "fail",
                   by="code", reason="; ".join(report.errors[:5]) or (
                       None if gated else f"{report.slicer} only, Bambu gate pending"))
        ctx.m.data.update({"paint_from": src.value, "iteration": state.iteration,
                           "palette": state.palette,
                           "slice_ok": report.ok if gated or not report.ok else None,
                           "supports": retarget.get("supports", False),
                           "slots_used": retarget.get("slots_used"),
                           "despeckle": retarget.get("despeckle")})
        sheet = ctx.path("render_sheet.png")
        if sheet.is_file():
            ctx.add_output(sheet.name, role="render_sheet", kind="png")
        x2d = ctx.path("final.3mf")
        proof_rows = (out.get("proof") or {}).get("images") or []
        for row in proof_rows:
            name = row["path"].removeprefix(ctx.rel_dir + "/")
            if ctx.path(name).is_file():
                ctx.add_output(name, role="proof_sheet" if row["view"] == "sheet"
                               else f"proof_{row['kind']}", kind="png")
        if (out.get("proof") or {}).get("sim"):
            ctx.m.data["proof_sim"] = out["proof"]["sim"]
        if not report.ok:
            images = [ctx.rel(sheet.name)] if sheet.is_file() else []
            await to_review(ctx, report, x2d, images)
            return
        ctx.add_output(x2d.name, role="final_3mf", kind="3mf")
        preview = ctx.add_output("preview.glb", role="preview_glb", kind="glb").path \
            if ctx.path("preview.glb").is_file() else None
        result = JobResult(threemf=ctx.rel(x2d.name),
                           render_sheet=ctx.rel(sheet.name) if sheet.is_file() else None,
                           preview_glb=preview, print_time_s=report.print_time_s,
                           filaments=filament_usage(report, state.palette),
                           baseline_only=False, slice_ok=True if gated else None,
                           proof_images=[ProofImage(kind=r["kind"], view=r["view"], path=r["path"])
                                         for r in proof_rows if r["view"] != "sheet"])
        await post_result(ctx, result)
        ctx.m.data.update({"threemf": result.threemf, "print_time_s": report.print_time_s,
                           "total_grams": report.total_grams})
        if not gated:
            await pending_review(ctx, report, [ctx.rel(sheet.name)] if sheet.is_file() else [])
        ctx.m.confidence = 1.0
