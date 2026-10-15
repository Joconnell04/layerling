"""Bambu Studio / PrusaSlicer per-triangle paint encoding (`paint_color` in Bambu 3MF files).

Derived from libslic3r (BambuStudio master, docs/research/bambu-3mf.md has line references):

- TriangleSelector::serialize() writes a bit stream per source triangle, depth first.
  Each node starts with 2 bits: the number of split sides (0 to 3).
  - Split node (1 to 3 sides): 2 more bits hold the special side, then the children are
    written in reverse order (last child first).
  - Leaf: the state. State 0 to 2 fits in 2 bits. State >= 3 writes the prefix 0b11, then
    (state - 3) as a run of 0b1111 nibbles (each worth 15) and a final nibble with the rest.
    That extension to 32 states is Bambu only; PrusaSlicer stops at 16.
- FacetsAnnotation::get_triangle_as_string() groups the bits into nibbles (first bit = least
  significant) and PREPENDS each hex digit, so the string reads last nibble first.
  Only upper case digits are valid (Bambu drops a triangle with anything else).

State 0 means "not painted, use the object's extruder". State n >= 1 is filament slot n.

Whole-triangle codes: slot 1 "4", 2 "8", 3 "0C", 4 "1C", ..., 17 "EC", 18 "0FC", ..., 32 "EFC".

Child geometry follows TriangleSelector::perform_split(). With the parent's vertices rotated
so the special side's vertex comes first (a, b, c):
  1 split side:  [a, b, m(b,c)], [m(b,c), c, a]                  areas 1/2, 1/2
  2 split sides: [a, m(a,b), m(a,c)], [m(a,b), b, m(a,c)],
                 [b, c, m(a,c)]                                   areas 1/4, 1/4, 1/2
  3 split sides: [a, m(a,b), m(c,a)], [m(a,b), b, m(b,c)],
                 [m(b,c), c, m(c,a)], [m(a,b), m(b,c), m(c,a)]   areas 1/4 each
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np

MAX_STATE = 32  # EnforcerBlockerType::ExtruderMax in BambuStudio
_HEX = "0123456789ABCDEF"
_HEX_VAL = {c: i for i, c in enumerate(_HEX)}


class PaintCodeError(ValueError):
    """A paint_color string that is not a valid TriangleSelector stream."""


# ------------------------------------------------------------------ tree model


@dataclass
class PaintNode:
    """One node of a triangle's split tree. Leaves carry `state`; split nodes carry children
    in perform_split() order (children[0] first)."""

    state: int = 0
    split_sides: int = 0
    special_side: int = 0
    children: list[PaintNode] = field(default_factory=list)

    @property
    def is_split(self) -> bool:
        return self.split_sides > 0


# ------------------------------------------------------------------ encoding


def _state_bits(state: int) -> list[int]:
    if not 0 <= state <= MAX_STATE:
        raise ValueError(f"paint state {state} out of range 0..{MAX_STATE}")
    bits = [0, 0]  # split_sides = 0
    if state >= 3:
        bits += [1, 1]
        n = state - 3
        while n >= 15:
            bits += [1, 1, 1, 1]
            n -= 15
        bits += [(n >> i) & 1 for i in range(4)]
    else:
        bits += [state & 1, (state >> 1) & 1]
    return bits


def _node_bits(node: PaintNode, out: list[int]) -> None:
    if node.is_split:
        if len(node.children) != node.split_sides + 1:
            raise ValueError("split node needs split_sides + 1 children")
        if node.split_sides == 3 and node.special_side != 0:
            raise ValueError("a 3-side split must have special_side 0")
        out += [node.split_sides & 1, (node.split_sides >> 1) & 1]
        out += [node.special_side & 1, (node.special_side >> 1) & 1]
        for child in reversed(node.children):
            _node_bits(child, out)
    else:
        out += _state_bits(node.state)


def _bits_to_string(bits: list[int]) -> str:
    if len(bits) % 4:
        raise AssertionError("bit stream is not nibble aligned")
    digits = []
    for i in range(0, len(bits), 4):
        v = bits[i] | bits[i + 1] << 1 | bits[i + 2] << 2 | bits[i + 3] << 3
        digits.append(_HEX[v])
    return "".join(reversed(digits))


@lru_cache(maxsize=64)
def encode_state(state: int) -> str:
    """paint_color for a whole (unsplit) triangle. State 0 returns "" (attribute omitted)."""
    if state == 0:
        return ""
    return _bits_to_string(_state_bits(state))


def encode_tree(node: PaintNode) -> str:
    """paint_color for a triangle with an arbitrary split tree. An unsplit state-0 leaf is ""."""
    if not node.is_split and node.state == 0:
        return ""
    bits: list[int] = []
    _node_bits(node, bits)
    return _bits_to_string(bits)


# ------------------------------------------------------------------ decoding


def _nibbles(code: str) -> list[int]:
    try:
        return [_HEX_VAL[c] for c in reversed(code)]
    except KeyError as e:
        raise PaintCodeError(f"invalid paint digit {e.args[0]!r} in {code!r}") from None


def decode_tree(code: str) -> PaintNode:
    """Parse a paint_color string into its split tree. "" is an unpainted leaf (state 0)."""
    if code == "":
        return PaintNode(state=0)
    nib = _nibbles(code)
    pos = 0

    def next_nibble() -> int:
        nonlocal pos
        if pos >= len(nib):
            raise PaintCodeError(f"truncated paint code {code!r}")
        v = nib[pos]
        pos += 1
        return v

    def read_node() -> PaintNode:
        c = next_nibble()
        split_sides = c & 0b11
        if split_sides:
            node = PaintNode(split_sides=split_sides, special_side=c >> 2)
            # Children are stored last first.
            kids = [read_node() for _ in range(split_sides + 1)]
            node.children = kids[::-1]
            return node
        if (c & 0b1100) == 0b1100:
            n = next_nibble()
            runs = 0
            while n == 0b1111:
                runs += 1
                n = next_nibble()
            return PaintNode(state=n + 15 * runs + 3)
        return PaintNode(state=c >> 2)

    root = read_node()
    if pos != len(nib):
        # Bambu stops after the root and ignores trailing bits; we are strict so that a bad
        # writer is caught in tests. Trailing zero nibbles only appear from padding, never.
        raise PaintCodeError(f"{len(nib) - pos} trailing nibbles in paint code {code!r}")
    return root


def _mid(p: np.ndarray, q: np.ndarray) -> np.ndarray:
    return (p + q) * 0.5


def split_children(tri: np.ndarray, split_sides: int, special_side: int) -> list[np.ndarray]:
    """Child triangles of `tri` (3 x D array: barycentric or xyz rows) in perform_split order."""
    s = special_side
    a, b, c = tri[s % 3], tri[(s + 1) % 3], tri[(s + 2) % 3]
    if split_sides == 1:
        m = _mid(b, c)
        return [np.stack([a, b, m]), np.stack([m, c, a])]
    if split_sides == 2:
        mab, mac = _mid(a, b), _mid(a, c)
        return [np.stack([a, mab, mac]), np.stack([mab, b, mac]), np.stack([b, c, mac])]
    if split_sides == 3:
        mab, mbc, mca = _mid(a, b), _mid(b, c), _mid(c, a)
        return [
            np.stack([a, mab, mca]),
            np.stack([mab, b, mbc]),
            np.stack([mbc, c, mca]),
            np.stack([mab, mbc, mca]),
        ]
    raise ValueError(f"split_sides must be 1..3, got {split_sides}")


_CHILD_AREA = {1: (0.5, 0.5), 2: (0.25, 0.25, 0.5), 3: (0.25, 0.25, 0.25, 0.25)}


def leaf_areas(node: PaintNode) -> dict[int, float]:
    """Fraction of the source triangle's area per state (sums to 1)."""
    out: dict[int, float] = {}

    def walk(n: PaintNode, frac: float) -> None:
        if not n.is_split:
            out[n.state] = out.get(n.state, 0.0) + frac
            return
        for child, f in zip(n.children, _CHILD_AREA[n.split_sides], strict=True):
            walk(child, frac * f)

    walk(node, 1.0)
    return out


