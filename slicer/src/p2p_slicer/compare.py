"""Parity between our numbers and Bambu Studio's for one sliced case.

Bambu's reference comes from its own files: result.json (times per feature, total and main grams
per filament, AMS swaps), slice_info.config (filament maps, per-layer filament lists) and, when
the G-code is there, our analysis of Bambu's G-code (tower and flush grams, change counts, layer
count), which reproduces result.json to the milligram and the second (tests/test_m1_parity.py).
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

from .report import Analysis, analyze_gcode, layer_filament_lists


def _pct(ours: float, ref: float) -> float | None:
    if ref == 0:
        return None if ours == 0 else float("inf")
    return 100.0 * (ours - ref) / ref


def parse_slice_info(text: str) -> dict[str, Any]:
    root = ET.fromstring(text)
    plate = root.find("plate")
    out: dict[str, Any] = {"filaments": {}, "layer_filament_lists": []}
    if plate is None:
        return out
    for m in plate.findall("metadata"):
        out[m.get("key")] = m.get("value")
    for f in plate.findall("filament"):
        out["filaments"][int(f.get("id"))] = dict(f.attrib)
    lfl = plate.find("layer_filament_lists")
    if lfl is not None:
        out["layer_filament_lists"] = [dict(e.attrib) for e in lfl]
    return out


def bambu_reference(folder: str | Path, *, analyze: bool = True) -> dict[str, Any]:
    """Reference numbers from a Bambu slice folder (result.json, sliced.gcode.3mf and/or
    plate_1.gcode), as build_truth.py keeps them."""
    folder = Path(folder)
    ref: dict[str, Any] = {"folder": str(folder)}
    rj = folder / "result.json"
    if rj.exists():
        ref["result"] = json.loads(rj.read_text())["sliced_plates"][0]
    s3 = folder / "sliced.gcode.3mf"
    if s3.exists():
        with zipfile.ZipFile(s3) as z:
            if "Metadata/slice_info.config" in z.namelist():
                ref["slice_info"] = parse_slice_info(z.read("Metadata/slice_info.config").decode())
    gcode = folder / "plate_1.gcode"
    if analyze and (gcode.exists() or s3.exists()):
        ref["analysis"] = analyze_gcode(gcode if gcode.exists() else s3)
    return ref


def compare(ours: Analysis, ref: dict[str, Any]) -> dict[str, Any]:
    """Rows of (quantity, ours, bambu, diff, pct). Bambu values come from result.json when it
    has them, else from the analysis of Bambu's G-code."""
    rows: list[dict[str, Any]] = []

    def add(q: str, o: float | str | None, b: float | str | None,
            group: str) -> None:
        row: dict[str, Any] = {"quantity": q, "group": group, "ours": o, "bambu": b}
        if isinstance(o, (int, float)) and isinstance(b, (int, float)):
            row["diff"] = o - b
            row["pct"] = _pct(float(o), float(b))
        rows.append(row)

    res = ref.get("result")
    ba: Analysis | None = ref.get("analysis")
    t = ours.time
    if res:
        add("time total s", t.total_s, res["total_predication"], "time")
        add("time main s", t.main_s, res["main_predication"], "time")
        ft_b = res["feature_type_times"]
        ft_o = t.feature_type_times()
        for k in sorted(set(ft_b) | set(ft_o)):
            add(f"time {k} s", ft_o.get(k, 0.0), ft_b.get(k, 0.0), "feature")
    if ba is not None:
        add("time prepare s", t.prepare_s, ba.time.prepare_s, "time")
    # grams
    u = ours.usage
    if res:
        tot_b = sum(f["total_used_g"] for f in res["filaments"])
        main_b = sum(f["main_used_g"] for f in res["filaments"])
        add("grams total", u.total_g, tot_b, "grams")
        add("grams model", u.bucket_g("model") + u.bucket_g("support"), main_b, "grams")
        add("grams purge", u.purge_g, tot_b - main_b, "grams")
        by_b = {f["id"]: f for f in res["filaments"]}
        for s in sorted(set(by_b) | set(u.per_slot)):
            fo = u.per_slot.get(s)
            fb = by_b.get(s)
            add(f"grams slot {s}", fo.grams() if fo else 0.0,
                fb["total_used_g"] if fb else 0.0, "filament")
            add(f"grams slot {s} model", (fo.grams("model") + fo.grams("support")) if fo else 0.0,
                fb["main_used_g"] if fb else 0.0, "filament")
    if ba is not None:
        add("grams tower", u.bucket_g("tower"), ba.usage.bucket_g("tower"), "grams")
        add("grams flush", u.bucket_g("flush"), ba.usage.bucket_g("flush"), "grams")
        add("filament changes", u.filament_changes, ba.usage.filament_changes, "counts")
        add("nozzle switches", u.nozzle_switches, ba.usage.nozzle_switches, "counts")
        add("layers", ours.layer_count, ba.layer_count, "counts")
    if res:
        add("ams swaps", u.ams_swaps, res.get("filament_change_times"), "counts")
        slots_b = sorted(f["id"] for f in res["filaments"])
        add("slots used", " ".join(map(str, u.slots_used)), " ".join(map(str, slots_b)), "counts")
    si = ref.get("slice_info")
    if si:
        add("filament maps", " ".join(map(str, ours.filament_maps)), si.get("filament_maps"),
            "counts")
        lfl_o = layer_filament_lists(ours)
        lfl_b = si.get("layer_filament_lists", [])
        add("layer filament lists", json.dumps(lfl_o), json.dumps(lfl_b), "counts")
    summary = {
        "time_pct": next((r["pct"] for r in rows if r["quantity"] == "time total s"), None),
        "grams_pct": next((r["pct"] for r in rows if r["quantity"] == "grams total"), None),
        "slots_equal": next((r["ours"] == r["bambu"] for r in rows
                             if r["quantity"] == "slots used"), None),
    }
    return {"rows": rows, "summary": summary}


