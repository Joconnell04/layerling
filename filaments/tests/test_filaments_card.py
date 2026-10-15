import numpy as np
import pytest
from p2p_filaments.calibrate import find_card
from p2p_filaments.card import build_parts, plan_card, read_card_sidecar, write_card
from p2p_filaments.layout import (
    DIGIT_RAISE,
    MARKER_IDS,
    STEPS_MM,
    TOP_Z,
    CardLayout,
    marker_bits,
)
from p2p_filaments.library import DEFAULT_LIBRARY, ams_palette, load
from p2p_filaments.render import CardColours, render_card, to_display
from p2p_threemf import read_3mf_full


@pytest.fixture(scope="module")
def palette():
    return ams_palette(load(DEFAULT_LIBRARY))


@pytest.fixture(scope="module")
def plan(palette):
    return plan_card(palette)


def _top_slot(parts, x, y):
    """Filament slot and height of the highest top face above (x, y)."""
    best = (None, -1.0)
    for _, slot, v, f in parts:
        tri = v[f]
        flat = np.ptp(tri[:, :, 2], axis=1) < 1e-9
        tri = tri[flat]
        a, b, c = tri[:, 0, :2], tri[:, 1, :2], tri[:, 2, :2]
        d = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
        ok = np.abs(d) > 1e-12
        dd = np.where(ok, d, 1)
        l1 = ((b[:, 1] - c[:, 1]) * (x - c[:, 0]) + (c[:, 0] - b[:, 0]) * (y - c[:, 1])) / dd
        l2 = ((c[:, 1] - a[:, 1]) * (x - c[:, 0]) + (a[:, 0] - c[:, 0]) * (y - c[:, 1])) / dd
        inside = ok & (l1 >= -1e-9) & (l2 >= -1e-9) & (1 - l1 - l2 >= -1e-9)
        if inside.any():
            z = tri[inside][:, 0, 2].max()
            if z > best[1]:
                best = (slot, z)
    return best


def test_card_size_and_bands(plan):
    lay = plan.layout
    assert lay.n == 5
    assert (lay.width, lay.height) == (75.0, 49.0)
    assert plan.black_slot == 1 and plan.white_slot == 5
    assert lay.labels == ("1", "2", "3", "4", "5")


def test_samples_stay_inside_their_regions(plan):
    lay = plan.layout
    for i in range(lay.n):
        tile, digits = lay.tile(i), lay.digit_box(i)
        for r in lay.tile_samples(i):
            assert tile.x0 < r.x0 < r.x1 < tile.x1 and tile.y0 < r.y0 < r.y1 < tile.y1
            overlap_x = min(r.x1, digits.x1) - max(r.x0, digits.x0)
            overlap_y = min(r.y1, digits.y1) - max(r.y0, digits.y0)
            assert overlap_x <= 0 or overlap_y <= 0
        assert sum(r.area for r in lay.tile_samples(i)) > 90
        for base in ("black", "white"):
            for j in range(len(STEPS_MM)):
                s, st = lay.step_sample(i, base, j), lay.step(i, base, j)
                assert st.x0 < s.x0 < s.x1 < st.x1 and s.area > 10


def test_mesh_top_view_matches_layout(plan):
    """Point-sample the printed top surface: tiles, wedge steps, marker cells, numbers."""
    parts = build_parts(plan)
    lay = plan.layout
    for i in range(lay.n):
        cx, cy = lay.tile_samples(i)[0].centre
        assert _top_slot(parts, cx, cy) == (i + 1, TOP_Z)
        for base in ("black", "white"):
            for j in range(len(STEPS_MM)):
                cx, cy = lay.step_sample(i, base, j).centre
                assert _top_slot(parts, cx, cy) == (i + 1, TOP_Z)
        r = lay.digit_runs(i)[0]
        slot, z = _top_slot(parts, *r.centre)
        assert slot == plan.number_slot(i) and z == pytest.approx(TOP_Z + DIGIT_RAISE)
    for mid in MARKER_IDS:
        for r, black in lay.marker_cells(mid):
            slot, _ = _top_slot(parts, *r.centre)
            assert slot == (plan.black_slot if black else plan.white_slot)


def _box_at(v, x, y):
    """(zmin, zmax) of the box (8 vertices each, as built) whose footprint holds (x, y)."""
    boxes = v.reshape(-1, 8, 3)
    lo, hi = boxes.min(axis=1), boxes.max(axis=1)
    hit = (lo[:, 0] < x) & (x < hi[:, 0]) & (lo[:, 1] < y) & (y < hi[:, 1])
    assert hit.sum() == 1
    return lo[hit][0, 2], hi[hit][0, 2]


def test_wedge_step_thickness_over_base(plan):
    """Under each step the colour is exactly t thick and the base fills the rest."""
    parts = {name: (slot, v) for name, slot, v, _ in build_parts(plan)}
    lay = plan.layout
    for j, t in enumerate(STEPS_MM):
        for base, base_part in (("black", "base_black"), ("white", "swatch_card")):
            x, y = lay.step(2, base, j).centre
            assert _box_at(parts["wedge_3"][1], x, y) == pytest.approx((TOP_Z - t, TOP_Z))
            assert _box_at(parts[base_part][1], x, y) == pytest.approx((0.0, TOP_Z - t))


def test_markers_are_distinct_and_rotation_unique():
    bits = [marker_bits(m) for m in MARKER_IDS]
    for a in range(4):
        for b in range(4):
            for k in range(4):
                if a != b or k:
                    assert not np.array_equal(bits[a], np.rot90(bits[b], k))


def test_write_card_reads_back(tmp_path, palette):
    out = tmp_path / "card.3mf"
    res = write_card(palette, out)
    assert res["slots_used"] == [1, 2, 3, 4, 5] and res["expected_slots"] == 5
    m = read_3mf_full(out)
    assert set(np.unique(m.face_slots)) == {1, 2, 3, 4, 5}
    names = [p["name"] for p in m.sidecar["parts"]]
    assert names[0] == "swatch_card" and "tile_4" in names and "wedge_4" in names
    lo, hi = m.vertices.min(axis=0), m.vertices.max(axis=0)
    assert np.allclose(hi - lo, [75.02, 49.02, TOP_Z + DIGIT_RAISE], atol=0.01)
    side = read_card_sidecar(out)
    assert [c["filament_id"] for c in side["columns"]] == [f.id for f in palette]
    assert side["layout"]["steps_mm"] == list(STEPS_MM)
    cfg = m.project_config
    assert cfg["sparse_infill_density"] == "100%"
    # deterministic bytes
    assert write_card(palette, tmp_path / "again.3mf")["sha256"] == res["sha256"]


def test_rendered_card_markers_are_found(plan):
    """The renderer and the marker geometry agree with OpenCV's ArUco detector."""
    lay = CardLayout(plan.layout.labels)
    tiles = np.array([[0.02] * 3, [0.1, 0.05, 0.02], [0.4, 0.03, 0.03], [0.02, 0.1, 0.2],
                      [0.8] * 3])
    colours = CardColours.from_model(tiles, 1, 5, [5, 5, 1, 5, 1], np.full((5, 3), 0.3))
    s = 12.0
    img = to_display(render_card(lay, colours, s))
    pad = np.full((img.shape[0] + 80, img.shape[1] + 80, 3), 60, np.uint8)
    pad[40:-40, 40:-40] = img
    found = find_card(pad, lay)
    assert found is not None and found.markers == [0, 1, 2, 3]
    assert found.reprojection_px < 1.0
    assert found.px_per_mm == pytest.approx(s, rel=0.01)
