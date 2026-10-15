"""The hanging proof view: the product turned so its center of mass hangs straight below the
loop's pivot, with the tilt printed on it."""

from __future__ import annotations

import numpy as np
from p2p_sim import Mesh
from p2p_sim.proof import hang_rotation, hanging_view
from PIL import Image


def _box(lo, hi):
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], float)
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                  [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    return v, f


def test_hang_rotation_puts_the_pivot_straight_above_the_center_of_mass():
    for d in ([0.0, -0.5, 1.0], [0.3, 0.2, 0.9], [0.0, 0.0, 1.0], [1.0, 0.0, 0.0]):
        R = hang_rotation(np.array(d))
        out = R @ (np.array(d) / np.linalg.norm(d))
        assert np.allclose(out, [0, 0, 1], atol=1e-9)
        assert np.allclose(R @ R.T, np.eye(3), atol=1e-9) and np.linalg.det(R) > 0


def test_hanging_view_writes_paint_and_print_with_the_tilt(tmp_path):
    # a long body (the COM mid-body) with the pivot over the front end: it hangs nose down
    v, f = _box((-5, -20, 0), (5, 20, 10))
    mesh = Mesh(vertices=v, faces=f)
    rgb = np.tile(np.array([[200, 180, 160]], np.uint8), (len(f), 1))
    pivot, com = [0.0, -18.0, 16.0], [0.0, 0.0, 5.0]
    tilt = float(np.degrees(np.arccos((16 - 5) / np.linalg.norm([0, -18, 11]))))
    hang = {"tilt_deg": tilt, "pivot_mm": pivot, "com_mm": com, "area": "head"}
    rows = hanging_view(mesh, "upright", hang, tmp_path, paint_rgb=rgb, size_px=256)
    assert [r["view"] for r in rows] == ["hanging"] and rows[0]["kind"] == "paint"
    assert rows[0]["tilt_deg"] == round(tilt, 1)
    im = np.asarray(Image.open(rows[0]["path"]))
    assert im.shape[:2] == (256, 256)
    # the body hangs nose down: the painted pixels are taller than wide in this side view
    px = im[..., :3].astype(int)
    body = (px[..., 0] - px[..., 2] > 15) & (px[..., 0] >= px[..., 1])  # warm: not the lines
    ys, xs = np.nonzero(body)
    assert (ys.max() - ys.min()) > (xs.max() - xs.min())


def test_hung_center_of_mass_is_straight_below_the_pivot():
    pivot, com = np.array([0.0, -18.0, 16.0]), np.array([0.0, 0.0, 5.0])
    R = hang_rotation(pivot - com)
    com_h = pivot + R @ (com - pivot)  # what hanging_view draws
    assert np.allclose(com_h[:2], pivot[:2], atol=1e-9) and com_h[2] < pivot[2]
