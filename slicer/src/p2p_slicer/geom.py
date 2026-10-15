"""Thin polygon layer over Clipper (pyclipper, Clipper1: the engine Bambu mostly uses).

Coordinates are int64 at 1e-5 mm (Bambu's SCALING_FACTOR). A path is an (n, 2) int64 array;
`Paths` is a list of paths with outer contours counter-clockwise and holes clockwise. `ExPoly`
is (outer, [holes]). Keep all polygon work here so the engine can be swapped (clipper2-py is
installed and pinned as the faster alternative).
"""

from __future__ import annotations

import math

import numpy as np
import pyclipper as pc

SCALE = 100000.0          # units per mm
UNIT = 1.0 / SCALE

Path = np.ndarray
Paths = list
ExPoly = tuple


def to_int(p_mm: np.ndarray) -> np.ndarray:
    return np.round(np.asarray(p_mm, dtype=np.float64) * SCALE).astype(np.int64)


def to_mm(p: np.ndarray) -> np.ndarray:
    return np.asarray(p, dtype=np.float64) * UNIT


def _arr(paths) -> list[np.ndarray]:
    return [np.asarray(p, dtype=np.int64).reshape(-1, 2) for p in paths if len(p) >= 3]


def area(p: np.ndarray) -> float:
    """Signed area in mm2 (CCW positive)."""
    if len(p) < 3:
        return 0.0
    q = np.asarray(p, dtype=np.float64)
    x = q[:, 0]
    y = q[:, 1]
    s = float(x[:-1] @ y[1:] - x[1:] @ y[:-1] + x[-1] * y[0] - x[0] * y[-1])
    return 0.5 * s * UNIT * UNIT


def total_area(paths: Paths) -> float:
    return sum(area(p) for p in paths)


def _exec(op: int, subj: Paths, clip: Paths | None = None, fill: int = pc.PFT_NONZERO) -> Paths:
    c = pc.Pyclipper()
    s = _arr(subj)
    if not s:
        return []
    try:
        c.AddPaths(s, pc.PT_SUBJECT, True)
    except pc.ClipperException:      # every path degenerate (collinear or repeated points)
        return []
    cl = _arr(clip or [])
    if cl:
        try:
            c.AddPaths(cl, pc.PT_CLIP, True)
        except pc.ClipperException:
            cl = []
    if not cl and op != pc.CT_UNION:
        if op == pc.CT_INTERSECTION:
            return []
        if op == pc.CT_DIFFERENCE:
            return _exec(pc.CT_UNION, s)
    return _arr(c.Execute(op, fill, fill))


def union(a: Paths, b: Paths | None = None) -> Paths:
    return _exec(pc.CT_UNION, list(a) + list(b or []))


def difference(a: Paths, b: Paths) -> Paths:
    if not b:
        return union(a)
    return _exec(pc.CT_DIFFERENCE, a, b)


def intersection(a: Paths, b: Paths) -> Paths:
    if not a or not b:
        return []
    return _exec(pc.CT_INTERSECTION, a, b)


def xor(a: Paths, b: Paths) -> Paths:
    return _exec(pc.CT_XOR, a, b)


_JOIN = {"miter": pc.JT_MITER, "round": pc.JT_ROUND, "square": pc.JT_SQUARE}


def offset(paths: Paths, delta_mm: float, join: str = "miter", miter_limit: float = 3.0,
           arc_tol_mm: float = 0.0125) -> Paths:
    """Grow (delta > 0) or shrink closed polygons. Result is a union."""
    p = _arr(paths)
    if not p:
        return []
    o = pc.PyclipperOffset(miter_limit, max(arc_tol_mm * SCALE, 1.0))
    try:
        o.AddPaths(p, _JOIN[join], pc.ET_CLOSEDPOLYGON)
    except pc.ClipperException:
        return []
    return _arr(o.Execute(delta_mm * SCALE))


def offset2(paths: Paths, d1_mm: float, d2_mm: float, join: str = "miter",
            miter_limit: float = 3.0) -> Paths:
    return offset(offset(paths, d1_mm, join, miter_limit), d2_mm, join, miter_limit)


def opening(paths: Paths, r_mm: float, join: str = "miter") -> Paths:
    """Shrink then grow: removes parts narrower than 2 r."""
    return offset2(paths, -r_mm, r_mm, join)


def closing(paths: Paths, r_mm: float, join: str = "miter") -> Paths:
    return offset2(paths, r_mm, -r_mm, join)


def expolygons(paths: Paths) -> list[ExPoly]:
    """Group a union's paths into (outer, holes) by containment (PolyTree)."""
    p = _arr(paths)
    if not p:
        return []
    c = pc.Pyclipper()
    try:
        c.AddPaths(p, pc.PT_SUBJECT, True)
    except pc.ClipperException:
        return []
    tree = c.Execute2(pc.CT_UNION, pc.PFT_NONZERO, pc.PFT_NONZERO)
    out: list[ExPoly] = []

    def walk(node) -> None:
        for ch in node.Childs:
            if not ch.IsHole:
                holes = [np.asarray(h.Contour, dtype=np.int64) for h in ch.Childs
                         if len(h.Contour) >= 3]
                out.append((np.asarray(ch.Contour, dtype=np.int64), holes))
                for h in ch.Childs:
                    walk(h)
    walk(tree)
    return out


def flatten(ex: list[ExPoly]) -> Paths:
    out: Paths = []
    for o, hs in ex:
        out.append(o)
        out.extend(hs)
    return out


