"""MoveTable -> G-code bytes in Bambu's dialect (section 9 and 16 of the spec).

Markers: `; CHANGE_LAYER` / `; Z_HEIGHT:` / `; LAYER_HEIGHT:` at each new layer, `; FEATURE:` and
`; LINE_WIDTH:` when the role or width changes, `; WIPE_START` / `; WIPE_END` around wipes,
`M204 S` rows, and rendered templates (TEXT rows) verbatim. Numbers: X, Y, Z, F with 3 decimals,
E with 5, rounded half away from zero, trailing zeros and the leading zero dropped (`E.0549`).
The result reads back through gcode_read to the same moves (tests/test_slicer_m2.py).
"""

from __future__ import annotations

import math
from typing import Any

from . import __version__
from .moves import ROLE_NAMES, Flag, Kind, MoveTable


def fmt(v: float, decimals: int) -> str:
    scale = 10 ** decimals
    q = abs(v) * scale
    n = math.floor(q + 0.5)
    if v < 0:
        n = -n
    if n == 0:
        return "0"
    s = f"{abs(n) / scale:.{decimals}f}".rstrip("0").rstrip(".")
    if s.startswith("0."):
        s = s[1:]
    return ("-" if n < 0 else "") + s


def _hms(sec: float) -> str:
    s = int(sec)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    if m or h or d:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def header_lines(mt: MoveTable, analysis: Any | None = None) -> list[str]:
    out = ["; HEADER_BLOCK_START", f"; p2p_slicer {__version__}"]
    if analysis is not None:
        t = analysis.time
        out.append(f"; model printing time: {_hms(t.model_s)}; total estimated time: "
                   f"{_hms(t.total_s)}")
        out.append(f"; total layer number: {analysis.layer_count}")
        u = analysis.usage
        used = [s for s in u.slots_used]
        out.append("; total filament length [mm] : " +
                   ",".join(f"{u.per_slot[s].metres() * 1000:.2f}" for s in used))
        out.append("; total filament volume [cm^3] : " +
                   ",".join(f"{u.per_slot[s].total_mm3:.2f}" for s in used))
        out.append("; total filament weight [g] : " +
                   ",".join(f"{u.per_slot[s].grams():.2f}" for s in used))
        out.append("; filament: " + ",".join(str(s) for s in used))
    lz = mt.meta.get("layer_z") or []
    if lz:
        out.append(f"; max_z_height: {max(lz):.2f}")
    out.append("; HEADER_BLOCK_END")
    return out


def to_gcode(mt: MoveTable, analysis: Any | None = None) -> bytes:
    rows = mt.rows
    texts = mt.meta.get("texts", [])
    cfg = mt.meta.get("config", {})
    lines: list[str] = header_lines(mt, analysis)
    lines.append("")
    lines.append("; CONFIG_BLOCK_START")
    for k, v in sorted(cfg.items()):
        lines.append(f"; {k} = {str(v).replace(chr(10), chr(92) + 'n')}")
    lines.append("; CONFIG_BLOCK_END")
    lines.append("")
    lines.append("; EXECUTABLE_BLOCK_START")
    layer_z = mt.meta.get("layer_z") or []
    layer_h = mt.meta.get("layer_h") or []
    x = y = z = 0.0
    f_cur = None
    role = -1
    width = -1.0
    layer = 0
    wiping = False
    kind = rows["kind"]
    flags = rows["flags"]
    for r in range(len(rows)):
        k = int(kind[r])
        fl = int(flags[r])
        if fl & Flag.TEMPLATE:
            x, y, z = float(rows["x"][r]), float(rows["y"][r]), float(rows["z"][r])
            if k <= Kind.WIPE:
                f_cur = float(rows["f"][r])
            role = int(rows["role"][r])
            continue
        lay = int(rows["layer"][r])
        if lay > layer:
            for L in range(layer + 1, lay + 1):
                lines.append("; CHANGE_LAYER")
                if L - 1 < len(layer_z):
                    lines.append(f"; Z_HEIGHT: {_z(layer_z[L - 1])}")
                if L - 1 < len(layer_h):
                    lines.append(f"; LAYER_HEIGHT: {_z(layer_h[L - 1])}")
            layer = lay
        if k == Kind.TEXT:
            if wiping:
                lines.append("; WIPE_END")
                wiping = False
            idx = int(rows["line"][r])
            if 0 <= idx < len(texts):
                lines.append(texts[idx])
            continue
        rr = int(rows["role"][r])
        if rr != role and k in (Kind.EXTRUDE, Kind.WIPE):
            lines.append(f"; FEATURE: {ROLE_NAMES[rr]}")
            role = rr
        if k == Kind.EXTRUDE:
            w = float(rows["width"][r])
            if abs(w - width) > 1e-6:
                lines.append(f"; LINE_WIDTH: {_z(w)}")
                width = w
        is_wipe = k == Kind.WIPE
        if is_wipe and not wiping:
            lines.append("; WIPE_START")
            wiping = True
        elif wiping and not is_wipe and k <= Kind.VIRTUAL:
            lines.append("; WIPE_END")
            wiping = False
        if k == Kind.M204:
            a = rows["args"][r]
            lines.append(f"M204 S{fmt(float(a[0]), 3)}")
            continue
        if k == Kind.DWELL:
            lines.append(f"G4 S{fmt(float(rows['dwell_s'][r]), 3)}")
            continue
        if k > Kind.WIPE:
            continue
        nx, ny, nz = float(rows["x"][r]), float(rows["y"][r]), float(rows["z"][r])
        e = float(rows["e"][r])
        f = float(rows["f"][r])
        arc = fl & (Flag.ARC_CW | Flag.ARC_CCW)
        parts = ["G2" if fl & Flag.ARC_CW else ("G3" if arc else "G1")]
        if arc and nz != z and nx == x and ny == y:
            lines.append("G17")
        if nx != x:
            parts.append("X" + fmt(nx, 3))
        if ny != y:
            parts.append("Y" + fmt(ny, 3))
        if nz != z:
            parts.append("Z" + fmt(nz, 3))
        if arc:
            parts.append("I" + fmt(float(rows["i"][r]), 3))
            parts.append("J" + fmt(float(rows["j"][r]), 3))
            if int(rows["p"][r]):
                parts.append(f"P{int(rows['p'][r])}")
        if e != 0.0:
            parts.append("E" + fmt(e, 5))
        if f_cur is None or abs(f - f_cur) > 1e-9:
            parts.append("F" + fmt(f, 3))
            f_cur = f
        lines.append(" ".join(parts))
        x, y, z = nx, ny, nz
    if wiping:
        lines.append("; WIPE_END")
    lines.append("; EXECUTABLE_BLOCK_END")
    return ("\n".join(lines) + "\n").encode()


def _z(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s or "0"


__all__ = ["fmt", "header_lines", "to_gcode"]
