"""Analysis of a MoveTable (time, grams, counts, per-layer filaments) and the two report shapes:
our SliceReport contract and Bambu's CLI result.json.

`analyze_gcode` works on Bambu's G-code (a .gcode file or a .gcode.3mf) and on ours, so the
inspector and the parity tools compute every number the same way for both.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from p2p_contracts.reports import SliceFilament, SliceReport

from . import __version__
from .estimate import TimeResult, estimate
from .gcode_read import config_floats, parse_list, read_gcode
from .moves import Kind, MoveTable, Role
from .usage import Usage, usage

RESULT_ROLES = ("Bottom surface", "Bridge", "Custom", "Flush", "Gap infill", "Inner wall",
                "Internal solid infill", "Outer wall", "Overhang wall", "Prime tower",
                "Sparse infill", "Support", "Support interface", "Support transition",
                "Top surface", "Undefined", "Brim", "Skirt", "Ironing")


@dataclass
class Analysis:
    moves: MoveTable
    time: TimeResult
    usage: Usage
    config: dict[str, str]
    filament_maps: list[int]                     # 1-based nozzle per slot (slice_info shape)
    filament_ids: list[str]                      # Bambu filament id per slot (GFL99 ...)
    layer_z: list[float]
    layer_filaments: list[list[int]]             # per layer (0-based), slots with model material
    source: str = "gcode"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def layer_count(self) -> int:
        return len(self.layer_z)


def _layer_filaments(mt: MoveTable) -> list[list[int]]:
    rows = mt.rows
    n_layers = mt.layer_count
    ext = (rows["kind"] == Kind.EXTRUDE) & (rows["e"] > 0) & (rows["role"] != Role.PRIME_TOWER) \
        & (rows["layer"] > 0) & (rows["slot"] > 0)
    out: list[set[int]] = [set() for _ in range(n_layers)]
    for lay, s in set(zip(rows["layer"][ext].tolist(), rows["slot"][ext].tolist())):
        out[lay - 1].add(int(s))
    return [sorted(s) for s in out]


def analyze(mt: MoveTable, cfg: dict[str, str] | None = None) -> Analysis:
    cfg = cfg if cfg is not None else mt.meta.get("config", {})
    t = estimate(mt, cfg)
    u = usage(mt, cfg)
    fm = [int(v) for v in config_floats(cfg, "filament_map")] if cfg.get("filament_map") else []
    if not fm and mt.meta.get("nozzle_of"):
        nz = mt.meta["nozzle_of"]
        fm = [nz.get(s, 0) + 1 for s in range(1, max(nz) + 1)]
    ids = parse_list(cfg.get("filament_ids", "")) if cfg.get("filament_ids") else []
    lz = [float(z) for z in mt.meta.get("layer_z", [])]
    return Analysis(moves=mt, time=t, usage=u, config=cfg, filament_maps=fm, filament_ids=ids,
                    layer_z=lz, layer_filaments=_layer_filaments(mt),
                    source=mt.meta.get("source", "gcode"))


def load_gcode_bytes(path: str | Path) -> bytes:
    """G-code bytes from a .gcode file or a (sliced) .gcode.3mf."""
    p = Path(path)
    if p.suffix == ".3mf" or zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as z:
            names = [n for n in z.namelist() if n.startswith("Metadata/plate_") and
                     n.endswith(".gcode")]
            if not names:
                raise FileNotFoundError(f"{p}: no Metadata/plate_N.gcode inside")
            return z.read(min(names))
    return p.read_bytes()


def analyze_gcode(path: str | Path) -> Analysis:
    return analyze(read_gcode(load_gcode_bytes(path)))


# ----------------------------------------------------------------------------- report shapes


def cli_result_json(a: Analysis, objects: list[dict] | None = None,
                    triangle_count: int | None = None) -> dict:
    """Bambu CLI result.json shape (one plate), from our numbers."""
    t = a.time
    ft = {k: float(v) for k, v in t.feature_type_times().items()}
    fils = []
    for slot, fu in a.usage.per_slot.items():
        if fu.total_mm3 <= 1e-9:
            continue
        fils.append({
            "filament_id": a.filament_ids[slot - 1] if slot - 1 < len(a.filament_ids) else "",
            "id": slot,
            "main_used_g": fu.grams("model") + fu.grams("support"),
            "total_used_g": fu.grams(),
        })
    plate: dict[str, Any] = {
        "feature_type_times": dict(sorted(ft.items())),
        "filament_change_times": a.usage.ams_swaps,
        "filaments": fils,
        "id": 1,
        "main_predication": t.main_s,
        "total_predication": t.total_s,
        "objects": objects or [],
        "warning_message": "",
    }
    if triangle_count is not None:
        plate["triangle_count"] = triangle_count
    return {"error_string": "Success.", "plate_index": 1, "return_code": 0,
            "sliced_plates": [plate], "slicer": "p2p_slicer", "slicer_version": __version__}


def slice_report(a: Analysis, *, printer_profile: str | None = None,
                 process_profile: str | None = None, expected_slots: int | None = None,
                 filament_ids: list[str] | None = None, ok: bool = True,
                 errors: list[str] | None = None, warnings: list[str] | None = None,
                 log: str | None = None) -> SliceReport:
    """Our SliceReport contract, filled from an Analysis. `filament_ids` are the project's
    library ids per slot (the gate reports those); Bambu ids are the fallback."""
    ids = filament_ids or a.filament_ids
    fils = []
    for slot, fu in a.usage.per_slot.items():
        if fu.total_mm3 <= 1e-9:
            continue
        fils.append(SliceFilament(
            slot=slot,
            filament_id=ids[slot - 1] if slot - 1 < len(ids) else None,
            grams=round(fu.grams(), 4),
            meters=round(fu.metres(), 4),
            model_grams=round(fu.grams("model") + fu.grams("support"), 4),
            flush_grams=round(fu.grams("flush"), 4),
            tower_grams=round(fu.grams("tower"), 4),
        ))
    u = a.usage
    t = a.time
    errs = list(errors or [])
    slots_used = len(u.slots_used)
    if expected_slots is not None and slots_used != expected_slots and ok:
        ok = False
        errs.append(f"{slots_used} filaments print, expected {expected_slots}")
    return SliceReport(
        ok=ok,
        slicer="p2p_slicer",
        slicer_version=__version__,
        printer_profile=printer_profile or a.config.get("printer_settings_id", ""),
        process_profile=process_profile or a.config.get("print_settings_id", ""),
        errors=errs,
        warnings=list(warnings or []),
        print_time_s=round(t.total_s, 1),
        filaments=fils,
        total_grams=round(u.total_g, 4),
        purge_grams=round(u.purge_g, 4),
        slots_used=slots_used,
        expected_slots=expected_slots,
        log=log,
        filament_changes=u.filament_changes,
        nozzle_changes=u.nozzle_switches,
        ams_swaps=u.ams_swaps,
        model_time_s=round(t.model_s, 1),
        prepare_time_s=round(t.prepare_s, 1),
        tower_grams=round(u.bucket_g("tower"), 4),
    )


def layer_filament_lists(a: Analysis) -> list[dict[str, str]]:
    """slice_info `layer_filament_lists` shape: 0-based filaments per inclusive layer range."""
    out: list[dict[str, str]] = []
    prev: list[int] | None = None
    start = 0
    lists = a.layer_filaments
    for i, fl in enumerate(lists + [None]):  # type: ignore[operator]
        if fl != prev:
            if prev is not None and prev:
                out.append({"filament_list": " ".join(str(s - 1) for s in prev),
                            "layer_ranges": f"{start} {i - 1}"})
            prev = fl
            start = i
    return out


def write_result_json(a: Analysis, path: str | Path, **kw: Any) -> Path:
    p = Path(path)
    p.write_text(json.dumps(cli_result_json(a, **kw), indent=4, sort_keys=True))
    return p


def bambu_result(path: str | Path) -> dict:
    """Bambu's reference numbers for a sliced case: a folder with result.json (and optionally
    sliced.gcode.3mf / plate_1.gcode), or a .gcode.3mf with result.json next to it."""
    p = Path(path)
    folder = p if p.is_dir() else p.parent
    out: dict[str, Any] = {}
    rj = folder / "result.json"
    if rj.exists():
        out["result"] = json.loads(rj.read_text())
    s3 = p if p.suffix == ".3mf" else folder / "sliced.gcode.3mf"
    if s3.exists():
        with zipfile.ZipFile(s3) as z:
            if "Metadata/slice_info.config" in z.namelist():
                out["slice_info"] = z.read("Metadata/slice_info.config").decode()
            if "Metadata/filament_sequence.json" in z.namelist():
                out["filament_sequence"] = json.loads(z.read("Metadata/filament_sequence.json"))
    return out


def _np_default(o: Any) -> Any:
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=1, default=_np_default)


__all__ = [
    "Analysis",
    "analyze",
    "analyze_gcode",
    "bambu_result",
    "cli_result_json",
    "layer_filament_lists",
    "load_gcode_bytes",
    "slice_report",
    "write_result_json",
]
