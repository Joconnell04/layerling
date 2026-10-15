"""End to end without a renderer: SAM 3 masks on a flat image, back-projected onto a grid mesh
whose face-ID buffer maps each pixel block to one cell. Prints the regions it finds.

    SAM3_BACKEND=local uv run --package p2p-sam3 --extra local \
        python sam3/scripts/chain_demo.py path/to/flat.png
"""

from __future__ import annotations

import sys
import time

import numpy as np
from p2p_sam3.backproject import ViewLabels, backproject, masks_from_result
from p2p_sam3.client import get_segmenter
from PIL import Image

CELL_PX = 4


def grid_for_image(h: int, w: int):
    """Grid mesh in the XZ plane (1 mm cells, normals -Y) and a front face-ID buffer."""
    ny, nx = h // CELL_PX, w // CELL_PX
    xs, zs = np.meshgrid(np.arange(nx + 1, dtype=float), np.arange(ny + 1, dtype=float))
    verts = np.stack([xs.ravel(), np.zeros(xs.size), zs.ravel()], axis=1)
    i, j = np.meshgrid(np.arange(nx), np.arange(ny))
    i, j = i.ravel(), j.ravel()
    v = j * (nx + 1) + i
    f0 = np.stack([v, v + 1, v + nx + 2], axis=1)
    f1 = np.stack([v, v + nx + 2, v + nx + 1], axis=1)
    faces = np.stack([f0, f1], axis=1).reshape(-1, 3)  # cell c -> faces 2c, 2c + 1
    rows, cols = np.mgrid[: ny * CELL_PX, : nx * CELL_PX]
    cell = (ny - 1 - rows // CELL_PX) * nx + cols // CELL_PX  # image row 0 is the top (max z)
    fid = (2 * cell + ((rows % CELL_PX) >= CELL_PX // 2)).astype(np.int32)
    return verts, faces, fid


def main() -> None:
    path = sys.argv[1]
    rgb = np.asarray(Image.open(path).convert("RGB"))
    h, w = (s - s % CELL_PX for s in rgb.shape[:2])
    rgb = np.ascontiguousarray(rgb[:h, :w])
    seg = get_segmenter()
    t = time.perf_counter()
    res = seg.segment([rgb], ["eye", "nose", "ear", "head"])
    print(f"{res.backend}: segment {time.perf_counter() - t:.2f} s")
    verts, faces, fid = grid_for_image(h, w)
    t = time.perf_counter()
    bp = backproject(
        verts, faces, [ViewLabels(fid, masks_from_result(res, 0), view_dir=(0, -1, 0))]
    )
    print(f"backproject {time.perf_counter() - t:.2f} s, {len(faces)} faces, {bp.stats}")
    for r in bp.regions:
        print(
            f"  {r.id:2d} {r.label:12s} faces {r.n_faces:5d} area {r.area_mm2:8.1f} mm2 "
            f"centroid x {r.centroid_mm[0]:6.1f} z {r.centroid_mm[2]:6.1f} conf {r.confidence}"
        )


if __name__ == "__main__":
    main()
