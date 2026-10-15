"""Mesh I/O and conversions: Meshy GLB loading, frame change, manifold3d round trips."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from manifold3d import CrossSection, Error, FillRule, Manifold, Mesh64

# glTF (Y up, front +Z) -> print frame (Z up, front -Y): (x, y, z) -> (x, -z, y). See regions.py.
GLTF_TO_PRINT = np.array(
    [[1.0, 0, 0, 0], [0, 0, -1.0, 0], [0, 1.0, 0, 0], [0, 0, 0, 1.0]], dtype=np.float64
)


@dataclass
class MeshyMesh:
    vertices: np.ndarray  # (n, 3) glTF scene coordinates, node transforms applied
    faces: np.ndarray  # (m, 3), exact GLB triangle order
    face_colors: np.ndarray | None  # (m, 3) uint8 sampled from the texture, for previews


def load_meshy_glb(path: str | Path, face_colors: bool = True) -> MeshyMesh:
    """Load a Meshy GLB keeping its triangle order (no merging, no reordering)."""
    import trimesh

    scene = trimesh.load(str(path), process=False, force="scene")
    meshes = []
    for node in scene.graph.nodes_geometry:
        transform, geom_name = scene.graph[node]
        g = scene.geometry[geom_name]
        v = trimesh.transform_points(np.asarray(g.vertices, dtype=np.float64), transform)
        meshes.append((v, np.asarray(g.faces, dtype=np.int64), g))
    offset = 0
    vs, fs, cs = [], [], []
    for v, f, g in meshes:
        vs.append(v)
        fs.append(f + offset)
        offset += len(v)
        if face_colors:
            cs.append(_face_colors(g))
    colors = None
    if face_colors and all(c is not None for c in cs):
        colors = np.concatenate(cs)
    return MeshyMesh(np.concatenate(vs), np.concatenate(fs), colors)


def _face_colors(g) -> np.ndarray | None:
    try:
        vc = g.visual.to_color().vertex_colors[:, :3].astype(np.float64)
    except Exception:
        return None
    return vc[g.faces].mean(axis=1).astype(np.uint8)


def apply_transform(vertices: np.ndarray, transform: np.ndarray) -> np.ndarray:
    v = np.asarray(vertices, dtype=np.float64)
    return v @ transform[:3, :3].T + transform[:3, 3]


def to_manifold(vertices: np.ndarray, faces: np.ndarray) -> Manifold:
    mesh = Mesh64(
        vert_properties=np.ascontiguousarray(vertices, dtype=np.float64),
        tri_verts=np.ascontiguousarray(faces, dtype=np.uint64),
    )
    m = Manifold(mesh)
    if m.status() != Error.NoError:
        raise ValueError(f"not a closed 2-manifold: {m.status()}")
    return m


def try_manifold(vertices: np.ndarray, faces: np.ndarray) -> Manifold | None:
    try:
        return to_manifold(vertices, faces)
    except ValueError:
        return None


def from_manifold(m: Manifold) -> tuple[np.ndarray, np.ndarray]:
    mesh = m.to_mesh64()
    v = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3].copy()
    f = np.asarray(mesh.tri_verts, dtype=np.int64).copy()
    return v, f


def cross_section_to_shapely(cs: CrossSection):
    """CrossSection -> shapely (Multi)Polygon using even-odd nesting of its contours."""
    from shapely.geometry import MultiPolygon, Polygon
    from shapely.ops import unary_union

    polys = [np.asarray(p) for p in cs.to_polygons() if len(p) >= 3]
    if not polys:
        return MultiPolygon()
    shells = []
    holes = []
    for p in polys:
        area = _signed_area(p)
        (shells if area > 0 else holes).append(Polygon(p))
    shape = unary_union(shells)
    for h in holes:
        shape = shape.difference(h)
    return shape


def shapely_to_cross_section(shape) -> CrossSection:
    from shapely.geometry import MultiPolygon, Polygon

    geoms = [shape] if isinstance(shape, Polygon) else list(getattr(shape, "geoms", []))
    contours = []
    for g in geoms:
        if not isinstance(g, Polygon) or g.is_empty:
            continue
        ext = np.asarray(g.exterior.coords)[:-1]
        if _signed_area(ext) < 0:
            ext = ext[::-1]
        contours.append(ext)
        for ring in g.interiors:
            r = np.asarray(ring.coords)[:-1]
            if _signed_area(r) > 0:
                r = r[::-1]
            contours.append(r)
    if not contours:
        return CrossSection()
    _ = MultiPolygon  # keep import for type checkers
    return CrossSection(contours, FillRule.Positive)


def _signed_area(p: np.ndarray) -> float:
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def projected_silhouette(vertices: np.ndarray, faces: np.ndarray) -> CrossSection:
    """XY shadow of any triangle soup (works on meshes that are not manifold).

    The shadow is the union of the triangles, each turned counterclockwise: the points whose
    winding number (how many triangles cover them) is positive. Winding numbers add up edge
    by edge, so an XY edge used once in each direction contributes nothing and is dropped;
    the edges left (silhouette folds, open borders) are chained into closed loops with the
    same winding everywhere, and FillRule.Positive gives the same region as the union of
    every triangle. On the sombrero's 1,291,050 faces 130,911 of 3.9M edges are left and the
    call takes under 1 s instead of 83 s (the union of every triangle was most of s06).
    """
    xy = np.asarray(vertices, dtype=np.float64)[:, :2] + 0.0  # -0.0 and 0.0: one point
    f = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
    if len(f) == 0:
        return CrossSection()
    tri = xy[f]
    area = 0.5 * (
        (tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1])
        - (tri[:, 2, 0] - tri[:, 0, 0]) * (tri[:, 1, 1] - tri[:, 0, 1])
    )
    keep = np.abs(area) > 1e-9
    f = f[keep]
    if len(f) == 0:
        return CrossSection()
    flip = area[keep] < 0
    f[flip] = f[flip][:, ::-1]
    pts, g = _xy_points(xy, f)
    src, dst = _net_edges(g, len(pts))
    if len(src) == 0:  # every edge cancelled: no point has a positive winding
        return CrossSection()
    return CrossSection([pts[loop] for loop in _loops(src, dst)], FillRule.Positive)


def _xy_points(xy: np.ndarray, f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The faces' distinct XY points (bit-exact) and the faces renumbered to them."""
    used = np.unique(f)
    pts = np.ascontiguousarray(xy[used])
    key = pts.view(np.dtype((np.void, pts.dtype.itemsize * 2))).ravel()
    _, first, vid = np.unique(key, return_index=True, return_inverse=True)
    remap = np.empty(len(xy), dtype=np.int64)
    remap[used] = vid.ravel()
    return pts[first], remap[f]


