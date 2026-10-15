"""Quick offscreen previews: a small numpy z-buffer rasterizer (orthographic, Lambert shading).

No GUI, no GL. Good enough to eyeball placement, loops, text and bases.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .color import hex_to_rgb01

CLAY = np.array([0.80, 0.74, 0.66])
BG = np.array([0x17, 0x18, 0x1A]) / 255.0


def _camera(azim_deg: float, elev_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Right, up, forward (toward the scene) for a camera at azimuth/elevation (VIEWS convention:
    azimuth around +Z from +X, the front camera at -Y is -90)."""
    a, e = np.radians(azim_deg), np.radians(elev_deg)
    pos = np.array([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)])
    fwd = -pos
    up0 = np.array([0.0, 0.0, 1.0]) if abs(elev_deg) < 89 else np.array([0.0, 1.0, 0.0])
    right = np.cross(fwd, up0)
    right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
    return right, up, fwd


def rasterize(tri: np.ndarray, col: np.ndarray, azim: float, elev: float, size: int,
              center: np.ndarray, radius: float, hero: bool = False,
              return_mask: bool = False, fill: float = 1.1):
    right, up, fwd = _camera(azim, elev)
    p = tri - center
    sx = (p @ right) / (radius * fill) * (size / 2) + size / 2
    sy = size / 2 - (p @ up) / (radius * fill) * (size / 2)
    dz = p @ fwd
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    light = -fwd + 0.6 * up - 0.4 * right
    light /= np.linalg.norm(light)
    if hero:
        # key light upper left, soft fill from the right, a little sky from above
        n_cam = np.where((n @ fwd)[:, None] > 0, -n, n)  # face the camera (open shells)
        fill_l = -fwd * 0.6 + 0.8 * right + 0.1 * up
        fill_l /= np.linalg.norm(fill_l)
        key = np.clip(n_cam @ light, 0, 1)
        soft = np.clip(n_cam @ fill_l, 0, 1)
        sky = np.clip(n_cam @ np.array([0.0, 0.0, 1.0]), 0, 1)
        shade = 0.24 + 0.56 * key + 0.14 * soft + 0.08 * sky  # peaks near 1, whites keep form
        shaded = np.clip(col * shade[:, None], 0, 1)
    else:
        lam = np.abs(n @ light)
        shaded = np.clip(col * (0.30 + 0.70 * lam[:, None]), 0, 1)

    x0 = np.clip(np.floor(sx.min(1)).astype(int), 0, size - 1)
    x1 = np.clip(np.ceil(sx.max(1)).astype(int), 0, size - 1)
    y0 = np.clip(np.floor(sy.min(1)).astype(int), 0, size - 1)
    y1 = np.clip(np.ceil(sy.max(1)).astype(int), 0, size - 1)
    nx, ny = x1 - x0 + 1, y1 - y0 + 1
    cnt = nx * ny
    img = np.tile(BG, (size, size, 1))
    depth = np.full(size * size, np.inf)
    idx_all = np.full(size * size, -1)
    # chunk to bound memory
    order = np.arange(len(tri))
    start = 0
    while start < len(order):
        stop = start
        acc = 0
        while stop < len(order) and acc < 4_000_000:
            acc += cnt[order[stop]]
            stop += 1
        sel = order[start:stop]
        start = stop
        c = cnt[sel]
        t_idx = np.repeat(sel, c)
        local = np.arange(int(c.sum())) - np.repeat(np.cumsum(c) - c, c)
        px = x0[t_idx] + local % nx[t_idx] + 0.5
        py = y0[t_idx] + local // nx[t_idx] + 0.5
        ax, ay = sx[t_idx, 0], sy[t_idx, 0]
        bx, by = sx[t_idx, 1], sy[t_idx, 1]
        cx, cy = sx[t_idx, 2], sy[t_idx, 2]
        den = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)
        with np.errstate(divide="ignore", invalid="ignore"):
            u = ((px - ax) * (cy - ay) - (cx - ax) * (py - ay)) / den
            v = ((bx - ax) * (py - ay) - (px - ax) * (by - ay)) / den
            inside = (den != 0) & (u >= 0) & (v >= 0) & (u + v <= 1)
        t_idx, px, py, u, v = t_idx[inside], px[inside], py[inside], u[inside], v[inside]
        d = dz[t_idx, 0] + u * (dz[t_idx, 1] - dz[t_idx, 0]) + v * (dz[t_idx, 2] - dz[t_idx, 0])
        pix = py.astype(int) * size + px.astype(int)
        o = np.lexsort((d, pix))
        pix, d, t_idx = pix[o], d[o], t_idx[o]
        first = np.ones(len(pix), dtype=bool)
        first[1:] = pix[1:] != pix[:-1]
        pix, d, t_idx = pix[first], d[first], t_idx[first]
        better = d < depth[pix]
        depth[pix[better]] = d[better]
        idx_all[pix[better]] = t_idx[better]
    hit = idx_all >= 0
    flat = img.reshape(-1, 3)
    flat[hit] = shaded[idx_all[hit]]
    if return_mask:
        return flat.reshape(size, size, 3), hit.reshape(size, size)
    return flat.reshape(size, size, 3)


