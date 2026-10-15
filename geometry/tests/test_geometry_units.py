import numpy as np
import pytest
from p2p_contracts.job import TextSpec
from p2p_geometry.addons import LoopSpec, PocketSpec, keyring_loop, magnet_pocket, plate
from p2p_geometry.color import ciede2000, find, hex_to_lab, highest_contrast
from p2p_geometry.meshio import from_manifold
from p2p_geometry.nameplate import fit_text, nameplate, snap_relief
from p2p_geometry.repair import repair_figure
from p2p_geometry.solid import voxelize
from p2p_geometry.text import layout_text, min_stroke_mm, text_mesh
from p2p_geometry.types import GeometrySettings, Part


def test_ciede2000_reference_pairs():
    # Sharma, Wu, Dalal (2005) test data, pairs 1 and 7.
    assert ciede2000((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485)) == pytest.approx(2.0425, abs=1e-4)
    assert ciede2000((50.0, 0.0, 0.0), (50.0, -1.0, 2.0)) == pytest.approx(2.3669, abs=1e-4)
    assert hex_to_lab("#FFFFFF") == pytest.approx((100.0, 0.0, 0.0), abs=0.01)


def test_text_contrast_prefers_same_material(library):
    black = find(library, "generic-pla-black")
    white = find(library, "generic-pla-white")
    assert highest_contrast(library, black)[0].id == "generic-pla-white"
    best, de, same = highest_contrast(library, white)
    assert best.id == "generic-pla-black" and same and de > 80
    # Never PETG on a PLA plate, even when it would contrast more.
    red = find(library, "bambu-pla-basic-red")
    assert highest_contrast(library, red)[0].material == "PLA"


def test_text_stroke_and_mesh():
    lay = layout_text("BISCUIT", 5.0)
    assert min_stroke_mm(lay.section) >= 0.8
    m, lay = text_mesh("BISCUIT", 10.0, 1.0, sink_mm=0.2)
    v, f = from_manifold(m)
    assert Part("t", "text", v, f, None).watertight
    assert m.bounding_box()[5] == pytest.approx(1.0)


def test_relief_snaps_to_layers():
    gs = GeometrySettings()
    w: list[str] = []
    assert snap_relief(0.5, gs, w) == pytest.approx(0.8)
    assert snap_relief(1.5, gs, w) == pytest.approx(1.2)
    assert snap_relief(1.0, gs, w) == pytest.approx(1.0)
    assert len(w) == 2


def test_too_long_name_suggests_a_wider_plate(library):
    gs = GeometrySettings()
    spec = TextSpec(text="Wolfeschlegelsteinhausen")  # 24 letters, nowhere to wrap
    fit = fit_text(spec, 40.0, 20.0, gs)
    assert fit.cap >= gs.min_cap_height_mm and not fit.fits and fit.lines == (spec.text,)
    res = nameplate(spec, 60.0, 30.0, find(library, "generic-pla-black"), library, gs)
    assert res.width_mm > 60.0
    assert any("suggest a" in w and "mm plate" in w for w in res.warnings)
    assert res.info["text"]["cap_height_mm"] >= 5.0
    assert res.info["text"]["thin_fraction"] <= 0.03


@pytest.mark.parametrize(("name", "n_lines"), [("Bartholomew", 1), ("Arthur, Maud & Biscuit", 2)])
@pytest.mark.parametrize("font", ["rounded-bold", "friendly"])
def test_long_names_fit_the_120_mm_plate(library, name, n_lines, font):
    """Same text region as the beagle nameplate: 72 x 46 mm right of the figure."""
    spec = TextSpec(text=name, font=font)
    res = nameplate(spec, 120.0, 56.0, find(library, "generic-pla-black"), library,
                    text_region=(-17.0, 55.0))
    t = res.info["text"]
    assert res.width_mm == 120.0 and not res.warnings, res.warnings
    assert len(t["lines"]) == n_lines and t["cap_height_mm"] >= 5.0
    assert t["thin_fraction"] <= 0.03


