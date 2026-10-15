"""Filament flush (purge) volume per swap, ported from Bambu Studio.

Source: bambulab/BambuStudio master, src/libslic3r/FlushVolCalc.cpp (calc_flush_vol,
calc_flush_vol_rgb) and src/libslic3r/FlushVolPredictor.cpp (GenericFlushPredictor), plus
get_min_flush_volumes in src/slic3r/GUI/Plater.cpp. Read 2026-09-23.

How Bambu Studio fills the flush matrix for a printer whose profile sets nozzle_flush_dataset
(the X2D 0.4 Direct Drive Standard nozzle uses dataset 1, file flush_data_dual_standard.txt):
1. If both colors are within CIEDE2000 5 of one of 19 measured reference colors, the measured
   table value for that (from, to) pair is used as is (60 to 900 mm3).
2. Otherwise a formula on HSV hue/saturation distance and luminance change:
      lumi_flush = 560 * (to_lumi - from_lumi) ** 0.7   when getting lighter
                 = 80 * (from_lumi - to_lumi)            when getting darker (and the hue term
                                                          is capped by the destination value)
      hs_flush   = 230 * hue/sat distance (capped at 1.2)
      flush      = third side of a triangle with sides hs_flush, lumi_flush at 120 degrees,
                   at least 60
   then x1.3 in a quirk case (see _bbs_quirk), plus the printer minimum (nozzle_volume, 92 mm3
   on the X2D), capped at 900 mm3.
3. The slicer multiplies by flush_multiplier (1.0 default). Each swap also primes 45 mm3 on the
   prime tower (filament_prime_volume default).
The measured table is read from the installed Bambu Studio (not copied into this repo); without
it only the formula is used.
"""

from __future__ import annotations

import colorsys
import os
from functools import lru_cache
from pathlib import Path

import numpy as np

from .color import ciede2000, hex_to_rgb, srgb_to_lab

MAX_FLUSH_MM3 = 900  # g_max_flush_volume
X2D_MIN_FLUSH_MM3 = 92  # nozzle_volume, Bambu Lab X2D 0.4 nozzle.json
X2D_FLUSH_DATASET = 1  # nozzle_flush_dataset[0] for Direct Drive Standard
PRIME_VOLUME_MM3 = 45.0  # filament_prime_volume default (PrintConfig.cpp)
FLUSH_MULTIPLIER = 1.0
SIMILAR_DE = 5.0  # FlushVolPredictor.hpp is_similar_color default threshold

_TABLE_FILES = {0: "flush_data_standard.txt", 1: "flush_data_dual_standard.txt",
                2: "flush_data_dual_highflow.txt"}
_BAMBU_RESOURCES = [
    Path("/Applications/BambuStudio.app/Contents/Resources/flush"),
    Path.home() / "Applications/BambuStudio.app/Contents/Resources/flush",
]


def _lum(r: float, g: float, b: float) -> float:
    return r * 0.3 + g * 0.59 + b * 0.11


def _delta_hs(h1, s1, v1, h2, s2, v2) -> float:
    h1r, h2r = np.radians(h1), np.radians(h2)
    dx = np.cos(h1r) * s1 * v1 - np.cos(h2r) * s2 * v2
    dy = np.sin(h1r) * s1 * v1 - np.sin(h2r) * s2 * v2
    return float(min(1.2, np.hypot(dx, dy)))


def flush_formula_mm3(src_rgb, dst_rgb) -> float:
    """Bambu calc_flush_vol_rgb: color-only estimate before the minimum is added."""
    sr, sg, sb = (x / 255.0 for x in src_rgb)
    dr, dg, db = (x / 255.0 for x in dst_rgb)
    fh, fs, fv = colorsys.rgb_to_hsv(sr, sg, sb)
    th, ts, tv = colorsys.rgb_to_hsv(dr, dg, db)
    hs = _delta_hs(fh * 360, fs, fv, th * 360, ts, tv)
    fl, tl = _lum(sr, sg, sb), _lum(dr, dg, db)
    if tl >= fl:
        lumi = (tl - fl) ** 0.7 * 560.0
    else:
        lumi = (fl - tl) * 80.0
        hs = min(0.67 * tv + 0.33 * fv, hs)
    hsf = 230.0 * hs
    v = np.sqrt(hsf * hsf + lumi * lumi - 2 * hsf * lumi * np.cos(np.radians(120.0)))
    return float(max(v, 60.0))


