"""Paint audit: painted details too narrow for the nozzle.

The slicer merges painted regions narrower than about two nozzle widths into their neighbour
without saying so (PLAN: simulate_print erodes below 2 x nozzle). This audit finds them before
slicing so the gate can say which detail will vanish.

A region is a set of edge-connected triangles with the same slot. Its width is estimated as
twice the largest distance from a triangle centroid in the region to the region's boundary
(boundary edge midpoints): the diameter of the largest inscribed circle, which is the disc
diameter for an eye and the strip width for a whisker, and is not fooled by the saw-tooth
outline that whole-triangle paint makes. Regions touching open mesh edges count those edges as
boundary too.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree


@dataclass
class Region:
    slot: int
    part: int
    faces: int
    area_mm2: float
    width_mm: float
    centroid: tuple[float, float, float]


@dataclass
class PaintAudit:
    min_width_mm: float
    regions: int
    thin: list[Region]
    slots_only_thin: list[int]  # slots whose every region is under the minimum

    def warnings(self, top: int = 3) -> list[str]:
        if not self.thin:
            return []
        by_slot: dict[int, list[Region]] = {}
        for r in self.thin:
            by_slot.setdefault(r.slot, []).append(r)
        parts = [f"slot {s}: {len(rs)} region(s), {sum(r.area_mm2 for r in rs):.2f} mm2"
                 for s, rs in sorted(by_slot.items())]
        out = [(f"thin paint: {len(self.thin)} painted region(s) narrower than "
                f"{self.min_width_mm:g} mm ({'; '.join(parts)})")]
        for r in sorted(self.thin, key=lambda r: -r.area_mm2)[:top]:
            c = ", ".join(f"{x:.1f}" for x in r.centroid)
            out.append(f"thin paint: slot {r.slot} region of {r.area_mm2:.2f} mm2 is about "
                       f"{r.width_mm:.2f} mm wide at ({c})")
        for s in self.slots_only_thin:
            out.append(f"thin paint: every region of slot {s} is under {self.min_width_mm:g} mm; "
                       "the slicer will likely drop that filament")
        return out

    def as_dict(self) -> dict:
        d = asdict(self)
        d["thin"] = [asdict(r) for r in self.thin]
        return d


def _edges(faces: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Unique undirected edges and, per face corner edge, its edge index (M*3,)."""
    e = np.stack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]], axis=1).reshape(-1, 2)
    e.sort(axis=1)
    uniq, inv = np.unique(e, axis=0, return_inverse=True)
    return uniq, inv.reshape(-1)


@dataclass
class _Analysis:
    label: np.ndarray  # region id per face
    region_slot: dict[int, int]
    region_part: dict[int, int]
    region_width: dict[int, float]  # inf for regions without a boundary
    region_faces: dict[int, np.ndarray]
    cross_a: np.ndarray  # face pairs across region boundaries
    cross_b: np.ndarray
    area: np.ndarray
    cent: np.ndarray


def _analyze(v: np.ndarray, f: np.ndarray, s: np.ndarray, part: np.ndarray) -> _Analysis:
    m = len(f)
    uniq, inv = _edges(f)
    face_of = np.repeat(np.arange(m), 3)
    order = np.argsort(inv, kind="stable")
    inv_sorted, faces_sorted = inv[order], face_of[order]
    counts = np.bincount(inv_sorted, minlength=len(uniq))
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    two = counts == 2  # manifold edges have exactly two faces
    a = faces_sorted[starts[two]]
    b = faces_sorted[starts[two] + 1]
    same = (s[a] == s[b]) & (part[a] == part[b])
    g = coo_matrix((np.ones(same.sum()), (a[same], b[same])), shape=(m, m))
    _, label = connected_components(g, directed=False)

    # Boundary edges: shared by faces of different regions, or not shared by exactly two faces.
    mid = (v[uniq[:, 0]] + v[uniq[:, 1]]) / 2
    boundary_pts: dict[int, list[np.ndarray]] = {}
    diff = ~same
    e_diff = np.flatnonzero(two)[diff]
    for lab_arr in (label[a[diff]], label[b[diff]]):
        order2 = np.argsort(lab_arr, kind="stable")
        for lab, idx in _group(lab_arr[order2], e_diff[order2]):
            boundary_pts.setdefault(lab, []).append(mid[idx])
    open_e = np.flatnonzero(~two)
    if len(open_e):
        lab_arr = label[faces_sorted[starts[open_e]]]
        order2 = np.argsort(lab_arr, kind="stable")
        for lab, idx in _group(lab_arr[order2], open_e[order2]):
            boundary_pts.setdefault(lab, []).append(mid[idx])

    tri = v[f]
    area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    cent = tri.mean(axis=1)
    order3 = np.argsort(label, kind="stable")
    r_slot, r_part, r_width, r_faces = {}, {}, {}, {}
    for lab, idx in _group(label[order3], order3):
        r_slot[lab] = int(s[idx[0]])
        r_part[lab] = int(part[idx[0]])
        r_faces[lab] = idx
        pts = boundary_pts.get(lab)
        if pts is None:
            r_width[lab] = float("inf")
            continue
        dist, _ = cKDTree(np.concatenate(pts)).query(cent[idx])
        r_width[lab] = 2.0 * float(dist.max())
    return _Analysis(label, r_slot, r_part, r_width, r_faces, a[diff], b[diff], area, cent)


