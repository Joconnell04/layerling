"""Stage 6, printability and add-ons.

1. Meshy Analyze Printability on the approved mesh (free). Recorded as printability.json.
   Repair (10 credits) is left to M3.
2. p2p_geometry.build_product (pinned in contracts/geometry.py):
     build_product(figure_vertices, figure_faces, product, addons, target_size_mm, library,
                   settings, out_dir) -> GeometryResult
   scales and orients the figure and builds the exact-geometry add-ons. This stage writes the
   result to geometry.json and records every mesh it names as an output.
   Until build_product exists, no geometry.json is written, a decision says why, and s07 calls
   retarget_3mf with geometry=None.
3. The approved product (p2p_worker.product_spec): the form the customer chose (full 3D figure
   or flat charm; geometry never flattens a figure the job did not ask to flatten), the plate
   shape, add-on color picks and the palette source, each with where it came from.
4. Add-on colors: the job's picks, then a vision read of the approved concept (concept_read,
   Sonnet 5 through the Claude Code login, cached by the concept's sha256), then the figure
   color the part touches (the Meshy texture per face). Unowned catalog colors (PLA Basic and
   Matte) are candidates only when the palette source is any.

Outputs: printability.json, geometry.json (when built), warnings.json, part meshes,
product_resolved.json, concept_read.json.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from p2p_contracts import StageId
from p2p_contracts.geometry import GeometryResult

from .. import printing as pr
from ..concept_read import cached as cached_read
from ..concept_read import read_concept, sha256
from ..errors import VendorError
from ..offload import run_heavy
from ..product_spec import read_spec, resolve_loop
from ..util import data_uri, read_json
from .adapters import find, load_library
from .base import Stage, StageContext
from .s05_mesh import fresh


class GeometryStage(Stage):
    id = StageId.S06_PRINTABILITY

    async def run(self, ctx: StageContext) -> None:
        job = ctx.job
        glb, glb_ref = ctx.use_output(StageId.S05_MESH, "approved_mesh")
        mesh_json, _ = ctx.use_output(StageId.S05_MESH, "mesh")
        ctx.use_output(StageId.S01_INTAKE, "addons")
        mesh = read_json(mesh_json)
        finished = datetime.fromisoformat(mesh["finished_at"]) if mesh.get("finished_at") else None

        # A Creative Lab build (flat charm) is not accepted as input_task_id: send the model.
        chain = fresh(finished) and mesh.get("chainable", True)
        src = ({"input_task_id": mesh["meshy_task_id"]} if chain
               else {"model_url": ctx.store.signed_url(job.id, glb_ref.path)
                     or data_uri(glb.read_bytes(), "model/gltf-binary")})
        try:
            analysis = await ctx.meshy_run("analyze", "print-analyze", src,
                                           "printability analysis",
                                           why="printability of the approved mesh (free)")
            printability = analysis.get("printability") or {}
        except VendorError as e:
            prior = None if e.retryable else self._prior_printability(ctx, glb_ref.sha256)
            if prior is None and e.retryable:
                raise
            printability = prior or {}
            ctx.decide("printability.reused", "prior attempt" if prior else "not run", by="code",
                       reason=f"Meshy unavailable ({e}); the analysis is free and advisory")
        ctx.write_json_output("printability.json", printability, role="printability")
        metrics = printability.get("metrics") or {}
        ctx.decide("printability.status", printability.get("status"), by="code",
                   reason=f"watertight={metrics.get('is_watertight')} "
                          f"non_manifold_edges={metrics.get('non_manifold_edges')} "
                          f"holes={metrics.get('holes')}")

        known = self._known(ctx)
        spec = read_spec(ctx.job_dir, job, ctx.store, set(known))
        ctx.write_json_output("product_resolved.json", spec.to_json(), role="product_resolved")
        ctx.decide("product.form", spec.form or "default", by="code", reason=spec.form_source)
        read = await self._concept_read(ctx)

        async def build() -> dict[str, Any]:  # a child process: the loop keeps running
            return await run_heavy(ctx, self._build, glb, spec, read, known,
                                   not mesh.get("chainable", True))

        geo = await ctx.step("geometry", build)
        warnings = list(geo.get("warnings") or [])
        shown = (read or {}).get("form")
        built = geo.get("form") or spec.form
        if shown in ("figure", "flat") and built and shown != built:
            # the customer approved an image of the other form: say so before any paint
            warnings.append(f"The approved concept shows a {'3D figure' if shown == 'figure' else 'flat charm'}; "
                            f"the job's form is {built} ({spec.form_source})")
            ctx.decide("product.form_vs_concept", "mismatch", by="code",
                       reason=f"concept {shown}, built {built}")
        if printability.get("error_count"):
            warnings.append(f"Meshy printability: {printability['error_count']} errors")
        ctx.write_json_output("warnings.json", warnings, role="geometry_warnings")
        parts: list[str] = []
        if geo.get("result"):
            result = GeometryResult.model_validate(geo["result"])
            ctx.write_json_output("geometry.json", result, role="geometry")
            for part in [result.figure, *result.parts]:
                parts.append(part.name)
                if part.mesh.path.startswith(ctx.rel_dir + "/"):
                    ctx.add_output(part.mesh.path.rsplit("/", 1)[-1], role=f"part_{part.role}")
        ctx.m.data.update({
            "geometry": geo["source"],
            "parts": parts,
            "warnings": len(warnings),
            "printability": printability.get("status"),
            "addons": [a.kind.value for a in job.addons],
            "form": geo.get("form") or spec.form,
            "form_source": spec.form_source,
            "plate_shape": spec.plate_shape,
            "palette_source": spec.palette_source,
            "addon_colors": geo.get("addon_colors"),
            "hang_tilt_deg": geo.get("hang_tilt_deg"),
            "hang": geo.get("hang"),
            "loop_area": geo.get("loop_area"),
        })
        ctx.m.confidence = 1.0 if geo["source"] == "p2p_geometry" else 0.5

    @staticmethod
    def _job_relative(ctx: StageContext, result: GeometryResult) -> GeometryResult:
        """Mesh paths as job-relative POSIX (build_product only knows out_dir)."""

        def fix(ref):
            p = Path(ref.path)
            if p.is_absolute():
                rel = p.resolve().relative_to(ctx.job_dir.resolve()).as_posix()
            elif (ctx.job_dir / p).is_file():
                rel = p.as_posix()
            else:
                rel = ctx.rel(p.as_posix())  # relative to out_dir
            return ref.model_copy(update={"path": rel})

        def part(pr):
            return pr.model_copy(update={"mesh": fix(pr.mesh)})

        upd: dict[str, Any] = {"figure": part(result.figure),
                               "parts": [part(p) for p in result.parts]}
        if result.face_index_map is not None:
            upd["face_index_map"] = fix(result.face_index_map)
        return result.model_copy(update=upd)

    @staticmethod
    def _prior_printability(ctx: StageContext, glb_sha: str) -> dict | None:
        for a in range(ctx.attempt - 1, 0, -1):
            m = ctx.store.load_manifest(ctx.job.id, StageId.S06_PRINTABILITY, a)
            if m is None or not any(i.role == "approved_mesh" and i.sha256 == glb_sha
                                    for i in m.inputs):
                continue
            p = ctx.job_dir / StageId.S06_PRINTABILITY.value / f"a{a}" / "printability.json"
            if p.is_file():
                return read_json(p) or None
        return None

    @staticmethod
    def _known(ctx: StageContext) -> dict:
        lib = load_library(ctx.settings.repo_root)
        known = {f.id: f for f in pr.load_catalog(ctx.settings.repo_root)}
        known.update({f.id: f for f in (lib.filaments if lib else [])})
        return known

    async def _concept_read(self, ctx: StageContext) -> dict | None:
        """The approved concept's vision read (cached by sha256), or None without a concept."""
        from ..concept_read import ConceptRead

        try:
            path, ref = ctx.use_output(StageId.S03_CONCEPT, "approved_concept")
        except Exception:  # noqa: BLE001 no concept stage (tests, old jobs)
            return None
        sha = ref.sha256 or sha256(path)
        prior = cached_read(ctx, sha)
        if prior is not None:
            ctx.write_json_output("concept_read.json", prior, role="concept_read")
            ctx.decide("concept_read", "cached", by="code", reason="same concept image")
            return prior["read"]

        client = ctx.svc.claude
        live = not getattr(client, "mock", False) and hasattr(client, "ask")

        async def call():
            return await read_concept(client, path)

        async def step():
            if not live:  # mock or a client without a vision call: nothing to book
                read, _ = await read_concept(client, path)
                return {"read": read.model_dump(), "model": "mock"}
            try:
                read = await ctx.claude("concept_read", 0.0, call)
            except VendorError as e:
                ctx.decide("concept_read", "failed", by="code", reason=str(e)[:300])
                return {"read": None, "error": str(e)[:300]}
            return {"read": ConceptRead.model_validate(read).model_dump(),
                    "model": "claude-sonnet-5"}

        out = await ctx.step("concept_read", step)
        ctx.write_json_output("concept_read.json", {"concept": ref.path, "concept_sha256": sha,
                                                    **out}, role="concept_read")
        return out.get("read")

    def _build(self, ctx: StageContext, glb, spec, read: dict | None,
               known: dict, relief_built: bool = False) -> dict[str, Any]:
        import inspect

        job = ctx.job
        fn = find("p2p_geometry", "build_product")
        if fn is None:
            reason = "p2p_geometry.build_product is not available yet"
        else:
            try:
                from p2p_geometry import AddonColorInputs
                from p2p_geometry.meshio import load_meshy_glb

                mesh = load_meshy_glb(glb, face_colors=True)
                vertices, faces = mesh.vertices, mesh.faces
                library = self._library(ctx, spec, known)
                addons = self._addons(job, spec, relief_built)
                addons, loop = resolve_loop(addons, read)
                if loop is not None:
                    ctx.decide("loop.area", loop["area"], by="code",
                               reason=f"{loop['source']}: position {loop['position']}"
                                      + (f"; {loop['conflict']}" if loop["conflict"] else ""))
                targets, sources = self._targets(spec, read, known)
                kw: dict[str, Any] = {}
                params = inspect.signature(fn).parameters
                if "form" in params:
                    kw["form"] = spec.form
                if "colors" in params:
                    kw["colors"] = AddonColorInputs(face_rgb=mesh.face_colors, targets=targets,
                                                    target_sources=sources,
                                                    source=spec.palette_source)
                result = fn(vertices, faces, job.product_type, addons, job.target_size_mm,
                            library, job.settings, ctx.dir, **kw)
                result = self._job_relative(ctx, GeometryResult.model_validate(
                    result.model_dump(mode="json") if hasattr(result, "model_dump") else result))
                ctx.decide("geometry.build_product", "ok", by="code")
                report = read_json(ctx.path("geometry_report.json")) or {}
                metrics = report.get("metrics") or {}
                warns = list(result.warnings)
                if loop is not None and loop["conflict"]:
                    warns.append(loop["conflict"])
                return {"source": "p2p_geometry", "result": result.model_dump(mode="json"),
                        "warnings": warns, "form": metrics.get("form"),
                        "addon_colors": metrics.get("addon_colors"),
                        "hang_tilt_deg": metrics.get("hang_tilt_deg"),
                        "hang": metrics.get("hang"), "loop_area": loop}
            except Exception as e:  # noqa: BLE001 keep the job moving; Jackson sees why
                reason = f"build_product failed: {type(e).__name__}: {e}"
        ctx.decide("geometry.build_product", "skipped", by="code", reason=reason)
        return {"source": "passthrough", "result": None,
                "warnings": [f"Add-ons not built: {reason}"] if job.addons else [reason]}

    @staticmethod
    def _library(ctx: StageContext, spec, known: dict):
        """filaments/library.json, plus the auto-pick catalog colors (owned false) when the
        palette source is any."""
        lib = load_library(ctx.settings.repo_root)
        if lib is None or spec.palette_source != "any":
            return lib
        have = {f.id for f in lib.filaments}
        extra = [f.model_copy(update={"owned": False, "ams_slot": None})
                 for f in pr.auto_pick(list(known.values()), have)]
        return lib.model_copy(update={"filaments": list(lib.filaments) + extra})

    @staticmethod
    def _addons(job, spec, relief_built: bool = False) -> list:
        """The job's add-ons with the plate shape the customer picked. A Creative Lab build is
        already a relief with its own base, lying face up: geometry keeps it as built (no plinth
        cut, no relief cut, no depth squeeze, not turned from a standing pose)."""
        out = []
        for a in job.addons:
            if a.kind.value == "nameplate" and spec.plate_shape and "shape" not in a.params:
                a = a.model_copy(update={"params": {**a.params, "shape": spec.plate_shape}})
            out.append(a)
        if relief_built and out:
            out[0] = out[0].model_copy(update={"params": {
                "remove_plinth": "keep", "relief_cut": "off", "source_pose": "face_up",
                "relief_scale": 1.0, **out[0].params}})
        return out

    @staticmethod
    def _targets(spec, read: dict | None, known: dict) -> tuple[dict, dict]:
        """Role -> hex: the job's picks first (a filament id becomes its hex), then what the
        approved concept shows."""
        from ..concept_read import ConceptRead

        targets: dict[str, str] = {}
        sources: dict[str, str] = {}
        for role, v in spec.colors.items():
            hx = known[v].hex if v in known else v
            targets[role], sources[role] = hx.upper(), "job"
        if read:
            for role, hx in ConceptRead.model_validate(read).addon_targets().items():
                if role not in targets:
                    targets[role], sources[role] = hx, "concept"
        return targets, sources
