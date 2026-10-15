"""Stage 8, palette and first pass: region map from Meshy's colors, filament subset, spool
suggestions.

Inputs: s07's Meshy Multi-Color 3MF (primary style), s05's approved GLB, s06's GeometryResult
(when built), s02's ColorSpec (else s01's color regions), and the filament library (the job
owner's Supabase `filaments` rows, else filaments/library.json; snapshot in filaments.json).

1. Meshy's paint moves onto the printed figure (p2p_worker.printing.transfer_paint), then
   despeckles at the Meshy-color level, so no region is narrower than 2 x nozzle.
2. Regions: connected patches of one Meshy color (source "meshy_color", color_lab = that
   color). The largest is `<subject>.body`, the rest `<subject>.marking_<color>_<n>`.
   face_regions.npy (int32 per figure face) and region_map.json (RegionMap) index the figure
   mesh the X2D project prints: s06's figure.ply, or Meshy's cleaned mesh as figure.ply here.
3. Each region's color goes to a loaded filament by CIEDE2000 with the achromatic guard. The
   filament subset is the filaments the regions use. PaintState: palette = every loaded spool in
   AMS order (project slot N = AMS slot N), assignment region -> filament, default_filament =
   the body's, part_filaments from GeometryResult.
4. Spool suggestions: a Meshy color covering at least 0.5% of the model, or a required ColorSpec
   part, with no loaded filament within dE00 10 gets "add a <color> PLA" (plus the closest
   catalog spool) in palette.json and manifest data, and one `spool_suggestion` message per
   spool (payloads.SpoolSuggestion: "gray PLA", target color, dE00 now and with the catalog
   spool, affected region labels, share of the model).
5. first_pass.png: the first pass rendered in filament colors (figure only).

Source any (the palette_edit's, else the job's `palette_source`): candidates are the owned
spools plus the catalog's auto-pick colors at dE00 + printing.UNOWNED_PENALTY_DE, and the
palette is the filaments used, at most ams_slots or max_colors (printing.reduce_palette).
palette_edit (manifest data `palette_edit`, set by the orchestrator): recolor swaps the
filaments of the paint state that shipped slot for slot; refit re-matches to the given slots
or auto-picks. See worker/README.md, "Palette edit".

Outputs: region_map.json, face_regions.npy, figure.ply (Meshy figure only), paint_state.json,
palette.json, filaments.json, first_pass.png. No Meshy or Claude spend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from p2p_contracts import (
    Filament,
    FilamentLibrary,
    FileRef,
    MeshRef,
    PaintState,
    Region,
    RegionMap,
    StageId,
)
from p2p_contracts.geometry import GeometryResult

from .. import printing as pr
from ..errors import VendorError
from ..offload import run_heavy
from ..util import read_json, sha256_file
from .adapters import library_for
from .base import Stage, StageContext
from .common import geometry_of, jsonable, meshy_3mf_of, optional, post_spool_suggestions


def spec_targets(ctx: StageContext) -> tuple[list[dict[str, Any]], str]:
    """What the customer's pet looks like: s02 ColorSpec parts, else s01 color regions."""
    m2 = optional(ctx, StageId.S02_COLOR_SPEC)
    if m2 is not None and any(o.role == "color_spec" for o in m2.outputs):
        path, _ = ctx.use_output(StageId.S02_COLOR_SPEC, "color_spec")
        spec = read_json(path)
        return [{"what": f"{s['subject_id']} {p['label'].replace('_', ' ')}", "hex": p["hex"],
                 "label": f"{s['subject_id']}.{p['label']}",
                 "required": p.get("required", True)}
                for s in spec.get("subjects", []) for p in s.get("parts", [])], "s02"
    m1 = optional(ctx, StageId.S01_INTAKE)
    if m1 is not None and any(o.role == "color_regions" for o in m1.outputs):
        path, _ = ctx.use_output(StageId.S01_INTAKE, "color_regions")
        return [{"what": f"{r['subject_id']} {r['region']}", "hex": r["hex"], "required": True}
                for r in read_json(path, []) if r.get("hex")], "s01"
    return [], "none"


