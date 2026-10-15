"""Minimal GLB reader for texture colors: triangles, UVs and base-color textures, numpy and PIL only.

Reads every triangle primitive of the default scene with node transforms applied. Face order is
primitive order within each mesh, meshes in node order (depth first), which matches how trimesh
and the Meshy exports concatenate a single-primitive mesh. Not supported: Draco or meshopt
compression, sparse accessors, KHR_texture_transform (ignored).

to_model_frame converts glTF (Y up, front +Z) to the pipeline frame (mm, Z up, front -Y, base on
z = 0, centered on the XY bbox center): (x, y, z)_gltf -> (x, -z, y)_ours, then scaled. Face order
is unchanged, so face_regions.npy indexes the same triangles.
"""

from __future__ import annotations

import io
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

_COMPONENTS = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32,
               5126: np.float32}  # fmt: skip
_WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
_NORM_MAX = {np.int8: 127.0, np.uint8: 255.0, np.int16: 32767.0, np.uint16: 65535.0}


@dataclass
class Material:
    base_color_factor: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    texture: Image.Image | None = None  # RGB base-color texture (sRGB)
    texcoord: int = 0


@dataclass
class GlbMesh:
    vertices: np.ndarray  # (V, 3) float64, glTF frame unless converted
    faces: np.ndarray  # (F, 3) int64
    uvs: np.ndarray | None  # (V, 2) float64 TEXCOORD_0, or None
    normals: np.ndarray | None  # (V, 3) float64 vertex normals, or None
    face_material: np.ndarray  # (F,) int32 index into materials, -1 = none
    materials: list[Material] = field(default_factory=list)


class GlbError(ValueError):
    pass


def _chunks(data: bytes) -> tuple[dict, bytes]:
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2:
        raise GlbError("not a glTF 2.0 binary")
    off, js, bin_ = 12, None, b""
    while off < min(length, len(data)):
        clen, ctype = struct.unpack_from("<I4s", data, off)
        body = data[off + 8 : off + 8 + clen]
        if ctype == b"JSON":
            js = json.loads(body)
        elif ctype == b"BIN\x00":
            bin_ = body
        off += 8 + clen
    if js is None:
        raise GlbError("GLB has no JSON chunk")
    return js, bin_


def _view_bytes(js: dict, bin_: bytes, view_index: int) -> tuple[bytes, int | None]:
    view = js["bufferViews"][view_index]
    if view.get("buffer", 0) != 0:
        raise GlbError("only the GLB binary buffer is supported")
    start = view.get("byteOffset", 0)
    return bin_[start : start + view["byteLength"]], view.get("byteStride")


def _accessor(js: dict, bin_: bytes, index: int) -> np.ndarray:
    acc = js["accessors"][index]
    if "sparse" in acc:
        raise GlbError("sparse accessors are not supported")
    dtype = np.dtype(_COMPONENTS[acc["componentType"]])
    width = _WIDTH[acc["type"]]
    count = acc["count"]
    if "bufferView" not in acc:
        return np.zeros((count, width), dtype)
    raw, stride = _view_bytes(js, bin_, acc["bufferView"])
    offset = acc.get("byteOffset", 0)
    item = dtype.itemsize * width
    if stride and stride != item:
        buf = np.frombuffer(raw, np.uint8)[offset:]
        rows = np.lib.stride_tricks.as_strided(buf, shape=(count, item), strides=(stride, 1))
        arr = np.ascontiguousarray(rows).view(dtype).reshape(count, width)
    else:
        arr = np.frombuffer(raw, dtype, count=count * width, offset=offset).reshape(count, width)
    if acc.get("normalized") and dtype.type in _NORM_MAX:
        arr = np.maximum(arr.astype(np.float64) / _NORM_MAX[dtype.type], -1.0)
    return arr


def _image(js: dict, bin_: bytes, tex_index: int, base: Path | None) -> Image.Image | None:
    tex = js.get("textures", [])[tex_index]
    src = tex.get("source")
    if src is None:
        return None
    img = js["images"][src]
    if "bufferView" in img:
        data, _ = _view_bytes(js, bin_, img["bufferView"])
    elif "uri" in img and img["uri"].startswith("data:"):
        import base64

        data = base64.b64decode(img["uri"].split(",", 1)[1])
    elif "uri" in img and base is not None:
        data = (base / img["uri"]).read_bytes()
    else:
        return None
    with Image.open(io.BytesIO(data)) as im:
        return im.convert("RGB")


def _node_matrix(node: dict) -> np.ndarray:
    if "matrix" in node:
        return np.asarray(node["matrix"], float).reshape(4, 4).T  # column-major
    t = np.asarray(node.get("translation", (0, 0, 0)), float)
    x, y, z, w = node.get("rotation", (0, 0, 0, 1))
    r = np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )
    s = np.asarray(node.get("scale", (1, 1, 1)), float)
    m = np.eye(4)
    m[:3, :3] = r * s[None, :]
    m[:3, 3] = t
    return m


