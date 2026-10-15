"""s06 outputs of a rerun against the stored run's: same parts, same faces, vertices within a
tolerance, same decisions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from p2p_worker.util import read_json, sha256_file

TOL_MM = 1e-3  # 1 micron; the printer's resolution is 100 times coarser (ASSUMED ample)
REL_TOL = 1e-6  # report metrics (areas, tilts, margins), relative


def _flat(obj: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            out.update(_flat(v, f"{prefix}{k}."))
        return out
    if isinstance(obj, list) and all(isinstance(x, (int, float)) for x in obj):
        return {prefix.rstrip("."): obj}
    if isinstance(obj, list):
        out = {}
        for i, v in enumerate(obj):
            out.update(_flat(v, f"{prefix}{i}."))
        return out
    return {prefix.rstrip("."): obj}


def _close(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= REL_TOL * max(1.0, abs(a), abs(b))
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def diff_json(a: Any, b: Any) -> list[str]:
    """Leaf paths whose values differ beyond REL_TOL (numbers) or at all (anything else)."""
    fa, fb = _flat(a), _flat(b)
    return sorted(k for k in set(fa) | set(fb) if not _close(fa.get(k), fb.get(k)))


def _parts(g: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["name"]: p for p in [g["figure"], *g.get("parts", [])]}


def compare_geometry(stored_dir: Path, new_dir: Path, tol_mm: float = TOL_MM) -> dict[str, Any]:
    from p2p_threemf.ply import read_ply

    gs, gn = read_json(stored_dir / "geometry.json"), read_json(new_dir / "geometry.json")
    if not gs or not gn:
        return {"compared": False, "ok": None,
                "reason": "no geometry.json in " + ("the stored run" if not gs else "the rerun")}
    ps, pn = _parts(gs), _parts(gn)
    out: dict[str, Any] = {
        "compared": True, "tol_mm": tol_mm,
        "parts": sorted(pn), "parts_same": sorted(ps) == sorted(pn),
        "filaments_same": all(ps[n].get("filament_id") == pn[n].get("filament_id")
                              for n in ps if n in pn),
        "orientation_same": gs.get("orientation") == gn.get("orientation"),
        "supports_same": gs.get("supports") == gn.get("supports"),
        "notes_same": gs.get("notes") == gn.get("notes"),
        "warnings_same": gs.get("warnings") == gn.get("warnings"),
        "transform_max_dev": float(np.abs(np.asarray(gs["transform"])
                                          - np.asarray(gn["transform"])).max()),
        "meshes": {},
    }
    worst = 0.0
    faces_same = True
    for name in sorted(set(ps) & set(pn)):
        a = stored_dir / Path(ps[name]["mesh"]["path"]).name
        b = new_dir / Path(pn[name]["mesh"]["path"]).name
        if not a.is_file() or not b.is_file():
            out["meshes"][name] = {"missing": True}
            faces_same = False
            continue
        if sha256_file(a) == sha256_file(b):
            out["meshes"][name] = {"identical": True, "max_dev_mm": 0.0}
            continue
        va, fa = read_ply(a)
        vb, fb = read_ply(b)
        same_f = fa.shape == fb.shape and bool(np.array_equal(fa, fb))
        dev = float(np.abs(np.asarray(va) - np.asarray(vb)).max()) \
            if np.shape(va) == np.shape(vb) else float("inf")
        out["meshes"][name] = {"identical": False, "faces_same": same_f, "max_dev_mm": dev,
                               "faces": [len(fa), len(fb)]}
        faces_same &= same_f
        worst = max(worst, dev)
    out["max_vertex_dev_mm"] = worst
    out["faces_same"] = faces_same
    rs = read_json(stored_dir / "geometry_report.json") or {}
    rn = read_json(new_dir / "geometry_report.json") or {}
    out["report_diffs"] = diff_json(rs.get("metrics") or {}, rn.get("metrics") or {})[:40]
    out["ok"] = bool(out["parts_same"] and out["filaments_same"] and out["orientation_same"]
                     and out["supports_same"] and faces_same and worst <= tol_mm
                     and out["transform_max_dev"] <= tol_mm)
    return out
