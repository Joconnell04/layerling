"""Bambu Studio preset resolution and project_settings.config assembly.

Bambu's shipped profiles (Contents/Resources/profiles/BBL/{machine,process,filament}) are partial:
each preset names a parent in `inherits` and may pull templates through `include`. Bambu resolves
them at startup (PresetBundle::load_vendor_configs_from_json): parent config, then each include's
keys, then the preset's own keys.

A project file stores one merged config (PresetBundle::full_fff_config): C++ defaults, process,
machine, every filament key as the concatenation of each filament's array, then bookkeeping keys
(printer_settings_id, print_settings_id, filament_settings_id, filament_ids, filament_map, ...).

About 140 keys in a real project come from C++ defaults or GUI state, not from any preset JSON
(extruder_ams_count, extruder_nozzle_stats, nozzle_volume_type, curr_bed_type, ...). Without some
of them the X2D slice fails ("G-code in unprintable area of multi-extruder printers"). We take
those values from a project Bambu Studio itself saved for the same printer (the "project
template"), and drop preset keys Bambu no longer writes (legacy names like wall_infill_order).

The snapshot (data/bbl_profiles.json) holds the flattened X2D presets plus that template, so
writing is deterministic and does not need Bambu Studio installed. `refresh_snapshot()` rebuilds
it from the installed app and a Bambu-saved reference project.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

BAMBU_APP = Path("/Applications/BambuStudio.app")
APP_PROFILES = BAMBU_APP / "Contents/Resources/profiles"
SNAPSHOT = "bbl_profiles.json"

META_KEYS = {
    "version", "is_custom_defined", "name", "url", "type", "setting_id", "filament_id", "from",
    "description", "instantiation", "inherits", "include",
}

# Presets captured in the snapshot (X2D, 0.4 mm nozzle).
SNAPSHOT_MACHINES = ["Bambu Lab X2D 0.4 nozzle"]
SNAPSHOT_PROCESSES = [
    "0.08mm High Quality @BBL X2D",
    "0.12mm High Quality @BBL X2D",
    "0.16mm High Quality @BBL X2D",
    "0.16mm Standard @BBL X2D",
    "0.20mm High Quality @BBL X2D",
    "0.20mm Standard @BBL X2D",
    "0.24mm Standard @BBL X2D",
]
SNAPSHOT_FILAMENT_PREFIXES = ("Bambu PLA ", "Generic PLA", "Bambu PETG ", "Generic PETG")
SNAPSHOT_FILAMENT_SUFFIX = "@BBL X2D 0.4 nozzle"

# Process preset per layer height for the X2D 0.4 nozzle.
PROCESS_BY_LAYER = {
    0.08: "0.08mm High Quality @BBL X2D",
    0.12: "0.12mm High Quality @BBL X2D",
    0.16: "0.16mm Standard @BBL X2D",
    0.20: "0.20mm Standard @BBL X2D",
    0.24: "0.24mm Standard @BBL X2D",
    0.28: "0.24mm Standard @BBL X2D",  # no 0.28 preset for the 0.4 nozzle; layer_height set
}

DEFAULT_FILAMENT_PRESET = "Bambu PLA Basic @BBL X2D 0.4 nozzle"

# Keys the writer sets itself; never taken from the template.
BOOKKEEPING_KEYS = {
    "name", "from", "version", "printer_settings_id", "print_settings_id", "filament_settings_id",
    "filament_ids", "filament_colour", "filament_multi_colour", "filament_colour_type",
    "default_filament_colour", "filament_self_index", "filament_map_mode", "filament_map",
    "filament_nozzle_map", "filament_volume_map", "flush_volumes_matrix", "flush_volumes_vector",
    "flush_multiplier", "different_settings_to_system", "inherits_group", "printer_technology",
    "print_compatible_printers",
}


@dataclass(frozen=True)
class Preset:
    name: str
    type: str  # machine | process | filament
    setting_id: str | None
    filament_id: str | None
    config: dict[str, Any]


class ProfileError(LookupError):
    pass


# ------------------------------------------------------------------ app profile resolution


class AppProfiles:
    """Resolver over an installed Bambu Studio profiles directory."""

    def __init__(self, root: Path = APP_PROFILES, vendor: str = "BBL"):
        self.root = Path(root)
        index = json.loads((self.root / f"{vendor}.json").read_text())
        self.vendor_version = index.get("version")
        self.paths: dict[str, Path] = {}
        self.kinds: dict[str, str] = {}
        for key, kind in (("machine_model_list", "machine_model"), ("machine_list", "machine"),
                          ("process_list", "process"), ("filament_list", "filament")):
            for item in index.get(key, []):
                self.paths[item["name"]] = self.root / vendor / item["sub_path"]
                self.kinds[item["name"]] = kind
        self._raw: dict[str, dict] = {}
        self._resolved: dict[str, dict] = {}

    def raw(self, name: str) -> dict:
        if name not in self._raw:
            path = self.paths.get(name)
            if path is None or not path.exists():
                raise ProfileError(f"Bambu preset not found: {name!r}")
            self._raw[name] = json.loads(path.read_text())
        return self._raw[name]

    def resolve(self, name: str) -> Preset:
        raw = self.raw(name)
        cfg = self._flatten(name)
        filament_id = raw.get("filament_id")
        cur = raw
        while filament_id is None and "inherits" in cur:  # inherited through the chain
            cur = self.raw(cur["inherits"])
            filament_id = cur.get("filament_id")
        return Preset(name, raw.get("type", ""), raw.get("setting_id"), filament_id, cfg)

    def _flatten(self, name: str) -> dict:
        if name in self._resolved:
            return self._resolved[name]
        raw = self.raw(name)
        cfg: dict[str, Any] = dict(self._flatten(raw["inherits"])) if "inherits" in raw else {}
        includes = raw.get("include", [])
        if isinstance(includes, str):
            includes = [includes]
        for inc in includes:
            cfg.update({k: v for k, v in self.raw(inc).items() if k not in META_KEYS})
        cfg.update({k: v for k, v in raw.items() if k not in META_KEYS})
        self._resolved[name] = cfg
        return cfg

    def names(self, kind: str) -> list[str]:
        return sorted(n for n, k in self.kinds.items() if k == kind)


def app_installed() -> bool:
    return (APP_PROFILES / "BBL.json").exists()


def app_version() -> str | None:
    plist = BAMBU_APP / "Contents/Info.plist"
    if not plist.exists():
        return None
    import plistlib

    with plist.open("rb") as f:
        return plistlib.load(f).get("CFBundleShortVersionString")


# ------------------------------------------------------------------ project template


def read_project_config(path: str | Path) -> dict:
    """project_settings.config from a .3mf, or a bare JSON config file."""
    p = Path(path)
    if p.suffix.lower() == ".3mf":
        with zipfile.ZipFile(p) as z:
            return json.loads(z.read("Metadata/project_settings.config"))
    return json.loads(p.read_text())


def build_template(reference: dict, preset_keys: set[str], filament_keys: set[str]) -> dict:
    """Split a Bambu-saved project config into values presets do not provide.

    fixed: key -> value copied as is. per_filament: key -> one filament's slice (replicated per
    filament when writing), for per-filament keys no filament preset sets (some sit in process
    presets by mistake; Bambu drops them there). keys: every key Bambu wrote; anything else we
    produce is dropped."""
    n = len(reference.get("filament_colour") or [])
    if n < 3:
        raise ValueError("reference project needs at least 3 filaments to tell per-filament keys apart")
    machine_lengths = {len(reference.get("nozzle_diameter") or []),
                       len(reference.get("printer_extruder_variant") or [])}
    fixed: dict[str, Any] = {}
    per_filament: dict[str, Any] = {}
    for key, value in reference.items():
        if key in filament_keys or key in BOOKKEEPING_KEYS:
            continue
        if (isinstance(value, list) and len(value) >= n and len(value) % n == 0
                and len(value) not in machine_lengths):
            unit = len(value) // n
            per_filament[key] = value[:unit]
        elif key not in preset_keys:
            fixed[key] = value
    return {"keys": sorted(reference), "fixed": dict(sorted(fixed.items())),
            "per_filament": dict(sorted(per_filament.items()))}


# ------------------------------------------------------------------ snapshot


def build_snapshot(reference: str | Path, app: AppProfiles | None = None) -> dict:
    app = app or AppProfiles()
    filaments = [
        n for n in app.names("filament")
        if n.endswith(SNAPSHOT_FILAMENT_SUFFIX) and n.startswith(SNAPSHOT_FILAMENT_PREFIXES)
    ]
    presets = {}
    preset_keys: set[str] = set()
    for name in SNAPSHOT_MACHINES + SNAPSHOT_PROCESSES + filaments:
        p = app.resolve(name)
        presets[name] = {"type": p.type, "setting_id": p.setting_id,
                         "filament_id": p.filament_id, "config": p.config}
    ref = read_project_config(reference)
    filament_keys: set[str] = set()
    for name in [ref.get("printer_settings_id"), ref.get("print_settings_id")]:
        try:
            preset_keys |= set(app.resolve(name).config)
        except ProfileError:
            pass
    for name in ref.get("filament_settings_id") or []:
        try:
            filament_keys |= set(app.resolve(name).config)
        except ProfileError:
            pass
    template = build_template(ref, preset_keys | filament_keys, filament_keys)
    template["source"] = (f"Bambu Studio {ref.get('version')} project, "
                          f"{ref.get('printer_settings_id')}, "
                          f"{len(ref.get('filament_colour') or [])} filaments")
    return {
        "source": "BambuStudio.app/Contents/Resources/profiles/BBL",
        "vendor_version": app.vendor_version,
        "app_version": app_version(),
        "presets": presets,
        "project_template": template,
        "flush_data": app_flush_data(),
    }


def app_flush_data() -> dict | None:
    from .flush import app_flush_data as _afd

    return _afd()


def snapshot_path() -> Path:
    return Path(str(resources.files("p2p_threemf") / "data" / SNAPSHOT))


def refresh_snapshot(reference: str | Path) -> Path:
    path = snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_snapshot(reference), indent=1, sort_keys=True) + "\n")
    load_snapshot.cache_clear()
    return path


@cache
def load_snapshot() -> dict:
    path = snapshot_path()
    if not path.exists():
        return {"presets": {}, "project_template": None}
    return json.loads(path.read_text())


@cache
def _app() -> AppProfiles | None:
    return AppProfiles() if app_installed() else None


def get_preset(name: str) -> Preset:
    snap = load_snapshot()["presets"].get(name)
    if snap is not None:
        return Preset(name, snap["type"], snap["setting_id"], snap["filament_id"], snap["config"])
    app = _app()
    if app is not None:
        return app.resolve(name)
    raise ProfileError(f"preset {name!r} is not in the snapshot and Bambu Studio is not installed")


def has_preset(name: str) -> bool:
    try:
        get_preset(name)
        return True
    except ProfileError:
        return False


# ------------------------------------------------------------------ name helpers


def machine_name(printer: str, nozzle_mm: float) -> str:
    return f"{printer} {nozzle_mm:g} nozzle"


def process_name(layer_mm: float, nozzle_mm: float = 0.4) -> str:
    if abs(nozzle_mm - 0.4) > 1e-6:
        raise ProfileError("only the 0.4 mm nozzle has process presets mapped so far")
    key = round(layer_mm, 2)
    if key not in PROCESS_BY_LAYER:
        raise ProfileError(f"no X2D process preset for layer height {layer_mm} mm")
    return PROCESS_BY_LAYER[key]


def filament_preset_name(slicer_preset: str | None, nozzle_mm: float = 0.4) -> str:
    """Normalise a library preset name to an existing X2D preset.

    filaments/library.example.json says "Bambu PLA Basic @BBL X2D"; Bambu 2.08 only ships
    "... @BBL X2D 0.4 nozzle" for the X2D, so the nozzle suffix is added when missing."""
    if not slicer_preset:
        return DEFAULT_FILAMENT_PRESET
    if has_preset(slicer_preset):
        return slicer_preset
    with_nozzle = f"{slicer_preset} {nozzle_mm:g} nozzle"
    if has_preset(with_nozzle):
        return with_nozzle
    raise ProfileError(f"unknown filament preset {slicer_preset!r}")


# ------------------------------------------------------------------ project config

def coerce_value(value: Any, current: Any) -> Any:
    """PrintSettings.process_overrides values are strings. When the project key holds a list,
    "a,b,c" becomes ["a", "b", "c"] and a single value fills the list."""
    if isinstance(value, str) and isinstance(current, list):
        parts = [p.strip() for p in value.split(",")]
        if len(parts) == 1 and current:
            return parts * len(current)
        return parts
    return value


# Keys filament presets carry that a project does not keep per filament.
_FILAMENT_SKIP = {"compatible_prints", "compatible_printers", "compatible_prints_condition",
                  "compatible_printers_condition", "inherits"}


def build_project_config(
    machine: Preset,
    process: Preset,
    filaments: list[Preset],
    colours: list[str],
    version: str,
    template: dict | None = None,
    flush_matrix: list[float] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merged project config in the shape Bambu Studio writes (PresetBundle::full_fff_config)."""
    if len(colours) != len(filaments):
        raise ValueError("one colour per filament")
    if template is None:
        template = load_snapshot().get("project_template")
    n = len(filaments)
    out: dict[str, Any] = {}
    if template:
        out.update(template["fixed"])
    out.update(process.config)
    out.update(machine.config)

    per_filament_defaults = dict(template["per_filament"]) if template else {}
    cfgs = [f.config for f in filaments]
    keys = (set().union(*(c.keys() for c in cfgs)) | set(per_filament_defaults)) - _FILAMENT_SKIP
    for key in sorted(keys):
        vals = [c.get(key) for c in cfgs]
        present = [v for v in vals if v is not None]
        if not present:
            if key in per_filament_defaults:
                out[key] = per_filament_defaults[key] * n
            continue
        if all(isinstance(v, list) for v in present):
            fill = per_filament_defaults.get(key, present[0])
            merged: list = []
            for v in vals:
                merged.extend(v if v is not None else fill)
            out[key] = merged
        else:
            out[key] = present[0]

    self_index: list[str] = []
    for i, c in enumerate(cfgs):
        fev = c.get("filament_extruder_variant") or ["Direct Drive Standard"]
        self_index += [str(i + 1)] * len(fev)

    n_ext = len(machine.config.get("nozzle_diameter", ["0.4"]))
    if flush_matrix is None:
        from .flush import flush_matrix as _fm

        # Bambu's measured table for the nozzle type first, else its colour formula plus the
        # nozzle volume (Plater get_min_flush_volumes). Matches the GUI's matrix exactly.
        nozzle_volume = machine.config.get("nozzle_volume") or ["0"]
        dataset = int((machine.config.get("nozzle_flush_dataset") or ["0"])[0])
        flush_matrix = _fm(colours, min_flush=float(nozzle_volume[0]), dataset=dataset,
                           filament_ids=[f.filament_id or "" for f in filaments])
    matrix_one = [f"{v:g}" for v in flush_matrix]

    out.update({
        "name": "project_settings",
        "from": "project",
        "version": version,
        "printer_settings_id": machine.name,
        "print_settings_id": process.name,
        "filament_settings_id": [f.name for f in filaments],
        "filament_ids": [f.filament_id or "" for f in filaments],
        "filament_colour": list(colours),
        "filament_multi_colour": list(colours),
        "filament_colour_type": ["1"] * n,
        "default_filament_colour": [""] * n,
        "filament_self_index": self_index,
        "filament_map_mode": "Auto For Flush",
        "filament_map": ["1"] * n,
        "filament_nozzle_map": ["0"] * n,
        "filament_volume_map": ["0"] * n,
        "flush_volumes_matrix": matrix_one * n_ext,
        "flush_volumes_vector": ["140"] * (2 * n),
        "flush_multiplier": ["1"] * n_ext,
        "different_settings_to_system": [""] * (n + 2),
        "printer_technology": "FFF",
    })
    if process.config.get("compatible_printers"):
        out["print_compatible_printers"] = process.config["compatible_printers"]
    if overrides:
        overrides = {k: coerce_value(v, out.get(k)) for k, v in overrides.items()}
        out.update(overrides)
        # Record changed process/printer keys the way Bambu does, so the GUI shows them as
        # edits to the system preset: [process, filament 1..n, printer], ';'-joined keys.
        diff = out["different_settings_to_system"]
        changed_p = sorted(k for k, v in overrides.items()
                           if k in process.config and process.config[k] != v)
        changed_m = sorted(k for k, v in overrides.items()
                           if k in machine.config and machine.config[k] != v)
        diff[0] = ";".join(changed_p)
        diff[-1] = ";".join(changed_m)
    if template:
        allowed = set(template["keys"]) | BOOKKEEPING_KEYS | set(overrides or {})
        out = {k: v for k, v in out.items() if k in allowed}
    for key in _FILAMENT_SKIP:  # full_fff_config erases these
        out.pop(key, None)
    return dict(sorted(out.items()))
