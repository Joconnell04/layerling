"""Photo of the printed swatch card next to a 24-patch checker -> measured CIELAB and td_mm.

1. Find the card by its four ArUco markers (or four clicked corners) -> card mm to pixels.
2. Find the checker patches (OpenCV mcc, or four clicked corner-patch centres).
3. Fit the colour correction on the checker (correction.py), try the chart both ways up.
4. Sample every tile and wedge step, correct to CIELAB D65 and linear sRGB.
5. Fit td_mm per filament from the wedges (wedge.py).
6. Write report.json and overlay.png, then lab / lab_source / td_mm into library.json.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import numpy as np

from . import checker as chk
from . import correction, wedge
from .card import read_card_sidecar
from .colour import ciede2000, lab_to_hex, srgb_decode
from .layout import ARUCO_DICT, BASES, MARKER_IDS, STEPS_MM, CardLayout
from .library import DEFAULT_LIBRARY, REPO, ams_palette, load, write_measured

DEFAULT_CARD = REPO / "filaments" / "swatch_card.3mf"
LUT = srgb_decode(np.arange(256) / 255.0)
MAX_FIT_DE = 5.0  # refuse to write the library above this mean checker error


class CalibrationError(RuntimeError):
    pass


# ------------------------------------------------------------------ sampling


@dataclass
class Sample:
    rgb_lin: np.ndarray  # (3,) trimmed mean of linearised pixels
    n: int
    clipped: float  # fraction of pixels with a channel at 254 or 255
    cv: float  # coefficient of variation of luminance (glare, texture, uneven light)
    quads: list[np.ndarray]


def sample(img_bgr: np.ndarray, quads: list[np.ndarray], trim: float = 0.1) -> Sample:
    import cv2

    h, w = img_bgr.shape[:2]
    pix = []
    for q in quads:
        q = np.asarray(q, dtype=np.float64)
        x0, y0 = np.floor(q.min(axis=0)).astype(int)
        x1, y1 = np.ceil(q.max(axis=0)).astype(int) + 1
        x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, w), min(y1, h)
        if x1 <= x0 or y1 <= y0:
            continue
        mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
        pts = np.round((q - [x0, y0]) * 16).astype(np.int32)
        cv2.fillPoly(mask, [pts], 1, lineType=cv2.LINE_8, shift=4)
        pix.append(img_bgr[y0:y1, x0:x1][mask > 0])
    px = np.concatenate(pix) if pix else np.zeros((0, 3), np.uint8)
    if len(px) == 0:
        return Sample(np.full(3, np.nan), 0, 0.0, 0.0, quads)
    rgb8 = px[:, ::-1]
    clipped = float(np.mean(np.any(rgb8 >= 254, axis=1)))
    lin = LUT[rgb8]
    lum = lin @ np.array([0.2126, 0.7152, 0.0722])
    order = np.argsort(lum)
    k = len(order)
    keep = order[int(k * trim):max(int(k * (1 - trim)), int(k * trim) + 1)]
    mean = lin[keep].mean(axis=0)
    cv = float(lum[keep].std() / max(lum[keep].mean(), 1e-6))
    return Sample(mean, k, clipped, cv, quads)


def _project(h: np.ndarray, pts_mm: np.ndarray) -> np.ndarray:
    import cv2

    return cv2.perspectiveTransform(np.asarray(pts_mm, np.float64)[None], h)[0]


# ------------------------------------------------------------------ finding the card


@dataclass
class CardFound:
    h: np.ndarray  # card mm (y up) -> photo px
    method: str
    markers: list[int]
    reprojection_px: float | None
    px_per_mm: float


def parse_points(text: str) -> np.ndarray:
    """'x,y x,y x,y x,y' (spaces or semicolons between points) -> (4, 2)."""
    parts = [p for p in text.replace(";", " ").split() if p]
    pts = np.array([[float(v) for v in p.split(",")] for p in parts], dtype=np.float64)
    if pts.shape != (4, 2):
        raise ValueError(f"need four x,y points, got {text!r}")
    return pts


def _px_per_mm(h: np.ndarray, layout: CardLayout) -> float:
    c = _project(h, np.array([[0, 0], [layout.width, 0], [0, layout.height]]))
    return float((np.linalg.norm(c[1] - c[0]) / layout.width
                  + np.linalg.norm(c[2] - c[0]) / layout.height) / 2)


def find_card(img_bgr: np.ndarray, layout: CardLayout, corners=None) -> CardFound | None:
    """corners: the card's outer corners in the photo, top left, top right, bottom right,
    bottom left, with the tile row at the top. Otherwise the ArUco markers are used."""
    import cv2

    if corners is not None:
        src = np.array([[0, layout.height], [layout.width, layout.height], [layout.width, 0],
                        [0, 0]], np.float64)
        h = cv2.getPerspectiveTransform(src.astype(np.float32),
                                        np.asarray(corners, np.float32))
        return CardFound(h, "manual", [], None, _px_per_mm(h, layout))
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(
        getattr(cv2.aruco, ARUCO_DICT)), params)
    found, ids, _ = det.detectMarkers(gray)
    if ids is None:
        return None
    src, dst, used = [], [], []
    for c, mid in zip(found, ids.ravel(), strict=True):
        mid = int(mid)
        if mid in MARKER_IDS and mid not in used:
            used.append(mid)
            src.append(layout.marker_corners(mid))
            dst.append(np.asarray(c, np.float64).reshape(4, 2))
    if not used:
        return None
    src_a, dst_a = np.concatenate(src), np.concatenate(dst)
    h, _ = cv2.findHomography(src_a, dst_a, 0)
    if h is None:
        return None
    err = float(np.sqrt(np.mean(np.sum((_project(h, src_a) - dst_a) ** 2, axis=1))))
    return CardFound(h, "auto", sorted(used), round(err, 3), _px_per_mm(h, layout))


class LightMap:
    """Relative light across the card from the four white-strip patches, bilinear in card
    mm, per camera channel (after the camera's tone curve a light gain g becomes g ** p per
    channel, which one grey gain cannot undo). 1 on average; card samples are divided by it."""

    def __init__(self, layout: CardLayout, refs: list[Sample]):
        rgb = np.array([r.rgb_lin for r in refs])  # (4, 3): tl, tr, bl, br
        self.gains = rgb / rgb.mean(axis=0)
        lum = rgb @ np.array([0.2126, 0.7152, 0.0722])
        self.lum_gains = lum / lum.mean()
        rects = layout.white_refs()
        self.xs = (rects[0].centre[0], rects[1].centre[0])
        self.ys = (rects[2].centre[1], rects[0].centre[1])  # bottom, top

    @property
    def spread(self) -> float:
        return float(self.lum_gains.max() / self.lum_gains.min() - 1)

    def gain(self, x: float, y: float) -> np.ndarray:
        tl, tr, bl, br = self.gains
        u = (x - self.xs[0]) / (self.xs[1] - self.xs[0])
        v = (y - self.ys[0]) / (self.ys[1] - self.ys[0])
        bottom = bl + (br - bl) * u
        top = tl + (tr - tl) * u
        return bottom + (top - bottom) * v


# ------------------------------------------------------------------ card description


@dataclass
class CardInfo:
    layout: CardLayout
    filament_ids: list[str]
    ams_slots: list[int | None]
    black_col: int
    white_col: int
    source: str


def card_info(card_path: Path | None, library) -> CardInfo:
    side = read_card_sidecar(card_path) if card_path else None
    if side:
        cols = sorted(side["columns"], key=lambda c: c["column"])
        labels = tuple(side["layout"]["labels"])
        return CardInfo(CardLayout(labels), [c["filament_id"] for c in cols],
                        [c["ams_slot"] for c in cols], side["black_slot"] - 1,
                        side["white_slot"] - 1, str(card_path))
    from .card import plan_card

    plan = plan_card(ams_palette(library))
    return CardInfo(plan.layout, [f.id for f in plan.palette],
                    [f.ams_slot for f in plan.palette], plan.black_slot - 1,
                    plan.white_slot - 1, "library AMS slots")


# ------------------------------------------------------------------ overlay


def _draw(img, quads, colour, label=None, thick=2):
    import cv2

    for q in quads:
        cv2.polylines(img, [np.round(q).astype(np.int32)], True, (0, 0, 0), thick + 2,
                      cv2.LINE_AA)
        cv2.polylines(img, [np.round(q).astype(np.int32)], True, colour, thick, cv2.LINE_AA)
    if label:
        c = np.concatenate(quads).mean(axis=0)
        _text(img, label, (int(c[0]), int(c[1])), centre=True)


def _text(img, text, org, scale=0.5, centre=False):
    import cv2

    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw, th), base = cv2.getTextSize(text, font, scale, 1)
    x, y = org
    if centre:
        x, y = x - tw // 2, y + th // 2
    cv2.rectangle(img, (x - 3, y - th - 3), (x + tw + 3, y + base), (20, 20, 20), -1)
    cv2.putText(img, text, (x, y), font, scale, (255, 255, 255), 1, cv2.LINE_AA)


def overlay(img_bgr, patches: list[Sample], patch_de, card: CardFound | None, layout,
            tiles: list[Sample], steps, labels, header: str, max_px: int = 2000) -> np.ndarray:
    import cv2

    s = min(1.0, max_px / max(img_bgr.shape[:2]))
    img = cv2.resize(img_bgr, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 \
        else img_bgr.copy()
    for i, (p, de) in enumerate(zip(patches, patch_de, strict=True)):
        col = (80, 220, 80) if de < 2 else (40, 200, 240) if de < 4 else (60, 60, 240)
        _draw(img, [q * s for q in p.quads], col, str(i + 1))
    if card is not None:
        for mid in MARKER_IDS:
            _draw(img, [_project(card.h, layout.marker_corners(mid)) * s], (240, 200, 40), thick=1)
        for i, t in enumerate(tiles):
            _draw(img, [q * s for q in t.quads], (255, 255, 255), labels[i])
        for i in range(len(tiles)):
            for b in range(len(BASES)):
                for smp in steps[i][b]:
                    _draw(img, [q * s for q in smp.quads], (255, 255, 255), thick=1)
    _text(img, header, (12, 28), scale=0.6)
    return img


def grid_image(img_bgr, step: int = 100, max_px: int = 2400) -> np.ndarray:
    """The photo with a labelled pixel grid (original pixel coordinates), for picking corners
    by eye when automatic detection fails."""
    import cv2

    s = min(1.0, max_px / max(img_bgr.shape[:2]))
    img = cv2.resize(img_bgr, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 \
        else img_bgr.copy()
    h, w = img_bgr.shape[:2]
    for x in range(0, w, step):
        cv2.line(img, (int(x * s), 0), (int(x * s), img.shape[0]), (0, 255, 255), 1)
        if x % (step * 5) == 0:
            _text(img, str(x), (int(x * s) + 2, 16), scale=0.4)
    for y in range(0, h, step):
        cv2.line(img, (0, int(y * s)), (img.shape[1], int(y * s)), (0, 255, 255), 1)
        if y % (step * 5) == 0:
            _text(img, str(y), (2, int(y * s) - 2), scale=0.4)
    return img


# ------------------------------------------------------------------ main entry


def read_photo(path: Path) -> np.ndarray | None:
    """8-bit BGR with EXIF rotation applied. HEIC/HEIF (iPhone default) goes through macOS
    `sips` to a lossless PNG first."""
    import cv2

    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None and path.suffix.lower() in {".heic", ".heif"} and shutil.which("sips"):
        with tempfile.TemporaryDirectory() as d:
            png = Path(d) / "photo.png"
            subprocess.run(["sips", "-s", "format", "png", str(path), "--out", str(png)],
                           capture_output=True, check=False, timeout=120)
            img = cv2.imread(str(png), cv2.IMREAD_COLOR)
    return img


def run(photo: str | Path, *, library: str | Path = DEFAULT_LIBRARY,
        card: str | Path | None = DEFAULT_CARD, checker: str = "classic24",
        checker_points=None, card_points=None, out_dir: str | Path | None = None,
        write: bool = True, threshold: float = 2.0) -> dict:
    import cv2

    photo = Path(photo)
    img = read_photo(photo)
    if img is None:
        raise CalibrationError(f"cannot read {photo}")
    out = Path(out_dir) if out_dir else REPO / "filaments" / "calibration" / photo.stem
    out.mkdir(parents=True, exist_ok=True)
    lib = load(library)
    info = card_info(Path(card) if card else None, lib)
    layout = info.layout
    warnings: list[str] = []
    if card and info.source == "library AMS slots":
        warnings.append(f"{card} not found or not a swatch card; columns taken from the "
                        "library's AMS slots")

    # locate
    found_card = find_card(img, layout, corners=card_points)
    if checker_points is not None:
        found_chk = chk.from_corner_patches(checker_points)
    else:
        found_chk = chk.detect_auto(img)
    if found_card is None or found_chk is None:
        cv2.imwrite(str(out / "grid.png"), grid_image(img))
        what = " and ".join(n for n, f in (("card markers", found_card),
                                            ("checker", found_chk)) if f is None)
        raise CalibrationError(
            f"could not find the {what} in {photo.name}. Pixel grid written to "
            f"{out / 'grid.png'}; pass --card-corners and/or --checker-corners (see README).")
    if found_card.method == "auto" and len(found_card.markers) < 4:
        warnings.append(f"only markers {found_card.markers} of 0-3 found; card position is "
                        "less certain")
    if found_card.reprojection_px is not None and found_card.reprojection_px > 2.0:
        warnings.append(f"marker fit error {found_card.reprojection_px:.1f} px; card may be bent")

    # checker fit, both ways up
    ref = chk.reference_lab_d65(checker)
    best = None
    for order, cand in (("as found", found_chk), ("reversed", chk.reversed_order(found_chk))):
        patches = [sample(img, [q]) for q in cand.quads]
        rgb = np.array([p.rgb_lin for p in patches])
        if np.isnan(rgb).any():
            continue
        ev = correction.evaluate(rgb, ref, list(chk.NAMES))
        if best is None or ev["summary"]["fit_de2000_mean"] < best[2]["summary"]["fit_de2000_mean"]:
            best = (order, patches, ev)
    if best is None:
        raise CalibrationError("checker patches fall outside the photo")
    order, patches, ev = best
    corr = ev["correction"]
    fit_mean = ev["summary"]["fit_de2000_mean"]
    for i, p in enumerate(patches):
        if p.clipped > 0.02:
            warnings.append(f"checker patch {i + 1} ({chk.NAMES[i]}) is clipped; lower exposure")
    grey = [patches[i].rgb_lin[1] for i in chk.NEUTRALS]
    if not all(a > b for a, b in pairwise(grey)):
        warnings.append("checker grey patches are not in order; detection may be wrong")
    if fit_mean > 3.0:
        warnings.append(f"checker fit mean dE2000 {fit_mean:.2f} is high (light mixed, glare "
                        "or wrong --checker?)")

    # card samples, flat-fielded with the light map from the white strips
    h = found_card.h
    refs = [sample(img, [_project(h, r.corners)]) for r in layout.white_refs()]
    if any(np.isnan(r.rgb_lin).any() or r.n == 0 for r in refs):
        raise CalibrationError("part of the card is outside the photo")
    light = LightMap(layout, refs)
    if any(r.clipped > 0.02 for r in refs):
        light.gains[:] = 1.0
        light.lum_gains[:] = 1.0
        warnings.append("white strip is clipped: no light correction across the card; "
                        "lower exposure")
    elif light.spread > 0.05:
        warnings.append(f"light varies {light.spread:.0%} across the card (corrected); "
                        "even light gives better numbers")

    def card_sample(rects):
        s = sample(img, [_project(h, r.corners) for r in rects])
        cx = sum(r.centre[0] * r.area for r in rects) / sum(r.area for r in rects)
        cy = sum(r.centre[1] * r.area for r in rects) / sum(r.area for r in rects)
        s.rgb_lin = s.rgb_lin / light.gain(cx, cy)
        return s

    tiles = [card_sample(layout.tile_samples(i)) for i in range(layout.n)]
    steps = [[[card_sample([layout.step_sample(i, base, j)])
               for j in range(len(STEPS_MM))] for base in BASES] for i in range(layout.n)]
    all_samples = [(f"tile {info.ams_slots[i]}", t) for i, t in enumerate(tiles)] + [
        (f"step {info.ams_slots[i]} {BASES[b]} {STEPS_MM[j]}", steps[i][b][j])
        for i in range(layout.n) for b in range(2) for j in range(len(STEPS_MM))]
    small = [n for n, s in all_samples if s.n < 30]
    if small:
        warnings.append(f"{len(small)} card areas have under 30 pixels; move closer "
                        f"(first: {small[0]})")
    if any(np.isnan(s.rgb_lin).any() for _, s in all_samples):
        raise CalibrationError("part of the card is outside the photo")
    for n, s in all_samples:
        if s.clipped > 0.02:
            warnings.append(f"{n} is clipped; lower exposure")
    for i, t in enumerate(tiles):
        if t.cv > 0.10:
            warnings.append(f"tile {info.ams_slots[i]} is uneven ({t.cv:.0%}); glare or shadow?")

    tile_lab = corr.lab(np.array([t.rgb_lin for t in tiles]))
    tile_lin = corr.linear_srgb(np.array([t.rgb_lin for t in tiles]))
    step_lin = corr.linear_srgb(np.array([[[s.rgb_lin for s in base] for base in col]
                                          for col in steps]))
    by_id = {f.id: f for f in lib.filaments}
    rows, updates = [], {}
    for i, fid in enumerate(info.filament_ids):
        est = wedge.estimate(STEPS_MM, step_lin[i, 0], step_lin[i, 1], tile_lin[i],
                             tile_lin[info.black_col], tile_lin[info.white_col], threshold)
        nominal = by_id[fid].lab if fid in by_id else None
        rows.append({
            "filament_id": fid,
            "ams_slot": info.ams_slots[i],
            "column": i,
            "lab_before": list(nominal) if nominal else None,
            "lab_measured": [round(float(x), 2) for x in tile_lab[i]],
            "hex_measured": lab_to_hex(tile_lab[i]),
            "de2000_vs_before": (round(float(ciede2000(tile_lab[i], nominal)), 2)
                                 if nominal else None),
            "tile": {"pixels": tiles[i].n, "clipped": round(tiles[i].clipped, 4),
                     "cv": round(tiles[i].cv, 4)},
            "wedge_lab": {base: [[round(float(x), 2) for x in lab] for lab in
                                 corr.lab(np.array([s.rgb_lin for s in steps[i][b]]))]
                          for b, base in enumerate(BASES)},
            "td": est,
        })
        if fid in by_id:
            updates[fid] = {"lab": tile_lab[i], "td_mm": est["td_mm"]}
        else:
            warnings.append(f"{fid} (column {i + 1}) is not in the library; not written")

    header = (f"checker dE00 {fit_mean:.2f} (LOO {ev['summary']['loo_de2000_mean']:.2f})  "
              f"card {found_card.method}"
              + (f" markers {len(found_card.markers)}/4" if found_card.method == "auto" else ""))
    labels = [f"{info.ams_slots[i]}" + (f"  dE {r['de2000_vs_before']:.1f}"
                                        if r["de2000_vs_before"] is not None else "")
              for i, r in enumerate(rows)]
    cv2.imwrite(str(out / "overlay.png"),
                overlay(img, patches, ev["de2000"], found_card, layout, tiles, steps, labels,
                        header))

    written, changes, reason = False, {}, None
    if not write:
        reason = "dry run"
    elif fit_mean > MAX_FIT_DE:
        reason = f"checker fit mean dE2000 {fit_mean:.2f} > {MAX_FIT_DE}; library not changed"
        warnings.append(reason)
    elif updates:
        changes = write_measured(library, updates)
        written = True

    report = {
        "photo": {"path": str(photo), "sha256": hashlib.sha256(photo.read_bytes()).hexdigest(),
                  "size_px": [int(img.shape[1]), int(img.shape[0])]},
        "checker": {
            "name": checker, "source": chk.SOURCES[checker], "method": found_chk.method,
            "order": order, **ev["summary"],
            "patches": [{"n": i + 1, "name": chk.NAMES[i],
                         "ref_lab_d65": [round(float(x), 2) for x in ref[i]],
                         "measured_lab": [round(float(x), 2) for x in corr.lab(p.rgb_lin)],
                         "de2000": round(float(ev["de2000"][i]), 2),
                         "de2000_loo": round(float(ev["de2000_loo"][i]), 2),
                         "pixels": p.n, "clipped": round(p.clipped, 4)}
                        for i, p in enumerate(patches)],
        },
        "card": {"source": info.source, "method": found_card.method,
                 "markers": found_card.markers, "reprojection_px": found_card.reprojection_px,
                 "light_gains_tl_tr_bl_br": [round(float(g), 4) for g in light.lum_gains],
                 "px_per_mm": round(found_card.px_per_mm, 2), "layout": layout.to_dict(),
                 "black_column": info.black_col, "white_column": info.white_col},
        "correction": corr.to_dict(),
        "td_definition": ("smallest thickness where the colour over black and over white are "
                          f"both within dE2000 {threshold} of the 2.0 mm tile (wedge.py)"),
        "filaments": rows,
        "library": {"path": str(library), "written": written, "reason": reason,
                    "changes": changes},
        "warnings": warnings,
        "overlay": str(out / "overlay.png"),
    }
    (out / "report.json").write_text(json.dumps(report, indent=1, default=_json) + "\n")
    report["report"] = str(out / "report.json")
    return report


def _json(o):
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, np.generic):
        return o.item()
    raise TypeError(type(o))
