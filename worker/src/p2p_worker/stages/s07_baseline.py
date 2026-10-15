"""Stage 7, baseline: Meshy Multi-Color, the X2D project, slicer gate, render sheet.

1. Meshy Multi-Color Print on the s05 task (Meshy needs the textured model), max_colors = AMS
   slots. Styles from BASELINE_STYLES (default cartoon only, 10 credits). The Meshy file is kept
   as baseline_<style>.3mf, role baseline_3mf_meshy. It is never the shipped result: it targets
   an X1 Carbon, and at 500k+ faces it can crash the Bambu CLI.
2. The X2D project baseline_x2d.3mf goes through p2p_worker.printing: Meshy's paint carried onto
   the s06 geometry figure (fitted to the UNCUT figure, so flat products keep their plinth cut),
   each Meshy color mapped to a loaded filament by CIEDE2000 with the achromatic guard (a gray
   coat never becomes brown), despeckled (islands under 2 x nozzle merge into a neighbour),
   written with the add-on parts and the supports GeometryResult asks for. Without a
   GeometryResult, Meshy's own figure prints. A Meshy color with no loaded filament within
   dE00 10 (and at least 0.5% of the model) gets a spool suggestion in data and a
   `spool_suggestion` message.
3. Thin slice (s07 ships): p2p_threemf.slice_check slices it headless; floating regions rebuild
   once with supports on. The SliceReport goes to slice_report.json.
   - Pass: render_sheet.png (the file as it prints, filament colors, as it sits on the plate)
     and preview.glb (per-face filament colors), then JobResult(threemf, render_sheet,
     preview_glb, slice_ok, print_time_s, filaments) is posted as `result`.
   - Fail: the X2D file stays only as debug_x2d.3mf, a `review_request` goes out with the
     errors and the render sheet, and the job moves to REVIEW. No result is posted.
   Full profile (s10 ships): no slicing here; baseline_x2d.3mf (role baseline_3mf) and the
   render sheet are inputs for s08 and the paint agent.

The build runs in a worker thread so the event loop keeps its heartbeat.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from p2p_contracts import SliceReport, StageId
from p2p_contracts.geometry import GeometryResult
from p2p_contracts.payloads import JobResult
from p2p_contracts.stages import PROFILE_FULL, PROFILE_THIN_SLICE

from .. import printing as pr
from .. import trace
from ..errors import VendorError
from ..logs import log_event
from ..offload import run_heavy
from ..util import data_uri, read_json
from .adapters import library_for
from .base import Stage, StageContext
from .common import (
    filament_usage,
    geometry_of,
    jsonable,
    pending_review,
    post_result,
    post_spool_suggestions,
    thumbnails,
    to_review,
)
from .s03_concept import gather_tasks
from .s05_mesh import fresh

PROFILES = {"thin_slice": PROFILE_THIN_SLICE, "full": PROFILE_FULL}


class BaselineStage(Stage):
    id = StageId.S07_BASELINE

    def _prior(self, ctx: StageContext, style: str, mesh_sha: str) -> tuple[Path, int] | None:
        """A Meshy 3MF an earlier attempt of this stage already downloaded for the same mesh."""
        for a in range(ctx.attempt - 1, 0, -1):
            m = ctx.store.load_manifest(ctx.job.id, self.id, a)
            if m is None or not any(i.role == "approved_mesh" and i.sha256 == mesh_sha
                                    for i in m.inputs):
                continue
            for o in m.outputs:
                p = ctx.abs(o.path)
                if o.path.endswith(f"/baseline_{style}.3mf") and p.is_file():
                    return p, a
        return None

    async def _one(self, ctx: StageContext, style: str, body_fn, mesh_sha: str) -> str:
        key = f"mc.{style}"
        name = f"baseline_{style}.3mf"
        prior = None if ctx.task(key) else self._prior(ctx, style, mesh_sha)
        if prior is not None:  # a rerun: reuse the file, spend nothing
            src, attempt = prior
            ref = ctx.copy_output(src, name, role="baseline_3mf_meshy")
            ctx.decide(f"baseline.reused.{style}", f"a{attempt}", by="code",
                       reason="same approved mesh; Meshy 3MF already downloaded")

            async def report(attempt=attempt) -> dict[str, Any]:
                m = ctx.store.load_manifest(ctx.job.id, self.id, attempt)
                paid = next((t for t in (m.meshy_tasks if m else [])
                             if (m.data.get("meshy_keys") or {}).get(key) == t.task_id), None)
                if paid is not None:
                    ctx.record_file_reuse(key, "print-multi-color", f"baseline 3MF ({style})",
                                          paid, attempt, why="same approved mesh")
                return {"task_id": paid.task_id if paid else None}

            await ctx.step(f"reused.{style}", report)
            ctx.save(f"reused:{name}")
            return ref.path
        await ctx.meshy_run(key, "print-multi-color", body_fn(style),
                            f"baseline 3MF ({style})",
                            why=f"Meshy's paint of the approved mesh ({style}) for s07 and s08")
        ref = await ctx.fetch_asset(key, lambda t: (t.get("model_urls") or {}).get("3mf"),
                                    f"baseline_{style}.3mf", role="baseline_3mf_meshy",
                                    kind="3mf")
        if ref is None:
            raise VendorError(f"Multi-Color {style} returned no 3MF", retryable=True)
        return ref.path

    def _build(self, ctx: StageContext, meshy_3mf: Path, glb: Path,
               geometry: GeometryResult | None, gate: bool) -> dict[str, Any]:
        """Paint transfer, guarded color map, despeckle, X2D write, slicer gate and previews
        (blocking; runs in a thread)."""
        settings = ctx.job.settings
        library, source = library_for(ctx.store, ctx.job.id, ctx.settings.repo_root)
        if library is None:
            rep = pr.failed_report(settings, "no filament library (Supabase filaments or "
                                             "filaments/library.json)")
            return {"retarget": {}, "report": rep.model_dump(mode="json"), "render": {}}
        pool = pr.candidates(library)
        full_catalog = pr.load_catalog(ctx.settings.repo_root)
        catalog = pr.auto_pick(full_catalog)  # for suggestions
        # add-on parts may use a color the job allowed but the AMS does not hold (source any)
        extra = pr.part_filaments_missing(geometry, pool, library, full_catalog)
        out = ctx.path("baseline_x2d.3mf")
        try:
            painted = pr.transfer_paint(meshy_3mf, glb, geometry, ctx.job_dir)
        except Exception as e:  # noqa: BLE001 a failed transfer is a failed gate
            rep = pr.failed_report(settings, f"paint transfer failed: {type(e).__name__}: {e}")
            return {"retarget": {}, "report": rep.model_dump(mode="json"), "render": {}}
        v, f = painted.vertices, painted.faces
        share = pr.area_share(v, f, painted.src_slot)
        used = sorted(share)
        matches = {s: pr.match_colour(painted.src_colours[s - 1], pool, catalog) for s in used}
        ids = [x.id for x in pool]
        lut = np.zeros(max(used) + 1, dtype=np.int64)
        for s in used:
            lut[s] = ids.index(matches[s].filament_id) + 1
        slots = lut[painted.src_slot]
        before = pr.islands(v, f, slots, settings)
        clean, changed = pr.despeckle(v, f, slots, settings)
        after = pr.islands(v, f, clean, settings)
        mapping = [{"src_slot": s, "share": round(share[s], 4), **asdict(matches[s])}
                   for s in used]
        despeckle = {"faces_changed": changed, "islands_before": before, "islands_after": after}
        sidecar = {"worker_paint": {"source": meshy_3mf.name, "library": source,
                                    "figure_source": painted.figure_source, "fit": painted.fit,
                                    "transfer": painted.transfer, "mapping": mapping,
                                    "despeckle": despeckle}}
        res, rep = pr.build_and_gate(
            out, lambda o: pr.write_print(out, v, f, clean, pool + extra, geometry, settings,
                                          ctx.job_dir, overrides=o, sidecar=sidecar),
            settings, ctx.dir, ctx.job_dir, gate=gate)
        render: dict[str, Any] = {}
        ok = rep is None or rep.ok
        if out.is_file():
            render = pr.render_outputs(out, ctx.path("render_sheet.png"),
                                       ctx.path("preview.glb") if ok else None,
                                       f"{str(ctx.job.product_type).capitalize()} "
                                       "print preview")
        retarget = {k: v for k, v in res.items() if k != "path"}
        retarget.update(mapping=mapping, despeckle=despeckle, library=source,
                        figure_source=painted.figure_source, fit=painted.fit,
                        transfer=painted.transfer)
        warnings = painted.warnings
        if rep is not None:
            rep.warnings.extend(w for w in warnings if w not in rep.warnings)
        return jsonable({"retarget": retarget, "render": render,
                         "report": rep.model_dump(mode="json") if rep else None,
                         "warnings": warnings})

    async def run(self, ctx: StageContext) -> None:
        job = ctx.job
        glb_path, glb_ref = ctx.use_output(StageId.S05_MESH, "approved_mesh")
        mesh_json, _ = ctx.use_output(StageId.S05_MESH, "mesh")
        geometry = geometry_of(ctx)
        mesh = read_json(mesh_json)
        finished = datetime.fromisoformat(mesh["finished_at"]) if mesh.get("finished_at") else None
        slots = job.settings.ams_slots
        def body(style: str) -> dict[str, Any]:  # built only when Meshy is called
            # A Creative Lab build (flat charm) is not accepted as input_task_id: the model.
            src: dict[str, Any] = (
                {"input_task_id": mesh["meshy_task_id"]}
                if fresh(finished) and mesh.get("chainable", True)
                else {"model_url": ctx.store.signed_url(job.id, glb_ref.path)
                      or data_uri(glb_path.read_bytes(), "model/gltf-binary")})
            return {**src, "max_colors": slots, "style": style}

        styles = list(ctx.settings.baseline_styles)
        results = await gather_tasks(
            [self._one(ctx, s, body, glb_ref.sha256) for s in styles])
        paths = {s: r for s, r in zip(styles, results, strict=True) if isinstance(r, str)}
        if not paths:
            raise next(r for r in results if isinstance(r, VendorError))
        for s, r in zip(styles, results, strict=True):
            if isinstance(r, VendorError):
                ctx.decide(f"baseline.failed.{s}", str(r), by="code", reason="kept the other style")
        primary_style = next(s for s in styles if s in paths)
        meshy_3mf = ctx.abs(paths[primary_style])
        ctx.decide("baseline.primary_style", primary_style, by="code")
        # In the full profile s10 ships and gates the painted file; s07 only builds the
        # baseline project for s08 and the paint agent.
        final = StageId.S10_WRITE not in PROFILES.get(job.profile, ())

        async def gate() -> dict[str, Any]:  # a child process: the loop keeps running
            return await run_heavy(ctx, self._build, meshy_3mf, glb_path, geometry, final)

        out = await ctx.step("gate", gate)
        retarget = out["retarget"]
        render = out.get("render") or {}
        x2d = ctx.path("baseline_x2d.3mf")
        mapping = retarget.get("mapping") or []
        ctx.m.data.update({"max_colors": slots, "styles": list(paths),
                           "retargeted": bool(retarget.get("palette")),
                           "supports": retarget.get("supports", False),
                           "palette": retarget.get("palette"),
                           "figure_source": retarget.get("figure_source"),
                           "mapping": mapping, "despeckle": retarget.get("despeckle"),
                           "library": retarget.get("library")})
        suggestions = sorted((m for m in mapping
                              if m.get("suggestion") and m["share"] >= pr.MIN_SHARE),
                             key=lambda m: -m["share"])
        ctx.m.data["spool_suggestions"] = [m["suggestion"] for m in suggestions]
        sheet = ctx.path("render_sheet.png")
        if sheet.is_file():
            ctx.add_output(sheet.name, role="render_sheet", kind="png")
        if out["report"] is None:  # full profile: no gate here
            if not x2d.is_file():
                raise VendorError("baseline X2D project was not written", code="internal",
                                  retryable=False)
            ctx.add_output(x2d.name, role="baseline_3mf", kind="3mf")
            ctx.decide("baseline.slicer_gate", "skipped", by="code",
                       reason="full profile: s10 gates the painted file")
            ctx.m.data["slice_ok"] = None
            ctx.m.confidence = 1.0
            return
        report = SliceReport.model_validate(out["report"])
        ctx.write_json_output("slice_report.json", report, role="slice_report")
        trace.record_slicer(report)  # also keeps the .slice.log as an output (to Storage)
        log_event("slicer_gate", ok=report.ok, print_time_s=report.print_time_s,
                  slots_used=report.slots_used, expected_slots=report.expected_slots,
                  errors=report.errors[:5] or None)
        gated = pr.ship_gated(report)
        ctx.decide("baseline.slicer_gate",
                   ("pass" if gated else "estimate") if report.ok else "fail", by="code",
                   reason="; ".join(report.errors[:5]) or (
                       None if gated else f"{report.slicer} only, Bambu gate pending"))
        ctx.m.data["slice_ok"] = report.ok if gated or not report.ok else None
        if suggestions:
            await post_spool_suggestions(ctx, suggestions)
        if not report.ok:
            images = ([sheet_rel] if (sheet_rel := ctx.rel(sheet.name)) and sheet.is_file()
                      else [])
            await to_review(ctx, report, x2d, images + thumbnails(ctx)[:4])
            return

        ctx.add_output(x2d.name, role="baseline_3mf", kind="3mf")
        glb_out = ctx.path("preview.glb")
        preview = None
        if glb_out.is_file():
            preview = ctx.add_output(glb_out.name, role="preview_glb", kind="glb").path
        palette = list(retarget.get("palette") or [])
        result = JobResult(threemf=ctx.rel(x2d.name),
                           render_sheet=ctx.rel(sheet.name) if sheet.is_file() else None,
                           preview_glb=preview or glb_ref.path,
                           print_time_s=report.print_time_s,
                           filaments=filament_usage(report, palette),
                           baseline_only=True, slice_ok=True if gated else None)
        await post_result(ctx, result)
        ctx.m.data.update({"threemf": result.threemf, "print_time_s": report.print_time_s,
                           "total_grams": report.total_grams,
                           "render_faces": render.get("faces")})
        if not gated:
            await pending_review(ctx, report, [ctx.rel(sheet.name)] if sheet.is_file() else [])
        ctx.m.confidence = 1.0

