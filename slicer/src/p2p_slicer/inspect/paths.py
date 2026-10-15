"""Toolpaths from a MoveTable (ours or Bambu's G-code through gcode_read): segments, ribbon
renders by filament and by feature, top-down layer views."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from p2p_sim.mesh import Mesh
from PIL import Image, ImageDraw

from ..moves import ROLE_NAMES, Flag, Kind, MoveTable, Role
from .scene import BED, ISO_DIR, hex_rgb, ortho_for_box, shade
from .style import ROLE_COLOURS

TRAVEL_RGB = (120, 126, 134)
ARC_STEP = math.radians(12)
MAX_LAYER_VIEWS = 12


@dataclass
class Segments:
    """Straight extrusion segments (arcs tessellated), one row each."""
    x0: np.ndarray
    y0: np.ndarray
    x1: np.ndarray
    y1: np.ndarray
    z: np.ndarray
    w: np.ndarray
    h: np.ndarray
    slot: np.ndarray
    role: np.ndarray
    layer: np.ndarray

    def __len__(self) -> int:
        return len(self.x0)

    def take(self, sel) -> Segments:
        return Segments(*(getattr(self, k)[sel] for k in self.__dataclass_fields__))


def _starts(rows: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Start point of every row = end point of the row before it."""
    xs = np.empty(len(rows))
    ys = np.empty(len(rows))
    zs = np.empty(len(rows))
    if len(rows):
        xs[0], ys[0], zs[0] = rows["x"][0], rows["y"][0], rows["z"][0]
        xs[1:], ys[1:], zs[1:] = rows["x"][:-1], rows["y"][:-1], rows["z"][:-1]
    return xs, ys, zs


def segments(mt: MoveTable, kinds=(Kind.EXTRUDE,), default_w: float = 0.42,
             default_h: float = 0.2) -> Segments:
    rows = mt.rows
    xs, ys, _ = _starts(rows)
    sel = np.isin(rows["kind"], np.array(kinds, dtype=np.uint8))
    flags = rows["flags"].astype(np.int64)
    arc = sel & ((flags & (Flag.ARC_CW | Flag.ARC_CCW)) != 0)
    straight = sel & ~arc
    w = np.where(rows["width"] > 0, rows["width"], default_w).astype(np.float64)
    h = np.where(rows["height"] > 0, rows["height"], default_h).astype(np.float64)

    def cols(idx, x0, y0, x1, y1):
        return [x0, y0, x1, y1, rows["z"][idx], w[idx], h[idx], rows["slot"][idx],
                rows["role"][idx], rows["layer"][idx]]

    si = np.nonzero(straight)[0]
    parts = [cols(si, xs[si], ys[si], rows["x"][si], rows["y"][si])]
    ai = np.nonzero(arc)[0]
    if len(ai):
        x0, y0 = xs[ai], ys[ai]
        x1, y1 = rows["x"][ai], rows["y"][ai]
        cx, cy = x0 + rows["i"][ai], y0 + rows["j"][ai]
        r = np.hypot(rows["i"][ai], rows["j"][ai])
        a0 = np.arctan2(y0 - cy, x0 - cx)
        a1 = np.arctan2(y1 - cy, x1 - cx)
        cw = (flags[ai] & Flag.ARC_CW) != 0
        sweep = np.where(cw, (a0 - a1) % (2 * np.pi), (a1 - a0) % (2 * np.pi))
        full = sweep < 1e-9
        turns = rows["p"][ai].astype(np.float64)
        sweep = np.where(full, 2 * np.pi, sweep) + 2 * np.pi * np.where(full, np.maximum(
            turns - 1, 0), turns)
        n = np.clip(np.ceil(sweep / ARC_STEP), 1, 96).astype(np.int64)
        rep = np.repeat(np.arange(len(ai)), n)
        k = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
        sign = np.where(cw, -1.0, 1.0)[rep]
        t0 = k / n[rep]
        t1 = (k + 1) / n[rep]
        ang0 = a0[rep] + sign * sweep[rep] * t0
        ang1 = a0[rep] + sign * sweep[rep] * t1
        px0 = cx[rep] + r[rep] * np.cos(ang0)
        py0 = cy[rep] + r[rep] * np.sin(ang0)
        px1 = cx[rep] + r[rep] * np.cos(ang1)
        py1 = cy[rep] + r[rep] * np.sin(ang1)
        last = k == n[rep] - 1
        px1[last], py1[last] = x1[rep][last], y1[rep][last]
        parts.append(cols(ai[rep], px0, py0, px1, py1))
    return Segments(*(np.concatenate([p[c] for p in parts]) for c in range(10)))


