"""Toolpath preview for the studio paint editor: a sliced MoveTable as polylines per layer, small
enough for a browser to draw layer by layer (contracts payloads.PaintPreview, toolpaths
version 1).

    from p2p_slicer.api import slice_project
    from p2p_slicer.preview import print_numbers, write_toolpaths

    res = slice_project(Path("final.3mf"))
    after = print_numbers(res.report, palette_ids, layers=res.analysis.layer_count)
    index = write_toolpaths(res.moves, out_dir, slots=[{"slot": 1, "filament_id": "...",
                                                        "hex": "#161616"}],
                            per_layer_s=res.analysis.time.per_layer_s)

Files: `<name>.json` (the index) and `<name>.bin`: uint16 (x, y) points at `scale_mm` from
`origin_mm`, then uint32 chains (first point, point count, slot << 8 | role). A chain is one
continuous extrusion (same layer, filament and feature, each segment starting where the last one
ended); travel is left out, arcs are cut into 12 degree steps, the prime tower is kept.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from .moves import ROLE_NAMES, Flag, Kind, MoveTable, Role

VERSION = 1
SCALE_MM = 0.01
ARC_STEP = math.radians(12)
JOIN_MM = 1e-4


def _starts(rows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    xs = np.empty(len(rows))
    ys = np.empty(len(rows))
    if len(rows):
        xs[0], ys[0] = rows["x"][0], rows["y"][0]
        xs[1:], ys[1:] = rows["x"][:-1], rows["y"][:-1]
    return xs, ys


def extrusion_segments(mt: MoveTable) -> dict[str, np.ndarray]:
    """Every extrusion as straight segments in print order (arcs tessellated), with the row it
    came from, its layer, filament slot and feature role."""
    rows = mt.rows
    xs, ys = _starts(rows)
    sel = (rows["kind"] == Kind.EXTRUDE) & (rows["layer"] > 0) & (rows["slot"] > 0)
    flags = rows["flags"].astype(np.int64)
    arc = sel & ((flags & (Flag.ARC_CW | Flag.ARC_CCW)) != 0)
    si = np.nonzero(sel & ~arc)[0]
    parts = [(si, xs[si], ys[si], rows["x"][si], rows["y"][si])]
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
        sweep = np.where(full, 2 * np.pi, sweep) + 2 * np.pi * np.where(
            full, np.maximum(turns - 1, 0), turns)
        n = np.clip(np.ceil(sweep / ARC_STEP), 1, 96).astype(np.int64)
        rep = np.repeat(np.arange(len(ai)), n)
        k = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
        sign = np.where(cw, -1.0, 1.0)[rep]
        ang0 = a0[rep] + sign * sweep[rep] * (k / n[rep])
        ang1 = a0[rep] + sign * sweep[rep] * ((k + 1) / n[rep])
        px0 = cx[rep] + r[rep] * np.cos(ang0)
        py0 = cy[rep] + r[rep] * np.sin(ang0)
        px1 = cx[rep] + r[rep] * np.cos(ang1)
        py1 = cy[rep] + r[rep] * np.sin(ang1)
        last = k == n[rep] - 1
        px1[last], py1[last] = x1[rep][last], y1[rep][last]
        px0[k == 0], py0[k == 0] = x0[rep][k == 0], y0[rep][k == 0]
        parts.append((ai[rep], px0, py0, px1, py1))
    row = np.concatenate([p[0] for p in parts]).astype(np.int64)
    order = np.argsort(row, kind="stable")  # arcs keep their step order within a row
    out = {"row": row[order]}
    for i, key in enumerate(("x0", "y0", "x1", "y1"), start=1):
        out[key] = np.concatenate([p[i] for p in parts])[order]
    for key in ("z", "layer", "slot", "role", "height"):
        out[key] = rows[key][out["row"]]
    return out


def _layer_changes(mt: MoveTable) -> dict[int, int]:
    """Filament changes per layer, counted like usage.filament_changes (a T to another filament
    after the first); one before the first layer counts on layer 1."""
    rows = mt.rows
    tc = (rows["kind"] == Kind.TOOL_CHANGE) & (rows["i"] > 0)
    lay = np.maximum(rows["layer"][tc], 1)
    vals, cnt = np.unique(lay, return_counts=True)
    return {int(v): int(c) for v, c in zip(vals, cnt, strict=True)}


def write_toolpaths(mt: MoveTable, out_dir: Path, *, slots: list[dict] | None = None,
                    per_layer_s: np.ndarray | None = None, name: str = "toolpaths"
                    ) -> dict[str, Any]:
    """Write `<name>.json` and `<name>.bin` into out_dir; returns the index."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    seg = extrusion_segments(mt)
    n = len(seg["row"])
    if n and (np.diff(seg["layer"]) < 0).any():  # never in slicer output; keep layers contiguous
        order = np.argsort(seg["layer"], kind="stable")
        seg = {k: v[order] for k, v in seg.items()}
    if n:
        length = np.hypot(seg["x1"] - seg["x0"], seg["y1"] - seg["y0"])
        keep = length > 1e-6
        seg = {k: v[keep] for k, v in seg.items()}
        n = len(seg["row"])
    if n:
        start = np.ones(n, bool)
        start[1:] = ((seg["layer"][1:] != seg["layer"][:-1]) | (seg["slot"][1:] != seg["slot"][:-1])
                     | (seg["role"][1:] != seg["role"][:-1])
                     | (np.abs(seg["x0"][1:] - seg["x1"][:-1]) > JOIN_MM)
                     | (np.abs(seg["y0"][1:] - seg["y1"][:-1]) > JOIN_MM))
        chain_of = np.cumsum(start) - 1
        firsts = np.nonzero(start)[0]
        n_chains = len(firsts)
        counts = np.diff(np.append(firsts, n)) + 1
        xs_all = np.concatenate([seg["x0"], seg["x1"]])
        ys_all = np.concatenate([seg["y0"], seg["y1"]])
        lo = (math.floor(float(xs_all.min())), math.floor(float(ys_all.min())))
        hi = (float(xs_all.max()), float(ys_all.max()))
        span = max(hi[0] - lo[0], hi[1] - lo[1], 1.0)
        scale = max(SCALE_MM, span / 65000.0)
        pts = np.zeros((n + n_chains, 2), np.float64)
        pts[np.arange(n) + chain_of + 1] = np.c_[seg["x1"], seg["y1"]]
        head = firsts + np.arange(n_chains)
        pts[head] = np.c_[seg["x0"][firsts], seg["y0"][firsts]]
        q = np.rint((pts - np.array(lo)) / scale).clip(0, 65535).astype("<u2")
        chains = np.zeros((n_chains, 3), "<u4")
        chains[:, 0] = head
        chains[:, 1] = counts
        chains[:, 2] = (seg["slot"][firsts].astype(np.int64) << 8) | seg["role"][firsts]
        chain_layer = seg["layer"][firsts]
    else:
        lo, hi, scale = (0, 0), (0.0, 0.0), SCALE_MM
        q = np.zeros((0, 2), "<u2")
        chains = np.zeros((0, 3), "<u4")
        chain_layer = np.zeros(0, np.int64)
        n_chains = 0
    blob = q.tobytes()
    pad = (-len(blob)) % 4
    chains_offset = len(blob) + pad
    (out_dir / f"{name}.bin").write_bytes(blob + b"\0" * pad + chains.tobytes())

    changes = _layer_changes(mt)
    layers = []
    layer_ids = np.unique(chain_layer)
    # layers only go up in print order, so each layer is one run of chains and of segments
    c_lo = np.searchsorted(chain_layer, layer_ids, side="left")
    c_hi = np.searchsorted(chain_layer, layer_ids, side="right")
    s_lo = np.searchsorted(seg["layer"], layer_ids, side="left") if n else c_lo
    s_hi = np.searchsorted(seg["layer"], layer_ids, side="right") if n else c_hi
    for k, lay in enumerate(layer_ids.tolist()):
        sl = slice(int(s_lo[k]), int(s_hi[k]))
        roles = seg["role"][sl]
        t = None
        if per_layer_s is not None and 0 <= lay < len(per_layer_s):
            t = round(float(per_layer_s[lay]), 2)
        layers.append({"z": round(float(seg["z"][sl].max()), 3),
                       "height": round(float(seg["height"][sl].max()), 3),
                       "chains": [int(c_lo[k]), int(c_hi[k] - c_lo[k])], "time_s": t,
                       "slots": sorted({int(s) for s in np.unique(
                           seg["slot"][sl][roles != Role.PRIME_TOWER])}),
                       "changes": changes.get(int(lay), 0)})
    index = {
        "version": VERSION, "bin": f"{name}.bin", "scale_mm": scale,
        "origin_mm": [float(lo[0]), float(lo[1])],
        "bbox_mm": [float(lo[0]), float(lo[1]), round(float(hi[0]), 3), round(float(hi[1]), 3)],
        "slots": list(slots or []), "roles": list(ROLE_NAMES),
        "points": {"offset": 0, "count": len(q)},
        "chains": {"offset": chains_offset, "count": int(n_chains)},
        "layers": layers,
    }
    (out_dir / f"{name}.json").write_text(json.dumps(index, separators=(",", ":")))
    return index


