"""Image work for the concept and multi-view stages (s03, s04). Local, free, deterministic.

- paste_back: an add-on edit (plate and name, loop, base) of the approved figure comes back from
  the image model; find the figure in it (scale and offset, masked SSD over an FFT), then build
  the product concept in the figure's own pixel frame with the figure's pixels copied in
  unchanged. The customer approves exactly the figure Meshy models (meshy-iteration.md section 4,
  step 3). A loop keeps the band where it attaches over the figure.
- composite_*: the free fallback when an edit fails its checks: the add-on drawn in code under
  or around the unchanged figure (a ring, a disc, a plate with the name in the geometry font).
- split_views, normalize_view: s04 views cropped to the subject and padded onto one flat
  background.
- palette, palette_gap: dominant colors of a subject and how far two palettes are apart
  (CIEDE2000), a code signal for the view check.

Images are RGBA float arrays in [0, 1] inside this module; files are PNG.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

BG = (255, 255, 255)  # the flat background every s04 view and s05 input shares
ALIGN_RMSE_OK = 0.12  # ASSUMED: an edit that kept the figure lands under this (0..1 per channel)


# ---------------------------------------------------------------- basics
def load_rgba(path: Path | str) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(im.convert("RGBA"), dtype=np.float32) / 255.0


def save_rgba(arr: np.ndarray, path: Path | str) -> None:
    Image.fromarray(to_u8(arr), "RGBA").save(path, "PNG")


def to_u8(arr: np.ndarray) -> np.ndarray:
    return np.clip(np.round(arr * 255.0), 0, 255).astype(np.uint8)


def resize(arr: np.ndarray, size: tuple[int, int], resample=Image.BILINEAR) -> np.ndarray:
    """size = (width, height). Works channel by channel in float."""
    w, h = max(1, int(size[0])), max(1, int(size[1]))
    if arr.ndim == 2:
        return np.asarray(Image.fromarray(arr.astype(np.float32), "F").resize((w, h), resample))
    return np.stack([np.asarray(Image.fromarray(arr[..., c].astype(np.float32), "F")
                                .resize((w, h), resample)) for c in range(arr.shape[2])], axis=-1)


def border_color(rgb: np.ndarray) -> np.ndarray:
    edge = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    return np.median(edge, axis=0)


def subject_mask(img: np.ndarray, tol: float = 0.08) -> np.ndarray:
    """Opaque pixels when the image has real transparency, else pixels away from the border
    color (a flat background)."""
    a = img[..., 3]
    if a.min() < 0.5:
        return a > 0.5
    bg = border_color(img[..., :3])
    return np.abs(img[..., :3] - bg).max(axis=-1) > tol


def bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """(y0, y1, x0, x1), end exclusive, or None for an empty mask."""
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return None
    return int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1


def dilate(mask: np.ndarray, px: int) -> np.ndarray:
    """Grow a mask by px (Euclidean), in linear time."""
    from scipy import ndimage

    if px <= 0 or not mask.any():
        return mask
    return ndimage.distance_transform_edt(~mask) <= px


def hexs(rgb) -> str:
    r, g, b = (int(v) for v in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def median_hex(rgb: np.ndarray, mask: np.ndarray) -> str | None:
    if not mask.any():
        return None
    return hexs(round(float(v) * 255) for v in np.median(rgb[mask], axis=0))


def pad_square(arr: np.ndarray, fill=(0.0, 0.0, 0.0, 0.0)) -> np.ndarray:
    h, w = arr.shape[:2]
    n = max(h, w)
    out = np.empty((n, n, 4), dtype=np.float32)
    out[...] = fill
    y, x = (n - h) // 2, (n - w) // 2
    out[y:y + h, x:x + w] = arr
    return out


# ---------------------------------------------------------------- alignment
@dataclass
class Alignment:
    scale: float  # figure px -> edit px
    ox: float  # edit x of the figure bbox's left edge
    oy: float
    rmse: float
    ok: bool


def _flat(img: np.ndarray, grey: float = 0.5) -> np.ndarray:
    a = img[..., 3:4]
    return img[..., :3] * a + grey * (1.0 - a)


def _mse_map(E: np.ndarray, T: np.ndarray, M: np.ndarray) -> np.ndarray:
    """Mean squared error of template T (masked by M) at every offset fully inside E."""
    from scipy.signal import fftconvolve

    Mf = M.astype(np.float64)
    n = max(Mf.sum(), 1.0)
    E = E.astype(np.float64)
    T = T.astype(np.float64)
    acc = fftconvolve((E * E).sum(axis=-1), Mf[::-1, ::-1], mode="valid")
    acc += float((Mf[..., None] * T * T).sum())
    for c in range(3):
        acc -= 2.0 * fftconvolve(E[..., c], (Mf * T[..., c])[::-1, ::-1], mode="valid")
    return np.maximum(acc / (3.0 * n), 0.0)


def _search(E: np.ndarray, T: np.ndarray, M: np.ndarray, r: float, scales,
            window: tuple[int, int, int, int] | None = None):
    """Best (mse, s, x, y) of the template at edit scale r over the given scales. x, y are the
    template's top-left in E's pixels at scale r."""
    best = None
    for s in scales:
        k = s * r
        th, tw = round(T.shape[0] * k), round(T.shape[1] * k)
        if th < 6 or tw < 6:
            continue
        Tk = resize(T, (tw, th))
        Mk = resize(M.astype(np.float32), (tw, th)) > 0.5
        if not Mk.any():
            continue
        y0, x0 = 0, 0
        Ew = E
        if window is not None:
            wy0, wy1, wx0, wx1 = window
            y0, x0 = max(0, wy0), max(0, wx0)
            Ew = E[y0:min(E.shape[0], wy1 + th), x0:min(E.shape[1], wx1 + tw)]
        if th > Ew.shape[0] or tw > Ew.shape[1]:
            continue
        mse = _mse_map(Ew, Tk, Mk)
        i = int(np.argmin(mse))
        yy, xx = divmod(i, mse.shape[1])
        cand = (float(mse[yy, xx]), float(s), x0 + xx, y0 + yy)
        if best is None or cand[0] < best[0]:
            best = cand
    return best


def align(figure: np.ndarray, edit: np.ndarray, *, coarse: int = 160, fine: int = 512
          ) -> Alignment | None:
    """Where the figure (RGBA, transparent background) sits in the edit, at what scale."""
    M = subject_mask(figure)
    b = bbox(M)
    if b is None:
        return None
    y0, y1, x0, x1 = b
    T = figure[y0:y1, x0:x1, :3]
    Mt = M[y0:y1, x0:x1]
    E = _flat(edit)
    H, W = E.shape[:2]
    r1 = coarse / max(H, W)
    E1 = resize(E, (W * r1, H * r1))
    first = _search(E1, T, Mt, r1, np.arange(0.40, 1.301, 0.03))
    if first is None:
        return None
    _, s1, x1c, y1c = first
    r2 = min(1.0, fine / max(H, W))
    E2 = resize(E, (W * r2, H * r2)) if r2 < 1.0 else E
    m = round(24 * r2 / r1 / 4) + 6  # a few coarse pixels of slack, in fine pixels
    cx, cy = round(x1c / r1 * r2), round(y1c / r1 * r2)
    second = _search(E2, T, Mt, r2, np.arange(s1 - 0.03, s1 + 0.0301, 0.01),
                     window=(cy - m, cy + m, cx - m, cx + m))
    mse, s, xf, yf = second or first
    rr = r2 if second else r1
    rmse = float(np.sqrt(mse))
    return Alignment(scale=float(s), ox=xf / rr, oy=yf / rr, rmse=rmse,
                     ok=rmse <= ALIGN_RMSE_OK and 0.4 <= s <= 1.3)


# ---------------------------------------------------------------- paste back
@dataclass
class PasteResult:
    ok: bool
    reason: str | None
    identical: float | None  # share of the figure's opaque pixels copied unchanged
    addon_share: float  # add-on pixels outside the figure, relative to the figure's area
    addon_hex: str | None
    alignment: dict | None

    def as_dict(self) -> dict:
        return asdict(self)


def paste_back(figure_path: Path, edit_path: Path, out_path: Path, *, mode: str = "full"
               ) -> PasteResult:
    """The product concept in the figure's pixel frame: the edit scaled onto the figure, the
    figure's pixels on top, unchanged. mode "loop" keeps the edit where the add-on overlaps the
    figure right next to it (the loop's attachment band)."""
    F = load_rgba(figure_path)
    E = load_rgba(edit_path)
    al = align(F, E)
    if al is None:
        return PasteResult(False, "figure not found in the edit", None, 0.0, None, None)
    if not al.ok:
        return PasteResult(False, f"the edit changed the figure (rmse {al.rmse:.3f}, scale "
                                  f"{al.scale:.2f})", None, 0.0, None, asdict(al))
    Mf = subject_mask(F)
    y0, _, x0, _ = bbox(Mf)  # type: ignore[misc]
    s = al.scale
    # edit placed in the figure frame: edit pixel (X, Y) -> ((X - ox) / s + x0, (Y - oy) / s + y0)
    ex0, ey0 = x0 - al.ox / s, y0 - al.oy / s
    ew, eh = E.shape[1] / s, E.shape[0] / s
    Hf, Wf = F.shape[:2]
    left, top = min(0.0, ex0), min(0.0, ey0)
    right, bottom = max(float(Wf), ex0 + ew), max(float(Hf), ey0 + eh)
    cx, cy = int(np.ceil(-left)), int(np.ceil(-top))
    W, H = int(np.ceil(right)) + cx, int(np.ceil(bottom)) + cy
    canvas = np.zeros((H, W, 4), dtype=np.float32)
    Er = resize(E, (round(ew), round(eh)))
    px, py = round(ex0) + cx, round(ey0) + cy
    ys, xs = slice(max(0, py), min(H, py + Er.shape[0])), slice(max(0, px), min(W, px + Er.shape[1]))
    canvas[ys, xs] = Er[ys.start - py:ys.stop - py, xs.start - px:xs.stop - px]
    edit_c = canvas.copy()

    fig = np.zeros_like(canvas)
    fig[cy:cy + Hf, cx:cx + Wf] = F
    Mc = np.zeros((H, W), dtype=bool)
    Mc[cy:cy + Hf, cx:cx + Wf] = Mf
    a = fig[..., 3:4]
    out = canvas.copy()
    out[..., :3] = fig[..., :3] * a + canvas[..., :3] * (1.0 - a)
    out[..., 3:4] = a + canvas[..., 3:4] * (1.0 - a)

    fig_h = max(1, int(Mf.sum() ** 0.5))
    outside = (edit_c[..., 3] > 0.5) & ~dilate(Mc, max(3, fig_h // 60))
    addon = _big_parts(outside, max(20, int(0.002 * Mf.sum())))
    keep = np.zeros_like(Mc)
    if mode == "loop" and addon.any():
        band = dilate(addon, max(4, int(0.05 * (bbox(Mf)[1] - y0))))  # type: ignore[index]
        differs = np.abs(edit_c[..., :3] - fig[..., :3]).max(axis=-1) > 0.15
        keep = Mc & band & differs
        out[keep] = edit_c[keep]
    save_rgba(pad_square(out), out_path)

    opaque = Mc & (fig[..., 3] >= 0.999)
    same = (to_u8(out[..., :3])[opaque] == to_u8(fig[..., :3])[opaque]).all(axis=-1)
    identical = float(same.mean()) if same.size else None
    share = float(addon.sum()) / max(1.0, float(Mf.sum()))
    return PasteResult(True, None, identical, round(share, 4),
                       median_hex(edit_c[..., :3], addon), asdict(al))


def _big_parts(mask: np.ndarray, min_px: int) -> np.ndarray:
    from scipy import ndimage

    lab, n = ndimage.label(mask)
    if n == 0:
        return mask
    sizes = ndimage.sum(mask, lab, index=np.arange(1, n + 1))
    keep = np.zeros(n + 1, dtype=bool)
    keep[1:] = sizes >= min_px
    return keep[lab]


def figure_identical(figure_path: Path, product_path: Path) -> float | None:
    """Share of the figure's opaque pixels found unchanged in the product image (same size, the
    figure at the same place, or the product padded around it)."""
    F, P = to_u8(load_rgba(figure_path)), to_u8(load_rgba(product_path))
    if P.shape == F.shape:
        opaque = F[..., 3] == 255
        return float((P[opaque][:, :3] == F[opaque][:, :3]).all(axis=-1).mean()) \
            if opaque.any() else None
    return None


# ---------------------------------------------------------------- code composites
def _pad(arr: np.ndarray, top: int = 0, bottom: int = 0, left: int = 0, right: int = 0
         ) -> np.ndarray:
    h, w = arr.shape[:2]
    out = np.zeros((h + top + bottom, w + left + right, 4), dtype=np.float32)
    out[top:top + h, left:left + w] = arr
    return out


def _over(base: np.ndarray, top: np.ndarray) -> np.ndarray:
    a = top[..., 3:4]
    out = base.copy()
    out[..., :3] = top[..., :3] * a + base[..., :3] * (1.0 - a)
    out[..., 3:4] = a + base[..., 3:4] * (1.0 - a)
    return out


def _layer(h: int, w: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    return im, ImageDraw.Draw(im)


def _rgb(hexs: str) -> tuple[int, int, int]:
    h = hexs.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _shade(rgb: tuple[int, int, int], f: float) -> tuple[int, int, int]:
    return tuple(int(max(0, min(255, round(c * f)))) for c in rgb)  # type: ignore[return-value]


def dominant_hex(img: np.ndarray, mask: np.ndarray | None = None) -> str:
    mask = subject_mask(img) if mask is None else mask
    cols = palette(img, mask, k=4)
    return cols[0][0] if cols else "#808080"


def composite_loop(figure_path: Path, out_path: Path, position: str = "top") -> dict:
    """A ring drawn where the loop attaches, behind the unchanged figure. Its color is the
    color it touches (DEEP-DIVE rule 3)."""
    F = load_rgba(figure_path)
    M = subject_mask(F)
    y0, y1, x0, x1 = bbox(M)  # type: ignore[misc]
    fh = y1 - y0
    R = max(6, int(0.08 * fh))  # ASSUMED loop size in the picture, not the printed size
    pad = 3 * R
    F2 = _pad(F, top=pad, left=pad, right=pad, bottom=pad)
    M2 = _pad(M[..., None].astype(np.float32), top=pad, left=pad, right=pad,
              bottom=pad)[..., 0] > 0.5
    y0, y1, x0, x1 = y0 + pad, y1 + pad, x0 + pad, x1 + pad
    if position in ("left", "right"):
        cols = np.nonzero(M2.any(axis=0))[0]
        edge = cols.min() if position == "left" else cols.max()
        rows = np.nonzero(M2[:, edge])[0]
        cy = int(rows.mean())
        cx = edge - int(0.55 * R) if position == "left" else edge + int(0.55 * R)
        touch = M2 & (np.abs(np.arange(M2.shape[1])[None, :] - edge) < R)
    else:
        top = np.nonzero(M2.any(axis=1))[0].min()
        if position in ("back", "any"):
            # over the silhouette's centroid, where p2p_geometry seats a back or any loop on a
            # standing animal (over the center of mass, so it hangs straight)
            cx = int(round(float(np.nonzero(M2)[1].mean())))
        else:
            # the middle of the head's top band (between the ears), then down that column to
            # the skull: the loop sits on the head, not on an ear tip
            band = M2[top:top + max(2, fh // 8)]
            xs = np.nonzero(band.any(axis=0))[0]
            cx = round((xs.min() + xs.max()) / 2)
        col = np.nonzero(M2[:, max(0, cx - 2):cx + 3].any(axis=1))[0]
        attach = int(col.min()) if len(col) else top
        cy = attach - int(0.5 * R)
        yy, xx = np.mgrid[:M2.shape[0], :M2.shape[1]]
        touch = M2 & (np.abs(xx - cx) < R) & (yy >= attach) & (yy < attach + R)
    color = _rgb(median_hex(F2[..., :3], touch) or dominant_hex(F))
    layer, d = _layer(*F2.shape[:2])
    t = max(3, int(0.38 * R))
    d.ellipse((cx - R, cy - R, cx + R, cy + R), outline=_shade(color, 0.7) + (255,), width=t + 2)
    d.ellipse((cx - R + 1, cy - R + 1, cx + R - 1, cy + R - 1), outline=color + (255,), width=t)
    ring = np.asarray(layer, dtype=np.float32) / 255.0
    out = _over(ring, F2)
    b = bbox(out[..., 3] > 0.02)
    out = out[b[0]:b[1], b[2]:b[3]] if b else out  # type: ignore[index]
    save_rgba(pad_square(out), out_path)
    return {"addon_hex": hexs(color), "position": position}


def composite_base(figure_path: Path, out_path: Path, kind: str = "round_base") -> dict:
    """A low disc (or oval) under the feet, behind the unchanged figure."""
    F = load_rgba(figure_path)
    M = subject_mask(F)
    y0, y1, x0, x1 = bbox(M)  # type: ignore[misc]
    fw, fh = x1 - x0, y1 - y0
    bw = int(fw * (1.45 if kind == "oval_base" else 1.15))
    bh = max(8, int(0.22 * bw))
    th = max(4, int(0.06 * fh))
    padx = max(0, (bw - fw) // 2 + 4)
    F2 = _pad(F, left=padx, right=padx, bottom=bh + th + 4)
    cx = x0 + padx + fw // 2
    cy = y1 - int(0.15 * bh)
    color = _rgb(dominant_hex(F, M))
    layer, d = _layer(*F2.shape[:2])
    d.rectangle((cx - bw // 2, cy, cx + bw // 2, cy + th), fill=_shade(color, 0.72) + (255,))
    d.ellipse((cx - bw // 2, cy + th - bh // 2, cx + bw // 2, cy + th + bh // 2),
              fill=_shade(color, 0.72) + (255,))
    d.ellipse((cx - bw // 2, cy - bh // 2, cx + bw // 2, cy + bh // 2), fill=color + (255,))
    base = np.asarray(layer, dtype=np.float32) / 255.0
    out = _over(base, F2)
    save_rgba(pad_square(out), out_path)
    return {"addon_hex": hexs(color), "kind": kind}


TAGS = {b"Weight": "wght", b"Width": "wdth"}


def _font(size: int, key: str | None = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """The geometry package's font (rounded-bold by default), so the preview letters match the
    printed ones; PIL's own font if it cannot load."""
    try:
        from p2p_geometry import text as gtext

        file, loc = gtext.FONTS[gtext.font_key(key)]
        f = ImageFont.truetype(str(gtext.FONT_DIR / file), size)
        try:
            axes = f.get_variation_axes()
            f.set_variation_by_axes([float(loc.get(TAGS.get(ax.get("name"), ""), ax["default"]))
                                     for ax in axes])
        except OSError:  # a static font
            pass
        return f
    except Exception:  # noqa: BLE001
        return ImageFont.load_default(size=size)


def composite_nameplate(figure_path: Path, out_path: Path, text: str, *,
                        shape: str = "rounded", side: str = "left") -> dict:
    """The figure standing on one end of a low plate with the name on the rest of its top, the
    layout p2p_geometry builds (figure about 0.42 of the plate width tall)."""
    F = load_rgba(figure_path)
    M = subject_mask(F)
    y0, y1, x0, x1 = bbox(M)  # type: ignore[misc]
    F = F[y0:y1, x0:x1]
    fh, fw = F.shape[:2]
    W = int(fh / 0.42)
    D = max(12, int(0.30 * fh))  # visible depth of the top face
    T = max(4, int(0.07 * fh))  # front edge
    m = int(0.04 * W)
    Hc, Wc = fh + D // 2 + T + 8, W + 8
    color = _rgb(dominant_hex(F))
    lum = 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]
    ink = (26, 26, 26) if lum > 140 else (245, 245, 240)
    layer, d = _layer(Hc, Wc)
    top_y = fh - D // 2
    box = (4, top_y, 4 + W, top_y + D)
    front = (4, top_y + D // 2, 4 + W, top_y + D + T)
    if shape == "oval":
        d.ellipse((box[0], front[1] - D // 2 + T, box[2], front[3]), fill=_shade(color, 0.72) + (255,))
        d.ellipse(box, fill=color + (255,))
    else:
        rad = int(0.35 * D) if shape == "rounded" else 0
        d.rounded_rectangle((front[0], box[1] + T, front[2], front[3]), radius=rad,
                            fill=_shade(color, 0.72) + (255,))
        d.rounded_rectangle(box, radius=rad, fill=color + (255,))
    fx = 4 + m if side != "right" else 4 + W - m - fw
    region = (fx + fw + m // 2, 4 + W - m) if side != "right" else (4 + m, fx - m // 2)
    size = max(8, int(0.62 * D))
    font = _font(size)
    while size > 8:
        tw = d.textlength(text, font=font)
        if tw <= region[1] - region[0]:
            break
        size = int(size * 0.9)
        font = _font(size)
    tw = d.textlength(text, font=font)
    tx = region[0] + max(0, (region[1] - region[0] - tw) / 2)
    d.text((tx, top_y + D / 2), text, font=font, fill=ink + (255,), anchor="lm")
    plate = np.asarray(layer, dtype=np.float32) / 255.0
    fig = np.zeros_like(plate)
    fy = top_y + D // 2 - fh + int(0.08 * D)
    fig[max(0, fy):fy + fh, fx:fx + fw] = F[max(0, -fy):, :min(fw, Wc - fx)]
    out = _over(plate, fig)
    save_rgba(pad_square(out), out_path)
    return {"addon_hex": hexs(color), "text_hex": hexs(ink),
            "shape": shape, "side": side, "text": text}


# ---------------------------------------------------------------- views
def split_views(paths: list[Path]) -> list[np.ndarray]:
    """One array per view. Several files are several views; one file is split on background
    gutters (a sheet of views), else kept whole."""
    imgs = [load_rgba(p) for p in paths]
    if len(imgs) != 1:
        return imgs
    img = imgs[0]
    mask = subject_mask(img)
    tiles = []
    for ys in _runs(mask.any(axis=1), img.shape[0]):
        for xs in _runs(mask[ys[0]:ys[1]].any(axis=0), img.shape[1]):
            tiles.append(img[ys[0]:ys[1], xs[0]:xs[1]])
    return tiles if len(tiles) > 1 else imgs


def _runs(occupied: np.ndarray, n: int) -> list[tuple[int, int]]:
    """Occupied spans separated by background gutters at least 1% of the size wide."""
    gap = max(2, n // 100)
    spans, start, empty = [], None, 0
    for i, v in enumerate(occupied):
        if v:
            if start is None:
                start = i
            empty = 0
        elif start is not None:
            empty += 1
            if empty >= gap:
                spans.append((start, i - empty + 1))
                start, empty = None, 0
    if start is not None:
        spans.append((start, n))
    # pad each span a little, never into the next
    return [(max(0, a - gap), min(n, b + gap)) for a, b in spans if b - a > n // 20]


def normalize_view(img: np.ndarray, *, size: int = 1024, margin: float = 0.08,
                   bg=BG) -> np.ndarray:
    """Crop to the subject, pad to a square with the same margin, flatten onto `bg`."""
    mask = subject_mask(img)
    b = bbox(mask)
    if b is not None:
        img = img[b[0]:b[1], b[2]:b[3]]
        mask = mask[b[0]:b[1], b[2]:b[3]]
    h, w = img.shape[:2]
    n = int(max(h, w) * (1 + 2 * margin))
    out = np.ones((n, n, 4), dtype=np.float32)
    out[..., :3] = np.array(bg, dtype=np.float32) / 255.0
    y, x = (n - h) // 2, (n - w) // 2
    a = img[..., 3:4] if img[..., 3].min() < 0.5 else mask[..., None].astype(np.float32)
    region = out[y:y + h, x:x + w]
    region[..., :3] = img[..., :3] * a + region[..., :3] * (1.0 - a)
    return resize(out, (size, size), Image.LANCZOS)


def near_identical(a: np.ndarray, b: np.ndarray, tol: float = 0.02, side: int = 128) -> bool:
    """Two views that are the same picture (mean absolute difference under tol at a small
    size): a model that hands the input back as a "view" would send one image twice."""
    x = resize(a[..., :3], (side, side))
    y = resize(b[..., :3], (side, side))
    return float(np.abs(x - y).mean()) < tol


def subject_share(img: np.ndarray) -> float:
    return float(subject_mask(img).mean())


# ---------------------------------------------------------------- palette
def palette(img: np.ndarray, mask: np.ndarray | None = None, k: int = 6,
            max_px: int = 20000) -> list[tuple[str, float]]:
    """Dominant colors of the subject: (hex, share), largest first. k-means in CIELAB."""
    from p2p_sim.color import lab_to_srgb, srgb_to_lab

    mask = subject_mask(img) if mask is None else mask
    px = img[..., :3][mask]
    if len(px) == 0:
        return []
    if len(px) > max_px:
        px = px[np.linspace(0, len(px) - 1, max_px).astype(int)]
    lab = srgb_to_lab(px.astype(np.float64))
    k = min(k, len(lab))
    centers = lab[np.linspace(0, len(lab) - 1, k).astype(int)].copy()
    for _ in range(12):
        d = ((lab[:, None, :] - centers[None]) ** 2).sum(-1)
        lbl = d.argmin(1)
        for j in range(k):
            sel = lab[lbl == j]
            if len(sel):
                centers[j] = sel.mean(0)
    d = ((lab[:, None, :] - centers[None]) ** 2).sum(-1)
    lbl = d.argmin(1)
    shares = np.bincount(lbl, minlength=k) / len(lbl)
    rgb = lab_to_srgb(centers)
    out = [(hexs(rgb[j]), float(shares[j]))
           for j in range(k) if shares[j] > 0]
    return sorted(out, key=lambda t: -t[1])


def palette_gap(a: list[tuple[str, float]], b: list[tuple[str, float]],
                min_share: float = 0.05) -> dict:
    """How far two palettes are apart: for each color over min_share in one, the CIEDE2000 to
    the nearest color of the other. `missing`: a's largest miss; `new`: b's largest miss."""
    from p2p_sim.color import ciede2000, srgb_to_lab

    def lab(h: str) -> np.ndarray:
        return srgb_to_lab(np.array(_rgb(h), dtype=np.uint8))

    def worst(x, y):
        big = [(h, s) for h, s in x if s >= min_share]
        if not big or not y:
            return 0.0, None
        ly = np.stack([lab(h) for h, _ in y])
        best = (0.0, None)
        for h, _ in big:
            de = float(ciede2000(lab(h)[None], ly).min())
            if de > best[0]:
                best = (de, h)
        return best

    miss, miss_hex = worst(a, b)
    new, new_hex = worst(b, a)
    return {"missing_de": round(miss, 1), "missing_hex": miss_hex, "new_de": round(new, 1),
            "new_hex": new_hex}