def travel_segments(mt: MoveTable) -> Segments:
    rows = mt.rows
    xs, ys, _ = _starts(rows)
    sel = rows["kind"] == Kind.TRAVEL
    moved = np.hypot(rows["x"] - xs, rows["y"] - ys) > 1e-6
    idx = np.nonzero(sel & moved)[0]
    z = rows["z"][idx]
    return Segments(xs[idx], ys[idx], rows["x"][idx], rows["y"][idx], z, np.full(len(idx), 0.1),
                    np.zeros(len(idx)), rows["slot"][idx], rows["role"][idx],
                    rows["layer"][idx])


def ribbon_mesh(seg: Segments, rgb: np.ndarray) -> tuple[Mesh, np.ndarray]:
    """Each segment as a flat ribbon (line width, at the layer top) plus a vertical quad (layer
    height) along its centre line: 4 triangles that read as stacked layers from any side."""
    dx, dy = seg.x1 - seg.x0, seg.y1 - seg.y0
    ln = np.hypot(dx, dy)
    keep = ln > 1e-6
    seg, rgb, dx, dy, ln = seg.take(keep), rgb[keep], dx[keep], dy[keep], ln[keep]
    nx, ny = -dy / ln * seg.w / 2, dx / ln * seg.w / 2
    zt = seg.z - 0.01
    zb = seg.z - seg.h
    v = np.stack([
        np.c_[seg.x0 + nx, seg.y0 + ny, zt], np.c_[seg.x1 + nx, seg.y1 + ny, zt],
        np.c_[seg.x1 - nx, seg.y1 - ny, zt], np.c_[seg.x0 - nx, seg.y0 - ny, zt],
        np.c_[seg.x0, seg.y0, zb], np.c_[seg.x1, seg.y1, zb],
        np.c_[seg.x1, seg.y1, zt], np.c_[seg.x0, seg.y0, zt],
    ], axis=1).reshape(-1, 3)
    base = (np.arange(len(seg)) * 8)[:, None]
    f = (base[:, None, :] + np.array([[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7]])[None]) \
        .reshape(-1, 3)
    return Mesh(v, f), np.repeat(rgb, 4, axis=0)


def slot_rgb(slots: np.ndarray, colours: list[str]) -> np.ndarray:
    lut = np.array([hex_rgb(c) for c in colours] or [(128, 128, 128)], np.uint8)
    return lut[np.clip(slots.astype(np.int64) - 1, 0, len(lut) - 1)]


def role_rgb(roles: np.ndarray) -> np.ndarray:
    lut = np.array([hex_rgb(ROLE_COLOURS.get(n, "#BBBBBB")) for n in ROLE_NAMES], np.uint8)
    return lut[np.clip(roles.astype(np.int64), 0, len(lut) - 1)]


def region_box(seg: Segments, margin: float = 2.0) -> tuple[np.ndarray, np.ndarray]:
    lo = np.array([min(seg.x0.min(), seg.x1.min()) - margin,
                   min(seg.y0.min(), seg.y1.min()) - margin, 0.0])
    hi = np.array([max(seg.x0.max(), seg.x1.max()) + margin,
                   max(seg.y0.max(), seg.y1.max()) + margin, float(seg.z.max())])
    return lo, hi


def _split_tower(seg: Segments) -> tuple[Segments, Segments]:
    tower = seg.role == Role.PRIME_TOWER
    model = seg.take(~tower) if (~tower).any() else seg
    return model, seg.take(tower)