def _inputs(vertices, faces, face_slots, face_part):
    v = np.asarray(vertices, dtype=np.float64)
    f = np.asarray(faces, dtype=np.int64)
    s = np.asarray(face_slots, dtype=np.int64)
    part = np.zeros(len(f), dtype=np.int64) if face_part is None else np.asarray(face_part)
    return v, f, s, part


def audit_paint(vertices: np.ndarray, faces: np.ndarray, face_slots: np.ndarray,
                face_part: np.ndarray | None = None, min_width_mm: float = 0.8) -> PaintAudit:
    v, f, s, part = _inputs(vertices, faces, face_slots, face_part)
    an = _analyze(v, f, s, part)
    thin: list[Region] = []
    slot_regions: dict[int, list[bool]] = {}
    for lab, idx in an.region_faces.items():
        slot = an.region_slot[lab]
        width = an.region_width[lab]
        is_thin = width < min_width_mm
        slot_regions.setdefault(slot, []).append(is_thin)
        if is_thin:
            w = an.area[idx]
            c = (an.cent[idx] * w[:, None]).sum(axis=0) / max(w.sum(), 1e-12)
            thin.append(Region(slot=slot, part=an.region_part[lab], faces=len(idx),
                               area_mm2=round(float(w.sum()), 4), width_mm=round(width, 3),
                               centroid=tuple(round(float(x), 3) for x in c)))
    only_thin = sorted(k for k, flags in slot_regions.items() if flags and all(flags))
    return PaintAudit(min_width_mm=min_width_mm, regions=len(an.region_faces), thin=thin,
                      slots_only_thin=only_thin)


def despeckle(vertices: np.ndarray, faces: np.ndarray, face_slots: np.ndarray,
              face_part: np.ndarray | None = None, min_width_mm: float = 0.8,
              min_area_mm2: float = 0.0, passes: int = 4) -> tuple[np.ndarray, int, int]:
    """Merge painted regions narrower than min_width_mm (or smaller than min_area_mm2) into
    the neighbouring filament they share the most boundary with. These are the details the
    slicer drops anyway, so merging them only removes filament changes.
    Returns (new face_slots, regions merged, faces changed)."""
    v, f, s, part = _inputs(vertices, faces, face_slots, face_part)
    s = s.copy()
    merged = changed = 0
    for _ in range(passes):
        an = _analyze(v, f, s, part)
        small = {lab for lab, idx in an.region_faces.items()
                 if an.region_width[lab] != float("inf")
                 and (an.region_width[lab] < min_width_mm
                      or float(an.area[idx].sum()) < min_area_mm2)}
        if not small:
            break
        la, lb = an.label[an.cross_a], an.label[an.cross_b]
        votes: dict[int, Counter] = {}
        for x, y, fx, fy in zip(la.tolist(), lb.tolist(), an.cross_a.tolist(),
                                an.cross_b.tolist(), strict=True):
            if x in small and part[fx] == part[fy]:
                votes.setdefault(x, Counter())[int(s[fy])] += 1
            if y in small and part[fx] == part[fy]:
                votes.setdefault(y, Counter())[int(s[fx])] += 1
        # Merge the smallest regions first so a speck inside a speck does not flip twice.
        pass_changed = 0
        for lab in sorted(small, key=lambda k: an.area[an.region_faces[k]].sum()):
            if lab not in votes:
                continue
            target = votes[lab].most_common(1)[0][0]
            idx = an.region_faces[lab]
            if target != an.region_slot[lab]:
                s[idx] = target
                merged += 1
                pass_changed += len(idx)
        changed += pass_changed
        if not pass_changed:
            break
    return s, merged, changed


def _group(keys: np.ndarray, values: np.ndarray):
    """Yield (key, values) for runs of equal keys in a sorted key array."""
    if len(keys) == 0:
        return
    cut = np.flatnonzero(np.diff(keys)) + 1
    bounds = np.concatenate([[0], cut, [len(keys)]])
    for i in range(len(bounds) - 1):
        lo, hi = bounds[i], bounds[i + 1]
        yield int(keys[lo]), values[lo:hi]