def test_kerning_tightens_pairs():
    from p2p_geometry.text import shape_line

    for key in ("rounded-bold", "friendly"):
        together = shape_line("AV", key)[-1][1]
        apart = shape_line("A", key)[-1][1] + shape_line("V", key)[-1][1]
        assert together < apart - 20, key  # font units


def test_two_fonts_are_bundled():
    a = layout_text("Biscuit", 10.0, "rounded-bold")
    b = layout_text("Biscuit", 10.0, "friendly")
    assert a.font == "rounded-bold" and b.font == "friendly"
    assert abs(a.width_mm - b.width_mm) > 1.0
    from p2p_geometry.text import FONT_DIR

    assert (FONT_DIR / "OFL-Nunito.txt").exists() and (FONT_DIR / "OFL-Fredoka.txt").exists()


def test_union_center_of_mass_counts_overlaps_once():
    import trimesh

    from p2p_geometry.product import union_center_of_mass

    a = trimesh.creation.box(bounds=[[0, 0, 0], [10, 10, 10]])
    b = trimesh.creation.box(bounds=[[0, 0, 0], [2, 10, 10]])  # fully inside a
    com = union_center_of_mass([(a.vertices, a.faces), (b.vertices, b.faces)])
    assert com[0] == pytest.approx(5.0, abs=1e-6)  # double counting would give 4.33


def test_engraved_name_is_a_flush_inlay(library):
    spec = TextSpec(text="ROSIE", style="engraved", relief_mm=1.0)
    res = nameplate(spec, 80.0, 30.0, find(library, "generic-pla-white"), library)
    top = res.plate.bounding_box()[5]
    assert res.text.bounding_box()[5] == pytest.approx(top, abs=1e-6)
    assert res.plate.volume() < plate(80.0, 30.0).volume()
    assert res.text_filament_id == "generic-pla-black"


def test_repair_keeps_face_order_and_closes_the_mesh(figure):
    r = repair_figure(figure.vertices, figure.faces)
    rep = r.report
    assert rep.removed_faces == {"fin": [figure.fin_face]}
    assert rep.holes_filled == 1 and rep.watertight
    assert rep.nonmanifold_edges_before == 1 and rep.nonmanifold_edges_after == 0
    kept = r.face_source[r.face_source >= 0]
    assert np.array_equal(kept, np.arange(figure.fin_face))  # order preserved, fin dropped
    assert (r.face_source == -1).sum() == rep.hole_faces_added
    colors = r.remap_face_values(figure.face_colors, fill=0)
    assert np.array_equal(colors[: figure.fin_face], figure.face_colors[: figure.fin_face])


def test_voxelize_sphere_volume():
    import trimesh

    s = trimesh.creation.icosphere(subdivisions=4, radius=10)
    sol = voxelize(s.vertices, s.faces, 0.4)
    assert sol.volume_mm3 == pytest.approx(4 / 3 * np.pi * 1000, rel=0.03)
    assert sol.odd_columns == 0
    assert sol.thickness_at(np.array([[0.0, 0.0, 0.0]]))[0] == pytest.approx(20, abs=1.0)


def test_loop_is_a_ring_with_a_clear_hole():
    spec = LoopSpec()
    m, info = keyring_loop(spec, (0.0, 0.0), 10.0, 9.0)
    assert m.genus() == 1
    v, f = from_manifold(m)
    sol = voxelize(v, f, 0.1)
    hc = info["hole_center"]
    inside = sol.contains(np.array([[0, hc[1], 2.0], [0, info["stem_bottom"] + 1, 2.0]]))
    assert list(inside) == [False, True]
    assert info["embed_min_mm"] >= 1.5


def test_magnet_pocket_is_open_on_the_back():
    slab = plate(40, 40, 4.0)
    spec = PocketSpec()
    cut, _ = magnet_pocket(slab, (0.0, 0.0), spec)
    assert slab.volume() - cut.volume() == pytest.approx(np.pi * (spec.d / 2) ** 2 * spec.depth, rel=0.05)
    v, f = from_manifold(cut)
    sol = voxelize(v, f, 0.1)
    pts = np.array([[0, 0, 0.5], [0, 0, 2.0], [0, 0, 3.5], [15, 0, 0.5]])
    assert list(sol.contains(pts)) == [False, False, True, True]