def subject_id(ctx: StageContext) -> str | None:
    m1 = optional(ctx, StageId.S01_INTAKE)
    if m1 is None:
        return None
    p = next((o.path for o in m1.outputs if o.role == "intake_result"), None)
    subjects = (read_json(ctx.abs(p), {}) or {}).get("subjects", []) if p else []
    return subjects[0]["id"] if len(subjects) == 1 else None


def build_regions(vertices: np.ndarray, faces: np.ndarray, labels: np.ndarray,
                  colours: list[str], subject: str | None
                  ) -> tuple[np.ndarray, list[Region], dict[int, int]]:
    """Connected same-color patches -> (face_regions int32, regions, region -> color slot)."""
    from p2p_sim import Mesh, region_geometry
    from p2p_sim.geodesic import components

    mesh = Mesh(vertices, faces)
    n, comp = components(mesh, np.ones(mesh.n_faces, bool), labels)
    area = np.bincount(comp, weights=mesh.face_areas, minlength=n)
    order = np.argsort(-area, kind="stable")  # region 1 = the largest patch
    rid = np.empty(n, dtype=np.int32)
    rid[order] = np.arange(1, n + 1, dtype=np.int32)
    face_regions = rid[comp].astype(np.int32)
    geo = region_geometry(mesh, face_regions, with_min_width=False)
    first_face = np.full(n + 1, -1, dtype=np.int64)
    first_face[face_regions[::-1]] = np.arange(len(face_regions))[::-1]
    prefix = f"{subject}." if subject else ""
    counts: dict[str, int] = {}
    regions = []
    slots: dict[int, int] = {}
    for r in range(1, n + 1):
        g = geo[r]
        slot = slots[r] = int(labels[first_face[r]])
        hx = colours[slot - 1]
        lab = pr.hex_to_lab(hx)
        if r == 1:
            part, label = "body", f"{prefix}body"
        else:
            name = pr.color_name(lab).replace(" ", "_")
            counts[name] = counts.get(name, 0) + 1
            part, label = "marking", f"{prefix}marking_{name}_{counts[name]}"
        regions.append(Region(
            id=r, label=label, subject_id=subject, part=part, source="meshy_color",
            n_faces=g.n_faces, area_mm2=round(g.area_mm2, 3),
            centroid_mm=tuple(round(x, 3) for x in g.centroid_mm),
            bbox_min_mm=tuple(round(x, 3) for x in g.bbox_min_mm),
            bbox_max_mm=tuple(round(x, 3) for x in g.bbox_max_mm), color_lab=lab))
    return face_regions, regions, slots


@dataclass
class Choice:
    """Which filaments s08 may use. The default is today's first pass: the loaded AMS spools,
    palette = all of them in AMS order."""

    pool: list[Filament]  # owned candidates, in slot order
    extra: list[Filament] = field(default_factory=list)  # unowned catalog colors (source any)
    fixed: list[str] | None = None  # refit with slots: the palette is exactly these, in order
    cap: int | None = None  # at most this many filaments (AMS slots, max_colors)
    used_only: bool = False  # palette = the filaments the model uses, not every loaded spool
    known: dict[str, Filament] = field(default_factory=dict)  # id -> filament, for lookups


def palette_source(ctx: StageContext, edit: dict[str, Any] | None) -> str:
    """owned (default) or any: the palette_edit's source, else the job's. The job-level switch
    has no contract field yet (see worker/README.md, contract change requests): the worker reads
    `palette_source` from input/request.json, then from the jobs row."""
    if edit is not None:
        return "any" if edit.get("source") == "any" else "owned"
    src = (read_json(ctx.abs("input/request.json"), {}) or {}).get("palette_source")
    if src is None:
        try:
            src = (ctx.store.job_row(ctx.job.id) or {}).get("palette_source")
        except Exception:  # noqa: BLE001 no row: owned
            src = None
    return "any" if src == "any" else "owned"


