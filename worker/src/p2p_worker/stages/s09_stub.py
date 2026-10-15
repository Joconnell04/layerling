"""Stage 9 switch: the paint agent, or a stub that keeps s08's first pass.

PAINT_AGENT=stub (default): paint_state.json is s08's PaintState, iteration 0, and a decision
records that no agent ran. PAINT_AGENT=agent: the paint workstream's stages/s09_paint.py runs
(its Stage subclass with id s09_paint). s10 reads role paint_state either way: a PaintState whose
region_map points at the region_map.json (and face_regions.npy) its assignment indexes.
"""

from __future__ import annotations

import importlib
import inspect

from p2p_contracts import PaintState, StageId

from ..errors import VendorError
from ..util import read_json
from .base import Stage, StageContext


def agent_stage() -> Stage | None:
    """The paint workstream's s09 Stage, if stages/s09_paint.py ships one."""
    try:
        mod = importlib.import_module(f"{__package__}.s09_paint")
    except ImportError:
        return None
    for obj in vars(mod).values():
        if isinstance(obj, Stage) and getattr(obj, "id", None) == StageId.S09_PAINT:
            return obj
        if (inspect.isclass(obj) and issubclass(obj, Stage) and obj is not Stage
                and obj.__module__ == mod.__name__
                and getattr(obj, "id", None) == StageId.S09_PAINT):
            return obj()
    return None


class PaintStub(Stage):
    id = StageId.S09_PAINT

    async def run(self, ctx: StageContext) -> None:
        path, _ = ctx.use_output(StageId.S08_PALETTE, "paint_state")
        state = PaintState.model_validate(read_json(path))
        ctx.add_input(state.region_map.path, from_stage=StageId.S08_PALETTE,
                      from_attempt=ctx.job.attempts.get(StageId.S08_PALETTE), role="region_map")
        ctx.write_json_output("paint_state.json", state.model_copy(update={"iteration": 0}),
                              role="paint_state")
        ctx.decide("paint.agent", "stub", by="code",
                   reason="PAINT_AGENT=stub: s08's first pass is the paint")
        ctx.m.data["agent"] = "stub"


class PaintSwitch(Stage):
    id = StageId.S09_PAINT

    @staticmethod
    def replay_safe(m, settings) -> bool:
        """An interrupted paint agent run starts over in a fresh attempt: its conversation
        died with the worker and its session folder holds half-applied edits, so a replay in
        place would paint on top of them (lifecycle.replay_safe). The stub, a palette edit's
        stub and a paint edit replay from their cached steps."""
        if m.data.get("paint_edit") or m.data.get("palette_edit_message"):
            return True
        if "paint" in (m.data.get("steps") or {}):  # the paint run finished and is cached
            return True
        return getattr(settings, "paint_agent", "stub") != "agent"

    async def run(self, ctx: StageContext) -> None:
        if ctx.m.data.get("paint_edit"):  # a paint edit attempt resumed after a crash
            from .s09_paint_edit import PaintEditStage

            await PaintEditStage().run(ctx)
            return
        if ctx.m.data.get("palette_edit_message"):  # a palette edit's s09, resumed
            await PaintStub().run(ctx)
            return
        if ctx.settings.paint_agent != "agent":
            await PaintStub().run(ctx)
            return
        stage = agent_stage()
        if stage is None:
            raise VendorError("PAINT_AGENT=agent but stages/s09_paint.py has no s09 stage",
                              code="internal", retryable=False)
        await stage.run(ctx)
