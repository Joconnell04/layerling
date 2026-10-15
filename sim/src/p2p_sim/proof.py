"""Proof renders: the product from all eight sides (contracts PROOF_VIEWS), in the pose a person
looks at it (figure up, face toward the front camera; a flat charm turned back upright), for
three kinds:

- model: the figure with its 3D model texture (per face), add-on parts in their filaments;
- paint: filament colors as painted;
- print: the simulated print (p2p_sim.simulate), with layer lines from the print frame.

One camera set per product, so the views compare across kinds and stages. Writes one PNG per
view and kind plus a 4 x 2 contact sheet per kind; returns [{kind, view, path}].
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from p2p_contracts.regions import PROOF_VIEWS
from PIL import Image, ImageDraw

from .camera import _frame, direction_from_angles
from .mesh import Mesh
from .raster import rasterize
from .render import BG, Pixels, _downsample, _face_rgb, _font, render, shade

PROOF_ORDER = ["front", "front_3q", "left_3q", "left", "right", "back", "back_3q", "top"]
KIND_TITLES = {"model": "3D model (texture)", "paint": "paint (filament colors)",
               "print": "simulated print"}
# p2p_geometry.product._R_FLAT["front"]: face up (+Z). Its inverse turns a flat charm back up.
R_FLAT_FRONT = np.array([[1.0, 0, 0], [0, 0, 1.0], [0, -1.0, 0]])


def display_vertices(vertices: np.ndarray, orientation: str) -> np.ndarray:
    """Print frame -> display pose, centered in XY with the bottom on z = 0."""
    v = np.asarray(vertices, dtype=np.float64)
    if orientation == "flat_face_up":
        v = v @ R_FLAT_FRONT
    lo, hi = v.min(axis=0), v.max(axis=0)
    return v - np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])


def cameras(mesh: Mesh, size_px: int) -> dict:
    center, radius = mesh.bounding_sphere
    out = {}
    for name in PROOF_ORDER:
        spec = PROOF_VIEWS[name]
        d = direction_from_angles(spec.azimuth_deg, spec.elevation_deg)
        out[name] = _frame(center, radius, d, spec.fov_deg, spec.fit_margin, size_px, name)
    return out


def _sim_image(mesh_disp: Mesh, mesh_print: Mesh, cam, sim, ss: int = 2) -> np.ndarray:
    """render kind sim, drawn in the display pose with layer lines from the print frame."""
    r = rasterize(mesh_disp, cam.resized(cam.width * ss, cam.height * ss))
    px = Pixels.of(r)
    col = _face_rgb(mesh_disp, sim.face_rgb)[px.fid].astype(np.float32)
    m = np.asarray(sim.face_bleed_mix, dtype=np.float32)[px.fid][:, None]
    col = col * (1 - m) + np.asarray(sim.face_bleed_rgb, dtype=np.float32)[px.fid] * m
    s = shade(mesh_disp, px)
    L = sim.params.layer_mm
    ph = (px.points(mesh_print)[:, 2] / L) % 1.0
    s = s * (1 - 0.07 * (np.abs(ph - 0.5) * 2) ** 4)
    return _downsample(px.compose(col * s[:, None], BG), ss)


def contact_sheet(images: list[tuple[str, np.ndarray]], out: Path, title: str,
                  cell_px: int = 384) -> Path:
    pad, label_h, head, cols = 10, 24, 34, 4
    rows = (len(images) + cols - 1) // cols
    w = cols * cell_px + (cols + 1) * pad
    h = head + rows * (cell_px + label_h) + (rows + 1) * pad
    sheet = Image.new("RGB", (w, h), (22, 22, 25))
    dr = ImageDraw.Draw(sheet)
    dr.text((pad + 2, pad + 4), title, fill=(230, 230, 235), font=_font(18))
    for i, (name, img) in enumerate(images):
        c, rr = i % cols, i // cols
        x = pad + c * (cell_px + pad)
        y = head + pad + rr * (cell_px + label_h + pad)
        sheet.paste(Image.fromarray(img).resize((cell_px, cell_px), Image.LANCZOS), (x, y))
        dr.text((x + 2, y + cell_px + 4), name.replace("_", " "), fill=(150, 152, 160),
                font=_font(14))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


def proof_set(mesh_print: Mesh, orientation: str, out_dir: str | Path, *,
              model_rgb: np.ndarray | None = None, paint_rgb: np.ndarray | None = None,
              sim=None, size_px: int = 768, title: str = "", fmt: str = "png",
              views: list[str] | None = None) -> list[dict]:
    """Render the kinds that have inputs (model_rgb / paint_rgb: (F, 3) uint8 per face of
    mesh_print; sim: a SimResult on mesh_print) from every proof view. Files:
    <kind>_<view>.<fmt> and <kind>_sheet.png in out_dir."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    disp = Mesh(vertices=display_vertices(mesh_print.vertices, orientation),
                faces=mesh_print.faces)
    cams = cameras(disp, size_px)
    views = views or PROOF_ORDER
    rows: list[dict] = []
    kinds = [("model", model_rgb), ("paint", paint_rgb), ("print", sim)]
    for kind, data in kinds:
        if data is None:
            continue
        imgs = []
        for v in views:
            cam = cams[v]
            if kind == "print":
                img = _sim_image(disp, mesh_print, cam, sim)
            else:
                img = render(disp, cam, "shaded", face_colors=data, size_px=size_px).image
            p = out_dir / f"{kind}_{v}.{fmt}"
            im = Image.fromarray(img)
            if fmt in ("jpg", "jpeg"):
                im.save(p, "JPEG", quality=85, optimize=True)
            else:
                im.save(p, compress_level=4)
            rows.append({"kind": kind, "view": v, "path": p})
            imgs.append((v, img))
        sheet = contact_sheet(imgs, out_dir / f"{kind}_sheet.png",
                              f"{title}{': ' if title else ''}{KIND_TITLES[kind]}")
        rows.append({"kind": kind, "view": "sheet", "path": sheet})
    return rows


