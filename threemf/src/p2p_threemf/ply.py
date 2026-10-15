"""Minimal PLY mesh reader and writer (ascii and binary little/big endian, triangles)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

_TYPES = {
    "char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1", "short": "i2", "int16": "i2",
    "ushort": "u2", "uint16": "u2", "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
    "float": "f4", "float32": "f4", "double": "f8", "float64": "f8",
}


def read_ply(path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """(vertices (N,3) float64, faces (M,3) int64). Polygons with more than 3 corners are fanned."""
    data = Path(path).read_bytes()
    end = data.find(b"end_header")
    if not data.startswith(b"ply") or end < 0:
        raise ValueError(f"{path}: not a PLY file")
    header = data[:end].decode("ascii", "replace").splitlines()
    body = data[data.index(b"\n", end) + 1:]
    fmt = None
    elements: list[dict] = []
    for line in header:
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "format":
            fmt = parts[1]
        elif parts[0] == "element":
            elements.append({"name": parts[1], "count": int(parts[2]), "props": []})
        elif parts[0] == "property":
            if parts[1] == "list":
                elements[-1]["props"].append((parts[4], "list", parts[2], parts[3]))
            else:
                elements[-1]["props"].append((parts[2], parts[1]))
    if fmt not in ("ascii", "binary_little_endian", "binary_big_endian"):
        raise ValueError(f"{path}: unsupported PLY format {fmt}")
    verts = faces = None
    if fmt == "ascii":
        tokens = body.split()
        pos = 0
        for el in elements:
            if el["name"] == "vertex":
                n_props = len(el["props"])
                arr = np.array(tokens[pos:pos + n_props * el["count"]], dtype=np.float64)
                arr = arr.reshape(el["count"], n_props)
                pos += n_props * el["count"]
                names = [p[0] for p in el["props"]]
                verts = arr[:, [names.index("x"), names.index("y"), names.index("z")]]
            elif el["name"] == "face":
                rows = []
                for _ in range(el["count"]):
                    k = int(tokens[pos])
                    rows.append([int(t) for t in tokens[pos + 1:pos + 1 + k]])
                    pos += 1 + k
                faces = _fan(rows)
            else:
                pos += sum(1 for _ in el["props"]) * el["count"]
    else:
        order = "<" if fmt == "binary_little_endian" else ">"
        off = 0
        for el in elements:
            if el["name"] == "vertex" or all(p[1] != "list" for p in el["props"]):
                dt = np.dtype([(p[0], order + _TYPES[p[1]]) for p in el["props"]])
                arr = np.frombuffer(body, dtype=dt, count=el["count"], offset=off)
                off += dt.itemsize * el["count"]
                if el["name"] == "vertex":
                    verts = np.stack([arr["x"], arr["y"], arr["z"]], axis=1).astype(np.float64)
            elif el["name"] == "face":
                (_name, _, ct, it) = el["props"][0]
                if len(el["props"]) != 1:
                    raise ValueError("face element with extra properties is not supported")
                cdt, idt = np.dtype(order + _TYPES[ct]), np.dtype(order + _TYPES[it])
                # Fast path: all triangles.
                rec = np.dtype([("n", cdt), ("i", idt, (3,))])
                arr = np.frombuffer(body, dtype=rec, count=el["count"], offset=off)
                if (arr["n"] == 3).all():
                    faces = arr["i"].astype(np.int64)
                    off += rec.itemsize * el["count"]
                else:
                    rows = []
                    for _ in range(el["count"]):
                        k = int(np.frombuffer(body, cdt, 1, off)[0])
                        off += cdt.itemsize
                        rows.append(np.frombuffer(body, idt, k, off).tolist())
                        off += idt.itemsize * k
                    faces = _fan(rows)
            else:
                raise ValueError(f"unsupported binary PLY element {el['name']}")
    if verts is None or faces is None:
        raise ValueError(f"{path}: PLY needs vertex and face elements")
    return verts, faces


def _fan(rows: list[list[int]]) -> np.ndarray:
    out = []
    for r in rows:
        for i in range(1, len(r) - 1):
            out.append((r[0], r[i], r[i + 1]))
    return np.array(out, dtype=np.int64).reshape(-1, 3)


def write_ply(path: str | Path, vertices, faces) -> None:
    """Binary little endian PLY with float32 vertices and int32 triangle indices."""
    v = np.asarray(vertices, dtype="<f4")
    f = np.asarray(faces, dtype="<i4")
    header = (f"ply\nformat binary_little_endian 1.0\nelement vertex {len(v)}\n"
              "property float x\nproperty float y\nproperty float z\n"
              f"element face {len(f)}\nproperty list uchar int vertex_indices\nend_header\n")
    rec = np.zeros(len(f), dtype=[("n", "u1"), ("i", "<i4", (3,))])
    rec["n"] = 3
    rec["i"] = f
    Path(path).write_bytes(header.encode() + v.tobytes() + rec.tobytes())
