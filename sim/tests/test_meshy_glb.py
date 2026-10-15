"""Checks against a real Meshy GLB (skipped when the fixture is not on this machine)."""

import numpy as np
from p2p_sim.demo import texture_regions
from p2p_sim.mesh import load_glb
from p2p_sim.regions import region_geometry
from p2p_sim.render import render
from p2p_sim.simulate import simulate_print_detailed
from PIL import Image


def _crop_small(img: np.ndarray, mask: np.ndarray, n: int = 48) -> np.ndarray:
    ys, xs = np.nonzero(mask)
    sub = img[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1]
    return np.asarray(Image.fromarray(sub).resize((n, n), Image.BILINEAR), dtype=float)


def test_meshy_glb_front_matches_meshy_thumbnail(meshy_glb):
    m = load_glb(meshy_glb, target_size_mm=60)
    assert m.texture is not None and m.uv is not None
    lo, hi = m.bounds
    assert np.isclose(lo[2], 0) and np.isclose(hi[2] - lo[2], 60, atol=0.01)  # standing up
    thumb = Image.open(meshy_glb.parent / "thumb_front.png").convert("RGBA")
    t = np.asarray(thumb)
    tmask = t[..., 3] > 0 if (t[..., 3] < 255).any() else (t[..., :3].min(axis=2) < 245)
    tsmall = _crop_small(t[..., :3], tmask)
    scores = {}
    for view in ("front", "back", "left", "right"):
        r = render(m, view, "texture", size_px=512)
        scores[view] = np.abs(_crop_small(r.image, r.face_id >= 0) - tsmall).mean()
    assert min(scores, key=scores.get) == "front", scores


def test_meshy_glb_tolerates_open_non_manifold_mesh(meshy_glb):
    m = load_glb(meshy_glb, target_size_mm=60)
    assert not m.is_watertight
    regions, cl, centers = texture_regions(m, k=4)
    geo = region_geometry(m, regions)
    assert sum(g.n_faces for g in geo.values()) == m.n_faces
    res = simulate_print_detailed(m, cl + 1, centers, face_regions=regions)
    assert res.report.layers == 300
    assert res.report.model_g_est > 1
