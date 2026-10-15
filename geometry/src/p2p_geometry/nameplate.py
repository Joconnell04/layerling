"""Nameplate: a chamfered plate with the name as raised text (or a flush inlay) in its own part."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from manifold3d import Manifold
from p2p_contracts.filaments import Filament, FilamentLibrary
from p2p_contracts.job import TextSpec

from . import addons as ad
from .color import ciede2000, find, highest_contrast
from .text import (
    DEFAULT_FONT,
    FONTS,
    HEAVIER,
    layout_text,
    min_glyph_gap_mm,
    min_stroke_mm,
    text_mesh,
    thin_fraction,
)
from .types import GeometrySettings

WRAP_GAIN = 1.2  # two lines must allow caps this much taller than one line to be chosen


@dataclass
class TextFit:
    lines: tuple[str, ...]
    font: str
    cap: float
    tracking: float
    width: float  # widest line at this cap, mm
    height: float  # ink height of the block, mm
    fits: bool  # False: needs `width` in a wider region (the caller widens and warns)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _splits(text: str) -> list[tuple[str, ...]]:
    """The text on one line, plus every two-line split at a space."""
    out: list[tuple[str, ...]] = [(text,)]
    for i, ch in enumerate(text):
        if ch == " ":
            a, b = text[:i].rstrip(), text[i + 1 :].lstrip()
            if a and b:
                out.append((a, b))
    return out


MAX_THIN = 0.03  # at most 3 % of the name's ink may be thinner than 2 x nozzle


def cap_floor(text: str, font: str, gs: GeometrySettings) -> float:
    """Smallest cap height (>= 5 mm) at which at most 3 % of the ink is thinner than 2 x nozzle."""
    cap = gs.min_cap_height_mm
    while cap < 40.0:
        if thin_fraction(layout_text(text, cap, font).section, gs.min_stroke_mm) <= MAX_THIN:
            return cap
        cap += 0.5
    return cap


def fit_text(spec: TextSpec, region_w: float, region_h: float, gs: GeometrySettings) -> TextFit:
    """Lines, weight, cap height and tracking for a name in a region.

    Tries one line and every two-line split at a space; the largest cap height wins, but two
    lines must beat one line by 20 %. Never below the printable minimum: 5 mm, raised if more
    than 3 % of the ink would be thinner than 2 x nozzle (the family's heavier weight is tried
    first when the name has to go that small). If nothing fits at the minimum, the narrowest
    layout is returned with fits=False.
    """
    warns: list[str] = []
    font = spec.font if spec.font in FONTS else DEFAULT_FONT
    if spec.font not in FONTS:
        warns.append(f"Font {spec.font!r} unknown; used {DEFAULT_FONT}")

    def options_for(f: str):
        out = []
        for lines in _splits(spec.text):
            lay = layout_text(lines, 10.0, f)
            w_per, h_per = lay.width_mm / 10.0, (lay.bbox[3] - lay.bbox[1]) / 10.0
            out.append((lines, min(region_w / w_per, region_h / h_per), w_per, h_per))
        return out

    def best_of(opts):
        return max(opts, key=lambda o: o[1] / (WRAP_GAIN if len(o[0]) > 1 else 1.0))

    options = options_for(font)
    floor = cap_floor(spec.text, font, gs)
    if best_of(options)[1] < floor and font in HEAVIER:
        heavy = HEAVIER[font]
        h_floor = cap_floor(spec.text, heavy, gs)
        if h_floor < floor:
            font, floor, options = heavy, h_floor, options_for(heavy)
            warns.append("Name set small; switched to the heavier weight so strokes print")
    one = options[0]
    best = best_of(options)
    if spec.cap_height_mm is not None:
        cap = max(float(spec.cap_height_mm), floor)
        if cap > spec.cap_height_mm:
            warns.append(f"Cap height {spec.cap_height_mm:g} mm raised to {cap:.1f} mm "
                         "(printable minimum)")
        fitting = [o for o in options if o[2] * cap <= region_w and o[3] * cap <= region_h]
        best = fitting[0] if fitting else min(options, key=lambda o: o[2])
        fits = bool(fitting)
    elif best[1] >= floor:
        cap = best[1]
        fits = True
    else:
        # Nothing fits at the minimum: the narrowest layout that still fits the height.
        tall_ok = [o for o in options if o[3] * floor <= region_h] or options
        best = min(tall_ok, key=lambda o: o[2])
        cap = floor
        fits = False
    lines = best[0]
    notes = []
    if len(lines) > 1 and best is not one:
        notes.append(f"Name set on two lines: {' / '.join(lines)}")
    # Open up letter gaps under 1.5 x nozzle, then shrink to fit the extra width (not below
    # the floor).
    for _ in range(5):
        gap = min_glyph_gap_mm(lines, cap, font, 0.0)
        tracking = max(0.0, 1.5 * gs.nozzle_mm - gap) if math.isfinite(gap) else 0.0
        lay = layout_text(lines, cap, font, tracking)
        if lay.width_mm <= region_w + 1e-6 or cap <= floor + 1e-9 or spec.cap_height_mm:
            break
        cap = max(floor, cap * region_w / lay.width_mm * 0.998)
    return TextFit(lines, font, cap, tracking, lay.width_mm, lay.bbox[3] - lay.bbox[1],
                   fits and lay.width_mm <= region_w + 1e-6, warns, notes)


def snap_relief(relief: float, gs: GeometrySettings, warnings: list[str]) -> float:
    """Clamp to 0.8 to 1.2 mm and snap to whole layers (4 to 6 at 0.2 mm)."""
    r = min(max(relief, gs.text_relief_min_mm), gs.text_relief_max_mm)
    r = max(gs.text_relief_min_mm, round(r / gs.layer_mm) * gs.layer_mm)
    if abs(r - relief) > 1e-6:
        warnings.append(f"Text relief {relief:g} mm set to {r:g} mm ({round(r / gs.layer_mm)} layers)")
    return r


@dataclass
class NameplateResult:
    plate: Manifold
    text: Manifold | None
    width_mm: float  # may be wider than asked when the name did not fit
    x0: float  # plate left edge
    text_filament_id: str | None
    info: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def nameplate(
    text: TextSpec | None,
    width_mm: float,
    depth_mm: float,
    plate_filament: Filament,
    library: FilamentLibrary,
    settings: GeometrySettings | None = None,
    thickness_mm: float = 4.0,
    x0: float | None = None,
    text_region: tuple[float, float] | None = None,
    grow: str = "right",
    margin_mm: float = 5.0,
    corner_mm: float | None = None,
    shape: str = "rounded",
    text_height_mm: float | None = None,
) -> NameplateResult:
    """Plate centered on y = 0 spanning x0 .. x0 + width, bottom on z = 0.

    text_region: (x_start, x_end) for the name; default the whole plate less margins. A long
    name shrinks to the minimum cap height or wraps onto two lines; if it still does not fit,
    the plate grows toward `grow` and a warning suggests the wider plate.
    Text color: text.filament_id, else the palette filament with the highest CIEDE2000 against
    the plate (same material only); p2p_geometry.addon_color may replace it afterwards with a
    color from the job or the concept.
    shape: rectangle, rounded or oval (addons.PLATE_SHAPES); corner_mm None uses the shape's
    radius. text_height_mm: the height the name may use (default the depth less margins; an
    oval passes the chord height its caller computed).
    """
    gs = settings or GeometrySettings()
    warns: list[str] = []
    notes: list[str] = []
    x0 = -width_mm / 2 if x0 is None else x0
    t = max(thickness_mm, gs.plate_min_thickness_mm)
    if t > thickness_mm:
        warns.append(f"Plate thickness raised to {t:g} mm")
    rx0, rx1 = text_region or (x0 + margin_mm, x0 + width_mm - margin_mm)
    info: dict = {"w_mm": width_mm, "d_mm": depth_mm, "t_mm": t, "shape": shape}
    text_solid = None
    text_fil = None
    if text is not None:
        fit = fit_text(text, rx1 - rx0,
                       text_height_mm if text_height_mm else depth_mm - 2 * margin_mm, gs)
        warns += fit.warnings
        notes += fit.notes
        if not fit.fits:
            extra = fit.width - (rx1 - rx0)
            asked = width_mm
            width_mm += max(extra, 0.0)
            if grow == "left":
                x0 -= extra
                rx0 -= extra
            else:
                rx1 += extra
            warns.append(
                f"Name {text.text!r} does not fit the {asked:.0f} mm plate at the "
                f"{fit.cap:.1f} mm minimum cap height, even on two lines; suggest a "
                f"{math.ceil(width_mm / 5) * 5:.0f} mm plate (widened to {width_mm:.0f} mm here)"
            )
        relief = snap_relief(text.relief_mm, gs, warns)
        text_fil = find(library, text.filament_id) if text.filament_id else None
        same = True
        if text_fil is None:
            text_fil, _, same = highest_contrast(library, plate_filament)
        if not same:
            warns.append("No text filament of the plate's material; layers may not bond")
        de = ciede2000(tuple(text_fil.lab), tuple(plate_filament.lab))
        raised, lay = text_mesh(fit.lines, fit.cap, relief, fit.font, fit.tracking, sink_mm=0.2)
        bx0, by0, bx1, by1 = lay.bbox
        tx, ty = (rx0 + rx1) / 2 - (bx0 + bx1) / 2, -(by0 + by1) / 2
        stroke = min_stroke_mm(lay.section)
        thin = thin_fraction(lay.section, gs.min_stroke_mm)
        if thin > MAX_THIN + 1e-3:
            warns.append(f"{thin:.0%} of the name is thinner than {gs.min_stroke_mm:g} mm")
        info["text"] = {
            "text": text.text, "lines": list(fit.lines), "font": fit.font,
            "cap_height_mm": fit.cap, "relief_mm": relief, "style": text.style,
            "min_stroke_mm": stroke, "thin_fraction": thin, "tracking_mm": fit.tracking,
            "width_mm": lay.width_mm,
            "filament_id": text_fil.id, "delta_e_vs_plate": de,
            "center_xy": ((rx0 + rx1) / 2, 0.0), "kerned": True,
        }
        text_solid = raised.translate((tx, ty, t))
    slab = ad.plate(width_mm, depth_mm, t, corner_mm, shape=shape).translate(
        (x0 + width_mm / 2, 0, 0))
    if text is not None and text.style == "engraved":
        cut, _ = text_mesh(fit.lines, fit.cap, 1.0, fit.font, fit.tracking, sink_mm=relief)
        slab = slab - cut.translate((tx, ty, t))
        inlay, _ = text_mesh(fit.lines, fit.cap, 0.0, fit.font, fit.tracking, sink_mm=relief)
        text_solid = inlay.translate((tx, ty, t))
        info["text"]["inlay"] = True
    info["w_mm"] = width_mm
    return NameplateResult(slab, text_solid, width_mm, x0, text_fil.id if text_fil else None,
                           info, warns, notes)
