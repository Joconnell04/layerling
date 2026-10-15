"""Text as exact geometry: glyph outlines from bundled OFL fonts, kerned by HarfBuzz from the
font's own GPOS/kern tables, extruded with manifold3d.

Fonts (SIL OFL 1.1, licenses in fonts/):
- Nunito, a rounded sans (keys rounded, rounded-bold, rounded-black).
- Fredoka, a chunkier, friendlier rounded sans (keys friendly, friendly-bold).
Both are variable fonts; each key pins the axes. Round terminals print cleanly at 0.4 mm.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise
from pathlib import Path

import numpy as np
from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont
from manifold3d import CrossSection, FillRule, JoinType, Manifold

FONT_DIR = Path(__file__).parent / "fonts"
NUNITO = "Nunito-wght.ttf"
FREDOKA = "Fredoka-wdth-wght.ttf"
FONTS: dict[str, tuple[str, dict[str, float]]] = {
    "rounded-bold": (NUNITO, {"wght": 800}),
    "rounded-black": (NUNITO, {"wght": 1000}),
    "rounded": (NUNITO, {"wght": 700}),
    "friendly": (FREDOKA, {"wght": 600, "wdth": 100}),
    "friendly-bold": (FREDOKA, {"wght": 700, "wdth": 100}),
}
DEFAULT_FONT = "rounded-bold"
# The heavier key of the same family, used when strokes would print too thin.
HEAVIER = {"rounded": "rounded-bold", "rounded-bold": "rounded-black",
           "friendly": "friendly-bold"}
LEADING = 1.35  # baseline to baseline, in cap heights


class _FlattenPen(BasePen):
    def __init__(self, glyph_set, step_units: float):
        super().__init__(glyph_set)
        self.step = step_units
        self.contours: list[list[tuple[float, float]]] = []
        self._cur: list[tuple[float, float]] | None = None

    def _moveTo(self, p):
        self._cur = [p]

    def _lineTo(self, p):
        self._cur.append(p)

    def _n(self, *pts) -> int:
        length = sum(math.dist(a, b) for a, b in pairwise(pts))
        return max(3, math.ceil(length / self.step))

    def _curveToOne(self, p1, p2, p3):
        p0 = self._getCurrentPoint()
        for t in np.linspace(0, 1, self._n(p0, p1, p2, p3) + 1)[1:]:
            mt = 1 - t
            x = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
            y = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
            self._cur.append((x, y))

    def _qCurveToOne(self, p1, p2):
        p0 = self._getCurrentPoint()
        for t in np.linspace(0, 1, self._n(p0, p1, p2) + 1)[1:]:
            mt = 1 - t
            x = mt**2 * p0[0] + 2 * mt * t * p1[0] + t**2 * p2[0]
            y = mt**2 * p0[1] + 2 * mt * t * p1[1] + t**2 * p2[1]
            self._cur.append((x, y))

    def _closePath(self):
        if self._cur and len(self._cur) >= 3:
            pts = self._cur
            if pts[0] == pts[-1]:
                pts = pts[:-1]
            self.contours.append(pts)
        self._cur = None

    _endPath = _closePath


@dataclass
class _Font:
    key: str
    font: TTFont
    glyphs: object
    order: list[str]
    upem: int
    cap_height: float
    hb_font: object


def font_key(key: str | None) -> str:
    return key if key in FONTS else DEFAULT_FONT


@lru_cache(maxsize=8)
def _load(key: str) -> _Font:
    import uharfbuzz as hb

    key = font_key(key)
    file, loc = FONTS[key]
    path = FONT_DIR / file
    f = TTFont(path)
    gs = f.getGlyphSet(location=loc) if "fvar" in f else f.getGlyphSet()
    cap = float(getattr(f["OS/2"], "sCapHeight", 0) or 0.7 * f["head"].unitsPerEm)
    face = hb.Face(hb.Blob.from_file_path(str(path)))
    hbf = hb.Font(face)
    upem = f["head"].unitsPerEm
    hbf.scale = (upem, upem)
    if loc:
        hbf.set_variations(loc)
    return _Font(key, f, gs, f.getGlyphOrder(), upem, cap, hbf)


@lru_cache(maxsize=1024)
def _glyph_contours(key: str, name: str) -> tuple[np.ndarray, ...]:
    fnt = _load(key)
    pen = _FlattenPen(fnt.glyphs, fnt.upem / 80)
    fnt.glyphs[name].draw(pen)
    return tuple(np.asarray(c, dtype=np.float64) for c in pen.contours)


@lru_cache(maxsize=512)
def shape_line(text: str, key: str) -> tuple[tuple[str, float, float], ...]:
    """HarfBuzz shaping with kerning on: ((glyph name, x, y) in font units, ...), last entry is
    ('', advance_end, 0)."""
    import uharfbuzz as hb

    fnt = _load(key)
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(fnt.hb_font, buf, {"kern": True, "liga": False})
    x = 0.0
    out = []
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions, strict=True):
        out.append((fnt.order[info.codepoint], x + pos.x_offset, float(pos.y_offset)))
        x += pos.x_advance
    out.append(("", x, 0.0))
    return tuple(out)


def _line_glyphs(text: str, key: str, cap_mm: float, tracking_mm: float
                 ) -> tuple[list[list[np.ndarray]], float]:
    """Per-glyph contour lists in mm (baseline y = 0, left at x = 0) and the line advance."""
    fnt = _load(key)
    k = cap_mm / fnt.cap_height
    shaped = shape_line(text, fnt.key)
    glyphs = []
    for n, (name, x, y) in enumerate(shaped[:-1]):
        off = np.array([x * k + n * tracking_mm, y * k])
        glyphs.append([c * k + off for c in _glyph_contours(fnt.key, name)])
    width = shaped[-1][1] * k + max(0, len(shaped) - 2) * tracking_mm
    return glyphs, width


@dataclass
class TextLayout:
    section: CrossSection  # mm; first baseline at y = 0, lines centered on x = width / 2
    cap_height_mm: float
    width_mm: float  # widest line's ink width
    bbox: tuple[float, float, float, float]  # xmin, ymin, xmax, ymax
    tracking_mm: float
    font: str
    lines: tuple[str, ...]


def _lines(text: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(text, str):
        return tuple(text.split("\n"))
    return tuple(text)


def layout_text(text: str | list[str] | tuple[str, ...], cap_height_mm: float,
                font: str = DEFAULT_FONT, tracking_mm: float = 0.0,
                leading: float = LEADING) -> TextLayout:
    """One or more centered lines (a list, or a string with newlines), kerned."""
    key = font_key(font)
    lines = _lines(text)
    per_line = [_line_glyphs(t, key, cap_height_mm, tracking_mm) for t in lines]
    widest = max((w for _, w in per_line), default=0.0)
    contours: list[np.ndarray] = []
    for i, (glyphs, w) in enumerate(per_line):
        dx, dy = (widest - w) / 2, -i * leading * cap_height_mm
        for g in glyphs:
            contours.extend(c + np.array([dx, dy]) for c in g)
    section = CrossSection(contours, FillRule.NonZero) if contours else CrossSection()
    if section.is_empty():
        return TextLayout(section, cap_height_mm, 0.0, (0, 0, 0, 0), tracking_mm, key, lines)
    b = section.bounds()
    return TextLayout(section, cap_height_mm, b[2] - b[0], tuple(b), tracking_mm, key, lines)


def min_stroke_mm(section: CrossSection, loss_tol: float = 0.01) -> float:
    """Estimate the thinnest stroke: widest opening radius that removes under loss_tol of area.

    Morphological opening (erode then dilate by r) deletes every feature narrower than 2 r.
    """
    area = section.area()
    if area <= 0:
        return 0.0

    def loss(r: float) -> float:
        opened = section.offset(-r, JoinType.Round, circular_segments=24).offset(
            r, JoinType.Round, circular_segments=24
        )
        return 1.0 - opened.area() / area

    lo, hi = 0.02, 3.0
    if loss(lo) > loss_tol:
        return 2 * lo
    for _ in range(12):
        mid = (lo + hi) / 2
        if loss(mid) > loss_tol:
            hi = mid
        else:
            lo = mid
    return 2 * lo


def thin_fraction(section: CrossSection, min_stroke: float) -> float:
    """Share of the ink in strokes narrower than min_stroke (lost to a morphological opening).
    Those bits print thinner or merge; a few percent is ink traps and punctuation tails."""
    area = section.area()
    if area <= 0:
        return 0.0
    r = min_stroke / 2
    opened = section.offset(-r, JoinType.Round, circular_segments=24).offset(
        r, JoinType.Round, circular_segments=24
    )
    return max(0.0, 1.0 - opened.area() / area)


def min_glyph_gap_mm(text: str | list[str] | tuple[str, ...], cap_height_mm: float, font: str,
                     tracking_mm: float) -> float:
    """Smallest gap between neighbouring glyphs on a line. Touching glyphs merge in print (fine);
    gaps under a nozzle width fill in and look smudged."""
    from .meshio import cross_section_to_shapely

    key = font_key(font)
    gaps = []
    for line in _lines(text):
        glyphs, _ = _line_glyphs(line, key, cap_height_mm, tracking_mm)
        shapes = [cross_section_to_shapely(CrossSection(g, FillRule.NonZero)) if g else None
                  for g in glyphs]
        shapes = [s for s in shapes if s is not None and not s.is_empty]
        gaps += [a.distance(b) for a, b in pairwise(shapes)]
    positive = [g for g in gaps if g > 1e-6]
    return min(positive) if positive else float("inf")


def text_mesh(
    text: str | list[str] | tuple[str, ...],
    cap_height_mm: float,
    relief_mm: float,
    font: str = DEFAULT_FONT,
    tracking_mm: float = 0.0,
    sink_mm: float = 0.0,
) -> tuple[Manifold, TextLayout]:
    """Extruded text: first baseline on y = 0, z from -sink_mm to relief_mm."""
    lay = layout_text(text, cap_height_mm, font, tracking_mm)
    solid = Manifold.extrude(lay.section, relief_mm + sink_mm).translate((0, 0, -sink_mm))
    return solid, lay
