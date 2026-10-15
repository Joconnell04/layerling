"""Flat renders of the card and a 24-patch checker, and synthetic photos made from them.

Used for the card preview PNG, the tests, and `demo-photo`. Images are linear sRGB floats
(H, W, 3) until the camera model turns them into an 8-bit BGR photo.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .colour import SRGB_TO_XYZ, lab_to_linear, srgb_encode
from .layout import BASES, STEPS_MM, CardLayout, Rect
from .wedge import wedge_colour


@dataclass
class CardColours:
    """Linear sRGB appearance of every visible surface of the card."""

    tiles: np.ndarray  # (n, 3)
    black: np.ndarray  # (3,) the black base filament's solid colour
    white: np.ndarray
    numbers: np.ndarray  # (n, 3)
    steps: np.ndarray  # (n, 2, len(STEPS_MM), 3), base 0 = black, 1 = white

    @classmethod
    def from_model(cls, tiles_lin, black_slot: int, white_slot: int, number_slots,
                   lam) -> CardColours:
        """Steps from the exponential wedge model with decay lengths lam (n, 3) mm."""
        tiles_lin = np.asarray(tiles_lin, dtype=np.float64)
        lam = np.asarray(lam, dtype=np.float64)
        black, white = tiles_lin[black_slot - 1], tiles_lin[white_slot - 1]
        t = np.asarray(STEPS_MM)[:, None]
        steps = np.stack([np.stack([wedge_colour(t, tiles_lin[i], base, lam[i])
                                    for base in (black, white)]) for i in range(len(tiles_lin))])
        numbers = np.array([tiles_lin[s - 1] for s in number_slots])
        return cls(tiles_lin, black, white, numbers, steps)


def _fill(img: np.ndarray, r: Rect, height_mm: float, s: float, rgb) -> None:
    c0, c1 = round(r.x0 * s), round(r.x1 * s)
    r0, r1 = round((height_mm - r.y1) * s), round((height_mm - r.y0) * s)
    img[max(r0, 0):r1, max(c0, 0):c1] = rgb


def render_card(layout: CardLayout, colours: CardColours, px_per_mm: float = 20.0) -> np.ndarray:
    """Top view, image row 0 at the card's top edge (y = height)."""
    h, s = layout.height, px_per_mm
    img = np.zeros((round(h * s), round(layout.width * s), 3), dtype=np.float32)
    for strip in layout.strips():
        _fill(img, strip, h, s, colours.white)
    for mid in range(4):
        for r, black in layout.marker_cells(mid):
            if black:
                _fill(img, r, h, s, colours.black)
    for i in range(layout.n):
        _fill(img, layout.tile(i), h, s, colours.tiles[i])
        for r in layout.digit_runs(i):
            _fill(img, r, h, s, colours.numbers[i])
        for b, base in enumerate(BASES):
            for j in range(len(STEPS_MM)):
                _fill(img, layout.step(i, base, j), h, s, colours.steps[i, b, j])
    return img


@dataclass
class CheckerGeometry:
    patch_mm: float = 10.0
    gap_mm: float = 1.5
    border_mm: float = 4.0

    @property
    def pitch(self) -> float:
        return self.patch_mm + self.gap_mm

    @property
    def size_mm(self) -> tuple[float, float]:
        w = 6 * self.pitch - self.gap_mm + 2 * self.border_mm
        h = 4 * self.pitch - self.gap_mm + 2 * self.border_mm
        return w, h

    def patch_centre(self, i: int) -> tuple[float, float]:
        """Centre of patch i in chart mm, origin top left, y down."""
        r, c = divmod(i, 6)
        return (self.border_mm + c * self.pitch + self.patch_mm / 2,
                self.border_mm + r * self.pitch + self.patch_mm / 2)


def render_checker(lab_d65: np.ndarray, geom: CheckerGeometry | None = None,
                   px_per_mm: float = 10.0, frame_lin: float = 0.012) -> np.ndarray:
    geom = geom or CheckerGeometry()
    w, h = geom.size_mm
    s = px_per_mm
    img = np.full((round(h * s), round(w * s), 3), frame_lin, dtype=np.float32)
    lin = lab_to_linear(lab_d65)  # may be negative outside sRGB (cyan); the camera
    # works in a wider space, see Camera
    half = geom.patch_mm / 2
    for i in range(24):
        cx, cy = geom.patch_centre(i)
        img[round((cy - half) * s):round((cy + half) * s),
            round((cx - half) * s):round((cx + half) * s)] = lin[i]
    return img


# ------------------------------------------------------------------ synthetic photo


# Linear Rec. 2020 from XYZ (D65): a wide-gamut stand-in for a camera's native RGB, so
# checker patches outside sRGB (cyan) still get positive camera values, as with a real sensor.
XYZ_TO_REC2020 = np.array([[1.7166512, -0.3556708, -0.2533663],
                           [-0.6666844, 1.6164812, 0.0157685],
                           [0.0176399, -0.0427706, 0.9421031]])


