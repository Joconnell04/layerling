"""Bambu Studio project 3MF writer with per-triangle filament paint and multi-part objects.

Layout matches what Bambu Studio 2.08 saves (see docs/research/bambu-3mf.md):

  [Content_Types].xml
  _rels/.rels
  3D/3dmodel.model                 build + one assembly object with a component per part
  3D/_rels/3dmodel.model.rels
  3D/Objects/object_1.model        one mesh object per part; paint_color on painted triangles
  Metadata/model_settings.config   object name + default extruder, parts (+ part extruders), plate 1
  Metadata/project_settings.config full merged printer + process + filament config
  Metadata/slice_info.config       client header
  Metadata/filament_sequence.json  empty plate sequence (Bambu writes it)
  Metadata/p2p_threemf.json        our sidecar: palette ids, slot counts (Bambu ignores it)

One printed object, several parts. The first part is the painted figure; add-on parts (keyring
loop, plate, raised text, base) can carry a whole-part filament (Bambu part-level `extruder`)
and no paint. Overlapping parts are fine: the slicer unions the parts of an object.

Output is deterministic: fixed zip timestamps and order, no dates, UUIDs derived from content.
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from p2p_contracts import Filament, PrintSettings

from . import __version__
from .encoding import MAX_STATE, encode_state
from .profiles import (
    build_project_config,
    filament_preset_name,
    get_preset,
    load_snapshot,
    machine_name,
    process_name,
)

BED_CENTER = (128.0, 128.0)  # X2D printable_area 0x0..256x256
CLIENT_VERSION = "02.08.02.61"  # Bambu Studio build these files are modelled on
ZIP_DATE = (1980, 1, 1, 0, 0, 0)

_NS = (
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
    'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
    'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"'
)
_REL_3DMODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
# Bambu's UUID scheme: <backup_id << 16 | part index, 8 hex digits> + fixed suffix.
_SUB_OBJECT_SUFFIX = "-81cb-4c03-9d28-80fed5dfa1dc"
_COMPONENT_SUFFIX = "-b206-40ff-9872-83e8017abed1"

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="png" ContentType="image/png"/>
 <Default Extension="gcode" ContentType="text/x.gcode"/>
</Types>
"""

ROOT_RELS = f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="{_REL_3DMODEL}"/>
</Relationships>
"""

MODEL_RELS = f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/Objects/object_1.model" Id="rel-1" Type="{_REL_3DMODEL}"/>
</Relationships>
"""

SLICE_INFO = f"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header>
    <header_item key="X-BBL-Client-Type" value="slicer"/>
    <header_item key="X-BBL-Client-Version" value="{CLIENT_VERSION}"/>
  </header>