def read_glb(path: str | Path) -> GlbMesh:
    path = Path(path)
    js, bin_ = _chunks(path.read_bytes())
    used = set(js.get("extensionsRequired", [])) - {"KHR_materials_emissive_strength"}
    if used:
        raise GlbError(f"unsupported required extensions: {sorted(used)}")
    materials = []
    for m in js.get("materials", []):
        pbr = m.get("pbrMetallicRoughness", {})
        tinfo = pbr.get("baseColorTexture")
        materials.append(
            Material(
                base_color_factor=tuple(pbr.get("baseColorFactor", (1.0, 1.0, 1.0, 1.0))),
                texture=_image(js, bin_, tinfo["index"], path.parent) if tinfo else None,
                texcoord=int(tinfo.get("texCoord", 0)) if tinfo else 0,
            )
        )

    verts, faces, uvs, norms, fmat = [], [], [], [], []
    n_v = 0
    any_uv = any_norm = False

    def visit(ni: int, parent: np.ndarray) -> None:
        nonlocal n_v, any_uv, any_norm
        node = js["nodes"][ni]
        world = parent @ _node_matrix(node)
        if "mesh" in node:
            for prim in js["meshes"][node["mesh"]]["primitives"]:
                if prim.get("mode", 4) != 4:
                    continue
                attrs = prim["attributes"]
                p = _accessor(js, bin_, attrs["POSITION"]).astype(np.float64)
                p = p @ world[:3, :3].T + world[:3, 3]
                if "indices" in prim:
                    idx = _accessor(js, bin_, prim["indices"]).astype(np.int64).reshape(-1, 3)
                else:
                    idx = np.arange(len(p), dtype=np.int64).reshape(-1, 3)
                mi = prim.get("material", -1)
                tc = materials[mi].texcoord if mi >= 0 else 0
                uv_key = f"TEXCOORD_{tc}"
                if uv_key in attrs:
                    uvs.append(_accessor(js, bin_, attrs[uv_key]).astype(np.float64))
                    any_uv = True
                else:
                    uvs.append(np.zeros((len(p), 2)))
                if "NORMAL" in attrs:
                    nrm = _accessor(js, bin_, attrs["NORMAL"]).astype(np.float64)
                    nrm = nrm @ np.linalg.inv(world[:3, :3])
                    nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)
                    norms.append(nrm)
                    any_norm = True
                else:
                    norms.append(np.zeros((len(p), 3)))
                verts.append(p)
                faces.append(idx + n_v)
                fmat.append(np.full(len(idx), mi, np.int32))
                n_v += len(p)
        for c in node.get("children", []):
            visit(c, world)

    scene = js.get("scenes", [{}])[js.get("scene", 0)] if js.get("scenes") else None
    roots = scene.get("nodes", []) if scene else range(len(js.get("nodes", [])))
    for r in roots:
        visit(r, np.eye(4))
    if not faces:
        raise GlbError("GLB has no triangles")
    return GlbMesh(
        vertices=np.concatenate(verts),
        faces=np.concatenate(faces),
        uvs=np.concatenate(uvs) if any_uv else None,
        normals=np.concatenate(norms) if any_norm else None,
        face_material=np.concatenate(fmat),
        materials=materials,
    )


GLTF_TO_MODEL = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])


def to_model_frame(mesh: GlbMesh, height_mm: float | None = None) -> GlbMesh:
    """Copy of mesh in the pipeline frame; scaled to height_mm tall (Z) if given, else 1 unit = 1 mm."""
    v = mesh.vertices @ GLTF_TO_MODEL.T
    if height_mm is not None:
        h = float(np.ptp(v[:, 2]))
        if h <= 0:
            raise GlbError("mesh has zero height")
        v = v * (height_mm / h)
    lo, hi = v.min(axis=0), v.max(axis=0)
    v = v - np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])
    return GlbMesh(
        vertices=v,
        faces=mesh.faces,
        uvs=mesh.uvs,
        normals=None if mesh.normals is None else mesh.normals @ GLTF_TO_MODEL.T,
        face_material=mesh.face_material,
        materials=mesh.materials,
    )


def sample_texture(texture: np.ndarray, uv: np.ndarray) -> np.ndarray:
    """Bilinear, repeat-wrapped samples of an (H, W, C) texture at glTF UVs (origin top left)."""
    h, w = texture.shape[:2]
    x = np.mod(uv[..., 0], 1.0) * w - 0.5
    y = np.mod(uv[..., 1], 1.0) * h - 0.5
    x0, y0 = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
    fx, fy = (x - x0)[..., None], (y - y0)[..., None]
    x0, x1 = np.mod(x0, w), np.mod(x0 + 1, w)
    y0, y1 = np.mod(y0, h), np.mod(y0 + 1, h)
    t = texture.astype(np.float32)
    top = t[y0, x0] * (1 - fx) + t[y0, x1] * fx
    bot = t[y1, x0] * (1 - fx) + t[y1, x1] * fx
    return top * (1 - fy) + bot * fy
