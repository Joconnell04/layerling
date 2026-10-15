"""Independent 3MF reader for Bambu Studio / PrusaSlicer / OrcaSlicer multicolor projects.

Handles inline meshes and the production extension (components pointing at 3D/Objects/*.model),
multi-part objects, per-object and per-part extruders from Metadata/model_settings.config, and
the full paint_color stream including subdivided triangles (see encoding.py).

For each source triangle we report the majority slot by area; unpainted area counts toward the
triangle's part/object extruder. `split` flags triangles whose paint was subdivided.
Large files: regex fast path over bytes (Bambu writes attributes in a fixed order), with an
ElementTree fallback when the fast path does not account for every tag.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import numpy as np

from .encoding import PaintCodeError, decode_summary

_OBJ_RE = re.compile(rb"<object\b([^>]*)>(.*?)</object>", re.DOTALL)
_ATTR_RE = re.compile(rb'([\w:]+)="([^"]*)"')
_VERT_RE = re.compile(rb'<vertex\s+x="([^"]*)"\s+y="([^"]*)"\s+z="([^"]*)"\s*/>')
_TRI_RE = re.compile(rb'<triangle\s+v1="(\d+)"\s+v2="(\d+)"\s+v3="(\d+)"([^>]*?)/?>')
_PAINT_RE = re.compile(rb'(?:paint_color|slic3rpe:mmu_segmentation|mmu_segmentation)="([^"]*)"')
_COMP_RE = re.compile(rb"<component\b([^>]*?)/?>")
_ITEM_RE = re.compile(rb"<item\b([^>]*?)/?>")
_META_RE = re.compile(rb'<metadata\s+name="([^"]*)"\s*>([^<]*)</metadata>')

IDENTITY = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1], [0, 0, 0]], dtype=np.float64)


def parse_transform(text: str | None) -> np.ndarray:
    """3MF transform "m00 m01 m02 m10 ... m32" as a 4x3 matrix; p' = p @ M[:3] + M[3]."""
    if not text:
        return IDENTITY.copy()
    vals = [float(x) for x in text.split()]
    if len(vals) != 12:
        raise ValueError(f"bad 3MF transform {text!r}")
    return np.array(vals, dtype=np.float64).reshape(4, 3)


def compose(inner: np.ndarray, outer: np.ndarray) -> np.ndarray:
    """Transform applying `inner` first, then `outer`."""
    out = np.empty((4, 3))
    out[:3] = inner[:3] @ outer[:3]
    out[3] = inner[3] @ outer[:3] + outer[3]
    return out


def apply(m: np.ndarray, pts: np.ndarray) -> np.ndarray:
    return pts @ m[:3] + m[3]


@dataclass
class _Mesh:
    vertices: np.ndarray
    faces: np.ndarray
    codes: list[str]  # "" for unpainted


@dataclass
class _Obj:
    id: int
    mesh: _Mesh | None = None
    components: list[tuple[str | None, int, np.ndarray]] = field(default_factory=list)


@dataclass
class ThreeMF:
    """Everything read_3mf_full recovers from a project."""

    vertices: np.ndarray  # (N,3) float64
    faces: np.ndarray  # (M,3) int64
    face_slots: np.ndarray  # (M,) int32, 1-based filament slot (majority by area)
    split: np.ndarray  # (M,) bool, paint subdivided within the triangle
    face_object: np.ndarray  # (M,) int32 build object id
    face_part: np.ndarray  # (M,) int32 mesh object id of the part (model_settings part id)
    filament_colours: list[str]
    filament_settings_id: list[str]
    printer_settings_id: str | None
    print_settings_id: str | None
    application: str | None
    object_extruders: dict[int, int]
    codes: list[str]  # raw paint string per face
    warnings: list[str] = field(default_factory=list)
    project_config: dict | None = None
    sidecar: dict | None = None


def _attrs(blob: bytes) -> dict[str, str]:
    return {k.decode(): v.decode() for k, v in _ATTR_RE.findall(blob)}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _mesh_fast(body: bytes) -> _Mesh | None:
    verts = _VERT_RE.findall(body)
    tris = _TRI_RE.findall(body)
    # "<triangles>" starts with the same letters as the elements it holds
    n_vert = body.count(b"<vertex")
    n_tri = body.count(b"<triangle") - body.count(b"<triangles")
    if len(verts) != n_vert or len(tris) != n_tri:
        return None
    v = np.array(verts, dtype="S").astype(np.float64) if verts else np.zeros((0, 3))
    if tris:
        idx = np.array([t[:3] for t in tris], dtype="S").astype(np.int64)
    else:
        idx = np.zeros((0, 3), dtype=np.int64)
    codes = []
    for t in tris:
        rest = t[3]
        if rest and b"=" in rest:
            m = _PAINT_RE.search(rest)
            codes.append(m.group(1).decode() if m else "")
        else:
            codes.append("")
    return _Mesh(v.reshape(-1, 3), idx.reshape(-1, 3), codes)


