"""Mesh container, GLB/PLY IO, and the contract geometry frame.

Contract frame (p2p_contracts.regions): mm, Z up, the model's front faces -Y, base on z = 0,
XY bbox centered on the origin. Meshy glTF is Y up with the front toward +Z, so
(x, y, z)_gltf -> (x, -z, y)_ours, then scaled.

Face order is sacred: face i here is face i in the file, because face_regions.npy indexes it.
Vertices may be duplicated along UV seams (as in every Meshy GLB); topology helpers work on
position-welded vertex ids so adjacency crosses those seams.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import numpy as np

GLTF_TO_CONTRACT = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])
"""Row-vector rotation: p_ours = GLTF_TO_CONTRACT @ p_gltf (x, y, z) -> (x, -z, y)."""


@dataclass(eq=False)
class Mesh:
    vertices: np.ndarray
    """(V, 3) float64, mm, contract frame."""
    faces: np.ndarray
    """(F, 3) int64 vertex indices, file order."""
    uv: np.ndarray | None = None
    """(V, 2) float32 texture coords, OpenGL convention (v up; row = (1 - v) * H)."""
    texture: np.ndarray | None = None
    """(H, W, 3) uint8 base-color texture."""
    vertex_colors: np.ndarray | None = None
    """(V, 3) uint8, if the file had them and no texture."""
    name: str = ""
    _cache: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.vertices = np.ascontiguousarray(self.vertices, dtype=np.float64)
        self.faces = np.ascontiguousarray(self.faces, dtype=np.int64)
        if self.uv is not None:
            self.uv = np.ascontiguousarray(self.uv, dtype=np.float32)
        if self.faces.ndim != 2 or self.faces.shape[1] != 3:
            raise ValueError("faces must be (F, 3)")

    # ------------------------------------------------------------------ basics

    @property
    def n_faces(self) -> int:
        return int(self.faces.shape[0])

    @property
    def n_vertices(self) -> int:
        return int(self.vertices.shape[0])

    @cached_property
    def triangles(self) -> np.ndarray:
        """(F, 3, 3) corner positions."""
        return self.vertices[self.faces]

    @cached_property
    def _cross(self) -> np.ndarray:
        t = self.triangles
        return np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])

    @cached_property
    def face_areas(self) -> np.ndarray:
        return 0.5 * np.linalg.norm(self._cross, axis=1)

    @cached_property
    def face_normals(self) -> np.ndarray:
        c = self._cross
        n = np.linalg.norm(c, axis=1, keepdims=True)
        out = np.zeros_like(c)
        ok = n[:, 0] > 0
        out[ok] = c[ok] / n[ok]
        return out

    @cached_property
    def face_centroids(self) -> np.ndarray:
        return self.triangles.mean(axis=1)

    @cached_property
    def face_edge_len(self) -> np.ndarray:
        """(F,) mean edge length, a local resolution measure."""
        t = self.triangles
        e = np.linalg.norm(t - np.roll(t, -1, axis=1), axis=2)
        return e.mean(axis=1)

    @cached_property
    def bounds(self) -> np.ndarray:
        return np.stack([self.vertices.min(axis=0), self.vertices.max(axis=0)])

    @cached_property
    def bounding_sphere(self) -> tuple[np.ndarray, float]:
        """(center, radius) using the bbox center."""
        c = self.bounds.mean(axis=0)
        r = float(np.linalg.norm(self.vertices - c, axis=1).max()) if self.n_vertices else 1.0
        return c, max(r, 1e-6)

    # ------------------------------------------------------------------ topology

    @cached_property
    def weld(self) -> np.ndarray:
        """(V,) id of each vertex after merging identical positions (1e-6 mm grid)."""
        q = np.round(self.vertices * 1e6).astype(np.int64)
        _, inv = np.unique(q, axis=0, return_inverse=True)
        return inv.reshape(-1).astype(np.int64)

    @cached_property
    def welded_faces(self) -> np.ndarray:
        return self.weld[self.faces]

    @cached_property
    def welded_vertices(self) -> np.ndarray:
        n = int(self.weld.max()) + 1 if self.n_vertices else 0
        out = np.zeros((n, 3))
        out[self.weld] = self.vertices
        return out

    @cached_property
    def _edges(self) -> dict[str, np.ndarray]:
        wf = self.welded_faces
        f = self.n_faces
        e = np.stack([wf[:, [0, 1]], wf[:, [1, 2]], wf[:, [2, 0]]], axis=1).reshape(-1, 2)
        es = np.sort(e, axis=1)
        nv = int(wf.max()) + 1 if f else 1
        key = es[:, 0] * nv + es[:, 1]
        order = np.argsort(key, kind="stable")
        ks = key[order]
        same = ks[1:] == ks[:-1]
        i = order[:-1][same]
        j = order[1:][same]
        fa, fb = i // 3, j // 3
        keep = fa != fb
        fa, fb, i = fa[keep], fb[keep], i[keep]
        # boundary (unpaired) edges
        paired = np.zeros(ks.shape[0], dtype=bool)
        paired[:-1] |= same
        paired[1:] |= same
        return {
            "face_adjacency": np.stack([fa, fb], axis=1),
            "adjacency_edges": es[i],
            "boundary_edges": es[order[~paired]],
        }

    @property
    def face_adjacency(self) -> np.ndarray:
        """(E, 2) pairs of faces sharing an edge (welded). Non-manifold edges give chains."""
        return self._edges["face_adjacency"]

    @property
    def adjacency_edges(self) -> np.ndarray:
        """(E, 2) welded vertex ids of the edge shared by each face_adjacency pair."""
        return self._edges["adjacency_edges"]

    @cached_property
    def adjacency_edge_len(self) -> np.ndarray:
        wv = self.welded_vertices
        e = self.adjacency_edges
        return np.linalg.norm(wv[e[:, 0]] - wv[e[:, 1]], axis=1)

    @property
    def is_watertight(self) -> bool:
        return self._edges["boundary_edges"].shape[0] == 0

    @cached_property
    def vertex_normals(self) -> np.ndarray:
        """(V, 3) area-weighted normals, smooth across UV seams."""
        nw = int(self.weld.max()) + 1 if self.n_vertices else 0
        acc = np.zeros((nw, 3))
        for k in range(3):
            np.add.at(acc, self.welded_faces[:, k], self._cross)
        n = np.linalg.norm(acc, axis=1, keepdims=True)
        acc = np.divide(acc, n, out=np.zeros_like(acc), where=n > 0)
        return acc[self.weld]

    def face_graph(self, mask_pairs: np.ndarray | None = None, metric_z: float = 1.0):
        """Symmetric CSR graph over faces weighted by centroid distance.

        mask_pairs: optional bool (E,) selecting which adjacency pairs to keep.
        metric_z: scale applied to the z component of distances (anisotropic metric).
        """
        from scipy.sparse import csr_matrix

        adj = self.face_adjacency
        if mask_pairs is not None:
            adj = adj[mask_pairs]
        d = self.face_centroids[adj[:, 0]] - self.face_centroids[adj[:, 1]]
        if metric_z != 1.0:
            d = d * np.array([1.0, 1.0, metric_z])
        w = np.linalg.norm(d, axis=1) + 1e-9
        f = self.n_faces
        rows = np.concatenate([adj[:, 0], adj[:, 1]])
        cols = np.concatenate([adj[:, 1], adj[:, 0]])
        return csr_matrix((np.concatenate([w, w]), (rows, cols)), shape=(f, f))

    # ------------------------------------------------------------------ measures

    @cached_property
    def volume_mm3(self) -> float:
        """Signed-tetrahedron volume. Exact for closed, consistently wound meshes."""
        t = self.triangles
        return float(abs(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum()) / 6.0)

    @property
    def surface_area_mm2(self) -> float:
        return float(self.face_areas.sum())

    def copy(self) -> Mesh:
        return Mesh(
            vertices=self.vertices.copy(),
            faces=self.faces.copy(),
            uv=None if self.uv is None else self.uv.copy(),
            texture=self.texture,
            vertex_colors=None if self.vertex_colors is None else self.vertex_colors.copy(),
            name=self.name,
        )

    def face_texture_colors(self) -> np.ndarray | None:
        """(F, 3) uint8 texture color sampled at each face centroid's UV, or None."""
        if self.texture is None or self.uv is None:
            if self.vertex_colors is not None:
                return self.vertex_colors[self.faces].mean(axis=1).round().astype(np.uint8)
            return None
        uvc = self.uv[self.faces].mean(axis=1)
        return sample_texture(self.texture, uvc, bilinear=False)