</config>
"""

FILAMENT_SEQUENCE = '{"plate_1":{"nozzle_sequence":[],"optimal_assignment":[],"sequence":[]}}'


@dataclass
class Part:
    """One part of the printed object, in the same frame as the figure (mm, Z up).

    Give either face_slots (per-triangle paint, 1-based palette slots) or slot (whole part in
    one filament, written as the part's extruder). subtype "negative_part" makes a cutter
    (e.g. a magnet pocket) that takes neither."""

    vertices: Any
    faces: Any
    face_slots: Any = None
    slot: int | None = None
    name: str = "part"
    subtype: str = "normal_part"


SUBTYPES = {"normal_part", "negative_part", "modifier_part", "support_enforcer",
            "support_blocker"}


@dataclass
class _Prepared:
    name: str
    v: np.ndarray
    f: np.ndarray
    s: np.ndarray  # per-face slot (0 for parts that are not printed)
    extruder: int | None  # part-level extruder written to model_settings (None = object's)
    subtype: str = "normal_part"


def _xml_attr(s: str) -> str:
    return (s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _fmt(v: float) -> str:
    return format(float(v), ".9g")


def _check_mesh(vertices, faces, what: str) -> tuple[np.ndarray, np.ndarray]:
    v = np.asarray(vertices, dtype=np.float64)
    f = np.asarray(faces)
    if v.ndim != 2 or v.shape[1] != 3 or len(v) == 0:
        raise ValueError(f"{what}: vertices must be (N, 3)")
    if f.ndim != 2 or f.shape[1] != 3 or len(f) == 0:
        raise ValueError(f"{what}: faces must be (M, 3)")
    if not np.issubdtype(f.dtype, np.integer):
        raise ValueError(f"{what}: faces must be integers")
    if not np.all(np.isfinite(v)):
        raise ValueError(f"{what}: vertices contain NaN or inf")
    if f.min() < 0 or f.max() >= len(v):
        raise ValueError(f"{what}: face index out of range")
    return v, f.astype(np.int64)


def _check_slots(face_slots, n_faces: int, n_palette: int, what: str) -> np.ndarray:
    s = np.asarray(face_slots)
    if s.shape != (n_faces,) or not np.issubdtype(s.dtype, np.integer):
        raise ValueError(f"{what}: face_slots must be (M,) integers")
    if s.min() < 1 or s.max() > n_palette:
        raise ValueError(f"{what}: face_slots must be in 1..{n_palette} (1-based palette index)")
    return s.astype(np.int64)


def default_slot(face_slots: np.ndarray) -> int:
    """Most common slot; ties go to the lower slot number."""
    return int(np.argmax(np.bincount(face_slots)))


def apply_transform(vertices: np.ndarray, transform) -> np.ndarray:
    """Apply a 4x4 (column-vector convention: p' = M @ [p, 1]) or 3x4 transform."""
    m = np.asarray(transform, dtype=np.float64)
    if m.shape == (4, 4):
        if not np.allclose(m[3], [0, 0, 0, 1]):
            raise ValueError("transform must be affine (last row 0 0 0 1)")
        m = m[:3]
    if m.shape != (3, 4):
        raise ValueError("transform must be 4x4 or 3x4")
    return vertices @ m[:, :3].T + m[:, 3]


def _mesh_xml(obj_id: int, index: int, p: _Prepared, obj_default: int) -> list[str]:
    base = p.extruder if p.extruder is not None else obj_default
    kind = "model" if p.subtype == "normal_part" else "other"
    out = [(f'  <object id="{obj_id}" p:UUID="{(1 << 16) + index:08x}{_SUB_OBJECT_SUFFIX}" '
            f'type="{kind}">\n'), "   <mesh>\n    <vertices>\n"]
    out += [f'     <vertex x="{_fmt(x)}" y="{_fmt(y)}" z="{_fmt(z)}"/>\n'
            for x, y, z in p.v.astype(np.float32).tolist()]
    out.append("    </vertices>\n    <triangles>\n")
    codes = {slot: encode_state(slot) for slot in range(1, int(p.s.max()) + 1)}
    codes[base] = ""
    codes[0] = ""
    for (a, b, c), slot in zip(p.f.tolist(), p.s.tolist(), strict=True):
        code = codes[slot]
        if code:
            out.append(f'     <triangle v1="{a}" v2="{b}" v3="{c}" paint_color="{code}"/>\n')
        else:
            out.append(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>\n')
    out.append("    </triangles>\n   </mesh>\n  </object>\n")
    return out


def _objects_model(parts: list[_Prepared], obj_default: int) -> bytes:
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        f'<model unit="millimeter" xml:lang="en-US" {_NS} requiredextensions="p">\n',
        ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n',
        " <resources>\n",
    ]
    for i, p in enumerate(parts):
        out += _mesh_xml(i + 1, i, p, obj_default)
    out.append(" </resources>\n <build/>\n</model>\n")
    return "".join(out).encode("utf-8")


def _main_model(name: str, app_version: str, transform: str, build_uuid: str,
                n_parts: int) -> bytes:
    asm_id = n_parts + 1
    comps = "".join(
        f'    <component p:path="/3D/Objects/object_1.model" objectid="{i + 1}" '
        f'p:UUID="{(1 << 16) + i:08x}{_COMPONENT_SUFFIX}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n'
        for i in range(n_parts))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" {_NS} requiredextensions="p">
 <metadata name="Application">BambuStudio-{app_version}</metadata>
 <metadata name="BambuStudio:3mfVersion">1</metadata>
 <metadata name="Title">{_xml_attr(name)}</metadata>
 <resources>
  <object id="{asm_id}" p:UUID="00000001-61cb-4c03-9d28-80fed5dfa1dc" type="model">
   <components>
{comps}   </components>
  </object>
 </resources>
 <build p:UUID="{build_uuid}">
  <item objectid="{asm_id}" p:UUID="00000002-b1ec-4553-aec9-835e5b724bb4" transform="{transform}" printable="1"/>
 </build>
</model>
""".encode()


def _model_settings(name: str, default: int, parts: list[_Prepared], digest: str,
                    cfg: dict[str, Any] | None = None) -> bytes:
    asm_id = len(parts) + 1
    n_faces = sum(len(p.f) for p in parts)
    rows = [
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n',
        f'  <object id="{asm_id}">\n',
        f'    <metadata key="name" value="{_xml_attr(name)}"/>\n',
        f'    <metadata key="extruder" value="{default}"/>\n',
        f'    <metadata face_count="{n_faces}"/>\n',
    ]
    for i, p in enumerate(parts):
        puid = uuid.uuid5(uuid.NAMESPACE_URL, f"p2p-threemf:part:{i}:{digest}")
        pn = _xml_attr(p.name)
        rows += [
            f'    <part id="{i + 1}" subtype="{p.subtype}" uuid="{puid}">\n',
            f'      <metadata key="name" value="{pn}"/>\n',
            '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n',
            f'      <metadata key="source_file" value="{_xml_attr(name)}.3mf"/>\n',
            '      <metadata key="source_object_id" value="0"/>\n',
            f'      <metadata key="source_volume_id" value="{i}"/>\n',
        ]
        if p.extruder is not None:
            rows.append(f'      <metadata key="extruder" value="{p.extruder}"/>\n')
        rows += [
            (f'      <mesh_stat face_count="{len(p.f)}" edges_fixed="0" degenerate_facets="0" '
             'facets_removed="0" facets_reversed="0" backwards_edges="0"/>\n'),
            "    </part>\n",
        ]
    rows += [
        "  </object>\n",
        "  <plate>\n",
        '    <metadata key="plater_id" value="1"/>\n',
        '    <metadata key="plater_name" value=""/>\n',
        '    <metadata key="locked" value="false"/>\n',
        *_plate_map(cfg or {}),
        "    <model_instance>\n",
        f'      <metadata key="object_id" value="{asm_id}"/>\n',
        '      <metadata key="instance_id" value="0"/>\n',
        '      <metadata key="identify_id" value="1"/>\n',
        "    </model_instance>\n",
        "  </plate>\n",
        "  <assemble>\n",
        (f'   <assemble_item object_id="{asm_id}" instance_id="0" '
         'transform="1 0 0 0 1 0 0 0 1 0 0 0" offset="0 0 0" />\n'),
        "  </assemble>\n",
        "</config>\n",
    ]
    return "".join(rows).encode()


# Writer defaults for purge and time, measured in docs/research/purge-and-time.md: together
# they cut total grams 15 to 21% and print time 8 to 11% on every fixture, with slots, read-back
# and thin paint unchanged. Saving primes 15 mm3 on the tower after a change instead of 45;
# the colour flush itself is untouched. PrintSettings.process_overrides and config_overrides
# win over these (set prime_volume_mode "Default" to go back).
DEFAULT_PROCESS_OVERRIDES: dict[str, str] = {
    "prime_volume_mode": "Saving",
    "flush_into_infill": "1",
}


# Prime tower footprint used for placement: Bambu's X2D default spot and width, and an
# ASSUMED depth (the tower's depth grows with purge volume; 45 mm covers the fixtures plus brim).
TOWER_DEFAULT_XY = (35.5, 145.0)
TOWER_DEPTH_MM = 45.0
TOWER_GAP_MM = 8.0
BED_MM = (256.0, 256.0)
TOWER_X_MIN = 24.0  # X2D extruder_printable_area: the right nozzle starts at x = 20.5


def _first(v: Any, default: float) -> float:
    if isinstance(v, (list, tuple)):
        v = v[0] if v else default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def place_prime_tower(footprint: tuple[float, float, float, float], width: float = 35.0,
                      start: tuple[float, float] = TOWER_DEFAULT_XY) -> tuple[float, float, bool]:
    """Tower corner (x, y) clear of the object's bed footprint (x0, y0, x1, y1) by 8 mm.

    Keeps `start` when it is already clear; otherwise tries behind the object, then right,
    left and in front, then the four bed corners. Returns (x, y, moved)."""
    x0, y0, x1, y1 = footprint
    bw, bh = BED_MM
    d, g, m = TOWER_DEPTH_MM, TOWER_GAP_MM, 5.0
    xmin = TOWER_X_MIN  # the right nozzle cannot reach x < 20.5 on the X2D

    def clear(x: float, y: float) -> bool:
        inside = xmin <= x and x + width <= bw - m and m <= y and y + d <= bh - m
        apart = x + width <= x0 - g or x >= x1 + g or y + d <= y0 - g or y >= y1 + g
        return inside and apart

    if clear(*start):
        return start[0], start[1], False
    g1 = g + 1.0  # place one mm past the minimum gap
    cx = min(max((x0 + x1) / 2 - width / 2, xmin), bw - m - width)
    cy = min(max((y0 + y1) / 2 - d / 2, m), bh - m - d)
    for x, y in ((cx, y1 + g1), (x1 + g1, cy), (x0 - g1 - width, cy), (cx, y0 - g1 - d),
                 (xmin, bh - m - d), (bw - m - width, bh - m - d), (xmin, m),
                 (bw - m - width, m)):
        x, y = round(x, 1), round(y, 1)
        if clear(x, y):
            return x, y, True
    return start[0], start[1], False


def _plate_map(cfg: dict[str, Any]) -> list[str]:
    """Plate 1 filament-to-nozzle mapping. Manual modes also carry the per-filament map."""
    mode = cfg.get("filament_map_mode", "Auto For Flush")
    rows = [f'    <metadata key="filament_map_mode" value="{mode}"/>\n']
    if mode in ("Manual", "Nozzle Manual") and cfg.get("filament_map"):
        rows.append(f'    <metadata key="filament_maps" value="{" ".join(cfg["filament_map"])}"/>\n')
    return rows


def project_config(palette: list[Filament], settings: PrintSettings,
                   overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """project_settings.config content for a palette and print settings."""
    machine = get_preset(machine_name(settings.printer, settings.nozzle_mm))
    process = get_preset(process_name(settings.layer_mm, settings.nozzle_mm))
    filaments = [get_preset(filament_preset_name(f.slicer_preset, settings.nozzle_mm))
                 for f in palette]
    colours = [f.hex.upper() for f in palette]
    snap = load_snapshot()
    version = snap.get("app_version") or CLIENT_VERSION
    ov: dict[str, Any] = {"layer_height": f"{settings.layer_mm:g}"}
    ov.update(DEFAULT_PROCESS_OVERRIDES)
    ov.update(settings.process_overrides)
    if overrides:
        ov.update(overrides)
    return build_project_config(machine, process, filaments, colours, version, overrides=ov)


def _zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            z.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    return buf.getvalue()


def _prepare(vertices, faces, face_slots, palette, parts, transform, object_name
             ) -> list[_Prepared]:
    n_pal = len(palette)
    v, f = _check_mesh(vertices, faces, object_name)
    if transform is not None:
        v = apply_transform(v, transform)
    out = [_Prepared(object_name, v, f, _check_slots(face_slots, len(f), n_pal, object_name),
                     None)]
    for i, part in enumerate(parts or []):
        what = part.name or f"part {i + 2}"
        pv, pf = _check_mesh(part.vertices, part.faces, what)
        if part.subtype not in SUBTYPES:
            raise ValueError(f"{what}: subtype must be one of {sorted(SUBTYPES)}")
        if part.subtype != "normal_part":
            out.append(_Prepared(what, pv, pf, np.zeros(len(pf), dtype=np.int64), None,
                                 part.subtype))
            continue
        if (part.face_slots is None) == (part.slot is None):
            raise ValueError(f"{what}: give exactly one of face_slots or slot")
        if part.slot is not None:
            if not 1 <= part.slot <= n_pal:
                raise ValueError(f"{what}: slot must be in 1..{n_pal}")
            ps = np.full(len(pf), part.slot, dtype=np.int64)
            out.append(_Prepared(what, pv, pf, ps, int(part.slot)))
        else:
            ps = _check_slots(part.face_slots, len(pf), n_pal, what)
            out.append(_Prepared(what, pv, pf, ps, default_slot(ps)))
    return out


def write_3mf(
    vertices,
    faces,
    face_slots,
    palette: list[Filament],
    settings: PrintSettings,
    out_path: str | Path,
    object_name: str = "model",
    config_overrides: dict[str, Any] | None = None,
    extra_sidecar: dict[str, Any] | None = None,
    parts: list[Part] | None = None,
    transform=None,
    layer_profile: list[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Write a Bambu Studio project 3MF with one filament per triangle.

    vertices (N,3) mm, Z up; faces (M,3) int; face_slots (M,) 1-based palette index. The most
    common slot of this (figure) part becomes the object's extruder and its faces stay
    unpainted; every other face gets a whole-triangle paint_color code.
    transform: optional 4x4 applied to the figure only (e.g. the geometry stage's placement).
    parts: extra parts of the same object, already in the final frame (see Part).
    layer_profile: optional variable layer height, [(z_mm, layer_mm), ...] from the bed up
    (Bambu's Metadata/layer_heights_profile.txt); see layers.adaptive_profile.
    The object is centred on the X2D plate with its lowest point on the bed.
    Returns dict(path, sha256, n_faces, slots_used, default_slot, palette, parts, bytes).
    """
    if not palette:
        raise ValueError("palette is empty")
    if len(palette) > MAX_STATE:
        raise ValueError(f"at most {MAX_STATE} filaments")
    if len(palette) > settings.ams_slots:
        raise ValueError(f"palette has {len(palette)} filaments but ams_slots is {settings.ams_slots}")
    prepared = _prepare(vertices, faces, face_slots, palette, parts, transform, object_name)
    default = default_slot(prepared[0].s)

    cfg = project_config(palette, settings, config_overrides)
    app_version = cfg["version"]

    all_v = np.concatenate([p.v for p in prepared if p.subtype == "normal_part"])
    lo, hi = all_v.min(axis=0), all_v.max(axis=0)
    tx = BED_CENTER[0] - (lo[0] + hi[0]) / 2
    ty = BED_CENTER[1] - (lo[1] + hi[1]) / 2
    tz = -lo[2]
    placement = f"1 0 0 0 1 0 0 0 1 {_fmt(tx)} {_fmt(ty)} {_fmt(tz)}"

    # Move the prime tower off wide objects (a 150 mm plate covers Bambu's default spot and
    # the slice fails with a path conflict), unless the caller placed it on purpose.
    tower = None
    asked = {**settings.process_overrides, **(config_overrides or {})}
    if "wipe_tower_x" not in asked and "wipe_tower_y" not in asked:
        foot = (lo[0] + tx, lo[1] + ty, hi[0] + tx, hi[1] + ty)
        start = (_first(cfg.get("wipe_tower_x"), TOWER_DEFAULT_XY[0]),
                 _first(cfg.get("wipe_tower_y"), TOWER_DEFAULT_XY[1]))
        x, y, moved = place_prime_tower(foot, _first(cfg.get("prime_tower_width"), 35.0), start)
        if moved:
            for k, val in (("wipe_tower_x", x), ("wipe_tower_y", y)):
                cur = cfg.get(k)
                cfg[k] = [f"{val:g}"] * max(1, len(cur)) if isinstance(cur, list) else f"{val:g}"
        tower = {"x": x, "y": y, "moved": moved}

    obj = _objects_model(prepared, default)
    digest = hashlib.sha256(obj).hexdigest()
    build_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"p2p-threemf:build:{digest}"))

    all_s = np.concatenate([p.s for p in prepared if p.subtype == "normal_part"])
    used = sorted(int(x) for x in np.unique(all_s))
    counts = np.bincount(all_s, minlength=len(palette) + 1)[1:]
    part_info = []
    for p in prepared:
        base = p.extruder if p.extruder is not None else default
        normal = p.subtype == "normal_part"
        part_info.append({"name": p.name, "subtype": p.subtype, "n_faces": len(p.f),
                          "extruder": base if normal else None,
                          "painted": bool(normal and np.any(p.s != base)),
                          "slots": sorted(int(x) for x in np.unique(p.s)) if normal else []})
    sidecar = {
        "generator": f"p2p_threemf {__version__}",
        "object_name": object_name,
        "n_faces": len(all_s),
        "n_vertices": len(all_v),
        "default_slot": default,
        "slots_used": used,
        "faces_per_slot": {str(i + 1): int(c) for i, c in enumerate(counts)},
        "parts": part_info,
        "palette": [{"slot": i + 1, "filament_id": p.id, "hex": p.hex.upper(),
                     "preset": cfg["filament_settings_id"][i]} for i, p in enumerate(palette)],
        "settings": settings.model_dump(mode="json"),
        "prime_tower": tower,
    }
    if extra_sidecar:
        sidecar.update(extra_sidecar)

    entries = [
        ("[Content_Types].xml", CONTENT_TYPES.encode()),
        ("_rels/.rels", ROOT_RELS.encode()),
        ("3D/3dmodel.model",
         _main_model(object_name, app_version, placement, build_uuid, len(prepared))),
        ("3D/_rels/3dmodel.model.rels", MODEL_RELS.encode()),
        ("3D/Objects/object_1.model", obj),
        ("Metadata/model_settings.config",
         _model_settings(object_name, default, prepared, digest, cfg)),
        ("Metadata/project_settings.config",
         (json.dumps(cfg, indent=4, ensure_ascii=False) + "\n").encode()),
        ("Metadata/slice_info.config", SLICE_INFO.encode()),
        ("Metadata/filament_sequence.json", FILAMENT_SEQUENCE.encode()),
        ("Metadata/p2p_threemf.json", (json.dumps(sidecar, indent=1) + "\n").encode()),
    ]
    if layer_profile:
        flat = ";".join(f"{x:f}" for zh in layer_profile for x in zh)
        entries.insert(-1, ("Metadata/layer_heights_profile.txt",
                            f"object_id=1|{flat}\n".encode()))
    data = _zip_bytes(entries)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return {
        "path": str(out),
        "sha256": hashlib.sha256(data).hexdigest(),
        "n_faces": len(all_s),
        "slots_used": used,
        "default_slot": default,
        "palette": [p.id for p in palette],
        "parts": part_info,
        "bytes": len(data),
    }
