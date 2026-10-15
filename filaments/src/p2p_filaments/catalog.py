"""filaments/catalog.json: every single-color Bambu Lab filament with an X2D preset, as a
FilamentLibrary of unowned spools (owned false, spools 0), for palette picks and suggestions.

Built from the vendored Bambu Studio files in filaments/vendor/bambu/ (see its README):

- filaments_color_codes.json gives the colors: fila_id, fila_type (the line), the English
  color name and the hex (alpha dropped). Only single-color entries (fila_color_type 单色);
  gradients and multi-color spools do not fit one slot.
- x2d_filament_presets.json gives slicer_preset: the "@BBL X2D 0.4 nozzle" preset whose
  filament_id equals fila_id. A color whose line has no such preset is skipped.

Support materials (PVA, "Support for ...") are skipped: they are not model colors.
Lab is computed from the hex (CIELAB D65, sRGB IEC 61966-2-1), lab_source "nominal".
Ids are stable slugs, bambu-<line>-<color>, e.g. bambu-pla-basic-jade-white. An id that equals
an owned filament id (library.json, the live owned rows) is skipped: the owned row covers it.
Order: PRIORITY_LINES first, then the other lines by name; colors keep Bambu's order.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from p2p_contracts import Filament, FilamentLibrary

from .library import REPO

VENDOR = REPO / "filaments" / "vendor" / "bambu"
COLORS = VENDOR / "filaments_color_codes.json"
PRESETS = VENDOR / "x2d_filament_presets.json"
DEFAULT_CATALOG = REPO / "filaments" / "catalog.json"
OWNED_LIBRARY = REPO / "filaments" / "library.json"

SINGLE_COLOR = "单色"
PRESET_SUFFIX = "@BBL X2D 0.4 nozzle"
PRIORITY_LINES = ("PLA Basic", "PLA Matte", "PETG Basic", "PETG HF")
SKIP_LINE = re.compile(r"^(PVA|Support\b)", re.IGNORECASE)
BRAND = "Bambu Lab"


def slug(text: str) -> str:
    """Lowercase [a-z0-9-] slug; '+' becomes 'plus' so PLA Silk+ and PLA Silk stay apart."""
    s = text.lower().replace("+", " plus ")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def filament_id(line: str, name: str) -> str:
    return f"bambu-{slug(line)}-{slug(name)}"


def x2d_presets(presets: dict) -> dict[str, dict]:
    """fila_id -> the X2D 0.4 nozzle preset record."""
    return {p["filament_id"]: p for p in presets["presets"] if p["name"].endswith(PRESET_SUFFIX)}


def line_order(lines: set[str]) -> list[str]:
    first = [x for x in PRIORITY_LINES if x in lines]
    return first + sorted(lines - set(first))


def build_catalog(colors: dict, presets: dict, owned_ids: set[str],
                  updated_at: datetime | None = None) -> tuple[FilamentLibrary, dict]:
    """Returns (catalog, report). report: counts per line and every skip with its reason."""
    from .colour import hex_to_lab

    by_fila = x2d_presets(presets)
    skipped: dict[str, int] = {"not single color": 0, "no X2D preset": 0, "support material": 0,
                               "owned id": 0, "duplicate id": 0}
    owned_hits: list[str] = []
    by_line: dict[str, list[Filament]] = {}
    seen: set[str] = set()
    for e in colors["data"]:
        if e.get("fila_color_type") != SINGLE_COLOR:
            skipped["not single color"] += 1
            continue
        line = e["fila_type"].strip()
        if SKIP_LINE.match(line):
            skipped["support material"] += 1
            continue
        preset = by_fila.get(e["fila_id"])
        if preset is None:
            skipped["no X2D preset"] += 1
            continue
        name = e["fila_color_name"]["en"].strip()
        fid = filament_id(line, name)
        if fid in owned_ids:
            skipped["owned id"] += 1
            owned_hits.append(fid)
            continue
        if fid in seen:
            skipped["duplicate id"] += 1
            continue
        seen.add(fid)
        hx = "#" + e["fila_color"][0].lstrip("#")[:6].upper()
        lab = tuple(round(float(x), 2) + 0.0 for x in hex_to_lab(hx))
        by_line.setdefault(line, []).append(Filament(
            id=fid, brand=BRAND, line=line, name=name, material=preset["filament_type"],
            hex=hx, lab=lab, lab_source="nominal", owned=False, spools=0,
            bambu_filament_id=e["fila_id"], slicer_preset=preset["name"],
            notes=f"Bambu color code {e['fila_color_code']}"))
    order = line_order(set(by_line))
    fils = [f for line in order for f in by_line[line]]
    lib = FilamentLibrary(updated_at=updated_at or datetime.now(UTC), filaments=fils)
    report = {"total": len(fils), "per_line": {line: len(by_line[line]) for line in order},
              "skipped": skipped, "owned_ids_skipped": owned_hits}
    return lib, report


def owned_ids_from(path: Path) -> set[str]:
    """Owned filament ids in a FilamentLibrary file (library.json)."""
    try:
        lib = FilamentLibrary.model_validate_json(Path(path).read_text())
    except (OSError, ValueError):
        return set()
    return {f.id for f in lib.filaments if f.owned}


def _dump(lib: FilamentLibrary) -> str:
    return json.dumps(lib.model_dump(mode="json"), indent=1, ensure_ascii=False) + "\n"


def write_catalog(out: Path = DEFAULT_CATALOG, *, colors: Path = COLORS, presets: Path = PRESETS,
                  owned: set[str] | None = None) -> dict:
    """Build and write the catalog. Rerunnable: an unchanged catalog keeps its updated_at, so
    the file only changes when the vendor data or the owned ids do."""
    owned = owned_ids_from(OWNED_LIBRARY) if owned is None else owned
    lib, report = build_catalog(json.loads(Path(colors).read_text()),
                                json.loads(Path(presets).read_text()), owned)
    out = Path(out)
    try:
        old = FilamentLibrary.model_validate_json(out.read_text())
    except (OSError, ValueError):
        old = None
    if old is not None and old.filaments == lib.filaments:
        lib = lib.model_copy(update={"updated_at": old.updated_at})
        report["changed"] = False
    else:
        report["changed"] = True
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_dump(lib))
    report["path"] = str(out)
    return report


def load_catalog(path: Path = DEFAULT_CATALOG) -> FilamentLibrary:
    return FilamentLibrary.model_validate_json(Path(path).read_text())