# ---------------------------------------------------------------------- texture


def sample_texture(tex: np.ndarray, uv: np.ndarray, bilinear: bool = True) -> np.ndarray:
    """Sample (H, W, C) uint8 texture at (N, 2) uv (repeat wrap, OpenGL v-up). Returns (N, 3)."""
    h, w = tex.shape[:2]
    u = np.mod(uv[..., 0], 1.0) * w - 0.5
    v = (1.0 - np.mod(uv[..., 1], 1.0)) * h - 0.5
    rgb = tex[..., :3]
    if not bilinear:
        x = np.clip(np.floor(u + 0.5).astype(np.int64), 0, w - 1)
        y = np.clip(np.floor(v + 0.5).astype(np.int64), 0, h - 1)
        return rgb[y, x]
    x0 = np.floor(u).astype(np.int64)
    y0 = np.floor(v).astype(np.int64)
    fx = (u - x0)[..., None]
    fy = (v - y0)[..., None]
    x0c, x1c = np.clip(x0, 0, w - 1), np.clip(x0 + 1, 0, w - 1)
    y0c, y1c = np.clip(y0, 0, h - 1), np.clip(y0 + 1, 0, h - 1)
    c = (
        rgb[y0c, x0c] * (1 - fx) * (1 - fy)
        + rgb[y0c, x1c] * fx * (1 - fy)
        + rgb[y1c, x0c] * (1 - fx) * fy
        + rgb[y1c, x1c] * fx * fy
    )
    return np.clip(np.round(c), 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------- frame


def to_contract_frame(
    vertices: np.ndarray, target_size_mm: float | None = None, from_gltf: bool = True
) -> np.ndarray:
    """Rotate glTF (Y up, front +Z) into the contract frame, scale, sit on z = 0, center XY.

    target_size_mm: the largest bbox extent after scaling. None means glTF meters -> mm (x1000).
    """
    v = np.asarray(vertices, dtype=np.float64)
    if from_gltf:
        v = v @ GLTF_TO_CONTRACT.T
    if v.shape[0] == 0:
        return v
    lo, hi = v.min(axis=0), v.max(axis=0)
    ext = float((hi - lo).max())
    if target_size_mm is not None:
        if ext <= 0:
            raise ValueError("degenerate mesh extent")
        v = v * (target_size_mm / ext)
    else:
        v = v * 1000.0
    lo, hi = v.min(axis=0), v.max(axis=0)
    shift = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    return v + shift


# ---------------------------------------------------------------------- IO


def _extract_texture(visual) -> tuple[np.ndarray | None, np.ndarray | None]:
    """(uv, texture) from a trimesh visual, or (None, None)."""
    uv = getattr(visual, "uv", None)
    mat = getattr(visual, "material", None)
    img = None
    if mat is not None:
        img = getattr(mat, "baseColorTexture", None)
        if img is None:
            img = getattr(mat, "image", None)
    tex = None
    if img is not None:
        tex = np.asarray(img.convert("RGB"), dtype=np.uint8)
    elif mat is not None and getattr(mat, "baseColorFactor", None) is not None and uv is not None:
        f = np.asarray(mat.baseColorFactor, dtype=np.float64).reshape(-1)[:3]
        if f.max() <= 1.0:
            f = f * 255
        tex = np.full((1, 1, 3), np.round(f), dtype=np.uint8)
    return (None if uv is None else np.asarray(uv, dtype=np.float32)), tex


def load_glb(path: str | Path, target_size_mm: float | None = None) -> Mesh:
    """Load a GLB/glTF into the contract frame, keeping face order, UVs and base-color texture.

    Multiple primitives are concatenated in scene-graph order. Only one texture is kept (the
    first textured primitive); others get their texture sampled into vertex colors is not
    attempted: a warning is raised instead. Meshy exports one primitive.
    """
    import trimesh

    scene = trimesh.load(str(path), force="scene", process=False, maintain_order=True)
    verts, faces, uvs, texture = [], [], [], None
    offset = 0
    any_uv = False
    for node in scene.graph.nodes_geometry:
        transform, geom_name = scene.graph[node]
        g = scene.geometry[geom_name]
        if not hasattr(g, "faces") or len(g.faces) == 0:
            continue
        v = trimesh.transform_points(np.asarray(g.vertices, dtype=np.float64), transform)
        uv, tex = _extract_texture(g.visual)
        if uv is not None and len(uv) == len(v):
            any_uv = True
            if tex is not None:
                if texture is None:
                    texture = tex
                elif tex.shape != texture.shape or not np.array_equal(tex, texture):
                    warnings.warn(f"{path}: several textures; only the first is kept", stacklevel=2)
        else:
            uv = None
        verts.append(v)
        faces.append(np.asarray(g.faces, dtype=np.int64) + offset)
        uvs.append(uv if uv is not None else np.zeros((len(v), 2), dtype=np.float32))
        offset += len(v)
    if not faces:
        raise ValueError(f"{path}: no triangle geometry")
    v = to_contract_frame(np.concatenate(verts), target_size_mm, from_gltf=True)
    return Mesh(
        vertices=v,
        faces=np.concatenate(faces),
        uv=np.concatenate(uvs) if any_uv else None,
        texture=texture if any_uv else None,
        name=Path(path).stem,
    )


def load_ply(path: str | Path) -> Mesh:
    """Load a PLY that is already in the contract frame (as every pipeline-written PLY is)."""
    import trimesh

    m = trimesh.load(str(path), process=False, maintain_order=True, force="mesh")
    vc = None
    uv, tex = None, None
    vis = m.visual
    if getattr(vis, "kind", None) == "vertex":
        vc = np.asarray(vis.vertex_colors)[:, :3].astype(np.uint8)
    elif getattr(vis, "kind", None) == "texture":
        uv, tex = _extract_texture(vis)
    return Mesh(
        vertices=np.asarray(m.vertices, dtype=np.float64),
        faces=np.asarray(m.faces, dtype=np.int64),
        uv=uv,
        texture=tex,
        vertex_colors=vc,
        name=Path(path).stem,
    )


def load_mesh(path: str | Path, target_size_mm: float | None = None) -> Mesh:
    """GLB/glTF -> converted into the contract frame; PLY -> read as is (already converted)."""
    suffix = Path(path).suffix.lower()
    if suffix in (".glb", ".gltf"):
        return load_glb(path, target_size_mm=target_size_mm)
    if suffix == ".ply":
        m = load_ply(path)
        if target_size_mm is not None:
            m.vertices = to_contract_frame(m.vertices, target_size_mm, from_gltf=False)
            m._cache.clear()
        return m
    raise ValueError(f"unsupported mesh format: {suffix}")


def save_ply(mesh: Mesh, path: str | Path, face_colors: np.ndarray | None = None) -> Path:
    """Binary PLY with vertices and faces in file order (plus optional per-face colors)."""
    import trimesh

    m = trimesh.Trimesh(vertices=mesh.vertices, faces=mesh.faces, process=False, validate=False)
    if face_colors is not None:
        fc = np.asarray(face_colors, dtype=np.uint8)
        m.visual.face_colors = np.concatenate([fc[:, :3], np.full((len(fc), 1), 255, np.uint8)], 1)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(m.export(file_type="ply", encoding="binary"))
    return path


def save_glb(mesh: Mesh, path: str | Path) -> Path:
    """Write a GLB in glTF convention (inverse of load_glb's frame change, meters)."""
    import trimesh
    from PIL import Image

    v = (mesh.vertices / 1000.0) @ GLTF_TO_CONTRACT  # inverse rotation (orthonormal)
    visual = None
    if mesh.uv is not None and mesh.texture is not None:
        mat = trimesh.visual.material.PBRMaterial(
            baseColorTexture=Image.fromarray(mesh.texture[..., :3]), metallicFactor=0.0,
            roughnessFactor=1.0,
        )
        visual = trimesh.visual.TextureVisuals(uv=mesh.uv, material=mat)
    m = trimesh.Trimesh(vertices=v, faces=mesh.faces, visual=visual, process=False, validate=False)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(m.export(file_type="glb"))
    return path