def print_numbers(report, filament_ids: list[str] | None = None, *, layers: int | None = None):
    """contracts PrintNumbers from a SliceReport, with our filament ids by slot (a native
    report carries Bambu's preset ids, GFL99 and the like)."""
    from p2p_contracts.payloads import PrintNumbers

    ids = list(filament_ids or [])
    fils = [f.model_copy(update={"filament_id": ids[f.slot - 1] if 0 < f.slot <= len(ids)
                                 else f.filament_id}) for f in report.filaments]
    return PrintNumbers(slicer=report.slicer, print_time_s=report.print_time_s,
                        total_grams=report.total_grams, purge_grams=report.purge_grams,
                        filament_changes=report.filament_changes, layers=layers,
                        filaments=fils)


def read_toolpaths(index_path: Path) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    """(index, points in mm (N, 2), chains (M, 3)) from a written pair."""
    index = json.loads(Path(index_path).read_text())
    data = (Path(index_path).parent / index["bin"]).read_bytes()
    q = np.frombuffer(data, "<u2", count=2 * index["points"]["count"]).reshape(-1, 2)
    pts = np.asarray(index["origin_mm"]) + q.astype(np.float64) * index["scale_mm"]
    chains = np.frombuffer(data, "<u4", count=3 * index["chains"]["count"],
                           offset=index["chains"]["offset"]).reshape(-1, 3)
    return index, pts, chains