def leaf_triangles(node: PaintNode, tri: np.ndarray) -> list[tuple[np.ndarray, int]]:
    """Leaf sub-triangles (same row format as `tri`) with their states, depth first."""
    out: list[tuple[np.ndarray, int]] = []

    def walk(n: PaintNode, t: np.ndarray) -> None:
        if not n.is_split:
            out.append((t, n.state))
            return
        for child, ct in zip(n.children, split_children(t, n.split_sides, n.special_side),
                             strict=True):
            walk(child, ct)

    walk(node, np.asarray(tri, dtype=float))
    return out


# Fast path table for whole-triangle codes (what our writer emits and what bucket fill makes).
WHOLE_CODES: dict[str, int] = {encode_state(s): s for s in range(1, MAX_STATE + 1)}


@lru_cache(maxsize=65536)
def decode_summary(code: str) -> tuple[int, bool, tuple[tuple[int, float], ...]]:
    """(majority state by area, is_split, ((state, area fraction), ...) sorted by state).

    Ties break toward the lower state number, so the result is deterministic."""
    s = WHOLE_CODES.get(code)
    if s is not None:
        return s, False, ((s, 1.0),)
    node = decode_tree(code)
    areas = leaf_areas(node)
    best = min(areas.items(), key=lambda kv: (-round(kv[1], 12), kv[0]))[0]
    return best, node.is_split, tuple(sorted(areas.items()))
