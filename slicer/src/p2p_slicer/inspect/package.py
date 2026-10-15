"""Read what the inspector needs from a project 3MF, a sliced .gcode.3mf (Bambu's or ours) or a
bare .gcode file. Nothing here slices, parses moves or estimates time."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# What Bambu Studio writes into a sliced file (docs/research/native-slicer.md section 16), besides
# one 3D/Objects/object_<n>.model per object.
BAMBU_SLICED_ENTRIES = (
    "[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model", "3D/_rels/3dmodel.model.rels",
    "Metadata/model_settings.config", "Metadata/_rels/model_settings.config.rels",
    "Metadata/project_settings.config", "Metadata/plate_1.gcode", "Metadata/plate_1.gcode.md5",
    "Metadata/slice_info.config", "Metadata/plate_1.json", "Metadata/filament_sequence.json",
    "Metadata/cut_information.xml", "Metadata/plate_1.png", "Metadata/plate_1_small.png",
    "Metadata/plate_no_light_1.png", "Metadata/top_1.png", "Metadata/pick_1.png",
)
THUMBNAILS = ("plate_1.png", "plate_no_light_1.png", "top_1.png", "plate_1_small.png",
              "pick_1.png")
_GCODE_RE = re.compile(r"Metadata/plate_(\d+)\.gcode")


@dataclass
class SliceInfoPlate:
    """Metadata/slice_info.config, plate 1, everything the GUI shows from it."""
    meta: dict[str, str] = field(default_factory=dict)
    filaments: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    nozzles: list[dict[str, str]] = field(default_factory=list)
    layer_filament_lists: list[dict[str, Any]] = field(default_factory=list)
    client_version: str | None = None


@dataclass
class Package:
    path: Path
    kind: str  # "gcode.3mf" (sliced), "3mf" (project), "gcode" (bare G-code)
    names: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)  # project settings, lists as lists
    slice_info: SliceInfoPlate | None = None
    plate_json: dict[str, Any] = field(default_factory=dict)
    sequence: dict[str, Any] = field(default_factory=dict)  # filament_sequence.json plate_1
    objects: list[dict[str, Any]] = field(default_factory=list)  # model_settings objects
    plate_settings: dict[str, str] = field(default_factory=dict)  # model_settings plate meta
    gcode: str | None = None
    gcode_name: str | None = None
    gcode_md5_ok: bool | None = None
    gcode_header: dict[str, str] = field(default_factory=dict)
    gcode_config: dict[str, str] = field(default_factory=dict)
    thumbnails: dict[str, bytes] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)  # Bambu result.json, if found
    result_path: Path | None = None
    application: str | None = None

    @property
    def sliced(self) -> bool:
        return self.gcode is not None

    def cfg(self, key: str, default: Any = None) -> Any:
        """Project config value; the G-code config block when the project has none."""
        if key in self.config:
            return self.config[key]
        if key in self.gcode_config:
            return self.gcode_config[key]
        return default

    def cfg_list(self, key: str) -> list[str]:
        v = self.cfg(key)
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x) for x in v]
        return split_list(str(v))

    def cfg_float(self, key: str, default: float | None = None, index: int = 0) -> float | None:
        vals = self.cfg_list(key)
        try:
            return float(str(vals[index]).rstrip("%")) if len(vals) > index else default
        except ValueError:
            return default


def split_list(value: str) -> list[str]:
    """Bambu config list: "a,b,c" or quoted '"x";"y"' (filament_settings_id in the G-code)."""
    v = value.strip()
    if v.startswith('"'):
        return [p.strip().strip('"') for p in re.split(r'";\s*"', v)]
    if ";" in v:
        return [p.strip() for p in v.split(";")]
    return [p.strip() for p in v.split(",")] if v else []


def gcode_blocks(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """HEADER_BLOCK and CONFIG_BLOCK of Bambu-dialect G-code as dicts of strings."""
    header: dict[str, str] = {}
    config: dict[str, str] = {}
    mode = None
    for line in text.splitlines():
        if not line.startswith(";"):
            continue
        s = line.strip()
        if s == "; HEADER_BLOCK_START":
            mode = "h"
        elif s == "; CONFIG_BLOCK_START":
            mode = "c"
        elif s == "; HEADER_BLOCK_END":
            mode = None
        elif s in ("; CONFIG_BLOCK_END", "; EXECUTABLE_BLOCK_START"):
            break
        elif mode == "h":
            body = s[1:].strip()
            if body.startswith("model printing time:"):
                m = re.search(r"model printing time: ([^;]+); total estimated time: (.+)$", body)
                if m:
                    header["model printing time"] = m.group(1).strip()
                    header["total estimated time"] = m.group(2).strip()
            elif ":" in body:
                k, _, v = body.partition(":")
                header[k.strip()] = v.strip()
            elif body:
                header.setdefault("generator", body)
        elif mode == "c":
            k, sep, v = s[2:].partition(" = ")
            if sep:
                config[k.strip()] = v
    return header, config


def _slice_info(data: bytes) -> SliceInfoPlate:
    root = ET.fromstring(data)
    out = SliceInfoPlate()
    for item in root.iter("header_item"):
        if item.get("key") == "X-BBL-Client-Version":
            out.client_version = item.get("value")
    for p in root.iter("plate"):
        meta = {m.get("key"): m.get("value") or "" for m in p.findall("metadata") if m.get("key")}
        if meta.get("index") not in (None, "1"):
            continue
        out.meta = meta
        out.filaments = [dict(f.attrib) for f in p.findall("filament")]
        out.warnings = [dict(w.attrib) for w in p.findall("warning")]
        out.nozzles = [dict(n.attrib) for n in p.findall("nozzle")]
        for lst in p.iter("layer_filament_list"):
            rng = [int(x) for x in (lst.get("layer_ranges") or "").split() if x.isdigit()]
            fl = [int(x) + 1 for x in (lst.get("filament_list") or "").split() if x.isdigit()]
            if len(rng) == 2:
                out.layer_filament_lists.append({"first": rng[0], "last": rng[1], "slots": fl})
        break
    return out


def _model_settings(data: bytes) -> tuple[list[dict[str, Any]], dict[str, str]]:
    root = ET.fromstring(data)
    objects = []
    for o in root.findall("object"):
        meta = {m.get("key"): m.get("value") for m in o.findall("metadata") if m.get("key")}
        faces = next((m.get("face_count") for m in o.findall("metadata") if m.get("face_count")),
                     None)
        parts = []
        for p in o.findall("part"):
            pm = {m.get("key"): m.get("value") for m in p.findall("metadata") if m.get("key")}
            ms = p.find("mesh_stat")
            parts.append({
                "id": int(p.get("id") or 0), "name": pm.get("name") or "",
                "subtype": p.get("subtype") or "normal_part",
                "filament": int(pm["extruder"]) if (pm.get("extruder") or "").isdigit() else None,
                "faces": int(ms.get("face_count")) if ms is not None and ms.get("face_count")
                else None})
        objects.append({"id": int(o.get("id") or 0), "name": meta.get("name") or "",
                        "filament": int(meta["extruder"]) if (meta.get("extruder") or "")
                        .isdigit() else 1,
                        "faces": int(faces) if faces and faces.isdigit() else None,
                        "parts": parts})
    plate: dict[str, str] = {}
    for p in root.findall("plate"):
        plate = {m.get("key"): m.get("value") or "" for m in p.findall("metadata") if m.get("key")}
        break
    return objects, plate


def _find_result(path: Path) -> Path | None:
    """result.json next to the file (the Bambu CLI's output folder), or the copy slice_check
    keeps as <stem>.slice_result.json."""
    stem = path.name
    for suffix in (".gcode.3mf", ".3mf", ".gcode"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    for cand in (path.parent / f"{stem}.slice_result.json", path.parent / "result.json"):
        if cand.is_file():
            return cand
    return None


def open_package(path: str | Path, result: str | Path | None = None) -> Package:
    """Read a .3mf / .gcode.3mf / .gcode. `result` is Bambu's result.json (found next to the
    file when not given)."""
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".gcode":
        text = path.read_text(errors="replace")
        pkg = Package(path=path, kind="gcode", gcode=text, gcode_name=path.name)
    else:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            pkg = Package(path=path, kind="3mf", names=names)
            if "Metadata/project_settings.config" in names:
                try:
                    pkg.config = json.loads(z.read("Metadata/project_settings.config"))
                except json.JSONDecodeError:
                    pkg.config = {}
            if "Metadata/slice_info.config" in names:
                pkg.slice_info = _slice_info(z.read("Metadata/slice_info.config"))
            if "Metadata/plate_1.json" in names:
                pkg.plate_json = json.loads(z.read("Metadata/plate_1.json"))
            if "Metadata/filament_sequence.json" in names:
                seq = json.loads(z.read("Metadata/filament_sequence.json"))
                pkg.sequence = seq.get("plate_1") or {}
            if "Metadata/model_settings.config" in names:
                pkg.objects, pkg.plate_settings = _model_settings(
                    z.read("Metadata/model_settings.config"))
            gname = next((n for n in names if _GCODE_RE.fullmatch(n)), None)
            if gname:
                raw = z.read(gname)
                pkg.kind = "gcode.3mf"
                pkg.gcode = raw.decode("utf-8", "replace")
                pkg.gcode_name = gname
                md5_name = gname + ".md5"
                if md5_name in names:
                    want = z.read(md5_name).decode().strip().upper()
                    pkg.gcode_md5_ok = hashlib.md5(raw).hexdigest().upper() == want
            for n in THUMBNAILS:
                if f"Metadata/{n}" in names:
                    pkg.thumbnails[n] = z.read(f"Metadata/{n}")
            m = re.search(rb'<metadata name="Application">([^<]*)<',
                          z.read("3D/3dmodel.model")[:4096]) if "3D/3dmodel.model" in names \
                else None
            pkg.application = m.group(1).decode() if m else None
    if pkg.gcode is not None:
        pkg.gcode_header, pkg.gcode_config = gcode_blocks(pkg.gcode)
    rpath = Path(result).resolve() if result else _find_result(path)
    if rpath is not None and rpath.is_file():
        try:
            pkg.result = json.loads(rpath.read_text())
            pkg.result_path = rpath
        except json.JSONDecodeError:
            pass
    return pkg


def package_check(pkg: Package) -> dict[str, Any]:
    """Completeness of a sliced file against what Bambu writes (section 16), and the G-code
    checksum. For a project file: whether it carries our sidecar and a config."""
    if pkg.kind == "gcode":
        return {"kind": "gcode", "entries": 0, "missing": [], "gcode_md5_ok": None}
    missing = []
    if pkg.kind == "gcode.3mf":
        missing = [n for n in BAMBU_SLICED_ENTRIES if n not in pkg.names]
        if not any(n.startswith("3D/Objects/") for n in pkg.names):
            missing.append("3D/Objects/object_1.model")
    return {"kind": pkg.kind, "entries": len(pkg.names), "missing": missing,
            "gcode_md5_ok": pkg.gcode_md5_ok,
            "thumbnails": sorted(pkg.thumbnails),
            "has_p2p_sidecar": "Metadata/p2p_threemf.json" in pkg.names,
            "has_config": bool(pkg.config) or bool(pkg.gcode_config),
            "application": pkg.application}
