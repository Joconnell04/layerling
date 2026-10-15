"""Generated test fixtures (no downloads): a hard procedural figure and the five product cases.

The figure is a sitting animal in the Meshy GLB frame (Y up, front +Z, about 1.9 units tall,
like the beagle) with the traps that matter for stage 6:
- two thin ears standing ABOVE the head (the highest points; the loop must not go there),
- four legs whose feet end at different heights (1 to 1.5 mm apart at product size),
- a thin tail sticking out the back,
- Meshy-style defects: UV seams (duplicated vertices), a small hole, and one dangling fin face
  on a non-manifold edge.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from manifold3d import Manifold, OpType
from p2p_contracts.job import AddOn, AddOnKind, ProductType, TextSpec

# print frame (Z up, front -Y) -> glTF (Y up, front +Z): (x, y, z) -> (x, z, -y)
_TO_GLTF = np.array([[1.0, 0, 0], [0, 0, 1.0], [0, -1.0, 0]])


@dataclass
class Figure:
    vertices: np.ndarray  # glTF frame
    faces: np.ndarray
    face_colors: np.ndarray  # (n_faces, 3) uint8
    fin_face: int  # index of the injected fin
    hole_faces_removed: int
    ear_x: float  # |x| of the ear tips (glTF units)
    feet_drop: float  # height difference between feet (units)


def _ellipsoid(r, center, n=48):
    return Manifold.sphere(1.0, n).scale(r).translate(center)


def _cyl(p0, p1, r, n=32):
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    if p1[2] < p0[2]:
        p0, p1 = p1, p0
    d = p1 - p0
    h = float(np.linalg.norm(d))
    m = Manifold.cylinder(h, r, r, n)
    z = np.array([0, 0, 1.0])
    axis = np.cross(z, d / h)
    s = np.linalg.norm(axis)
    if s > 1e-9:
        ang = np.degrees(np.arctan2(s, np.dot(z, d / h)))
        axis = axis / s
        m = _rotate_axis(m, axis, ang)
    return m.translate(tuple(p0))


def _rotate_axis(m, axis, ang_deg):
    a = np.radians(ang_deg)
    k = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    r = np.eye(3) + np.sin(a) * k + (1 - np.cos(a)) * k @ k
    t = np.hstack([r, np.zeros((3, 1))])
    return m.transform(t)


def hard_figure(seed: int = 0, plinth: bool = False) -> Figure:
    """Build in the print frame (units ~ Meshy), union with manifold3d, then add defects.

    plinth=True stands the figure on a Meshy-style disc (0.08 units thick, top at z = 0.08,
    the feet sunk into it)."""
    body = _ellipsoid((0.36, 0.42, 0.46), (0, 0.05, 0.62))
    chest = _ellipsoid((0.30, 0.28, 0.36), (0, -0.18, 0.78))
    head = _ellipsoid((0.33, 0.31, 0.30), (0, -0.20, 1.30))
    snout = _ellipsoid((0.14, 0.14, 0.10), (0, -0.48, 1.22))
    ear_x = 0.19
    ears = [
        _cyl((sx * ear_x, -0.18, 1.45), (sx * (ear_x + 0.03), -0.16, 1.93), 0.035, 16)
        for sx in (-1, 1)
    ]
    feet_drop = 0.06
    legs = [
        _cyl((-0.17, -0.38, 0.85), (-0.17, -0.38, 0.05), 0.075),
        _cyl((0.17, -0.38, 0.85), (0.17, -0.38, 0.05), 0.075),
        _cyl((-0.24, 0.05, 0.40), (-0.24, 0.05, feet_drop + 0.05), 0.09),
        _cyl((0.24, 0.05, 0.40), (0.24, 0.05, feet_drop + 0.05), 0.09),
    ]
    paws = [
        _ellipsoid((0.09, 0.12, 0.05), (x, y, z + 0.05))
        for x, y, z in ((-0.17, -0.38, 0.0), (0.17, -0.38, 0.0),
                        (-0.24, 0.05, feet_drop), (0.24, 0.05, feet_drop))
    ]
    tail = _cyl((0, 0.40, 0.45), (0, 0.72, 0.62), 0.04, 16)
    extra = [Manifold.cylinder(0.08, 0.55, 0.55, 96).translate((0, -0.15, 0.0))] if plinth else []
    solid = Manifold.batch_boolean(
        [body, chest, head, snout, *ears, *legs, *paws, tail, *extra], OpType.Add
    )
    mesh = solid.to_mesh()
    v = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    f = np.asarray(mesh.tri_verts, dtype=np.int64)

    # Per-face colors: ears and snout dark, head brown, body white (for previews).
    c = v[f].mean(axis=1)
    col = np.full((len(f), 3), (235, 232, 225), dtype=np.uint8)
    col[c[:, 2] > 1.05] = (124, 75, 0)
    col[c[:, 2] > 1.47] = (30, 30, 30)
    col[(c[:, 1] < -0.50) & (c[:, 2] > 1.1)] = (22, 22, 22)

    rng = np.random.default_rng(seed)
    # Defect 1: UV seams. Give a third of the faces their own copies of their vertices.
    split = rng.random(len(f)) < 0.33
    idx = np.nonzero(split)[0]
    new_v = v[f[idx]].reshape(-1, 3)
    base = len(v)
    f[idx] = base + np.arange(len(idx) * 3).reshape(-1, 3)
    v = np.concatenate([v, new_v])

    # Defect 2: a small hole on the back of the body (two adjacent faces removed).
    cen = v[f].mean(axis=1)
    back = np.nonzero((cen[:, 1] > 0.3) & (np.abs(cen[:, 0]) < 0.1) & (cen[:, 2] > 0.55)
                      & (cen[:, 2] < 0.75))[0]
    k0 = back[0]
    removed = [k0]
    e0 = {tuple(sorted(e)) for e in ((f[k0, 0], f[k0, 1]), (f[k0, 1], f[k0, 2]), (f[k0, 2], f[k0, 0]))}
    for k in back[1:]:
        ek = {tuple(sorted(e)) for e in ((f[k, 0], f[k, 1]), (f[k, 1], f[k, 2]), (f[k, 2], f[k, 0]))}
        if e0 & ek:
            removed.append(k)
            break
    keep = np.ones(len(f), dtype=bool)
    keep[removed] = False
    f = f[keep]
    col = col[keep]

    # Defect 3: a dangling fin on an existing edge (non-manifold edge, two open edges).
    a, b = f[10, 0], f[10, 1]
    tip = (v[a] + v[b]) / 2 + np.array([0.0, 0.0, 0.002])
    v = np.concatenate([v, tip[None]])
    fin = np.array([[b, a, len(v) - 1]])
    f = np.concatenate([f, fin])
    col = np.concatenate([col, col[10:11]])

    return Figure(
        vertices=v @ _TO_GLTF.T, faces=f, face_colors=col, fin_face=len(f) - 1,
        hole_faces_removed=len(removed), ear_x=ear_x, feet_drop=feet_drop,
    )


# The five product cases (same as p2p_geometry.demo.CASES, spelled out for the tests).
PRODUCT_CASES: dict[str, tuple[ProductType, list[AddOn], float]] = {
    # the flat charm form (the default keychain form is the full 3D figure, see
    # test_geometry_fidelity.py)
    "keychain": (ProductType.KEYCHAIN,
                 [AddOn(kind=AddOnKind.KEYRING_LOOP, params={"form": "flat"})], 45.0),
    "nameplate": (
        ProductType.NAMEPLATE,
        [AddOn(kind=AddOnKind.NAMEPLATE, text=TextSpec(text="BISCUIT"))],
        120.0,
    ),
    "ornament": (ProductType.ORNAMENT, [AddOn(kind=AddOnKind.HANGING_LOOP)], 70.0),
    "magnet": (
        ProductType.MAGNET,
        [AddOn(kind=AddOnKind.FLAT_BACK),
         AddOn(kind=AddOnKind.MAGNET_POCKET, params={"pocket_d_mm": 10, "pocket_depth_mm": 2})],
        60.0,
    ),
    "figurine": (ProductType.FIGURINE, [AddOn(kind=AddOnKind.ROUND_BASE)], 90.0),
}


def standing_figure() -> Figure:
    """A standing animal like the Frenchie concept: long body, head forward and up (the
    highest part), four legs. Its center of mass is well behind the head, so a loop on top of
    the head hangs tilted. Head and ears black, body white (face colors)."""
    body = _ellipsoid((0.30, 0.62, 0.30), (0, 0.10, 0.78))
    head = _ellipsoid((0.30, 0.28, 0.28), (0, -0.55, 1.18))
    neck = _cyl((0, -0.30, 0.95), (0, -0.50, 1.10), 0.22)
    ears = [_ellipsoid((0.08, 0.05, 0.16), (sx * 0.17, -0.52, 1.50)) for sx in (-1, 1)]
    legs = [_cyl((sx * 0.17, sy, 0.70), (sx * 0.17, sy, 0.02), 0.10)
            for sx in (-1, 1) for sy in (-0.30, 0.52)]
    solid = Manifold.batch_boolean([body, head, neck, *ears, *legs], OpType.Add)
    mesh = solid.to_mesh()
    v = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    f = np.asarray(mesh.tri_verts, dtype=np.int64)
    c = v[f].mean(axis=1)
    col = np.full((len(f), 3), (240, 238, 232), dtype=np.uint8)
    col[(c[:, 1] < -0.33) & (c[:, 2] > 0.95)] = (24, 24, 24)
    return Figure(vertices=v @ _TO_GLTF.T, faces=f, face_colors=col, fin_face=-1,
                  hole_faces_removed=0, ear_x=0.17, feet_drop=0.0)
