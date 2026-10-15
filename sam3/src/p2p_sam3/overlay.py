"""Mask overlay PNGs for eyeballing segmentation results."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from .types import ImageSegments

COLORS = [
    (230, 25, 75),
    (60, 180, 75),
    (0, 130, 200),
    (245, 130, 48),
    (145, 30, 180),
    (70, 240, 240),
    (240, 50, 230),
    (210, 245, 60),
]


def overlay(rgb: np.ndarray, img: ImageSegments, prompts: list[str]) -> Image.Image:
    """One color per prompt; big masks drawn first so small parts stay visible; boxes labeled."""
    out = rgb.astype(np.float32).copy()
    layers = []
    for pi, p in enumerate(prompts):
        pm = img.get(p)
        layers += [(int(m.sum()), pi, m) for m in (pm.decoded() if pm else [])]
    for _, pi, m in sorted(layers, key=lambda t: -t[0]):
        out[m] = 0.35 * rgb[m] + 0.65 * np.array(COLORS[pi % len(COLORS)])
    im = Image.fromarray(out.astype(np.uint8))
    draw = ImageDraw.Draw(im)
    for pi, p in enumerate(prompts):
        pm = img.get(p)
        for box, s in zip(pm.boxes if pm else [], pm.scores if pm else [], strict=True):
            draw.rectangle(box, outline=COLORS[pi % len(COLORS)], width=2)
            draw.text((box[0] + 2, box[1] + 2), f"{p} {s:.2f}", fill=(255, 255, 255))
    draw.rectangle((0, 0, 8 + 7 * sum(len(p) + 2 for p in prompts), 16), fill=(0, 0, 0))
    x = 4
    for pi, p in enumerate(prompts):
        draw.text((x, 2), p, fill=COLORS[pi % len(COLORS)])
        x += 7 * (len(p) + 2)
    return im
