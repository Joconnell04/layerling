import hashlib
import itertools
import time

import numpy as np
import pytest
from p2p_contracts.reports import SimParams, SimReport
from p2p_sim.color import srgb_to_lab
from p2p_sim.fixtures import box_mesh, grid_plane, sphere_mesh
from p2p_sim.simulate import (
    layer_presence,
    quantize_layers,
    simulate_print,
    simulate_print_detailed,
)

WHITE_BLACK = srgb_to_lab(np.array([[245, 245, 242], [20, 20, 22]], dtype=np.uint8))
TWO_MID = srgb_to_lab(np.array([[200, 60, 50], [60, 90, 200]], dtype=np.uint8))


def _stripe(m, width, axis=0, center=0.0):
    c = m.face_centroids[:, axis]
    return np.where(np.abs(c - center) < width / 2, 2, 1)


@pytest.mark.parametrize("vertical", [False, True])
def test_thin_stripe_merges_wide_stripe_survives(vertical):
    m = grid_plane(10, 10, 0.1, z=5.0, vertical=vertical)
    p = SimParams(nozzle_mm=0.4)
    thin = _stripe(m, 0.5)
    out, rep = simulate_print(m, thin, TWO_MID, p)
    assert (out[thin == 2] == 1).all()  # 0.5 mm < 2 x 0.4 mm: gone
    assert len(rep.merged) == 1
    mr = rep.merged[0]
    assert mr.region_id != mr.into_region_id and mr.reason in ("too_thin", "island")
    assert mr.area_mm2 == pytest.approx(m.face_areas[thin == 2].sum(), rel=1e-6)
    wide = _stripe(m, 1.2)
    out, rep = simulate_print(m, wide, TWO_MID, p)
    assert (out[wide == 2] == 2).mean() > 0.97  # 1.2 mm survives
    assert all(x.area_mm2 < 0.5 for x in rep.merged) or not rep.merged


def test_horizontal_band_on_a_wall_is_layer_resolved():
    """Layers resolve z: a 0.5 mm tall band (2.5 layers) prints; a 0.5 mm wide one does not."""
    m = grid_plane(10, 10, 0.1, z=0.0, vertical=True)
    band = _stripe(m, 0.5, axis=2, center=5.0)
    out, _ = simulate_print(m, band, TWO_MID)
    assert (out[band == 2] == 2).mean() > 0.9
    sliver = _stripe(m, 0.25, axis=2, center=5.0)
    out, _ = simulate_print(m, sliver, TWO_MID)
    assert (out[sliver == 2] == 2).mean() < 0.1


def test_bleed_flag_on_thin_white_over_black():
    thin = box_mesh((20, 20, 0.6), spacing=0.3, center=(0, 0, 0.3))
    slots = np.where(thin.face_normals[:, 2] < -0.5, 2, 1)  # black underside, white elsewhere
    res = simulate_print_detailed(thin, slots, WHITE_BLACK, face_regions=slots)
    flags = res.report.bleed_flags
    assert flags, "white over 0.6 mm to black must be flagged"
    f = flags[0]
    assert (f.region_id, f.over_region_id) == (1, 2)
    assert f.light_layers < 5 and f.area_mm2 > 300
    top = thin.face_normals[:, 2] > 0.5
    assert np.isfinite(res.face_light_layers[top]).mean() > 0.95
    # the same colors on a 6 mm slab: plenty of white behind the top. Only the white walls'
    # lowest millimetre, which sits on the black bottom skin, is flagged.
    thick = box_mesh((20, 20, 6.0), spacing=0.5, center=(0, 0, 3.0))
    slots = np.where(thick.face_normals[:, 2] < -0.5, 2, 1)
    res = simulate_print_detailed(thick, slots, WHITE_BLACK, face_regions=slots)
    top = thick.face_normals[:, 2] > 0.5
    assert not np.isfinite(res.face_light_layers[top]).any()
    # the black bottom paint penetrates 3 layers (0.6 mm) up the white walls ...
    z = thick.face_centroids[:, 2]
    walls = np.abs(thick.face_normals[:, 2]) < 0.1
    assert (res.face_slots[walls & (z < 0.5)] == 2).mean() > 0.95
    # ... and only the next millimetre of white above it is flagged
    flagged = np.isfinite(res.face_light_layers)
    assert flagged.any() and (z[flagged] < 0.6 + 1.0 + 0.2).all()
    # swap the colors: the black top is never flagged (dark over light is fine), while the
    # now-white underside is light over dark when seen from below
    inv = 3 - np.where(thin.face_normals[:, 2] < -0.5, 2, 1)
    res = simulate_print_detailed(thin, inv, WHITE_BLACK)
    top = thin.face_normals[:, 2] > 0.5
    assert not np.isfinite(res.face_light_layers[top]).any()
    assert np.isfinite(res.face_light_layers[thin.face_normals[:, 2] < -0.5]).mean() > 0.9


def _banded_box(n_bands):
    m = box_mesh((10, 10, 8), spacing=0.5, center=(0, 0, 4))
    z = m.face_centroids[:, 2]
    band = np.minimum((z / (8.0 / n_bands)).astype(int), n_bands - 1)
    return m, (band % 2) + 1


def test_purge_monotonic_in_swaps():
    swaps, purge = [], []
    for n in (1, 2, 4, 8):
        m, slots = _banded_box(n)
        _, rep = simulate_print(m, slots, WHITE_BLACK)
        swaps.append(rep.swaps_total)
        purge.append(rep.purge_g_est)
    assert swaps == sorted(swaps) and swaps[0] == 0 and swaps[-1] >= 7
    assert all(b > a for a, b in itertools.pairwise(purge))
    assert purge[0] == 0.0