@dataclass
class Camera:
    """Linear sRGB scene (values may be negative for wide-gamut colours) -> 8-bit JPEG:
    convert to linear Rec. 2020, a colour cast (3 x 3), a power per channel, sRGB encoding,
    noise, 8-bit rounding and optional JPEG compression."""

    cast: np.ndarray = field(default_factory=lambda: np.array(
        [[0.92, 0.06, 0.00], [0.03, 0.85, 0.04], [0.00, 0.08, 0.66]]))
    power: np.ndarray = field(default_factory=lambda: np.array([1.08, 0.95, 1.12]))
    exposure: float = 0.95
    noise: float = 1.2  # 8-bit counts, standard deviation
    jpeg_quality: int | None = 92

    def shoot(self, scene_lin: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        import cv2

        to_cam = self.cast * self.exposure @ XYZ_TO_REC2020 @ SRGB_TO_XYZ
        x = np.clip(scene_lin @ to_cam.T, 0, 1) ** self.power
        v = srgb_encode(x) * 255 + rng.normal(0, self.noise, x.shape)
        rgb = np.clip(np.round(v), 0, 255).astype(np.uint8)
        bgr = np.ascontiguousarray(rgb[..., ::-1])
        if self.jpeg_quality:
            _, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
            bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        return bgr


@dataclass
class SyntheticPhoto:
    bgr: np.ndarray
    card_mm_to_px: np.ndarray  # 3 x 3 homography, card mm (y up) -> photo px
    checker_centres: np.ndarray  # (24, 2) photo px, patch centres
    card_corners: np.ndarray  # (4, 2) photo px: top left, top right, bottom right, bottom left


def _warp_into(canvas: np.ndarray, src: np.ndarray, dst_quad: np.ndarray) -> np.ndarray:
    """Paste src (float image) into canvas through the homography taking src's corners
    (TL, TR, BR, BL) to dst_quad. Returns that homography (src px -> canvas px)."""
    import cv2

    h, w = src.shape[:2]
    src_quad = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    m = cv2.getPerspectiveTransform(src_quad, dst_quad.astype(np.float32))
    size = (canvas.shape[1], canvas.shape[0])
    warped = cv2.warpPerspective(src, m, size, flags=cv2.INTER_LINEAR)
    mask = cv2.warpPerspective(np.ones((h, w), np.float32), m, size, flags=cv2.INTER_LINEAR)
    canvas[:] = canvas * (1 - mask[..., None]) + warped * mask[..., None]
    return m


def synth_photo(layout: CardLayout, colours: CardColours, checker_lab_d65: np.ndarray,
                camera: Camera | None = None, seed: int = 0, size=(2400, 1500),
                card_quad=None, checker_quad=None, card_px_per_mm: float = 24.0,
                checker_px_per_mm: float = 12.0, card_light=None) -> SyntheticPhoto:
    """A grey table with the card on the left and a small 24-patch checker on the right, both
    seen in mild perspective at roughly 12 px/mm, then shot with `camera`.
    card_light: (left, right) light gain across the card only, e.g. (0.88, 1.12), for a
    window falloff that the checker does not see."""
    import cv2

    rng = np.random.default_rng(seed)
    camera = camera or Camera()
    w, h = size
    canvas = np.empty((h, w, 3), np.float32)
    canvas[:] = np.array([0.20, 0.19, 0.18], np.float32)
    canvas += rng.normal(0, 0.004, canvas.shape).astype(np.float32)
    card = render_card(layout, colours, card_px_per_mm)
    if card_quad is None:
        card_quad = np.array([[250, 330], [1180, 300], [1215, 930], [230, 950]], np.float64)
    m_card = _warp_into(canvas, card, np.asarray(card_quad))
    if card_light is not None:
        q = np.asarray(card_quad)
        x0, x1 = int(q[:, 0].min()) - 5, int(q[:, 0].max()) + 5
        ramp = np.linspace(card_light[0], card_light[1], x1 - x0, dtype=np.float32)
        canvas[:, x0:x1] *= ramp[None, :, None]
    geom = CheckerGeometry()
    chk = render_checker(checker_lab_d65, geom, checker_px_per_mm)
    if checker_quad is None:
        checker_quad = np.array([[1350, 420], [2230, 380], [2260, 1010], [1370, 1050]],
                                np.float64)
    m_chk = _warp_into(canvas, chk, np.asarray(checker_quad))
    bgr = camera.shoot(canvas, rng)

    # card mm (y up) -> card render px -> photo px
    s = card_px_per_mm
    mm_to_card_px = np.array([[s, 0, 0], [0, -s, layout.height * s], [0, 0, 1]])
    card_h = m_card @ mm_to_card_px
    card_h /= card_h[2, 2]
    corners_mm = np.array([[0, layout.height], [layout.width, layout.height],
                           [layout.width, 0], [0, 0]], np.float64)
    corners_px = cv2.perspectiveTransform(corners_mm[None], card_h)[0]
    centres_mm = np.array([geom.patch_centre(i) for i in range(24)]) * checker_px_per_mm
    centres_px = cv2.perspectiveTransform(centres_mm[None], m_chk)[0]
    return SyntheticPhoto(bgr, card_h, centres_px, corners_px)


def to_display(lin: np.ndarray) -> np.ndarray:
    """Linear float image -> 8-bit BGR for writing a PNG."""
    rgb = np.clip(np.round(np.clip(srgb_encode(lin), 0, 1) * 255), 0, 255).astype(np.uint8)
    return np.ascontiguousarray(rgb[..., ::-1])
