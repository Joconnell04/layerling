"""Variable layer height: coarse layers where a slice holds one filament, fine where colours meet.

Time scales with the number of layers; purge scales with filament changes, which happen on
layers that hold more than one filament. Single-filament stretches (a base, a plain body) can
use thick layers without touching colour boundaries. Written as Bambu's layer height profile.
"""

from __future__ import annotations

import numpy as np


def adaptive_profile(vertices: np.ndarray, faces: np.ndarray, face_slots: np.ndarray,
                     fine: float = 0.2, coarse: float = 0.28, bin_mm: float = 0.2,
                     margin_mm: float = 0.6, min_run_mm: float = 2.0
                     ) -> list[tuple[float, float]]:
    """[(z, layer height)] pairs over the object's height (z measured from its lowest point).

    A bin is "mixed" when faces of more than one filament cross it; mixed bins and a margin
    around them stay at `fine`, single-filament runs longer than min_run_mm go `coarse`."""
    v = np.asarray(vertices, dtype=np.float64)
    f = np.asarray(faces, dtype=np.int64)
    s = np.asarray(face_slots, dtype=np.int64)
    z = v[:, 2] - v[:, 2].min()
    height = float(z.max())
    n = max(1, int(np.ceil(height / bin_mm)))
    zmin = z[f].min(axis=1)
    zmax = z[f].max(axis=1)
    lo = np.clip((zmin / bin_mm).astype(int), 0, n - 1)
    hi = np.clip((zmax / bin_mm).astype(int), 0, n - 1)
    slots_in = [set() for _ in range(n)]
    for a, b, sl in zip(lo.tolist(), hi.tolist(), s.tolist(), strict=True):
        for i in range(a, b + 1):
            slots_in[i].add(sl)
    mixed = np.array([len(x) > 1 for x in slots_in])
    grow = int(np.ceil(margin_mm / bin_mm))
    fine_mask = mixed.copy()
    for k in range(1, grow + 1):
        fine_mask[:-k] |= mixed[k:]
        fine_mask[k:] |= mixed[:-k]
    # Bambu drops a profile whose first height differs from the first layer height.
    fine_mask[: max(1, int(np.ceil(1.0 / bin_mm)))] = True
    # Short coarse runs are not worth the height changes.
    run = int(np.ceil(min_run_mm / bin_mm))
    i = 0
    while i < n:
        if fine_mask[i]:
            i += 1
            continue
        j = i
        while j < n and not fine_mask[j]:
            j += 1
        if j - i < run:
            fine_mask[i:j] = True
        i = j
    prof: list[tuple[float, float]] = []
    for i in range(n):
        h = fine if fine_mask[i] else coarse
        if not prof or prof[-1][1] != h:
            if prof:
                prof.append((round(i * bin_mm, 4), prof[-1][1]))
            prof.append((round(i * bin_mm, 4), h))
    prof.append((round(height, 4), prof[-1][1]))
    return prof