def ex_area(e: ExPoly) -> float:
    return area(e[0]) + sum(area(h) for h in e[1])


def clip_lines(lines: list[np.ndarray], polys: Paths) -> list[np.ndarray]:
    """Open polylines clipped to polygons (parts inside)."""
    if not lines or not polys:
        return []
    c = pc.Pyclipper()
    n = 0
    for ln in lines:
        if len(ln) >= 2:
            try:
                c.AddPath(np.asarray(ln, dtype=np.int64), pc.PT_SUBJECT, False)
                n += 1
            except pc.ClipperException:
                continue
    if not n:
        return []
    try:
        c.AddPaths(_arr(polys), pc.PT_CLIP, True)
    except pc.ClipperException:
        return []
    tree = c.Execute2(pc.CT_INTERSECTION, pc.PFT_NONZERO, pc.PFT_NONZERO)
    return [np.asarray(p, dtype=np.int64) for p in pc.OpenPathsFromPolyTree(tree) if len(p) >= 2]


def simplify(p: np.ndarray, tol_mm: float) -> np.ndarray:
    """Douglas-Peucker on a closed path (keeps at least 3 points)."""
    if len(p) <= 4 or tol_mm <= 0:
        return p
    pts = p.astype(np.float64)
    tol = tol_mm * SCALE
    keep = np.zeros(len(pts), dtype=bool)
    # split the closed loop at its two farthest-apart points (first and farthest from it)
    far = int(np.argmax(((pts - pts[0]) ** 2).sum(1)))
    keep[0] = keep[far] = True
    stack = [(0, far), (far, len(pts))]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        pa = pts[a]
        pb = pts[b % len(pts)]
        seg = pb - pa
        L = math.hypot(seg[0], seg[1])
        mid = pts[a + 1:b]
        if L == 0:
            d = np.hypot(mid[:, 0] - pa[0], mid[:, 1] - pa[1])
        else:
            d = np.abs(seg[0] * (mid[:, 1] - pa[1]) - seg[1] * (mid[:, 0] - pa[0])) / L
        k = int(np.argmax(d))
        if d[k] > tol:
            i = a + 1 + k
            keep[i] = True
            stack.append((a, i))
            stack.append((i, b))
    out = p[keep]
    return out if len(out) >= 3 else p


try:
    from numba import njit
except ImportError:  # pragma: no cover
    def njit(*a, **k):  # type: ignore[misc]
        if a and callable(a[0]):
            return a[0]
        return lambda f: f


@njit(cache=True)
def _rdp_keep(x: np.ndarray, y: np.ndarray, tol: float) -> np.ndarray:
    n = x.shape[0]
    keep = np.zeros(n, dtype=np.bool_)
    far = 0
    best = -1.0
    for k in range(n):
        d = (x[k] - x[0]) ** 2 + (y[k] - y[0]) ** 2
        if d > best:
            best = d
            far = k
    keep[0] = True
    keep[far] = True
    stack_a = np.empty(2 * n + 4, dtype=np.int64)
    stack_b = np.empty(2 * n + 4, dtype=np.int64)
    sp = 0
    stack_a[sp] = 0
    stack_b[sp] = far
    sp += 1
    stack_a[sp] = far
    stack_b[sp] = n
    sp += 1
    while sp > 0:
        sp -= 1
        a = stack_a[sp]
        b = stack_b[sp]
        if b - a < 2:
            continue
        bb = b % n
        sx = x[bb] - x[a]
        sy = y[bb] - y[a]
        L = (sx * sx + sy * sy) ** 0.5
        dmax = -1.0
        kmax = -1
        for k in range(a + 1, b):
            if L == 0.0:
                d = ((x[k] - x[a]) ** 2 + (y[k] - y[a]) ** 2) ** 0.5
            else:
                d = abs(sx * (y[k] - y[a]) - sy * (x[k] - x[a])) / L
            if d > dmax:
                dmax = d
                kmax = k
        if dmax > tol:
            keep[kmax] = True
            stack_a[sp] = a
            stack_b[sp] = kmax
            sp += 1
            stack_a[sp] = kmax
            stack_b[sp] = b
            sp += 1
    return keep


def simplify_paths(paths: Paths, tol_mm: float) -> Paths:
    """Douglas-Peucker on closed paths (Bambu simplifies contours at `resolution`, 0.012 mm)."""
    out: Paths = []
    tol = tol_mm * SCALE
    for p in paths:
        if len(p) <= 4:
            out.append(p)
            continue
        q = p.astype(np.float64)
        keep = _rdp_keep(q[:, 0], q[:, 1], tol)
        s = p[keep]
        if len(s) >= 3:
            out.append(s)
    return out


def bbox(paths: Paths) -> tuple[int, int, int, int] | None:
    if not paths:
        return None
    a = np.concatenate([np.asarray(p) for p in paths])
    return int(a[:, 0].min()), int(a[:, 1].min()), int(a[:, 0].max()), int(a[:, 1].max())


def point_in(pt: tuple[int, int], paths: Paths) -> bool:
    """Inside the union of paths (non-zero rule)."""
    wn = 0
    for p in paths:
        r = pc.PointInPolygon(pt, p)
        if r == -1:
            return True
        if r == 1:
            wn += 1 if area(p) > 0 else -1
    return wn > 0


def perimeter(p: np.ndarray, closed: bool = True) -> float:
    q = p.astype(np.float64)
    d = np.diff(np.vstack([q, q[:1]]) if closed else q, axis=0)
    return float(np.hypot(d[:, 0], d[:, 1]).sum()) * UNIT