def _mesh_slow(body: bytes) -> _Mesh:
    root = ET.fromstring(b"<r xmlns:slic3rpe='x' xmlns:p='y'>" + body + b"</r>")
    verts, faces, codes = [], [], []
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "vertex":
            verts.append((float(el.get("x")), float(el.get("y")), float(el.get("z"))))
        elif tag == "triangle":
            faces.append((int(el.get("v1")), int(el.get("v2")), int(el.get("v3"))))
            code = ""
            for k, val in el.attrib.items():
                if _local(k) in ("paint_color", "mmu_segmentation"):
                    code = val
            codes.append(code)
    return _Mesh(np.array(verts, dtype=np.float64).reshape(-1, 3),
                 np.array(faces, dtype=np.int64).reshape(-1, 3), codes)


def _parse_model(data: bytes) -> tuple[dict[int, _Obj], list[tuple[int, np.ndarray]], dict[str, str]]:
    objs: dict[int, _Obj] = {}
    for m in _OBJ_RE.finditer(data):
        a = _attrs(m.group(1))
        oid = int(a["id"])
        body = m.group(2)
        obj = _Obj(oid)
        if b"<mesh" in body:
            obj.mesh = _mesh_fast(body) or _mesh_slow(body)
        for c in _COMP_RE.finditer(body):
            ca = _attrs(c.group(1))
            path = ca.get("p:path") or ca.get("path")
            obj.components.append((path, int(ca["objectid"]), parse_transform(ca.get("transform"))))
        objs[oid] = obj
    build_start = data.rfind(b"<build")
    items = []
    if build_start >= 0:
        for it in _ITEM_RE.finditer(data, build_start):
            ia = _attrs(it.group(1))
            items.append((int(ia["objectid"]), parse_transform(ia.get("transform"))))
    meta = {k.decode(): v.decode() for k, v in _META_RE.findall(data[:20000])}
    return objs, items, meta


def _root_model_path(z: zipfile.ZipFile) -> str:
    try:
        rels = z.read("_rels/.rels").decode("utf-8", "replace")
        m = re.search(r'Target="([^"]+)"[^>]*Type="[^"]*3dmodel"', rels) or re.search(
            r'Type="[^"]*3dmodel"[^>]*Target="([^"]+)"', rels)
        if m:
            return m.group(1).lstrip("/")
    except KeyError:
        pass
    return "3D/3dmodel.model"


def _model_settings(z: zipfile.ZipFile):
    """(object id -> extruder, (object id, part id) -> extruder, (object id, part id) -> subtype)."""
    obj_ext: dict[int, int] = {}
    part_ext: dict[tuple[int, int], int] = {}
    part_sub: dict[tuple[int, int], str] = {}
    for name in ("Metadata/model_settings.config", "Metadata/Slic3r_PE_model.config"):
        if name not in z.namelist():
            continue
        root = ET.fromstring(z.read(name))
        for ob in root.iter("object"):
            oid = int(ob.get("id"))
            for md in ob.findall("metadata"):
                if md.get("key") == "extruder" and md.get("value", "").strip().isdigit():
                    obj_ext[oid] = int(md.get("value"))
            for part in list(ob.findall("part")) + list(ob.findall("volume")):
                pid = part.get("id")
                if part.get("subtype") and pid and pid.isdigit():
                    part_sub[(oid, int(pid))] = part.get("subtype")
                for md in part.findall("metadata"):
                    if md.get("key") == "extruder" and md.get("value", "").strip().isdigit():
                        key = (oid, int(pid) if pid and pid.isdigit() else -1)
                        part_ext[key] = int(md.get("value"))
        break
    return obj_ext, part_ext, part_sub


def _project_config(z: zipfile.ZipFile) -> dict | None:
    if "Metadata/project_settings.config" in z.namelist():
        try:
            return json.loads(z.read("Metadata/project_settings.config"))
        except json.JSONDecodeError:
            return None
    if "Metadata/Slic3r_PE.config" in z.namelist():
        cfg = {}
        for line in z.read("Metadata/Slic3r_PE.config").decode("utf-8", "replace").splitlines():
            m = re.match(r";\s*(\w+)\s*=\s*(.*)$", line)
            if m:
                cfg[m.group(1)] = m.group(2)
        out: dict = {}
        for key in ("filament_colour", "extruder_colour"):
            if key in cfg:
                out[key] = [c for c in cfg[key].split(";") if c]
        return out
    return None


def _norm(path: str, base: str) -> str:
    if path.startswith("/"):
        return path.lstrip("/")
    return str(PurePosixPath(base).parent / path)


