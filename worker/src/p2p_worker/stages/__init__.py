"""Stage runners, s01 to s10. s09 is a switch: PAINT_AGENT=stub keeps s08's first pass,
PAINT_AGENT=agent runs the paint workstream's stages/s09_paint.py."""

from p2p_contracts import StageId

from .base import Stage, StageContext
from .s01_intake import IntakeStage
from .s02_color_spec import ColorSpecStage
from .s03_concept import ConceptStage
from .s04_multiview import MultiViewStage
from .s05_mesh import MeshStage
from .s06_printability import GeometryStage
from .s07_baseline import BaselineStage
from .s08_palette import PaletteStage
from .s09_stub import PaintSwitch
from .s10_write import WriteStage

RUNNERS: dict[StageId, Stage] = {
    s.id: s for s in (IntakeStage(), ColorSpecStage(), ConceptStage(), MultiViewStage(),
                      MeshStage(), GeometryStage(), BaselineStage(), PaletteStage(),
                      PaintSwitch(), WriteStage())
}

__all__ = ["RUNNERS", "Stage", "StageContext"]
