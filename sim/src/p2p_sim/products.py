"""Procedural product fixtures that stress the simulator: realistic, hard cases.

- nameplate(): 70 x 22 x 3 mm plate with 1.0 mm tall raised block text. Row one uses 1.0 mm
  strokes, row two 0.5 mm strokes. Three paint modes:
    "tops"  one part; only the flat letter tops are painted the text color (sides stay plate
            color). The 0.5 mm tops are narrower than 2 x 0.4 mm and must merge.
    "full"  one part; letter tops and sides painted.
    "parts" two parts, plate and text (text sunk 0.2 mm into the plate), each with a filament.
- keychain(): 45 mm cat-head keychain printed lying flat (face up), gently domed top, key loop.
  Painted eyes, nose, mouth, whiskers and highlights sit on both sides of the 0.8 mm limit.
- multipart_figure(): a penguin figure part, a key loop part through the top of the head, a
  beak part and a base plate part, all overlapping; the figure also carries painted belly paint.

Each returns a Product with the mesh, per-face slots, region ids, labels, palette and, for
multi-part prints, face_parts and part_slots.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .color import hex_to_rgb, srgb_to_lab
from .mesh import Mesh


@dataclass
class Product:
    name: str
    mesh: Mesh
    face_slots: np.ndarray
    face_regions: np.ndarray
    labels: dict[int, str]
    palette_hex: list[str]
    face_parts: np.ndarray | None = None
    part_slots: np.ndarray | None = None
    info: dict = field(default_factory=dict)

    @property
    def palette_labs(self) -> np.ndarray:
        return srgb_to_lab(np.array([hex_to_rgb(h) for h in self.palette_hex], dtype=np.uint8))

    @property
    def palette_rgb(self) -> np.ndarray:
        return np.array([hex_to_rgb(h) for h in self.palette_hex], dtype=np.uint8)

    @property
    def face_colors(self) -> np.ndarray:
        return self.palette_rgb[self.face_slots - 1]

    def region_id(self, label: str) -> int:
        return next(k for k, v in self.labels.items() if v == label)


def _mesh_of(manifold) -> Mesh:
    mm = manifold.to_mesh()
    return Mesh(np.asarray(mm.vert_properties)[:, :3].astype(np.float64),
                np.asarray(mm.tri_verts).astype(np.int64))


def _concat(meshes: list[Mesh]) -> tuple[Mesh, np.ndarray]:
    verts, faces, parts, off = [], [], [], 0
    for i, m in enumerate(meshes):
        verts.append(m.vertices)
        faces.append(m.faces + off)
        parts.append(np.full(m.n_faces, i, dtype=np.int64))
        off += m.n_vertices
    return Mesh(np.concatenate(verts), np.concatenate(faces)), np.concatenate(parts)


# ---------------------------------------------------------------------- nameplate

# Block letters on a 4 x 6 unit grid, as stroke segments.
_FONT = {
    "H": [((0, 0), (0, 6)), ((4, 0), (4, 6)), ((0, 3), (4, 3))],
    "E": [((0, 0), (0, 6)), ((0, 6), (4, 6)), ((0, 3), (3, 3)), ((0, 0), (4, 0))],
    "L": [((0, 0), (0, 6)), ((0, 0), (4, 0))],
    "O": [((0, 0), (4, 0)), ((4, 0), (4, 6)), ((4, 6), (0, 6)), ((0, 6), (0, 0))],
    "W": [((0, 6), (1, 0)), ((1, 0), (2, 4)), ((2, 4), (3, 0)), ((3, 0), (4, 6))],
    "R": [((0, 0), (0, 6)), ((0, 6), (4, 6)), ((4, 6), (4, 3)), ((4, 3), (0, 3)),
          ((1.5, 3), (4, 0))],
    "D": [((0, 0), (0, 6)), ((0, 6), (3, 6)), ((3, 6), (4, 5)), ((4, 5), (4, 1)),
          ((4, 1), (3, 0)), ((3, 0), (0, 0))],
}


def _text_strokes(text: str, origin, unit: float, width: float, z0: float, z1: float,
                  spacing: float):
    import manifold3d as m3

    out = []
    x0, y0 = origin
    for i, ch in enumerate(text):
        for (ax, ay), (bx, by) in _FONT[ch]:
            a = np.array([x0 + (ax + i * 6) * unit, y0 + ay * unit])
            b = np.array([x0 + (bx + i * 6) * unit, y0 + by * unit])
            d = b - a
            length = float(np.linalg.norm(d)) + width  # square caps overlap at joints
            ang = float(np.degrees(np.arctan2(d[1], d[0])))
            box = m3.Manifold.cube((length, width, z1 - z0), True)
            box = box.rotate((0.0, 0.0, ang)).translate(
                (float((a[0] + b[0]) / 2), float((a[1] + b[1]) / 2), (z0 + z1) / 2))
            out.append(box)
    return m3.Manifold.batch_boolean(out, m3.OpType.Add).refine_to_length(spacing)


def nameplate(mode: str = "tops", text_height: float = 1.0, spacing: float = 0.09,
              row1: str = "HELLO", row2: str = "WORLD") -> Product:
    """Nameplate; row1 strokes 1.0 mm wide, row2 strokes 0.5 mm wide."""
    import manifold3d as m3

    pal = ["#1F2A44", "#F4F4F0"]  # navy plate, white text
    plate_h = 3.0
    r = 3.0
    corners = [m3.Manifold.cylinder(plate_h, r, r, 48).translate((sx * (35 - r), sy * (11 - r), 0))
               for sx in (-1, 1) for sy in (-1, 1)]
    plate = m3.Manifold.batch_hull(corners).refine_to_length(1.5)
    unit = 1.2
    z1 = plate_h + text_height
    sunk = 0.2  # sunk into the plate so the union merges (touching solids stay separate shells)
    t1 = _text_strokes(row1, (-17.0, 1.5), unit, 1.0, plate_h - sunk, z1, spacing)
    t2 = _text_strokes(row2, (-17.0, -9.0), unit, 0.5, plate_h - sunk, z1, spacing)
    text = t1 + t2
    labels = {1: "plate", 2: "text_1mm", 3: "text_05mm"}
    if mode == "parts":
        pm, tm = _mesh_of(plate), _mesh_of(text)
        mesh, parts = _concat([pm, tm])
        c = mesh.face_centroids
        regions = np.where(parts == 0, 1, np.where(c[:, 1] > 0, 2, 3)).astype(np.int32)
        slots = np.where(parts == 0, 1, 2).astype(np.int64)
        return Product("nameplate_parts", mesh, slots, regions, labels, pal, parts,
                       np.array([1, 2]), {"text_height": text_height})
    mesh = _mesh_of(plate + text)
    c = mesh.face_centroids
    n = mesh.face_normals
    on_text = c[:, 2] > plate_h + 1e-3
    if mode == "tops":
        painted = on_text & (n[:, 2] > 0.99) & (np.abs(c[:, 2] - z1) < 1e-3)
    elif mode == "full":
        painted = on_text
    else:
        raise ValueError(mode)
    regions = np.where(painted, np.where(c[:, 1] > 0, 2, 3), 1).astype(np.int32)
    slots = np.where(painted, 2, 1).astype(np.int64)
    return Product(f"nameplate_{mode}", mesh, slots, regions, labels, pal,
                   info={"text_height": text_height})


# ---------------------------------------------------------------------- keychain


def keychain(spacing: float = 0.12) -> Product:
    """45 mm cat-head keychain lying face up; face paint near the 0.8 mm limit."""
    import manifold3d as m3

    pal = ["#3A3A3C", "#F4F4F0", "#111111", "#F29AB2", "#E0702A"]
    # slots: 1 dark grey body, 2 white, 3 black, 4 pink, 5 orange
    thick = 5.0
    head = m3.Manifold.cylinder(thick, 18.0, 18.0, 96).translate((0, -3.0, 0))
    ear_l = m3.Manifold.batch_hull([
        m3.Manifold.cylinder(thick, 2.0, 2.0, 24).translate((-11.5, 13.5, 0)),
        m3.Manifold.cylinder(thick, 4.0, 4.0, 24).translate((-9.0, 5.0, 0)),
        m3.Manifold.cylinder(thick, 4.0, 4.0, 24).translate((-15.0, 3.0, 0)),
    ])
    ear_r = ear_l.mirror((1.0, 0.0, 0.0))
    ring = (m3.Manifold.cylinder(3.0, 4.5, 4.5, 48) - m3.Manifold.cylinder(3.0, 2.3, 2.3, 48)
            ).translate((0, 19.0, 0))
    body = head + ear_l + ear_r
    dome = m3.Manifold.sphere(60.0, 256).translate((0, -3.0, thick - 60.0 + 1.2))
    slab = m3.Manifold.cube((60.0, 60.0, thick - 1.2), False).translate((-30.0, -33.0, 0))
    body = body ^ (dome + slab)  # flat bottom, sides, gently domed top (1.2 mm crown)
    solid = (body + ring).refine_to_length(spacing)
    mesh = _mesh_of(solid)
    lo, hi = mesh.bounds
    mesh.vertices[:, :2] -= (lo[:2] + hi[:2]) / 2
    mesh._cache.clear()
    c = mesh.face_centroids
    n = mesh.face_normals
    top = n[:, 2] > 0.5
    x, y = c[:, 0], c[:, 1]
    cy = -3.0 - (lo[1] + hi[1]) / 2  # head center after recentering
    labels = {1: "body", 2: "muzzle", 3: "eye_left", 4: "eye_right", 5: "highlight_left",
              6: "highlight_right", 7: "nose", 8: "mouth_thick", 9: "whisker_left",
              10: "whisker_right", 11: "blush_dot_left", 12: "blush_dot_right"}
    slot = {1: 1, 2: 2, 3: 3, 4: 3, 5: 2, 6: 2, 7: 4, 8: 3, 9: 3, 10: 3, 11: 5, 12: 5}
    reg = np.ones(mesh.n_faces, dtype=np.int32)

    def disk(x0, y0, rad):
        return top & ((x - x0) ** 2 + (y - y0) ** 2 < rad ** 2)

    def seg(ax, ay, bx, by, w):
        a, b = np.array([ax, ay]), np.array([bx, by])
        d = b - a
        t = np.clip(((x - ax) * d[0] + (y - ay) * d[1]) / (d @ d), 0, 1)
        return top & (np.hypot(x - (ax + t * d[0]), y - (ay + t * d[1])) < w / 2)

    reg[disk(0, cy - 4.0, 6.5)] = 2  # white muzzle
    reg[disk(-5.5, cy + 2.5, 0.55)] = 3  # eyes: 1.1 mm dots (survive)
    reg[disk(5.5, cy + 2.5, 0.55)] = 4
    reg[disk(-5.2, cy + 2.8, 0.2)] = 5  # highlights: 0.4 mm dots (merge)
    reg[disk(5.8, cy + 2.8, 0.2)] = 6
    reg[disk(0, cy - 2.2, 0.35)] = 7  # nose: 0.7 mm dot (merge, just under the limit)
    reg[seg(-2.0, cy - 4.6, 2.0, cy - 4.6, 1.0)] = 8  # mouth: 1.0 mm line (survives)
    reg[seg(-3.5, cy - 3.2, -9.5, cy - 2.4, 0.5)] = 9  # whiskers: 0.5 mm (merge)
    reg[seg(3.5, cy - 3.2, 9.5, cy - 2.4, 0.5)] = 10
    reg[disk(-9.0, cy - 5.5, 0.6)] = 11  # blush dots: 1.2 mm (survive)
    reg[disk(9.0, cy - 5.5, 0.6)] = 12
    lut = np.array([1] + [slot[r] for r in range(1, 13)], dtype=np.int64)
    slots = lut[reg]
    return Product("keychain_45mm", mesh, slots, reg, labels, pal,
                   info={"size_mm": float((hi - lo)[:2].max())})


# ---------------------------------------------------------------------- multi-part figure


def multipart_figure(spacing: float = 0.35) -> Product:
    """Penguin figure part + key loop part + beak part + base plate part, overlapping."""
    import manifold3d as m3

    pal = ["#15161A", "#F4F4F0", "#F08A24", "#8A93A6"]
    # slots: 1 black figure, 2 white loop and belly paint, 3 orange beak, 4 grey plate
    figure = m3.Manifold.batch_boolean([
        m3.Manifold.sphere(1.0, 64).scale((9.0, 8.0, 12.0)).translate((0, 0, 13.5)),
        m3.Manifold.sphere(7.0, 64).translate((0, -0.5, 27.0)),
        m3.Manifold.sphere(1.0, 32).scale((3.5, 4.5, 1.6)).translate((-4.0, -5.0, 2.6)),
        m3.Manifold.sphere(1.0, 32).scale((3.5, 4.5, 1.6)).translate((4.0, -5.0, 2.6)),
    ], m3.OpType.Add).smooth_out(60.0, 0.0).refine_to_length(spacing)
    torus = m3.Manifold.revolve(
        m3.CrossSection.circle(1.2, 32).translate((4.0, 0.0)), 64).rotate((90.0, 0.0, 0.0))
    loop = torus.translate((0, -0.5, 36.2)).refine_to_length(spacing)
    beak = m3.Manifold.batch_hull([
        m3.Manifold.sphere(2.2, 32).translate((0, -5.0, 26.0)),
        m3.Manifold.sphere(0.7, 32).translate((0, -9.5, 25.3)),
    ]).refine_to_length(spacing)
    plate = m3.Manifold.cylinder(3.0, 15.0, 15.0, 96).refine_to_length(spacing * 2)
    meshes = [_mesh_of(p) for p in (figure, loop, beak, plate)]
    mesh, parts = _concat(meshes)
    part_slots = np.array([1, 2, 3, 4])
    c = mesh.face_centroids
    slots = part_slots[parts].copy()
    belly = (parts == 0) & (c[:, 1] < -3.0) & (c[:, 2] > 5.0) & (c[:, 2] < 21.0)
    slots[belly] = 2
    reg = np.where(belly, 5, parts + 1).astype(np.int32)
    labels = {1: "figure", 2: "loop", 3: "beak", 4: "plate", 5: "belly"}
    return Product("multipart_figure", mesh, slots.astype(np.int64), reg, labels, pal, parts,
                   part_slots)
