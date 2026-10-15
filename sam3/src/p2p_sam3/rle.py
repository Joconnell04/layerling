"""COCO run-length encoding for binary masks, numpy only (no pycocotools).

Format matches pycocotools: {"size": [h, w], "counts": str}. Runs are over the mask flattened in
column-major (Fortran) order and start with a run of zeros (possibly empty). The string form is
pycocotools' compressed encoding (6-bit chars, offset 48, deltas against the run two back), so
`pycocotools.mask.decode` reads what `encode` writes and vice versa.

The Runpod handler (sam3/runpod/handler.py) carries its own copy of `encode` so the GPU image
does not depend on this package; a test keeps the two in sync.
"""

from __future__ import annotations

from typing import TypedDict

import numpy as np


class RLE(TypedDict):
    size: list[int]
    counts: str


def runs(mask: np.ndarray) -> np.ndarray:
    """Run lengths of a 2D mask in COCO order: column-major, first run counts zeros."""
    if mask.ndim != 2:
        raise ValueError(f"mask must be 2D, got shape {mask.shape}")
    flat = np.asarray(mask, dtype=bool).ravel(order="F")
    n = flat.size
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    change = np.flatnonzero(flat[1:] != flat[:-1]) + 1
    bounds = np.concatenate(([0], change, [n]))
    lengths = np.diff(bounds)
    if flat[0]:
        lengths = np.concatenate(([0], lengths))
    return lengths.astype(np.int64)


def counts_to_string(counts: np.ndarray | list[int]) -> str:
    """pycocotools rleToString."""
    out: list[str] = []
    cnts = [int(c) for c in counts]
    for i, c in enumerate(cnts):
        x = c - cnts[i - 2] if i > 2 else c
        more = True
        while more:
            ch = x & 0x1F
            x >>= 5
            more = (x != -1) if (ch & 0x10) else (x != 0)
            if more:
                ch |= 0x20
            out.append(chr(ch + 48))
    return "".join(out)


def string_to_counts(s: str) -> list[int]:
    """pycocotools rleFrString."""
    cnts: list[int] = []
    p = 0
    n = len(s)
    while p < n:
        x = 0
        k = 0
        more = True
        while more:
            if p >= n:
                raise ValueError("truncated RLE string")
            c = ord(s[p]) - 48
            if c < 0 or c > 63:
                raise ValueError(f"invalid RLE character {s[p]!r}")
            x |= (c & 0x1F) << (5 * k)
            more = bool(c & 0x20)
            p += 1
            k += 1
            if not more and (c & 0x10):
                x |= -1 << (5 * k)
        if len(cnts) > 2:
            x += cnts[-2]
        cnts.append(x)
    return cnts


def encode(mask: np.ndarray) -> RLE:
    """Binary mask (H, W) -> compressed COCO RLE."""
    h, w = mask.shape
    return {"size": [int(h), int(w)], "counts": counts_to_string(runs(mask))}


def decode(rle: RLE | dict) -> np.ndarray:
    """COCO RLE (compressed string or uncompressed list counts) -> bool mask (H, W)."""
    h, w = (int(v) for v in rle["size"])
    counts = rle["counts"]
    if isinstance(counts, bytes):
        counts = counts.decode("ascii")
    cnts = string_to_counts(counts) if isinstance(counts, str) else [int(c) for c in counts]
    total = sum(cnts)
    if total != h * w:
        raise ValueError(f"RLE covers {total} pixels, expected {h * w}")
    if any(c < 0 for c in cnts):
        raise ValueError("negative run length")
    values = np.zeros(len(cnts), dtype=bool)
    values[1::2] = True
    flat = np.repeat(values, cnts)
    return flat.reshape((h, w), order="F")


def area(rle: RLE | dict) -> int:
    """Foreground pixel count without decoding."""
    counts = rle["counts"]
    cnts = string_to_counts(counts) if isinstance(counts, str) else list(counts)
    return int(sum(cnts[1::2]))


def bbox(mask: np.ndarray) -> list[float] | None:
    """[x0, y0, x1, y1] pixel bounds (x1, y1 exclusive), or None for an empty mask."""
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return None
    return [float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)]
