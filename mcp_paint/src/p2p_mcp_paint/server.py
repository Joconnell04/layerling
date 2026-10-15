"""The paint MCP server (stdio), name "paint".

Every tool in p2p_contracts.mcp_tools.TOOLS is registered with exactly its contract input
fields (the tool signature is generated from the input model) and returns its contract output
model as JSON text, plus MCP ImageContent for renders. mcp 2.x renamed FastMCP to MCPServer;
this is that server.

    uv run python -m p2p_mcp_paint --job <job_id> [--attempt N] [--jobs-dir DIR]
    uv run python -m p2p_mcp_paint --job <job_id> --base current     # studio paint edits
"""

from __future__ import annotations

import argparse
import inspect
import os
import sys
import traceback
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image
from mcp_types import TextContent
from p2p_contracts.mcp_tools import TOOLS, OpenJobIn
from pydantic import Field

from .session import PaintSession, ToolError, ToolResult

DESCRIPTIONS = {
    "open_job": "Open a job (new s09 attempt unless attempt is given) and build its region map.",
    "get_state": "Current palette, region count, checkpoint and paint area per filament.",
    "list_regions": "Regions with id, label, part, filament, area and thinnest width.",
    "label_regions": "Rerun SAM 3 part labels. replace=false only adds missing face parts.",
    "render_views": "Render kinds (flat, shaded, overlay, crops, sim, texture) at the fixed "
                    "cameras. Returns images; overlay shows region ids.",
    "paint_region": "Assign a filament to regions by id.",
    "merge_regions": "Merge regions into the first id.",
    "split_region": "Split a region: connected_components, plane, geodesic_seed or prompt.",
    "grow": "Grow a region by mm on the surface.",
    "shrink": "Shrink a region by mm; the neighbors take the faces.",
    "smooth_boundary": "Close notches and remove spurs narrower than 2 x mm.",
    "snap_to_creases": "Move boundaries onto nearby geometric creases.",
    "despeckle": "Merge pieces under min_area_mm2 into the neighbor they share most edge with.",
    "select_by_pixel": "Last resort: faces under a pixel of a 1024 px view (sheet cell x 2).",
    "suggest_palette": "Best k filaments by CIEDE2000 with the gray guard; spool swaps when a "
                       "required color has no loaded match within dE00 10. apply=true "
                       "repaints every region to its nearest palette filament.",
    "simulate_print": "Apply slicer limits and render the result. Judge this, not the ideal "
                      "paint.",
    "run_checklist": "The PLAN checklist items code can decide, with evidence.",
    "checkpoint": "Save the current paint as a checkpoint (one per iteration).",
    "undo": "Restore a checkpoint (default: the last one, or the one before if clean).",
    "write_3mf": "Write the Bambu X2D project 3MF.",
    "slice_check": "Slice the 3MF with Bambu Studio CLI: the gate.",
    "apply_paint_ops": "The studio editor's edits, in order: fill_region (region ids), "
                       "brush_faces (figure face ranges, e.g. 0-9,15), replace_slot (every face "
                       "of one slot). Filament by filament_id or palette slot.",
    "preview_print": "Simulated print, final.3mf, p2p_slicer time, grams, purge and per-layer "
                     "toolpaths, 8 proof views. before = the previous preview (run once before "
                     "editing). Returns the print sheet image.",
}


def _signature(model) -> inspect.Signature:
    params = []
    for name, f in model.model_fields.items():
        default = inspect.Parameter.empty if f.is_required() else f.get_default(
            call_default_factory=True)
        meta = [*f.metadata, Field(description=f.description)] if f.description else [
            *f.metadata]
        ann = Annotated[(f.annotation, *meta)] if meta else f.annotation
        params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default,
                                        annotation=ann))
    return inspect.Signature(params, return_annotation=Any)


def _content(res: ToolResult) -> list:
    out: list = [TextContent(type="text", text=res.out.model_dump_json(indent=1,
                                                                        exclude_none=False))]
    for label, png in res.images:
        out.append(TextContent(type="text", text=f"image: {label}"))
        fmt = "jpeg" if png[:2] == b"\xff\xd8" else "png"
        out.append(Image(data=png, format=fmt).to_image_content())
    return out


def open_session(req: OpenJobIn, root: Path | None, sam3_backend: str | None) -> PaintSession:
    """open_job: a new or resumed attempt with SAM 3 labels, or (base given, no attempt) a new
    attempt seeded from a finished paint for studio edits (edits.open_edit_session)."""
    if req.base and req.attempt is None:
        from .edits import open_edit_session

        return open_edit_session(req.job_id, base=req.base, root=root, sam3_backend=sam3_backend)
    return PaintSession.open(req, root, sam3_backend)


class Holder:
    def __init__(self, root: Path | None, sam3_backend: str | None):
        self.root = root
        self.sam3_backend = sam3_backend
        self.session: PaintSession | None = None

    def need(self) -> PaintSession:
        if self.session is None:
            raise ToolError("no job open; call open_job first")
        return self.session


def build_server(holder: Holder) -> MCPServer:
    server = MCPServer("paint", instructions=(
        "Paint a multicolor 3D print by region id. Edit by region id, judge simulate_print "
        "renders, checkpoint once per iteration."))

    def make(name: str, in_model):
        def fn(**kwargs):
            try:
                if name == "open_job":
                    req = OpenJobIn(**kwargs)
                    holder.session = open_session(req, holder.root, holder.sam3_backend)
                    res = holder.session.get_state()
                    return _content(res)
                s = holder.need()
                method = getattr(s, name)
                res = method(in_model(**kwargs)) if in_model is not None else method()
                return _content(res)
            except ToolError as e:
                return [TextContent(type="text", text=f"refused: {e}")]
            except Exception as e:  # noqa: BLE001 - the agent should see the failure, not crash
                traceback.print_exc(file=sys.stderr)
                return [TextContent(type="text", text=f"error: {type(e).__name__}: {e}")]

        fn.__name__ = name
        fn.__doc__ = DESCRIPTIONS.get(name, name)
        fn.__signature__ = (_signature(in_model) if in_model is not None
                            else inspect.Signature([], return_annotation=Any))
        return fn

    for name, (in_model, _out) in TOOLS.items():
        server.add_tool(make(name, in_model), name=name, description=DESCRIPTIONS.get(name),
                        structured_output=False)
    return server


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="p2p_mcp_paint")
    ap.add_argument("--job", help="open this job at start")
    ap.add_argument("--attempt", type=int, default=None)
    ap.add_argument("--base", default=None,
                    help="start from a finished paint: current, or an attempt folder")
    ap.add_argument("--jobs-dir", default=os.environ.get("P2P_JOBS_DIR"))
    ap.add_argument("--sam3", default=None, help="SAM 3 backend: local, mock, runpod")
    a = ap.parse_args(argv)
    holder = Holder(Path(a.jobs_dir) if a.jobs_dir else None, a.sam3)
    if a.job:
        holder.session = open_session(OpenJobIn(job_id=a.job, attempt=a.attempt, base=a.base),
                                      holder.root, a.sam3)
        print(f"paint: opened {a.job} a{holder.session.attempt}", file=sys.stderr, flush=True)
    build_server(holder).run("stdio")


if __name__ == "__main__":
    main()