def render_hero_3mf(threemf: Path | str, out_png: Path | str, px: int = 1600, azim: float = -55,
                    elev: float = 20, supersample: int = 2) -> Path:
    """Clean 3/4 view of a Bambu project 3MF in its filament colors, transparent background.

    px is the output size (1600 = 2x of an 800 px slot); rendered at px * supersample and
    downsampled for smooth edges."""
    from PIL import Image

    from p2p_threemf.reader import read_3mf_full

    m = read_3mf_full(threemf, world=True)
    colours = [np.array(hex_to_rgb01(c)) for c in m.filament_colours]
    col = np.stack([colours[s - 1] if 0 < s <= len(colours) else CLAY for s in m.face_slots])
    tri = m.vertices[m.faces]
    lo, hi = m.vertices.min(axis=0), m.vertices.max(axis=0)
    center = (lo + hi) / 2
    radius = float(np.linalg.norm(hi - lo)) / 2
    big = px * supersample
    rgb, mask = rasterize(tri, col, azim, elev, big, center, radius, hero=True,
                          return_mask=True, fill=1.02)
    rgba = np.dstack([rgb, mask.astype(float)])
    im = Image.fromarray((rgba * 255).round().astype(np.uint8), "RGBA")
    # premultiplied downsample so edges do not pick up the background color
    arr = np.asarray(im).astype(np.float64) / 255.0
    arr[..., :3] *= arr[..., 3:4]
    small = arr.reshape(px, supersample, px, supersample, 4).mean(axis=(1, 3))
    a = small[..., 3:4]
    small[..., :3] = np.where(a > 0, small[..., :3] / np.maximum(a, 1e-6), 0)
    out = Image.fromarray((small * 255).round().astype(np.uint8), "RGBA")
    bbox = out.getbbox()
    if bbox:  # trim to the object with an 4 % margin, keep it square
        x0, y0, x1, y1 = bbox
        side = int(max(x1 - x0, y1 - y0) * 1.08)
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(out.crop((x0, y0, x1, y1)), (side // 2 - (cx - x0), side // 2 - (cy - y0)))
        out = canvas.resize((px, px), Image.LANCZOS)
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_png)
    return out_png


def render_product(geo, out_png: Path | str, library=None, figure_colors: np.ndarray | None = None,
                   views: list[tuple[str, float, float]] | None = None, title: str | None = None,
                   size_px: int = 720) -> Path:
    """Render the product from a few fixed cameras side by side into one PNG.

    figure_colors: optional (n_source_faces, 3) uint8 per Meshy face, carried through the repair
    map. Parts use their filament colors.
    """
    from PIL import Image, ImageDraw

    hexes = {f.id: f.hex for f in library.filaments} if library is not None else {}
    fig_v, fig_f = geo.figure.vertices, geo.figure.faces
    if figure_colors is not None:
        fc = geo.figure.remap_face_values(np.asarray(figure_colors)[:, :3], fill=200) / 255.0
    else:
        fc = np.tile(CLAY, (len(fig_f), 1))
    tris, cols = [fig_v[fig_f]], [fc]
    for p in geo.parts:
        if p.role == "cutter":
            continue
        rgb = np.array(hex_to_rgb01(hexes.get(p.filament_id, "#8A8F99")))
        rgb = np.maximum(rgb, 0.16)  # keep black parts readable on the dark background
        tris.append(p.vertices[p.faces])
        cols.append(np.tile(rgb, (len(p.faces), 1)))
    tri = np.concatenate(tris)
    col = np.concatenate(cols)
    if views is None:
        views = [("front 3/4", -55, 20), ("front", -90, 5), ("top", -90, 89.5)]
        if geo.orientation == "flat":
            views = [("top", -90, 89.5), ("3/4", -60, 40), ("side", 0, 5)]
    lo, hi = geo.bbox_min, geo.bbox_max
    center = (lo + hi) / 2
    radius = float(np.linalg.norm(hi - lo)) / 2
    tiles = [rasterize(tri, col, az, el, size_px, center, radius) for _, az, el in views]
    img = np.concatenate(tiles, axis=1)
    im = Image.fromarray((img * 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    size = geo.size_mm
    d.text((12, 10), f"{title or geo.product.value}  {size[0]:.0f} x {size[1]:.0f} x {size[2]:.0f} mm",
           fill=(232, 234, 237))
    for k, (name, _, _) in enumerate(views):
        d.text((k * size_px + 12, size_px - 22), name, fill=(154, 160, 166))
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_png)
    return out_png