def _bbs_quirk(src_rgb, dst_rgb) -> bool:
    """Bambu multiplies by 1.3 when is_from_dark && is_to_light, but computes luminance on
    0-255 values against 0-1 thresholds, so in practice this fires for any source that is not
    pure black going to pure black. Ported as written."""
    return _lum(*src_rgb) > 180 / 255 and _lum(*dst_rgb) < 75 / 255


@lru_cache(maxsize=4)
def load_flush_table(dataset: int = X2D_FLUSH_DATASET, path: str | None = None):
    """Measured flush table from an installed Bambu Studio: (ref_rgb (N,3), {(i,j): mm3})."""
    cands = []
    if path:
        cands.append(Path(path))
    env = os.environ.get("P2P_BAMBU_FLUSH_DIR")
    if env:
        cands.append(Path(env) / _TABLE_FILES.get(dataset, ""))
    cands += [d / _TABLE_FILES.get(dataset, "") for d in _BAMBU_RESOURCES]
    for p in cands:
        if p.is_file():
            lines = p.read_text().splitlines()
            colors = [hex_to_rgb(c) for c in lines[1].split()]
            index = {c: i for i, c in enumerate(colors)}
            table = {}
            for ln in lines[3:]:
                parts = ln.split()
                if len(parts) != 3:
                    continue
                a, b = hex_to_rgb(parts[0]), hex_to_rgb(parts[1])
                if a in index and b in index:
                    table[(index[a], index[b])] = float(parts[2])
            return np.array(colors, dtype=np.uint8), table, str(p)
    return None


def flush_volume_mm3(
    src_rgb, dst_rgb, dataset: int = X2D_FLUSH_DATASET, min_flush: int = X2D_MIN_FLUSH_MM3,
    max_flush: int = MAX_FLUSH_MM3, use_table: bool = True,
) -> float:
    """Flush volume for one swap, as Bambu Studio fills its flush matrix (before multiplier)."""
    src_rgb = tuple(int(x) for x in src_rgb)
    dst_rgb = tuple(int(x) for x in dst_rgb)
    if src_rgb == dst_rgb:
        return 0.0
    tab = load_flush_table(dataset) if (use_table and dataset != 0) else None
    if tab is not None:
        ref, table, _ = tab
        ref_lab = srgb_to_lab(ref)
        s_lab = srgb_to_lab(np.array(src_rgb, dtype=np.uint8))
        d_lab = srgb_to_lab(np.array(dst_rgb, dtype=np.uint8))
        s_hit = np.nonzero(ciede2000(ref_lab, s_lab) <= SIMILAR_DE)[0]
        d_hit = np.nonzero(ciede2000(ref_lab, d_lab) <= SIMILAR_DE)[0]
        if s_hit.size and d_hit.size and (int(s_hit[0]), int(d_hit[0])) in table:
            return float(min(table[(int(s_hit[0]), int(d_hit[0]))], max_flush))
    v = float(int(flush_formula_mm3(src_rgb, dst_rgb)))  # calc_flush_vol_rgb returns int
    if dataset != 0 and _bbs_quirk(src_rgb, dst_rgb):
        v *= 1.3
    return float(min(int(v + min_flush), max_flush))


def flush_matrix(
    palette_rgb: np.ndarray, palette_labs: np.ndarray | None = None,
    dark_to_light_weight: float = 2.0, **kw,
) -> np.ndarray:
    """(K, K) mm3 per swap from row color to column color, times FLUSH_MULTIPLIER.

    dark_to_light_weight (SimParams) scales swaps to a lighter color (L* up by more than 5)
    by weight / 2. The default 2.0 leaves Bambu Studio's numbers unchanged: its data already
    charges dark to light 3 to 10 times more than the reverse. Raise it if a calibration print
    shows dark to light swaps purging more than estimated.
    """
    rgb = np.asarray(palette_rgb, dtype=np.uint8).reshape(-1, 3)
    labs = srgb_to_lab(rgb) if palette_labs is None else np.asarray(palette_labs).reshape(-1, 3)
    k = len(rgb)
    m = np.zeros((k, k))
    for i in range(k):
        for j in range(k):
            if i == j:
                continue
            v = flush_volume_mm3(rgb[i], rgb[j], **kw) * FLUSH_MULTIPLIER
            if labs[j, 0] - labs[i, 0] > 5.0:
                v *= dark_to_light_weight / 2.0
            m[i, j] = v
    return m