def make_choice(ctx: StageContext, library: FilamentLibrary, catalog: list[Filament],
                edit: dict[str, Any] | None, source: str) -> Choice:
    known = {f.id: f for f in catalog}
    known.update({f.id: f for f in library.filaments})
    pool = pr.candidates(library)
    ams = ctx.job.settings.ams_slots
    if edit is None and source == "owned":
        return Choice(pool=pool, known=known)
    cap = min(edit.get("max_colors") or ams, ams) if edit else ams
    if edit is not None and edit.get("mode") == "refit" and edit.get("slots"):
        fixed = list(edit["slots"])
        missing = [i for i in fixed if i not in known]
        if missing:
            raise VendorError(f"palette_edit names unknown filaments: {missing}",
                              code="validation", retryable=False)
        return Choice(pool=[known[i] for i in fixed], fixed=fixed, cap=min(cap, len(fixed)),
                      used_only=True, known=known)
    extra: list[Filament] = []
    if source == "any":  # owned spools not in the AMS count as owned; then the catalog
        pool = pool + [f for f in library.filaments if f.owned and f not in pool]
        extra = pr.auto_pick(catalog, {f.id for f in pool})
    return Choice(pool=pool, extra=extra, cap=cap, used_only=True, known=known)


class PaletteStage(Stage):
    id = StageId.S08_PALETTE

    def _build(self, ctx: StageContext, meshy_3mf: Path, glb: Path,
               geometry: GeometryResult | None, choice: Choice, catalog: list[Filament],
               targets: list[dict[str, Any]], subject: str | None) -> dict[str, Any]:
        settings = ctx.job.settings
        pool = choice.pool
        if not pool:
            raise VendorError("the filament library has no loaded or owned spools",
                              code="validation", retryable=False)
        extra = choice.extra or None
        painted = pr.transfer_paint(meshy_3mf, glb, geometry, ctx.job_dir)
        v, f = painted.vertices, painted.faces
        if geometry is not None:
            mesh_rel = geometry.figure.mesh.path
        else:  # Meshy's own figure: keep the exact faces the ids index into
            from p2p_threemf.ply import write_ply

            write_ply(ctx.path("figure.ply"), v, f)
            mesh_rel = ctx.rel("figure.ply")
        share = pr.area_share(v, f, painted.src_slot)
        clean, changed = pr.despeckle(v, f, painted.src_slot, settings)
        face_regions, regions, slot_of_region = build_regions(v, f, clean, painted.src_colours,
                                                              subject)
        np.save(ctx.path("face_regions.npy"), face_regions)

        used = sorted({int(s) for s in np.unique(clean)})
        matches = {s: pr.match_colour(painted.src_colours[s - 1], pool, catalog, extra)
                   for s in used}
        part_filaments = {p.name: p.filament_id for p in (geometry.parts if geometry else [])
                          if p.role != "cutter" and p.filament_id}
        if choice.fixed is not None:  # refit to the customer's slots: parts move too
            for name, fid in part_filaments.items():
                if fid not in choice.fixed:
                    part_filaments[name] = pr.match_colour(
                        choice.known[fid].hex if fid in choice.known else "#808080",
                        pool).filament_id
        if choice.cap:
            matches = pr.reduce_palette(matches, share, pool, choice.extra, choice.cap,
                                        keep=set(part_filaments.values()))
        assignment = {str(r.id): matches[slot_of_region[r.id]].filament_id for r in regions}
        if choice.fixed is not None:
            palette = list(choice.fixed)
        elif choice.used_only:
            order = [x.id for x in pool] + [x.id for x in choice.extra]
            ids = set(assignment.values()) | set(part_filaments.values())
            palette = sorted(ids, key=lambda i: (order.index(i) if i in order else len(order), i))
        else:
            palette = [x.id for x in pool]
            # add-on colors the AMS does not hold (geometry picked them from the job or the
            # concept) still need a slot
            palette += [i for i in dict.fromkeys(part_filaments.values()) if i not in palette]
        body = assignment["1"]
        used_ids = sorted(set(assignment.values()) | set(part_filaments.values()),
                          key=palette.index)

        suggestions: list[dict[str, Any]] = []
        for s in sorted(used, key=lambda s: -share.get(s, 0)):
            m = matches[s]
            if m.suggestion and share.get(s, 0) >= pr.MIN_SHARE:
                suggestions.append({"source": "meshy", "what": f"{share[s]:.0%} of the model",
                                    "share": share[s],
                                    "regions": [r.label for r in regions
                                                if slot_of_region[r.id] == s],
                                    **pr.asdict_matches([m])[0]})
        for t in targets:
            m = pr.match_colour(t["hex"], pool, catalog, extra)
            if m.suggestion and t.get("required", True):
                suggestions.append({"source": "spec", "what": t["what"],
                                    "regions": [t.get("label") or t["what"]],
                                    **pr.asdict_matches([m])[0]})

        fr_ref = FileRef(path=ctx.rel("face_regions.npy"),
                         sha256=sha256_file(ctx.path("face_regions.npy")), kind="npy",
                         role="face_regions")
        rmap = RegionMap(mesh=MeshRef(path=mesh_rel, sha256=sha256_file(ctx.abs(mesh_rel)),
                                      n_faces=len(f), n_vertices=len(v)),
                         face_regions=fr_ref, regions=regions, next_region_id=len(regions) + 1)
        ctx.path("region_map.json").write_text(rmap.model_dump_json(indent=1))

        # first pass in filament colors, figure only
        from p2p_sim import Mesh, render_sheet

        fil = {**choice.known, **{x.id: x for x in pool + choice.extra}}
        rgb = np.array([[int(fil[x].hex[i:i + 2], 16) if x in fil else 128 for i in (1, 3, 5)]
                        for x in palette], np.uint8)
        lut = np.zeros(len(regions) + 1, dtype=np.int64)
        for r in regions:
            lut[r.id] = palette.index(assignment[str(r.id)])
        render_sheet(Mesh(v, f), rgb[lut[face_regions]], ctx.path("first_pass.png"),
                     kind="shaded", cell_px=320, title=f"{str(ctx.job.product_type).capitalize()} first pass")
        return jsonable({
            "palette": palette, "used": used_ids, "assignment": assignment,
            "default_filament": body, "part_filaments": part_filaments,
            "mesh": mesh_rel, "figure_source": painted.figure_source,
            "regions": len(regions), "despeckle_faces": changed,
            "mapping": [{"src_slot": s, "share": round(share.get(s, 0), 4),
                         **pr.asdict_matches([matches[s]])[0]} for s in used],
            "suggestions": suggestions, "fit": painted.fit, "transfer": painted.transfer,
            "warnings": painted.warnings,
            "unowned": [i for i in palette if i in {x.id for x in choice.extra}],
        })

    def _snapshot(self, ctx: StageContext, library: FilamentLibrary, palette: list[str],
                  known: dict[str, Filament]) -> None:
        """filaments.json: the library, plus the catalog colors the palette uses (s10 reads the
        palette's filaments from here)."""
        have = {f.id for f in library.filaments}
        add = [known[i] for i in palette if i not in have and i in known]
        ctx.write_json_output("filaments.json",
                              library.model_copy(update={"filaments": library.filaments + add}),
                              role="filament_library")

    async def _recolor(self, ctx: StageContext, edit: dict[str, Any], library: FilamentLibrary,
                       lib_source: str, known: dict[str, Filament],
                       base: PaintState | None) -> None:
        """palette_edit mode recolor: the region -> slot assignment stays, slot i takes
        slots[i]. base: the paint state that shipped (edit.previous_paint_state), else this
        attempt's own first pass (a thin-slice job has no earlier paint state)."""
        prev = edit.get("previous_paint_state")
        if base is None:
            ctx.add_input(prev, role="previous_paint_state")  # an old attempt: never changes
            base = PaintState.model_validate(read_json(ctx.abs(prev)))
        slots = list(edit.get("slots") or [])
        if len(slots) != len(base.palette):
            raise VendorError(f"recolor needs {len(base.palette)} slots, got {len(slots)}",
                              code="validation", retryable=False)
        swap = dict(zip(base.palette, slots, strict=True))
        state = PaintState(region_map=base.region_map, palette=slots,
                           assignment={r: swap[f] for r, f in base.assignment.items()},
                           default_filament=swap[base.default_filament],
                           part_filaments={k: swap.get(v, v)
                                           for k, v in base.part_filaments.items()})
        used = sorted(set(state.assignment.values()) | set(state.part_filaments.values())
                      | {state.default_filament}, key=slots.index)
        self._snapshot(ctx, library, slots, known)
        ctx.write_json_output("paint_state.json", state, role="paint_state")
        ctx.write_json_output("palette.json", {
            "library": lib_source, "mode": "recolor", "from": prev, "palette": slots,
            "used": used, "swap": [{"slot": i + 1, "from": a, "to": b}
                                   for i, (a, b) in enumerate(zip(base.palette, slots))],
        }, role="palette")
        ctx.decide("palette_edit.recolor", slots, by="user",
                   reason=f"slot i takes slots[i]; regions keep their slot (from {prev or 'first pass'})")
        ctx.m.data.update({"palette": slots, "used": used, "library": lib_source,
                           "palette_edit_mode": "recolor"})
        ctx.m.confidence = 1.0

    async def run(self, ctx: StageContext) -> None:
        # Set by Orchestrator.apply_palette_edit on a finished job's new s08 attempt
        # (payloads.PaletteEdit plus message_id and previous_paint_state); None otherwise.
        edit: dict[str, Any] | None = ctx.m.data.get("palette_edit")
        library, lib_source = library_for(ctx.store, ctx.job.id, ctx.settings.repo_root)
        if library is None:
            raise VendorError("no filament library (Supabase filaments or "
                              "filaments/library.json)", code="validation", retryable=False)
        catalog_all = pr.load_catalog(ctx.settings.repo_root)
        source = palette_source(ctx, edit)
        recolor = edit is not None and edit.get("mode") == "recolor"
        choice = (make_choice(ctx, library, catalog_all, None, "owned") if recolor
                  else make_choice(ctx, library, catalog_all, edit, source))
        if recolor and edit.get("previous_paint_state"):
            await self._recolor(ctx, edit, library, lib_source, choice.known, None)
            return
        glb, _ = ctx.use_output(StageId.S05_MESH, "approved_mesh")
        meshy_3mf = meshy_3mf_of(ctx)
        geometry = geometry_of(ctx)
        targets, spec_source = spec_targets(ctx)
        subject = subject_id(ctx)
        suggest = pr.auto_pick(catalog_all)  # suggestions name PLA Basic or Matte colors

        async def build() -> dict[str, Any]:  # a child process: the loop keeps running
            return await run_heavy(ctx, self._build, meshy_3mf, glb, geometry, choice, suggest,
                                   targets, subject)

        out = await ctx.step("first_pass", build)
        self._snapshot(ctx, library, out["palette"], choice.known)
        ctx.m.data.update({"source": source, "unowned": out.get("unowned", [])})
        ctx.add_output("face_regions.npy", role="face_regions", kind="npy")
        if out["figure_source"] == "meshy":
            ctx.add_output("figure.ply", role="figure_mesh", kind="ply")
        rm_ref = ctx.add_output("region_map.json", role="region_map", kind="json")
        state = PaintState(region_map=FileRef(path=rm_ref.path, sha256=rm_ref.sha256,
                                              kind="json", role="region_map"),
                           palette=out["palette"], assignment=out["assignment"],
                           default_filament=out["default_filament"],
                           part_filaments=out["part_filaments"])
        ctx.write_json_output("paint_state.json", state, role="paint_state")
        sugg = out["suggestions"]
        ctx.write_json_output("palette.json", {
            "library": lib_source, "palette": out["palette"], "used": out["used"],
            "mapping": out["mapping"], "suggestions": sugg, "spec_source": spec_source,
            "source": source, "unowned": out.get("unowned", []),
            "mode": edit.get("mode") if edit else "first_pass",
        }, role="palette")
        if ctx.path("first_pass.png").is_file():
            ctx.add_output("first_pass.png", role="first_pass_render", kind="png")
        for key in ("used", "regions", "despeckle_faces", "figure_source", "fit", "transfer",
                    "warnings"):
            ctx.m.data[key] = out[key]
        ctx.m.data.update({"palette": out["palette"], "library": lib_source,
                           "spec_source": spec_source,
                           "spool_suggestions": sorted({s["suggestion"] for s in sugg})})
        for m in out["mapping"]:
            ctx.decide(f"palette.src{m['src_slot']}", m["filament_id"], by="code",
                       reason=f"{m['src_hex']} dE00 {m['delta_e']} "
                              f"(plain nearest {m['unguarded_id']} {m['unguarded_de']})")
        if sugg:
            await post_spool_suggestions(ctx, sugg)
        ctx.m.confidence = 1.0
        if recolor:  # no earlier paint state (thin slice): recolor this first pass
            await self._recolor(ctx, edit, library, lib_source, choice.known, state)