def _display_transform(vertices: np.ndarray, orientation: str):
    """(rotation, shift) with display = vertices @ rotation - shift (display_vertices)."""
    rot = R_FLAT_FRONT if orientation == "flat_face_up" else np.eye(3)
    v = np.asarray(vertices, dtype=np.float64) @ rot
    lo, hi = v.min(axis=0), v.max(axis=0)
    return rot, np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])


def hang_rotation(d: np.ndarray) -> np.ndarray:
    """Rotation matrix taking the direction d onto +Z by the smallest turn (Rodrigues)."""
    d = np.asarray(d, dtype=np.float64)
    d = d / (np.linalg.norm(d) + 1e-12)
    z = np.array([0.0, 0.0, 1.0])
    axis = np.cross(d, z)
    sin, cos = float(np.linalg.norm(axis)), float(d @ z)
    if sin < 1e-9:
        return np.eye(3) if cos > 0 else np.diag([1.0, -1.0, -1.0])
    k = axis / sin
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + sin * K + (1 - cos) * (K @ K)


def hanging_view(mesh_print: Mesh, orientation: str, hang: dict, out_dir: str | Path, *,
                 paint_rgb: np.ndarray | None = None, sim=None, size_px: int = 768,
                 fmt: str = "png") -> list[dict]:
    """The product as it hangs from its loop: turned so the center of mass sits straight below
    the pivot (the top of the loop's hole), seen from the side the tilt shows on, with the key
    ring's line above the pivot, a plumb line through the center of mass and the tilt printed.
    hang: p2p_geometry metrics.hang (pivot_mm, com_mm and tilt_deg in the print frame).
    Files <kind>_hanging.<fmt>; returns [{kind, view: "hanging", path, tilt_deg}]."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rot, shift = _display_transform(mesh_print.vertices, orientation)
    disp_v = np.asarray(mesh_print.vertices, np.float64) @ rot - shift
    pivot = np.asarray(hang["pivot_mm"], np.float64) @ rot - shift
    com = np.asarray(hang["com_mm"], np.float64) @ rot - shift
    d = pivot - com
    R = hang_rotation(d)
    hung = Mesh(vertices=(disp_v - pivot) @ R.T + pivot, faces=mesh_print.faces)
    tilt = float(hang.get("tilt_deg") or 0.0)
    # look along the tilt axis (the side the tilt shows on); level: from the front
    axis = np.cross(d / (np.linalg.norm(d) + 1e-12), [0.0, 0.0, 1.0])
    if np.linalg.norm(axis) < 0.02:
        look = np.array([0.0, -1.0, 0.0])
    else:
        look = axis / np.linalg.norm(axis)
        if look @ np.array([-1.0, -0.4, 0.0]) < 0:  # prefer the viewer's left or front
            look = -look
        # 35 degrees toward the front: the tilt still shows and a loop facing the front is not
        # seen edge on
        front = np.array([0.0, -1.0, 0.0]) - look * float(look @ [0.0, -1.0, 0.0])
        if np.linalg.norm(front) > 1e-6:
            look = 0.82 * look + 0.57 * front / np.linalg.norm(front)
    look = look + np.array([0.0, 0.0, 0.12])
    center, radius = hung.bounding_sphere
    chain = pivot + np.array([0.0, 0.0, 0.25 * radius])
    pts = np.vstack([hung.vertices[:: max(1, len(hung.vertices) // 20000)], chain[None]])
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    c = (lo + hi) / 2
    r = float(np.linalg.norm(pts - c, axis=1).max())
    cam = _frame(c, r, look, 30.0, 1.12, size_px, "hanging")
    rows = []
    for kind, data in (("paint", paint_rgb), ("print", sim)):
        if data is None:
            continue
        if kind == "print":
            img = _sim_image(hung, mesh_print, cam, sim)
        else:
            img = render(hung, cam, "shaded", face_colors=data, size_px=size_px).image
        im = Image.fromarray(img)
        dr = ImageDraw.Draw(im)
        com_h = pivot + R @ (com - pivot)  # straight below the pivot once it hangs
        xy, _ = cam.project(np.vstack([pivot, com_h, chain]))
        (px, py), (cx, cy), _ = xy
        for y in range(int(py), im.height, 14):  # plumb line through the center of mass
            dr.line([(px, y), (px, min(y + 7, im.height))], fill=(235, 235, 240), width=2)
        dr.line([(px, 0), (px, py)], fill=(200, 200, 205), width=3)  # key ring line
        # the center of mass: a dot that shows on white and on black
        dr.ellipse([cx - 7, cy - 7, cx + 7, cy + 7], fill=(230, 60, 60), outline=(20, 20, 24),
                   width=2)
        where = hang.get("area") or hang.get("where") or ""
        dr.text((14, 12), f"Hangs {tilt:.0f} degrees", fill=(245, 245, 248), font=_font(26))
        if where:
            dr.text((14, 44), f"Loop: {where}", fill=(215, 215, 220), font=_font(18))
        p = out_dir / f"{kind}_hanging.{fmt}"
        if fmt in ("jpg", "jpeg"):
            im.save(p, "JPEG", quality=85, optimize=True)
        else:
            im.save(p, compress_level=4)
        rows.append({"kind": kind, "view": "hanging", "path": p, "tilt_deg": round(tilt, 1)})
    return rows


def side_by_side(left: Path | np.ndarray, right: Path, out: Path, left_label: str = "concept",
                 right_label: str = "print", height: int = 768) -> Path:
    """The approved concept next to a proof sheet, for reviewers and the concept check."""
    a = Image.open(left).convert("RGB") if not isinstance(left, np.ndarray) else Image.fromarray(
        left)
    b = Image.open(right).convert("RGB")
    a = a.resize((max(1, int(a.width * height / a.height)), height))
    b = b.resize((max(1, int(b.width * height / b.height)), height))
    pad, head = 10, 30
    im = Image.new("RGB", (a.width + b.width + 3 * pad, height + head + 2 * pad), (22, 22, 25))
    dr = ImageDraw.Draw(im)
    im.paste(a, (pad, head + pad))
    im.paste(b, (2 * pad + a.width, head + pad))
    dr.text((pad, 8), left_label, fill=(230, 230, 235), font=_font(18))
    dr.text((2 * pad + a.width, 8), right_label, fill=(230, 230, 235), font=_font(18))
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    return out