def render_toolpaths(seg: Segments, colours: list[str], out_dir: Path,
                     size: tuple[int, int] = (960, 720)) -> dict[str, str]:
    """Iso views of the model's extrusion coloured by filament and by feature, framed on the
    model, and the prime tower on its own by filament."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if not len(seg):
        return {}
    model, tower = _split_tower(seg)
    out = {}
    lo, hi = region_box(model)
    cam = ortho_for_box(lo, hi, ISO_DIR, size[0], size[1], "toolpaths")
    for name, rgb in (("paths_filament", slot_rgb(model.slot, colours)),
                      ("paths_feature", role_rgb(model.role))):
        mesh, frgb = ribbon_mesh(model, rgb)
        Image.fromarray(shade(mesh, cam, frgb, bg=BED)).save(out_dir / f"{name}.png",
                                                              optimize=True)
        out[name] = f"{name}.png"
    if len(tower) and len(tower) < len(seg):
        lo, hi = region_box(tower)
        cam = ortho_for_box(lo, hi, ISO_DIR, size[1], size[1], "tower")
        mesh, frgb = ribbon_mesh(tower, slot_rgb(tower.slot, colours))
        Image.fromarray(shade(mesh, cam, frgb, bg=BED)).save(out_dir / "paths_tower.png",
                                                              optimize=True)
        out["paths_tower"] = "paths_tower.png"
    return out


def pick_layers(layer_sets: list[tuple[int, ...]], switches: list[int],
                n_max: int = MAX_LAYER_VIEWS) -> list[int]:
    """0-based layers to draw: first, last, most switches, 25/50/75 percent, then each change
    of the extruded filament set (section 15)."""
    n = len(layer_sets)
    if n == 0:
        return []
    pick: list[int] = []

    def add(i: int) -> None:
        if 0 <= i < n and i not in pick and len(pick) < n_max:
            pick.append(i)

    add(0)
    add(n - 1)
    if switches and max(switches) > 0:
        add(int(np.argmax(switches)))
    for q in (0.25, 0.5, 0.75):
        add(round(q * (n - 1)))
    for i in range(1, n):
        if layer_sets[i] != layer_sets[i - 1]:
            add(i)
    return sorted(pick)


def _draw_layer(img: Image.Image, seg: Segments, trav: Segments, rgb: np.ndarray,
                lo: np.ndarray, scale: float, height: int) -> None:
    d = ImageDraw.Draw(img)

    def px(x, y):
        return (x - lo[0]) * scale, height - (y - lo[1]) * scale

    for i in range(len(trav)):
        d.line([px(trav.x0[i], trav.y0[i]), px(trav.x1[i], trav.y1[i])], fill=TRAVEL_RGB,
               width=1)
    for i in range(len(seg)):
        wpx = max(1, round(seg.w[i] * scale))
        a, b = px(seg.x0[i], seg.y0[i]), px(seg.x1[i], seg.y1[i])
        c = tuple(int(v) for v in rgb[i])
        d.line([a, b], fill=c, width=wpx)
        if wpx > 2:
            r = wpx / 2 - 0.5
            d.ellipse([b[0] - r, b[1] - r, b[0] + r, b[1] + r], fill=c)


def render_layers(seg: Segments, trav: Segments, layers: list[int], colours: list[str],
                  out_dir: Path, panel_px: int = 640) -> dict[int, str]:
    """Top-down view of each chosen layer (0-based): the model in filament colours, the model in
    feature colours, and the prime tower in filament colours (same scale), travel moves as thin
    grey lines. Layer k in the MoveTable is k + 1."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if not len(seg):
        return {}
    model, tower = _split_tower(seg)
    lo, hi = region_box(model, margin=3.0)
    span = max(hi[0] - lo[0], hi[1] - lo[1], 1.0)
    scale = panel_px / span
    boxes = [(lo, hi, "filament"), (lo, hi, "feature")]
    if len(tower) and len(tower) < len(seg):
        tlo, thi = region_box(tower, margin=3.0)
        boxes.append((tlo, thi, "filament"))
    sizes = [(math.ceil((b[1][0] - b[0][0]) * scale), math.ceil((b[1][1] - b[0][1]) * scale))
             for b in boxes]
    gap = 12
    width = sum(w for w, _ in sizes) + gap * (len(sizes) - 1)
    height = max(h for _, h in sizes)
    out = {}
    for layer in layers:
        s = seg.take(seg.layer == layer + 1)
        t = trav.take(trav.layer == layer + 1)
        sheet = Image.new("RGB", (width, height), (255, 255, 255))
        x = 0
        for (blo, _bhi, colour_by), (w, h) in zip(boxes, sizes, strict=True):
            panel = Image.new("RGB", (w, height), BED)  # shorter regions sit centred
            rgb = slot_rgb(s.slot, colours) if colour_by == "filament" else role_rgb(s.role)
            _draw_layer(panel, s, t, rgb, blo, scale, height - (height - h) / 2)
            sheet.paste(panel, (x, 0))
            x += w + gap
        name = f"layer_{layer + 1:04d}.png"
        sheet.save(out_dir / name, optimize=True)
        out[layer] = name
    return out