def test_vertical_split_swaps_every_layer():
    m = box_mesh((10, 10, 4), spacing=0.5, center=(0, 0, 2))
    slots = np.where(m.face_centroids[:, 0] > 0, 2, 1)
    res = simulate_print_detailed(m, slots, WHITE_BLACK)
    assert res.report.layers == 20
    assert res.report.swaps_total >= 19  # at least one change per layer after the first
    assert res.report.swaps_max_per_layer == 1


def test_dark_to_light_weight_raises_purge():
    # one nozzle: with two, black and white sit on different nozzles and never flush
    m, slots = _banded_box(4)
    _, base = simulate_print(m, slots, WHITE_BLACK,
                             SimParams(dark_to_light_weight=2.0, nozzles=1))
    _, heavy = simulate_print(m, slots, WHITE_BLACK,
                              SimParams(dark_to_light_weight=4.0, nozzles=1))
    assert heavy.purge_g_est > base.purge_g_est


def test_quantize_plan_model_snaps_gentle_slope_boundary():
    m = sphere_mesh(10, 0.12)
    c = m.face_centroids
    p = SimParams()
    slots = np.where(c[:, 2] > 9.03, 2, 1)  # tilt ~25 deg there, between planes 8.9 and 9.1
    q = quantize_layers(m, slots, p, "plan")
    assert (q != slots).sum() > 100
    assert c[q == 2, 2].min() > 9.05  # the boundary moved up toward the 9.1 slice plane
    # a boundary running down the slope barely moves
    side = np.where(c[:, 0] > 0.3, 2, 1)
    assert (quantize_layers(m, side, p, "plan") != side).mean() < 0.002


def test_quantize_bambu_model_keeps_tops_and_snaps_walls():
    p = SimParams()
    m = sphere_mesh(10, 0.12)
    c = m.face_centroids
    top_cap = np.where(c[:, 2] > 9.03, 2, 1)  # painted top: projected, not snapped
    assert np.array_equal(quantize_layers(m, top_cap, p, "bambu"), top_cap)
    wall = grid_plane(10, 10, 0.05, vertical=True)
    z = wall.face_centroids[:, 2]
    band = np.where(z > 5.03, 2, 1)  # boundary 0.03 above the layer line at 5.0
    q = quantize_layers(wall, band, p, "bambu")
    # layer 25 spans 5.0 to 5.2 and its slice plane (5.1) is above the boundary: all color 2
    assert (q[(z > 5.0) & (z < 5.2)] == 2).all()
    assert (q[z < 4.99] == 1).all() and (q[z > 5.2] == 2).all()


def test_layer_presence_counts_layers():
    m = box_mesh((4, 4, 2), spacing=0.5, center=(0, 0, 1))
    pres = layer_presence(m, np.ones(m.n_faces, dtype=int), 1, SimParams())
    assert pres.shape == (10, 1) and pres.all()


def test_report_file_and_contract(tmp_path, fig50k):
    job = tmp_path / "job"
    out = job / "s09_paint" / "a1"
    t = time.time()
    sim_slots, rep = simulate_print(fig50k.mesh, fig50k.face_slots, fig50k.palette_labs,
                                    face_regions=fig50k.face_regions, out_dir=out, job_dir=job)
    assert time.time() - t < 5
    SimReport.model_validate(rep.model_dump())
    assert rep.face_filaments.path == "s09_paint/a1/sim_face_filaments.npy"
    data = (job / rep.face_filaments.path).read_bytes()
    assert hashlib.sha256(data).hexdigest() == rep.face_filaments.sha256
    saved = np.load(job / rep.face_filaments.path)
    assert saved.dtype == np.int16 and np.array_equal(saved, sim_slots)
    assert rep.layers == int(np.ceil(fig50k.mesh.bounds[1, 2] / 0.2 - 1e-9))
    assert rep.model_g_est > 5 and rep.purge_g_est > 0


@pytest.mark.parametrize("which", ["fig50k", "fig200k"])
def test_figurine_sim_catches_the_planted_problems(request, which):
    fx = request.getfixturevalue(which)
    t = time.time()
    res = simulate_print_detailed(fx.mesh, fx.face_slots, fx.palette_labs,
                                  face_regions=fx.face_regions)
    assert time.time() - t < 6
    rep = res.report
    names = {v: k for k, v in fx.labels.items()}
    tag = [f for f in rep.bleed_flags if f.region_id == names["s2.tag_front"]]
    assert tag and tag[0].over_region_id == names["s2.tag_back"] and tag[0].light_layers <= 3
    # the 1.6 mm back stripes keep their area, less a triangle of jagged edge (at 50k a stripe is
    # only ~3 triangles wide); the 0.5 mm forehead stripes lose most of theirs
    area = fx.mesh.face_areas
    sel = fx.face_regions == names["s1.stripe_2"]
    keep = 0.9 if which == "fig200k" else 0.7
    assert area[sel & (res.face_slots == res.face_slots_in)].sum() > keep * area[sel].sum()
    for name in ("s1.stripe_thin_1", "s1.stripe_thin_2"):
        sel = fx.face_regions == names[name]
        assert area[sel & (res.face_slots != res.face_slots_in)].sum() > 0.4 * area[sel].sum()


def test_rejects_unresolved_slots(ico):
    with pytest.raises(ValueError):
        simulate_print(ico, np.zeros(ico.n_faces, dtype=int), WHITE_BLACK)
