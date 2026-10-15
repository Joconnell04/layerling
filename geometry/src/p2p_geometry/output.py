"""build_product: the pinned stage 6 entry point. Writes PLYs, geometry.json, geometry_report.json.

Signature pinned in p2p_contracts.geometry:
    build_product(figure_vertices, figure_faces, product, addons, target_size_mm, library,
                  settings, out_dir) -> GeometryResult
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
from p2p_contracts.filaments import FilamentLibrary
from p2p_contracts.geometry import GeometryResult, PartRef
from p2p_contracts.job import AddOn, PrintSettings, ProductType, SizeTier
from p2p_contracts.manifest import FileRef

from .meshio import load_meshy_glb
from .product import build_product_geometry
from .types import GeometrySettings, ProductGeometry


def _job_root(out_dir: Path) -> Path:
    """jobs/<id>/<stage>/a<N>/ -> jobs/<id>/; anything else -> out_dir itself."""
    if re.fullmatch(r"a\d+", out_dir.name) and out_dir.parent.name.startswith("s"):
        return out_dir.parent.parent
    return out_dir


def _ref(path: Path, root: Path, kind: str, role: str | None = None) -> FileRef:
    data = path.read_bytes()
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = path.as_posix()
    return FileRef(path=rel, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), kind=kind,
                   role=role)


def write_ply(path: Path, vertices: np.ndarray, faces: np.ndarray) -> None:
    """Binary little-endian PLY; face order exactly as given."""
    v = np.ascontiguousarray(vertices, dtype="<f4")
    f = np.asarray(faces, dtype="<i4")
    header = (
        "ply\nformat binary_little_endian 1.0\ncomment p2p_geometry, mm, Z up, front -Y\n"
        f"element vertex {len(v)}\nproperty float x\nproperty float y\nproperty float z\n"
        f"element face {len(f)}\nproperty list uchar int vertex_indices\nend_header\n"
    ).encode()
    rec = np.zeros(len(f), dtype=[("n", "u1"), ("i", "<i4", (3,))])
    rec["n"] = 3
    rec["i"] = f
    path.write_bytes(header + v.tobytes() + rec.tobytes())


def write_outputs(geo: ProductGeometry, out_dir: Path, job_dir: Path | None = None,
                  source_glb: Path | None = None) -> GeometryResult:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    root = Path(job_dir) if job_dir else _job_root(out_dir)
    glb_ref = _ref(Path(source_glb), root, "glb", "source_glb") if source_glb else None

    fig_path = out_dir / "figure.ply"
    write_ply(fig_path, geo.figure.vertices, geo.figure.faces)
    fig_ref = PartRef(
        name="figure", role="figure", mesh=_ref(fig_path, root, "ply", "figure"),
        filament_id=None, watertight=geo.figure.report.watertight,
    )
    fim = None
    fs = geo.figure.face_source
    if len(fs) != geo.figure.report.source_faces or not np.array_equal(fs, np.arange(len(fs))):
        p = out_dir / "face_index_map.npy"
        np.save(p, fs.astype(np.int32))
        fim = _ref(p, root, "npy", "face_index_map")

    parts = []
    used: set[str] = set()
    for part in geo.parts:
        name = part.name
        k = 2
        while name in used:
            name = f"{part.name}_{k}"
            k += 1
        used.add(name)
        p = out_dir / f"{name}.ply"
        write_ply(p, part.vertices, part.faces)
        parts.append(PartRef(
            name=name, role=part.role, mesh=_ref(p, root, "ply", part.role),
            filament_id=part.filament_id, watertight=part.watertight,
        ))

    result = GeometryResult(
        source_glb=glb_ref,
        transform=tuple(tuple(float(x) for x in row) for row in geo.transform),
        figure=fig_ref,
        face_index_map=fim,
        parts=parts,
        bbox_min_mm=tuple(float(x) for x in geo.bbox_min),
        bbox_max_mm=tuple(float(x) for x in geo.bbox_max),
        orientation=geo.contract_orientation,
        supports=geo.supports,
        notes=list(geo.notes),
        warnings=list(geo.warnings),
    )
    (out_dir / "geometry.json").write_text(result.model_dump_json(indent=2))
    (out_dir / "geometry_report.json").write_text(json.dumps(geo.summary(), indent=2))
    return result


def _find_library(start: Path) -> Path | None:
    for d in [start, *start.parents, Path.cwd(), *Path.cwd().parents]:
        p = d / "filaments" / "library.json"
        if p.is_file():
            return p
    return None


def build_product(
    figure_vertices: np.ndarray | str | Path,
    figure_faces: np.ndarray | None = None,
    product: ProductType | str = ProductType.FIGURINE,
    addons: list[AddOn] | None = None,
    target_size_mm: float | None = None,
    library: FilamentLibrary | str | Path | None = None,
    settings: PrintSettings | GeometrySettings | None = None,
    out_dir: Path | str = ".",
    size_tier: SizeTier | str | None = None,
    form: str | None = None,
    colors=None,
) -> GeometryResult:
    """Stage 6. figure_vertices/faces: the Meshy GLB mesh in glTF scene coordinates (Y up,
    front +Z, node transforms applied), faces in GLB order. A GLB path with faces=None also
    works. library=None looks for filaments/library.json above out_dir. size_tier (xs, s, m,
    l) sets the size from SIZE_TIERS_MM and wins over target_size_mm. form: the job's product
    form, 'figure' or 'flat' (product.resolve_form). colors: addon_color.AddonColorInputs for
    add-on part colors. See README."""
    out_dir = Path(out_dir)
    glb = None
    if figure_faces is None:
        glb = Path(figure_vertices)
        mesh = load_meshy_glb(glb, face_colors=False)
        figure_vertices, figure_faces = mesh.vertices, mesh.faces
    if library is None:
        library = _find_library(out_dir.resolve())
    geo = build_product_geometry(
        figure_vertices, figure_faces, product, addons, target_size_mm, library, settings,
        size_tier=size_tier, form=form, colors=colors,
    )
    return write_outputs(geo, out_dir, source_glb=glb)
