"""Plain exports for checking geometry: an STL per part, and a core-spec 3MF with ONE object
whose components are the figure and every part (the slicer unions overlapping parts).

The threemf workstream owns the final multicolor Bambu project; this file is for the slicer
check and for eyeballing in any viewer.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import numpy as np

from .types import ProductGeometry

_CT = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>"""
_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""


def product_meshes(geo: ProductGeometry, include_cutters: bool = False):
    yield "figure", geo.figure.vertices, geo.figure.faces
    for p in geo.parts:
        if p.role == "cutter" and not include_cutters:
            continue
        yield p.name, p.vertices, p.faces


def write_stl_set(geo: ProductGeometry, out_dir: Path | str, stem: str) -> list[Path]:
    import trimesh

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, v, f in product_meshes(geo, include_cutters=True):
        p = out_dir / f"{stem}_{name}.stl"
        trimesh.Trimesh(v, f, process=False).export(p)
        paths.append(p)
    return paths


def _mesh_xml(v: np.ndarray, f: np.ndarray) -> str:
    vs = "\n".join(f'<vertex x="{x:.5f}" y="{y:.5f}" z="{z:.5f}"/>' for x, y, z in v)
    ts = "\n".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in f)
    return f"<mesh><vertices>\n{vs}\n</vertices><triangles>\n{ts}\n</triangles></mesh>"


def write_plain_3mf(geo: ProductGeometry, path: Path | str, bed_center=(128.0, 128.0)) -> Path:
    """One object, one component per part, placed at bed_center (XY) with z unchanged."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    objs = []
    comps = []
    for i, (name, v, f) in enumerate(product_meshes(geo), start=1):
        objs.append(f'<object id="{i}" type="model" name="{name}">{_mesh_xml(v, f)}</object>')
        comps.append(f'<component objectid="{i}"/>')
    top = len(objs) + 1
    c = (geo.bbox_min + geo.bbox_max) / 2
    tx, ty = bed_center[0] - c[0], bed_center[1] - c[1]
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
        f'<metadata name="Title">{geo.product.value}</metadata>\n<resources>\n'
        + "\n".join(objs)
        + f'\n<object id="{top}" type="model" name="{geo.product.value}"><components>'
        + "".join(comps)
        + "</components></object>\n</resources>\n<build>"
        f'<item objectid="{top}" transform="1 0 0 0 1 0 0 0 1 {tx:.4f} {ty:.4f} 0"/>'
        "</build>\n</model>\n"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", model)
    return path
