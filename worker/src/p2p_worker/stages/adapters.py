"""Calls into sibling workstreams (p2p_geometry, p2p_threemf) with the pinned signatures in
contracts/src/p2p_contracts/geometry.py, plus the inputs they need."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from p2p_contracts import FilamentLibrary

from ..util import read_json


def find(module: str, name: str) -> Callable[..., Any] | None:
    """The function if the sibling package already ships it, else None."""
    try:
        mod = importlib.import_module(module)
    except ImportError:
        return None
    fn = getattr(mod, name, None)
    return fn if callable(fn) else None


def load_figure(glb: Path):
    """Meshy GLB -> (vertices float64 (n, 3), faces int64 (m, 3)) in Meshy's glTF frame, node
    transforms applied, triangles in Meshy's order (p2p_geometry's own loader when present)."""
    import numpy as np

    try:
        from p2p_geometry.meshio import load_meshy_glb
    except ImportError:
        import trimesh

        mesh = trimesh.load(glb, file_type="glb", force="mesh", process=False)
    else:
        mesh = load_meshy_glb(glb)
    return np.asarray(mesh.vertices, dtype=np.float64), np.asarray(mesh.faces, dtype=np.int64)


FILAMENT_FIELDS = ("id", "brand", "line", "name", "material", "hex", "lab_source", "td_mm",
                   "owned", "spools", "ams_slot", "bambu_filament_id", "slicer_preset",
                   "density_g_cm3", "notes")


def library_from_rows(rows: list[dict[str, Any]]) -> FilamentLibrary:
    """Supabase `filaments` rows -> FilamentLibrary (lab_l/a/b -> lab, swatch_path -> swatch)."""
    from ..util import utcnow

    fils = []
    for r in rows:
        d = {k: r[k] for k in FILAMENT_FIELDS if r.get(k) is not None}
        d["lab"] = (float(r["lab_l"]), float(r["lab_a"]), float(r["lab_b"]))
        if r.get("swatch_path"):
            d["swatch"] = r["swatch_path"]
        fils.append(d)
    return FilamentLibrary.model_validate({"updated_at": utcnow().isoformat(),
                                          "filaments": fils})


def library_for(store, job_id: str, repo_root: Path) -> tuple[FilamentLibrary | None, str]:
    """The filament library for a job: the owner's Supabase rows when the store has them,
    else filaments/library.json. Returns (library, source)."""
    try:
        rows = store.filament_rows(job_id)
    except Exception:  # noqa: BLE001 fall back to the file; the source says which was used
        rows = None
    if rows:
        try:
            return library_from_rows(rows), "supabase"
        except ValueError:
            pass
    return load_library(repo_root), "filaments/library.json"


def load_library(repo_root: Path) -> FilamentLibrary | None:
    """filaments/library.json as a FilamentLibrary, or None if missing or invalid."""
    path = Path(repo_root) / "filaments" / "library.json"
    data = read_json(path)
    if not data:
        return None
    try:
        return FilamentLibrary.model_validate(data)
    except ValueError:
        return None
