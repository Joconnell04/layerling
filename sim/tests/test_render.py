import time

import numpy as np
from p2p_sim.render import (
    BG,
    render,
    render_crops,
    render_sheet,
    render_views,
)
from PIL import Image


def test_flat_colors_are_exact(fig50k):
    m = fig50k.mesh
    res = render(m, "front", "flat", face_colors=fig50k.face_colors, size_px=512)
    img = res.image.reshape(-1, 3)
    allowed = {tuple(c) for c in fig50k.palette_rgb} | {BG}
    got = {tuple(c) for c in np.unique(img, axis=0)}
    assert got <= allowed
    # every covered pixel shows exactly its face's color
    fid = res.face_id.reshape(-1)
    cov = fid >= 0
    assert np.array_equal(img[cov], fig50k.face_colors[fid[cov]])


def test_shaded_and_texture_are_deterministic(fig50k):
    m = fig50k.mesh
    a = render(m, "front_3q", "shaded", face_colors=fig50k.face_colors, size_px=256).image
    b = render(m, "front_3q", "shaded", face_colors=fig50k.face_colors, size_px=256).image
    assert np.array_equal(a, b)
    t = render(m, "front_3q", "texture", size_px=256)
    # texture render (unlit part) tracks the ground-truth paint: compare interior pixels
    flat = render(m, "front_3q", "flat", face_colors=fig50k.face_colors, size_px=256)
    cov = flat.face_id >= 0
    diff = np.abs(t.image.astype(int) - flat.image.astype(int)).max(axis=2)[cov]
    assert np.median(diff) < 60  # lighting changes brightness, hue stays


def test_overlay_labels_sit_inside_their_region(fig50k):
    res = render(fig50k.mesh, "front", "overlay", face_colors=fig50k.face_colors,
                 face_regions=fig50k.face_regions, size_px=768)
    assert len(res.labels) >= 8
    for rid, (x, y) in res.labels.items():
        f = res.face_id[y, x]
        assert f >= 0 and fig50k.face_regions[f] == rid
    # boundary lines are drawn
    dark = (res.image.astype(int).sum(axis=2) < 60).mean()
    assert dark > 0.002


def test_crops_frame_the_region(fig50k):
    rid = 9  # s1.eye_left, small
    out = render_crops(fig50k.mesh, fig50k.face_regions, [rid], fig50k.face_colors, size_px=256)
    r = out[0]
    assert r.region_id == rid and r.camera.width == 256
    vis = np.isin(r.face_id, np.nonzero(fig50k.face_regions == rid)[0])
    assert vis.mean() > 0.02  # the small region fills a real part of the crop
    ys, xs = np.nonzero(vis)
    assert 40 < xs.mean() < 216 and 40 < ys.mean() < 216


def test_render_views_and_sheet(tmp_path, fig50k):
    res = render_views(fig50k.mesh, ["flat", "shaded"], ["front", "back"],
                       fig50k.face_colors, size_px=128)
    assert [(r.kind, r.view) for r in res] == [
        ("flat", "front"), ("shaded", "front"), ("flat", "back"), ("shaded", "back")]
    p = render_sheet(fig50k.mesh, fig50k.face_colors, tmp_path / "s.png", cell_px=128)
    im = Image.open(p)
    assert im.size == (3 * 128 + 4 * 10, 2 * (128 + 26) + 3 * 10)


def test_render_speed_at_200k(fig200k):
    m = fig200k.mesh
    render(m, "front", "shaded", face_colors=fig200k.face_colors)  # warm caches and GL
    for kind in ("flat", "shaded", "texture", "overlay"):
        t = time.time()
        render(m, "front_3q", kind, face_colors=fig200k.face_colors,
               face_regions=fig200k.face_regions, size_px=1024)
        assert time.time() - t < 1.0, kind
