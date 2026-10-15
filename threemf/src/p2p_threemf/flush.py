"""Flush (purge) volumes the way Bambu Studio computes them (FlushVolCalculator::calc_flush_vol).

For each colour pair Bambu first looks the pair up in its measured table for the nozzle type
(BambuStudio.app/Contents/Resources/flush/flush_data_*.txt; a colour matches a table colour when
CIEDE2000 <= 5). Without a match it uses a colour formula plus the nozzle's minimum flush. A
measured filament-id pair (flush_data_material_pair.json) can raise the result. Capped at 900.
The tables are copied into the profile snapshot so writing does not need Bambu installed.

The X2D 0.4 nozzle uses dataset 1 (dual, standard flow).
"""

from __future__ import annotations

import colorsys
import math
from functools import cache
from pathlib import Path

MAX_FLUSH = 900  # g_max_flush_volume
DATASETS = {0: "flush_data_standard.txt", 1: "flush_data_dual_standard.txt",
            2: "flush_data_dual_highflow.txt"}
MATERIAL_PAIRS = "flush_data_material_pair.json"
APP_FLUSH_DIR = Path("/Applications/BambuStudio.app/Contents/Resources/flush")


def _rgb255(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb(hex_colour: str) -> tuple[float, float, float]:
    r, g, b = _rgb255(hex_colour)
    return r / 255.0, g / 255.0, b / 255.0


def _lum(r: float, g: float, b: float) -> float:
    return r * 0.3 + g * 0.59 + b * 0.11


def _bambu_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """FlushPredict::RGB2LAB, constants as in Bambu's source."""
    def gamma(x: float) -> float:
        return ((x + 0.055) / 1.055) ** 2.4 if x > 0.04045 else x / 12.92

    r, g, b = (gamma(c / 255.0) * 100 for c in rgb)
    x = 0.412453 * r + 0.357580 * g + 0.180423 * b
    y = 0.212671 * r + 0.715160 * g + 0.072169 * b
    z = 0.019334 * r + 0.119193 * g + 0.950227 * b

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 0.137931

    xn, yn, zn = f(x / 95.0489), f(y / 100.0), f(z / 108.8840)
    return 116.0 * yn - 16.0, 500.0 * (xn - yn), 200.0 * (yn - zn)


def parse_dataset(text: str) -> tuple[list[str], dict[tuple[str, str], float]]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    colours = [c.upper() for c in lines[1].split()]
    table = {}
    for ln in lines[3:]:
        a, b, v = ln.split()
        table[(a.upper(), b.upper())] = float(v)
    return colours, table


def app_flush_data() -> dict | None:
    """Flush tables from the installed app, for the profile snapshot."""
    if not APP_FLUSH_DIR.exists():
        return None
    import json

    out: dict = {"datasets": {}, "material_pairs": []}
    for k, name in DATASETS.items():
        p = APP_FLUSH_DIR / name
        if p.exists():
            out["datasets"][str(k)] = p.read_text()
    p = APP_FLUSH_DIR / MATERIAL_PAIRS
    if p.exists():
        out["material_pairs"] = json.loads(p.read_text()).get("pairs", [])
    return out


@cache
def _data() -> dict:
    from .profiles import load_snapshot

    data = load_snapshot().get("flush_data")
    return data or app_flush_data() or {"datasets": {}, "material_pairs": []}


@cache
def _dataset(k: int) -> tuple[list[tuple[str, tuple]], dict[tuple[str, str], float]] | None:
    text = _data()["datasets"].get(str(k))
    if not text:
        return None
    colours, table = parse_dataset(text)
    return [(c, _bambu_lab(_rgb255(c))) for c in colours], table


def _similar(lab, colours) -> str | None:
    from .colour import ciede2000

    for hx, clab in colours:
        if ciede2000(clab, lab) <= 5.0:
            return hx
    return None


def measured(src: str, dst: str, dataset: int) -> float | None:
    """Bambu's measured flush for the table colours nearest to src and dst, if any."""
    ds = _dataset(dataset)
    if ds is None:
        return None
    colours, table = ds
    a = _similar(_bambu_lab(_rgb255(src)), colours)
    b = _similar(_bambu_lab(_rgb255(dst)), colours)
    if a is None or b is None:
        return None
    return table.get((a, b))


def formula(src: str, dst: str) -> float:
    """calc_flush_vol_rgb: colour distance in HSV plus luminance, at least 60."""
    sr, sg, sb = _rgb(src)
    dr, dg, db = _rgb(dst)
    fh, fs, fv = colorsys.rgb_to_hsv(sr, sg, sb)
    th, ts, tv = colorsys.rgb_to_hsv(dr, dg, db)
    fh, th = fh * 2 * math.pi, th * 2 * math.pi
    dx = math.cos(fh) * fs * fv - math.cos(th) * ts * tv
    dy = math.sin(fh) * fs * fv - math.sin(th) * ts * tv
    hs_dist = min(1.2, math.hypot(dx, dy))
    from_l, to_l = _lum(sr, sg, sb), _lum(dr, dg, db)
    if to_l >= from_l:
        lumi_flush = (to_l - from_l) ** 0.7 * 560.0
    else:
        lumi_flush = (from_l - to_l) * 80.0
        hs_dist = min(0.67 * tv + 0.33 * fv, hs_dist)
    hs_flush = 230.0 * hs_dist
    vol = math.sqrt(hs_flush**2 + lumi_flush**2
                    - 2 * hs_flush * lumi_flush * math.cos(math.radians(120)))
    return max(vol, 60.0)


def flush_volume(src: str, dst: str, min_flush: float = 0.0, dataset: int = 1,
                 src_id: str | None = None, dst_id: str | None = None) -> int:
    """mm^3 to purge going from colour `src` to colour `dst` on one nozzle."""
    if src.upper() == dst.upper() and (src_id or "") == (dst_id or ""):
        return 0
    table = measured(src, dst, dataset)
    if dataset != 0 and table is not None:
        vol = table  # dual-nozzle tables are used as is
    else:
        # Dataset 0 (single nozzle): the table value replaces the formula, then the minimum
        # flush is added, as in calc_flush_vol_rgb / calc_flush_vol.
        vol = table if table is not None else formula(src, dst)
        is_from_dark = _lum(*_rgb255(src)) > 180.0 / 255.0  # Bambu compares 0-255 values here
        is_to_light = _lum(*_rgb255(dst)) < 75.0 / 255.0
        if dataset != 0 and is_from_dark and is_to_light:
            vol *= 1.3
        vol += min_flush
    predicted = min(int(vol), MAX_FLUSH)
    if src_id and dst_id:
        for p in _data().get("material_pairs", []):
            if p.get("from") == src_id and p.get("to") == dst_id:
                return min(max(predicted, int(p["purge"])), MAX_FLUSH)
    return predicted


def flush_matrix(colours: list[str], min_flush: float = 0.0, dataset: int = 1,
                 filament_ids: list[str] | None = None) -> list[float]:
    """Row-major N x N matrix (from row to column), diagonal 0, as Bambu stores per extruder."""
    ids = filament_ids or [None] * len(colours)
    out = []
    for i, a in enumerate(colours):
        for j, b in enumerate(colours):
            out.append(0.0 if i == j else
                       float(flush_volume(a, b, min_flush, dataset, ids[i], ids[j])))
    return out