def format_rows(rows: list[dict[str, Any]], groups: tuple[str, ...] | None = None) -> str:
    out = ["| Quantity | Ours | Bambu | Diff | % |", "|---|---|---|---|---|"]
    for r in rows:
        if groups and r["group"] not in groups:
            continue
        o, b = r["ours"], r["bambu"]
        if isinstance(o, float) or isinstance(b, float):
            os_ = f"{o:.3f}" if isinstance(o, (int, float)) else str(o)
            bs = f"{b:.3f}" if isinstance(b, (int, float)) else str(b)
        else:
            os_, bs = str(o), str(b)
        d = r.get("diff")
        p = r.get("pct")
        ds = "" if d is None else (f"{d:+.3f}" if isinstance(d, float) else f"{d:+d}")
        ps = "" if p is None else f"{p:+.3f}"
        out.append(f"| {r['quantity']} | {os_} | {bs} | {ds} | {ps} |")
    return "\n".join(out)


def truth_dir_candidates(repo: Path) -> list[Path]:
    """Where Bambu's kept slices live: P2P_TRUTH_DIR, this checkout, the main checkout, and the
    slicer-truth worktree (the G-code is gitignored, so it may only exist in one of them)."""
    import os
    c = []
    if os.environ.get("P2P_TRUTH_DIR"):
        c.append(Path(os.environ["P2P_TRUTH_DIR"]))
    c.append(repo / "slicer" / "calibration" / "_truth")
    main = Path(os.environ.get("P2P_MAIN_CHECKOUT", str(repo)))
    c.append(main / "slicer" / "calibration" / "_truth")
    c.append(main.parent / "MeshyBot-wt" / "slicer-truth" / "slicer" / "calibration" / "_truth")
    return c


def find_truth_dir(repo: Path | None = None) -> Path | None:
    repo = repo or Path(__file__).resolve().parents[3]
    for c in truth_dir_candidates(repo):
        if c.is_dir() and any(c.glob("*/result.json")):
            return c
    return None

