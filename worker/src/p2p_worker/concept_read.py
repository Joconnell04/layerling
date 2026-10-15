"""A vision read of the approved product concept: which add-ons it shows and their colors,
the product form it shows, and the figure's main colors. s06 uses it for add-on colors (the
customer approved this image); the final-vs-concept check reads the same file.

One Sonnet 5 call through the Claude Code login (CLAUDE.md: color work is Sonnet), cached by
the concept's sha256: an earlier s06 attempt's concept_read.json for the same image is reused,
so a rerun costs nothing. MockClaude answers deterministically (nothing shown).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from p2p_contracts import ModelCall, StageId
from pydantic import BaseModel, Field, model_validator

HEX = r"^#[0-9A-Fa-f]{6}$"
ROLES = ("plate", "text", "loop", "back", "base")

SYSTEM = """You read one product concept image for a small multicolor 3D print (a pet or
object figure, possibly with a keyring loop, a name plate with raised text, a flat back or a
base). Report only what is drawn in the image; never guess a part that is not visible.
- form: "figure" for a full 3D figure (a sculpted animal seen at an angle, with depth), "flat"
  for a flat charm or relief (a front view with a flat back), "unclear" if you cannot tell.
- figure_colors: the figure's main color areas, largest first, at most 8: area (coat, face
  mask, saddle, chest, ears, inner ears, eyes, nose, collar, ...), hex as it looks under even
  light, share (rough fraction of the figure's visible area, 0 to 1).
- For each add-on (plate, text, loop, back, base): shown true only if it is clearly drawn; hex
  its main color when shown, else null.
- loop_spot: where a keyring or hanging loop attaches, when one is drawn: "head" (on top of
  the head), "back" (on the back, behind the head), "side", or "other"; null when no loop.
- text_string: the exact letters of any name on the product, else null.
- plate_shape: rectangle, rounded (rounded corners), oval, none (no plate), or other.
Reply with one JSON object matching the schema only."""
TEXT = "Read this approved product concept."


class ColorArea(BaseModel):
    area: str
    hex: str = Field(pattern=HEX)
    share: float = Field(ge=0, le=1)


class AddonSeen(BaseModel):
    shown: bool
    hex: str | None = Field(None, pattern=HEX)


class ConceptRead(BaseModel):
    form: Literal["figure", "flat", "unclear"]
    figure_colors: list[ColorArea] = Field(min_length=1, max_length=8)
    plate: AddonSeen = AddonSeen(shown=False)
    text: AddonSeen = AddonSeen(shown=False)
    loop: AddonSeen = AddonSeen(shown=False)
    back: AddonSeen = AddonSeen(shown=False)
    base: AddonSeen = AddonSeen(shown=False)
    text_string: str | None = None
    plate_shape: Literal["rectangle", "rounded", "oval", "none", "other"] | None = None
    loop_spot: Literal["head", "back", "side", "other"] | None = None

    @model_validator(mode="before")
    @classmethod
    def _unwrap(cls, v):
        """claude -p with --json-schema sometimes wraps the answer as {"input": "<json>"}."""
        if isinstance(v, dict) and set(v) == {"input"} and isinstance(v["input"], str):
            try:
                return json.loads(v["input"])
            except ValueError:
                return v
        return v

    def addon_targets(self) -> dict[str, str]:
        """Role -> hex for every add-on the concept shows with a color."""
        out = {}
        for role in ROLES:
            a = getattr(self, role)
            if a.shown and a.hex:
                out[role] = a.hex.upper()
        return out


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cached(ctx, concept_sha: str) -> dict | None:
    """An earlier s06 attempt's read of the same concept image."""
    for a in range(ctx.attempt - 1, 0, -1):
        p = ctx.job_dir / StageId.S06_PRINTABILITY.value / f"a{a}" / "concept_read.json"
        if p.is_file():
            try:
                d = json.loads(p.read_text())
            except ValueError:
                continue
            if d.get("concept_sha256") == concept_sha and (d.get("read") or {}).get(
                    "figure_colors"):
                return d
    return None


def mock_read() -> ConceptRead:
    return ConceptRead(form="unclear", figure_colors=[ColorArea(area="unknown", hex="#808080",
                                                                share=1.0)])


async def read_concept(client, image: Path) -> tuple[ConceptRead, ModelCall | None]:
    """(read, model call). Clients without a structured vision call return an empty read."""
    if getattr(client, "mock", False):
        return mock_read(), None
    ask = getattr(client, "ask", None)
    if ask is None:
        return mock_read(), None
    from .clients.claude import SONNET

    read, call = await ask(purpose="concept_read", system=SYSTEM, text=TEXT, images=[image],
                           output=ConceptRead, model=SONNET, max_tokens=900)
    return read, call  # type: ignore[return-value]


def normalize_hex(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if re.fullmatch(r"#?[0-9A-Fa-f]{6}", v):
        return ("#" + v.lstrip("#")).upper()
    return None