def read_3mf_full(path: str | Path, world: bool = False) -> ThreeMF:
    """Read every build item's mesh and paint. world=True applies build transforms too
    (bed coordinates); otherwise meshes are in object coordinates (component transforms only)."""
    warnings: list[str] = []
    with zipfile.ZipFile(path) as z:
        root_path = _root_model_path(z)
        cache: dict[str, tuple[dict[int, _Obj], list, dict]] = {}

        def model(p: str):
            if p not in cache:
                cache[p] = _parse_model(z.read(p))
            return cache[p]

        root_objs, items, meta = model(root_path)
        obj_ext, part_ext, part_sub = _model_settings(z)
        cfg = _project_config(z)
        sidecar = None
        if "Metadata/p2p_threemf.json" in z.namelist():
            sidecar = json.loads(z.read("Metadata/p2p_threemf.json"))

        colours = list((cfg or {}).get("filament_colour") or (cfg or {}).get("extruder_colour")
                       or [])
        n_fil = len(colours) or 32

        all_v, all_f, all_codes, all_default, all_obj, all_part = [], [], [], [], [], []
        offset = 0

        def emit(obj: _Obj, objs: dict[int, _Obj], model_path: str, xf: np.ndarray,
                 default: int, build_id: int, depth: int = 0) -> None:
            nonlocal offset
            if depth > 16:
                raise ValueError("component nesting too deep")
            if obj.mesh is not None and len(obj.mesh.faces):
                v = apply(xf, obj.mesh.vertices)
                all_v.append(v)
                all_f.append(obj.mesh.faces + offset)
                all_codes.extend(obj.mesh.codes)
                all_default.append(np.full(len(obj.mesh.faces), default, dtype=np.int32))
                all_obj.append(np.full(len(obj.mesh.faces), build_id, dtype=np.int32))
                all_part.append(np.full(len(obj.mesh.faces), obj.id, dtype=np.int32))
                offset += len(v)
            for comp_path, cid, cxf in obj.components:
                if comp_path:
                    p = _norm(comp_path, model_path)
                    sub_objs = model(p)[0]
                else:
                    p, sub_objs = model_path, objs
                child = sub_objs.get(cid)
                if child is None:
                    warnings.append(f"component object {cid} missing in {p}")
                    continue
                if part_sub.get((build_id, cid), "normal_part") != "normal_part":
                    continue  # cutters, modifiers and support volumes are not printed
                d = part_ext.get((build_id, cid), default)
                emit(child, sub_objs, p, compose(cxf, xf), d, build_id, depth + 1)

        if not items:
            items = [(oid, IDENTITY.copy()) for oid in root_objs]
        for oid, bxf in items:
            obj = root_objs.get(oid)
            if obj is None:
                warnings.append(f"build item references missing object {oid}")
                continue
            default = obj_ext.get(oid, 1) or 1
            emit(obj, root_objs, root_path, bxf if world else IDENTITY.copy(), default, oid)

    if not all_f:
        raise ValueError(f"{path}: no triangles found")
    vertices = np.concatenate(all_v)
    faces = np.concatenate(all_f)
    defaults = np.concatenate(all_default)
    face_obj = np.concatenate(all_obj)
    face_part = np.concatenate(all_part)

    slots = np.empty(len(faces), dtype=np.int32)
    split = np.zeros(len(faces), dtype=bool)
    memo: dict[tuple[str, int], tuple[int, bool]] = {}
    bad = 0
    for i, (code, d) in enumerate(zip(all_codes, defaults.tolist(), strict=True)):
        key = (code, d)
        r = memo.get(key)
        if r is None:
            if code == "":
                r = (d, False)
            else:
                try:
                    _, is_split, areas = decode_summary(code.upper())
                except PaintCodeError:
                    bad += 1
                    r = (d, False)
                else:
                    acc: dict[int, float] = {}
                    for state, frac in areas:
                        # Bambu treats states beyond the filament count as unpainted.
                        s = d if state == 0 or state > n_fil else state
                        acc[s] = acc.get(s, 0.0) + frac
                    best = min(acc.items(), key=lambda kv: (-round(kv[1], 12), kv[0]))[0]
                    r = (best, is_split)
            memo[key] = r
        slots[i], split[i] = r
    if bad:
        warnings.append(f"{bad} triangles had invalid paint codes and were read as unpainted")

    cfg = cfg or {}
    return ThreeMF(
        vertices=vertices,
        faces=faces,
        face_slots=slots,
        split=split,
        face_object=face_obj,
        face_part=face_part,
        filament_colours=colours,
        filament_settings_id=list(cfg.get("filament_settings_id") or []),
        printer_settings_id=cfg.get("printer_settings_id"),
        print_settings_id=cfg.get("print_settings_id"),
        application=meta.get("Application"),
        object_extruders=obj_ext,
        codes=all_codes,
        warnings=warnings,
        project_config=cfg or None,
        sidecar=sidecar,
    )


def read_3mf(path: str | Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """(vertices (N,3), faces (M,3), face_slots (M,) 1-based majority slot, filament_colours).

    Use read_3mf_full for the split flags, presets, and world coordinates."""
    r = read_3mf_full(path)
    return r.vertices, r.faces, r.face_slots, r.filament_colours