def _net_edges(g: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """The triangles' directed edges with opposite pairs cancelled, each left edge repeated
    by its net count (two triangles folded over one edge keep it twice)."""
    src = g.ravel()
    dst = g[:, [1, 2, 0]].ravel()
    lo, hi = np.minimum(src, dst), np.maximum(src, dst)
    keys, inv = np.unique(lo * np.int64(n) + hi, return_inverse=True)
    net = np.bincount(inv.ravel(), weights=np.where(src < dst, 1.0, -1.0),
                      minlength=len(keys)).round().astype(np.int64)
    nz = net != 0
    keys, net = keys[nz], net[nz]
    a, b = keys // n, keys % n
    reps = np.abs(net)
    return (np.repeat(np.where(net > 0, a, b), reps),
            np.repeat(np.where(net > 0, b, a), reps))


def _loops(src: np.ndarray, dst: np.ndarray) -> list[np.ndarray]:
    """Closed loops that use every directed edge once. Each point has as many edges out as
    in (a triangle adds one of each, a cancelled pair removes one of each), so a walk that
    leaves a point always comes back to it."""
    order = np.argsort(src, kind="stable")
    src, dst = src[order], dst[order]
    nv = int(max(src.max(), dst.max())) + 1
    starts = np.searchsorted(src, np.arange(nv + 1))
    nxt, end, to = starts[:-1].tolist(), starts[1:].tolist(), dst.tolist()
    loops: list[np.ndarray] = []
    for v in np.unique(src).tolist():
        while nxt[v] < end[v]:
            path, u = [v], v
            while True:
                e = nxt[u]
                if e >= end[u]:  # only an unbalanced edge set gets here
                    raise ValueError("silhouette edges do not close")
                nxt[u] = e + 1
                u = to[e]
                if u == v:
                    break
                path.append(u)
            loops.append(np.asarray(path, dtype=np.int64))
    return loops
