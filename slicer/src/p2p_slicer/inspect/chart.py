"""Per-layer time chart as a static SVG: stacked bars (model, tower, flush, other) and the elapsed
time line. Hovering a bar shows the layer's numbers (SVG <title>, no script)."""

from __future__ import annotations

import math
from html import escape
from typing import Any

import numpy as np

SERIES = (("model", "Model", "#6B7A90"), ("tower", "Tower", "#7CC47A"),
          ("flush", "Flush", "#C597DE"), ("other", "Other", "#CDD2D9"))
LINE = "#1F2937"
GRID = "#E6E8EB"
TEXT = "#6B7280"


def fmt_s(s: float | None) -> str:
    if s is None:
        return ""
    s = float(s)
    if s >= 3600:
        return f"{int(s // 3600)}h {int(s % 3600 // 60):02d}m"
    if s >= 60:
        return f"{int(s // 60)}m {int(s % 60):02d}s"
    return f"{s:.1f}s"


def _nice(v: float) -> float:
    if v <= 0:
        return 1.0
    e = 10 ** np.floor(np.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if m * e >= v:
            return float(m * e)
    return float(10 * e)


def _time_top(v: float) -> float:
    """Axis top: four ticks of a round time step (seconds)."""
    for step in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200):
        if 4 * step >= v:
            return 4.0 * step
    return 4.0 * 3600 * math.ceil(v / 4 / 3600)


def _minute_top(v: float) -> float:
    for step in (1, 2, 5, 10, 15, 30, 45, 60, 90, 120, 150, 180, 240, 300, 360, 480, 600):
        if 4 * step >= v:
            return 4.0 * step
    return 4.0 * 60 * math.ceil(v / 240)


def layer_time_svg(layers: list[dict[str, Any]], cat_s=None, width: int = 1040,
                   height: int = 250) -> str:
    n = len(layers)
    ml, mr, mt_, mb = 52, 58, 14, 30
    pw, ph = width - ml - mr, height - mt_ - mb
    t = np.array([layer.get("time_s") or 0.0 for layer in layers], float)
    split = None
    if cat_s is not None and len(cat_s) >= n + 1:
        split = np.asarray(cat_s, float)[1:n + 1]
    ymax = _time_top(float(t.max()) if n else 1.0)
    elapsed = np.array([layer.get("elapsed_s") or 0.0 for layer in layers], float)
    emax = _minute_top(float(elapsed.max()) / 60 if n else 1.0) * 60
    bw = pw / max(n, 1)
    out = [(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'class="chart" role="img" aria-label="Time per layer">'),
           ('<style>text{font:11px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;'
            f'fill:{TEXT}}}.bar:hover rect{{opacity:.75}}</style>')]
    for k in range(5):
        y = mt_ + ph - ph * k / 4
        out.append(f'<line x1="{ml}" x2="{ml + pw}" y1="{y:.1f}" y2="{y:.1f}" '
                   f'stroke="{GRID}"/>')
        out.append(f'<text x="{ml - 6}" y="{y + 4:.1f}" text-anchor="end">'
                   f'{fmt_s(ymax * k / 4) if k else "0"}</text>')
        out.append(f'<text x="{ml + pw + 6}" y="{y + 4:.1f}">'
                   f'{emax * k / 4 / 60:.0f} min</text>')
    for i, layer in enumerate(layers):
        x = ml + i * bw
        tip = (f"Layer {layer['index'] + 1}, z {layer.get('z') or 0:.2f} mm, "
               f"{fmt_s(layer.get('time_s'))}, elapsed {fmt_s(layer.get('elapsed_s'))}")
        if layer.get("changes"):
            tip += f", {layer['changes']} changes"
        out.append(f'<g class="bar"><title>{escape(tip)}</title>')
        y0 = mt_ + ph
        parts = (list(zip((s[2] for s in SERIES), split[i], strict=True)) if split is not None
                 else [(SERIES[0][2], t[i])])
        for colour, v in parts:
            hgt = ph * v / ymax
            if hgt <= 0:
                continue
            y0 -= hgt
            out.append(f'<rect x="{x:.2f}" y="{y0:.2f}" width="{max(bw - 0.4, 0.6):.2f}" '
                       f'height="{hgt:.2f}" fill="{colour}"/>')
        out.append("</g>")
    if n:
        pts = " ".join(f"{ml + (i + 0.5) * bw:.1f},{mt_ + ph - ph * e / emax:.1f}"
                       for i, e in enumerate(elapsed))
        out.append(f'<polyline points="{pts}" fill="none" stroke="{LINE}" stroke-width="1.5"/>')
    step = max(1, int(_nice(n / 8)))
    for i in range(0, n, step):
        out.append(f'<text x="{ml + (i + 0.5) * bw:.1f}" y="{height - 10}" '
                   f'text-anchor="middle">{i + 1}</text>')
    out.append("</svg>")
    return "".join(out)


def legend_html() -> str:
    items = "".join(f'<span><i style="background:{c}"></i>{label}</span>'
                    for _, label, c in SERIES)
    return (f'<div class="legend">{items}<span><i class="line" style="background:{LINE}">'
            "</i>Elapsed</span></div>")
