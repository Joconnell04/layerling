"""Procedural test fixtures standing in for Meshy output until real GLBs exist.

make_figurine(): two chunky animals hugging on a round base (cat on the viewer's left, dog on
the right), built from spheres, ellipsoids, capsules and round cones unioned with manifold3d,
refined to a target face count. Ground-truth paint comes from nearest-primitive part labels plus
pattern rules (stripes, patches, eyes, nose, collar, a thin dog tag). A per-face UV atlas with a
baked base-color texture mimics a Meshy GLB (UV seams everywhere, vertices split per face).

Deliberate test features:
- s1.stripe_thin_*: 0.5 mm vertical forehead stripes, narrower than 2 x 0.4 mm -> eroded
- s1.stripe_*: 1.6 mm back stripes -> survive
- s2.collar: 1.8 mm horizontal band -> survives
- s2.tag_front: white face of a 1.0 mm dog tag with a black back -> bleed flag
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .color import hex_to_rgb, srgb_to_lab
from .mesh import Mesh

PALETTE = [
    ("white", "#F4F4F0"),
    ("black", "#1C1C1E"),
    ("ginger", "#E0702A"),
    ("dark_brown", "#553522"),
    ("pink", "#F29AB2"),
    ("green", "#3E9E4A"),
    ("red", "#C12E1F"),
    ("slate", "#667085"),
]
SLOT = {name: i + 1 for i, (name, _) in enumerate(PALETTE)}


@dataclass
class Fixture:
    mesh: Mesh
    face_regions: np.ndarray
    """(F,) int32 region id, 0 = unlabeled."""
    labels: dict[int, str]
    region_slot: dict[int, int]
    """region id -> 3MF slot (palette index + 1)."""
    palette_hex: list[str]
    palette_names: list[str] = field(default_factory=list)

    @property
    def palette_labs(self) -> np.ndarray:
        return srgb_to_lab(np.array([hex_to_rgb(h) for h in self.palette_hex], dtype=np.uint8))

    @property
    def palette_rgb(self) -> np.ndarray:
        return np.array([hex_to_rgb(h) for h in self.palette_hex], dtype=np.uint8)

    @property
    def face_slots(self) -> np.ndarray:
        lut = np.zeros(max(self.labels) + 1, dtype=np.int16)
        for rid, s in self.region_slot.items():
            lut[rid] = s
        out = lut[self.face_regions]
        out[out == 0] = SLOT["slate"]
        return out

    @property
    def face_colors(self) -> np.ndarray:
        return self.palette_rgb[self.face_slots - 1]


# ---------------------------------------------------------------------- primitives


@dataclass
class Prim:
    part: str  # "s1.body" etc.
    kind: str  # sphere | ellipsoid | capsule | cylz | cyly
    a: np.ndarray
    b: np.ndarray | None = None
    r0: float = 1.0
    r1: float | None = None
    radii: np.ndarray | None = None
    h: float = 0.0

    def sdf(self, p: np.ndarray) -> np.ndarray:
        if self.kind == "sphere":
            return np.linalg.norm(p - self.a, axis=1) - self.r0
        if self.kind == "ellipsoid":
            q = (p - self.a) / self.radii
            return (np.linalg.norm(q, axis=1) - 1.0) * self.radii.min()
        if self.kind == "capsule":
            ab = self.b - self.a
            t = np.clip(((p - self.a) @ ab) / (ab @ ab), 0, 1)
            r = self.r0 if self.r1 is None else self.r0 + (self.r1 - self.r0) * t
            return np.linalg.norm(p - (self.a + t[:, None] * ab), axis=1) - r
        if self.kind in ("cylz", "cyly"):
            ax = 2 if self.kind == "cylz" else 1
            other = [i for i in range(3) if i != ax]
            d = p - self.a
            radial = np.linalg.norm(d[:, other], axis=1) - self.r0
            axial = np.abs(d[:, ax]) - self.h / 2
            return np.maximum(radial, axial)
        raise ValueError(self.kind)

    def manifold(self, segs: int):
        import manifold3d as m3

        if self.kind == "sphere":
            return m3.Manifold.sphere(self.r0, segs).translate(tuple(self.a))
        if self.kind == "ellipsoid":
            return m3.Manifold.sphere(1.0, segs).scale(tuple(self.radii)).translate(tuple(self.a))
        if self.kind == "capsule":
            r1 = self.r1 if self.r1 is not None else self.r0
            s0 = m3.Manifold.sphere(self.r0, segs).translate(tuple(self.a))
            s1 = m3.Manifold.sphere(r1, segs).translate(tuple(self.b))
            return m3.Manifold.batch_hull([s0, s1])
        if self.kind == "cylz":
            c = m3.Manifold.cylinder(self.h, self.r0, self.r0, segs * 2, center=True)
            return c.translate(tuple(self.a))
        if self.kind == "cyly":
            c = m3.Manifold.cylinder(self.h, self.r0, self.r0, segs, center=True)
            return c.rotate((90.0, 0.0, 0.0)).translate(tuple(self.a))
        raise ValueError(self.kind)


def _v(*x) -> np.ndarray:
    return np.array(x, dtype=np.float64)


def figurine_prims() -> list[Prim]:
    P = Prim
    return [
        P("base", "cylz", _v(0, 0, 2.0), r0=27.0, h=4.0),
        # cat (s1), viewer's left = -X
        P("s1.body", "ellipsoid", _v(-11, 2, 14), radii=_v(9, 8, 11)),
        P("s1.head", "sphere", _v(-11, -1, 30), r0=8.5),
        P("s1.ear_left", "capsule", _v(-15.5, 0, 34.5), _v(-17, 0.5, 41), r0=3.4, r1=0.9),
        P("s1.ear_right", "capsule", _v(-6.5, 0, 34.5), _v(-5, 0.5, 41), r0=3.4, r1=0.9),
        P("s1.tail", "capsule", _v(-18, 8, 6), _v(-23, 11, 19), r0=2.6),
        P("s1.paw_left", "sphere", _v(-15, -6.5, 5), r0=3.6),
        P("s1.paw_right", "sphere", _v(-7, -6.5, 5), r0=3.6),
        P("s1.arm", "capsule", _v(-6, -3.5, 22.5), _v(6.5, -5.5, 20.0), r0=2.9),
        # dog (s2), viewer's right = +X
        P("s2.body", "ellipsoid", _v(11, 2, 14), radii=_v(9.5, 8.5, 11)),
        P("s2.head", "sphere", _v(11, -1, 30), r0=8.5),
        P("s2.snout", "capsule", _v(11, -6, 27.5), _v(11, -11, 27), r0=3.8),
        P("s2.ear_left", "ellipsoid", _v(3.2, 0.5, 29), radii=_v(2.2, 3.8, 6.5)),
        P("s2.ear_right", "ellipsoid", _v(18.8, 0.5, 29), radii=_v(2.2, 3.8, 6.5)),
        P("s2.paw_left", "sphere", _v(7, -6.5, 5), r0=3.6),
        P("s2.paw_right", "sphere", _v(15, -6.5, 5), r0=3.6),
        P("s2.arm", "capsule", _v(5.5, -3.5, 17.0), _v(-6.5, -5.5, 15.5), r0=2.9),
        P("s2.tag", "cyly", _v(12.5, -7.6, 18.2), r0=2.7, h=1.0),
        P("s2.tag_ring", "capsule", _v(12.5, -7.5, 20.5), _v(12.5, -5.0, 21.5), r0=0.9),
    ]


def build_manifold_mesh(prims: list[Prim], target_faces: int) -> tuple[np.ndarray, np.ndarray]:
    import manifold3d as m3

    solid = m3.Manifold.batch_boolean([p.manifold(24) for p in prims], m3.OpType.Add)
    # smooth_out turns the coarse union into curved patches (sharp edges over 60 degrees stay
    # sharp, union creases become fillets); refine_to_length then samples it evenly.
    smooth = solid.smooth_out(60.0, 0.0)
    edge = np.sqrt(4 * solid.surface_area() / (np.sqrt(3) * target_faces))
    for _ in range(2):
        refined = smooth.refine_to_length(float(edge))
        edge *= np.sqrt(refined.num_tri() / target_faces)
    mesh = smooth.refine_to_length(float(edge)).to_mesh()
    v = np.asarray(mesh.vert_properties)[:, :3].astype(np.float64)
    f = np.asarray(mesh.tri_verts).astype(np.int64)
    return v, f


# ---------------------------------------------------------------------- paint rules

REGION_LABELS = {
    1: ("base", "slate"),
    2: ("s1.body", "ginger"),
    3: ("s1.stripe_1", "dark_brown"),
    4: ("s1.stripe_2", "dark_brown"),
    5: ("s1.stripe_3", "dark_brown"),
    6: ("s1.stripe_thin_1", "dark_brown"),
    7: ("s1.stripe_thin_2", "dark_brown"),
    8: ("s1.muzzle", "white"),
    9: ("s1.eye_left", "green"),
    10: ("s1.eye_right", "green"),
    11: ("s1.pupil_left", "black"),
    12: ("s1.pupil_right", "black"),
    13: ("s1.nose", "pink"),
    14: ("s1.ear_inner_left", "pink"),
    15: ("s1.ear_inner_right", "pink"),
    16: ("s2.body", "white"),
    17: ("s2.ear_left", "black"),
    18: ("s2.patch_eye", "black"),
    19: ("s2.eye_left", "dark_brown"),
    20: ("s2.eye_right", "dark_brown"),
    21: ("s2.nose", "black"),
    22: ("s2.collar", "red"),
    23: ("s2.tag_front", "white"),
    24: ("s2.tag_back", "black"),
    25: ("s1.tail_tip", "white"),
    26: ("s2.ear_right", "white"),
}


def paint_points(p: np.ndarray, n: np.ndarray, prims: list[Prim]) -> np.ndarray:
    """Ground-truth region id for surface points p with normals n."""
    parts = [pr.part for pr in prims]
    d = np.stack([pr.sdf(p) for pr in prims], axis=1)
    owner = np.array(parts, dtype=object)[np.argmin(d, axis=1)]
    x, y, z = p[:, 0], p[:, 1], p[:, 2]
    reg = np.zeros(len(p), dtype=np.int32)
    is_cat = np.char.startswith(owner.astype(str), "s1.")
    is_dog = np.char.startswith(owner.astype(str), "s2.")
    reg[owner == "base"] = 1
    reg[is_cat] = 2
    reg[is_dog] = 16

    def sph(c, r):
        return np.linalg.norm(p - np.asarray(c), axis=1) < r

    # cat back stripes: vertical bands on the back (y > 3), 1.6 mm wide
    body = (owner == "s1.body") | (owner == "s1.head")
    for rid, x0 in ((3, -15.0), (4, -11.0), (5, -7.0)):
        reg[body & (y > 3.0) & (np.abs(x - x0) < 0.8) & (z > 8)] = rid
    # thin forehead stripes, 0.5 mm wide, vertical
    head = owner == "s1.head"
    for rid, x0 in ((6, -12.4), (7, -9.6)):
        reg[head & (y < -3.0) & (np.abs(x - x0) < 0.25) & (z > 33.0) & (z < 37.5)] = rid
    reg[head & sph((-11, -8.0, 26.6), 4.2)] = 8
    reg[head & sph((-14.0, -7.4, 31.2), 1.9)] = 9
    reg[head & sph((-8.0, -7.4, 31.2), 1.9)] = 10
    reg[head & sph((-14.2, -8.6, 31.0), 0.9)] = 11
    reg[head & sph((-7.8, -8.6, 31.0), 0.9)] = 12
    reg[head & sph((-11, -9.4, 28.6), 1.3)] = 13
    for rid, part in ((14, "s1.ear_left"), (15, "s1.ear_right")):
        reg[(owner == part) & (n[:, 1] < -0.55)] = rid
    reg[(owner == "s1.tail") & (z > 15.5)] = 25
    # dog
    reg[owner == "s2.ear_left"] = 17
    reg[owner == "s2.ear_right"] = 26
    dhead = (owner == "s2.head") | (owner == "s2.snout")
    reg[dhead & sph((7.4, -7.0, 32.0), 3.9)] = 18
    reg[dhead & sph((7.4, -7.6, 31.6), 1.5)] = 19
    reg[dhead & sph((14.6, -7.6, 31.6), 1.5)] = 20
    reg[(owner == "s2.snout") & sph((11, -14.6, 27.4), 2.4)] = 21
    neck = is_dog & (np.abs(z - 22.8) < 0.9) & (owner != "s2.tag_ring")
    reg[neck] = 22
    tag = owner == "s2.tag"
    reg[tag] = 24
    reg[tag & (n[:, 1] < -0.7)] = 23
    reg[owner == "s2.tag_ring"] = 24
    return reg


# ---------------------------------------------------------------------- atlas + texture

CELL = 4  # texels per atlas cell side; one face per cell


def _atlas(n_faces: int) -> tuple[np.ndarray, int]:
    """Per-face UV triangles in a grid atlas. Returns (F, 3, 2) uv and texture size."""
    per_row = int(np.ceil(np.sqrt(n_faces)))
    size = per_row * CELL
    size = int(2 ** np.ceil(np.log2(size)))
    per_row = size // CELL
    i = np.arange(n_faces)
    cx, cy = (i % per_row) * CELL, (i // per_row) * CELL
    # corners at texel centers: (0.5, 0.5), (CELL - 0.5, 0.5), (0.5, CELL - 0.5) in texel units
    corners = np.array([[0.5, 0.5], [CELL - 0.5, 0.5], [0.5, CELL - 0.5]])
    px = cx[:, None] + corners[None, :, 0]
    py = cy[:, None] + corners[None, :, 1]
    uv = np.stack([px / size, 1.0 - py / size], axis=-1)  # OpenGL v-up
    return uv.astype(np.float32), size


def bake_texture(tri: np.ndarray, normals: np.ndarray, prims, region_rgb: np.ndarray):
    """Per-face atlas texture: every texel of a face's cell gets the paint at its 3D point."""
    n_faces = len(tri)
    uv, size = _atlas(n_faces)
    per_row = size // CELL
    tex = np.zeros((size, size, 3), dtype=np.uint8)
    tex[:] = region_rgb[1]
    ii, jj = np.meshgrid(np.arange(CELL), np.arange(CELL), indexing="xy")
    s = (np.minimum(ii, CELL - 1 - jj) / (CELL - 1)).reshape(-1)  # mirror unused half inward
    t = (np.minimum(jj, CELL - 1 - ii) / (CELL - 1)).reshape(-1)
    b = np.stack([1 - s - t, s, t], axis=1)  # (C*C, 3)
    chunk = 20000
    for f0 in range(0, n_faces, chunk):
        f1 = min(n_faces, f0 + chunk)
        pts = np.einsum("ck,fkd->fcd", b, tri[f0:f1]).reshape(-1, 3)
        nn = np.repeat(normals[f0:f1], len(b), axis=0)
        reg = paint_points(pts, nn, prims)
        col = region_rgb[reg].reshape(f1 - f0, CELL, CELL, 3)
        fi = np.arange(f0, f1)
        cx, cy = (fi % per_row) * CELL, (fi // per_row) * CELL
        for dy in range(CELL):
            for dx in range(CELL):
                tex[cy + dy, cx + dx] = col[:, dy, dx]
    return uv, tex


def make_figurine(target_faces: int = 50_000, texture: bool = True) -> Fixture:
    prims = figurine_prims()
    v, f = build_manifold_mesh(prims, target_faces)
    # The layout is already in the contract frame: the base cylinder is centered on the origin
    # and sits on z = 0, so the paint rules can use absolute coordinates.
    mesh = Mesh(v, f, name=f"figurine_{target_faces // 1000}k")
    face_regions = paint_points(mesh.face_centroids, mesh.face_normals, prims)
    pal_rgb = np.array([hex_to_rgb(h) for _, h in PALETTE], dtype=np.uint8)
    region_rgb = np.zeros((max(REGION_LABELS) + 1, 3), dtype=np.uint8)
    region_rgb[0] = pal_rgb[SLOT["slate"] - 1]
    for rid, (_, color) in REGION_LABELS.items():
        region_rgb[rid] = pal_rgb[SLOT[color] - 1]
    if texture:
        uvf, tex = bake_texture(mesh.triangles, mesh.face_normals, prims, region_rgb)
        # split vertices per face so each face owns its UV triangle (like a Meshy atlas seam)
        verts = mesh.triangles.reshape(-1, 3)
        faces = np.arange(len(verts)).reshape(-1, 3)
        mesh = Mesh(verts, faces, uv=uvf.reshape(-1, 2), texture=tex, name=mesh.name)
    labels = {rid: name for rid, (name, _) in REGION_LABELS.items()}
    region_slot = {rid: SLOT[color] for rid, (_, color) in REGION_LABELS.items()}
    return Fixture(mesh, face_regions.astype(np.int32), labels, region_slot,
                   [h for _, h in PALETTE], [n for n, _ in PALETTE])


# ---------------------------------------------------------------------- small analytic meshes


def grid_plane(
    width: float, depth: float, spacing: float, z: float = 0.0, vertical: bool = False
) -> Mesh:
    """Regular triangulated rectangle. Horizontal (normal +Z) or vertical (normal -Y, front)."""
    nx = round(width / spacing)
    ny = round(depth / spacing)
    xs = np.linspace(-width / 2, width / 2, nx + 1)
    ys = np.linspace(-depth / 2, depth / 2, ny + 1)
    X, Y = np.meshgrid(xs, ys, indexing="xy")
    if vertical:
        v = np.stack([X.ravel(), np.zeros(X.size), (Y + depth / 2).ravel() + z], axis=1)
    else:
        v = np.stack([X.ravel(), Y.ravel(), np.full(X.size, z)], axis=1)
    idx = np.arange((nx + 1) * (ny + 1)).reshape(ny + 1, nx + 1)
    a, b = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel()
    c, d = idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
    if vertical:  # wind so normals face -Y
        f = np.concatenate([np.stack([a, c, b], 1), np.stack([b, c, d], 1)])
    else:
        f = np.concatenate([np.stack([a, b, c], 1), np.stack([b, d, c], 1)])
    return Mesh(v, f, name="grid")


def sphere_mesh(radius: float = 10.0, spacing: float = 0.25, center=(0.0, 0.0, 0.0)) -> Mesh:
    """Evenly triangulated sphere (manifold3d sphere refined, vertices pushed onto the sphere)."""
    import manifold3d as m3

    s = m3.Manifold.sphere(float(radius), 96).refine_to_length(float(spacing))
    mm = s.to_mesh()
    v = np.asarray(mm.vert_properties)[:, :3].astype(np.float64)
    v = v / np.linalg.norm(v, axis=1, keepdims=True) * radius + np.asarray(center, dtype=float)
    return Mesh(v, np.asarray(mm.tri_verts).astype(np.int64), name="sphere")


def box_mesh(size=(20.0, 20.0, 2.0), spacing: float = 0.5, center=(0.0, 0.0, 1.0)) -> Mesh:
    """Closed box with a regular triangulation on every side (outward normals)."""
    import manifold3d as m3

    _ = size
    c = m3.Manifold.cube(tuple(float(s) for s in size), True).translate(tuple(center))
    c = c.refine_to_length(float(spacing))
    mm = c.to_mesh()
    return Mesh(np.asarray(mm.vert_properties)[:, :3].astype(np.float64),
                np.asarray(mm.tri_verts).astype(np.int64), name="box")
