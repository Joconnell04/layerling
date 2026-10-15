"""Fidelity rules for stage 6 (docs/runs/fidelity/DEEP-DIVE.md): the product form is the job's
choice, a 3D keychain hangs straight, flat charms are true reliefs, plates take the shape the
customer picked, and add-on colors come from the product, not the darkest spool."""

import numpy as np
import pytest
from geo_fixtures import hard_figure, standing_figure
from p2p_contracts.filaments import Filament, FilamentLibrary
from p2p_contracts.job import AddOn, AddOnKind, ProductType, TextSpec
from p2p_geometry import AddonColorInputs, build_product, build_product_geometry
from p2p_geometry import addons as ad
from p2p_geometry.product import loop_area, normalize_form, resolve_form


@pytest.fixture(scope="module")
def standing():
    return standing_figure()


@pytest.fixture(scope="module")
def sitting():
    return hard_figure()


def _keyring(**params):
    return [AddOn(kind=AddOnKind.KEYRING_LOOP, params={"position": "top", **params})]


def test_form_aliases_and_defaults():
    assert normalize_form("3D figure") == "figure" and normalize_form("flat-charm") == "flat"
    assert normalize_form("nonsense") is None
    assert resolve_form(ProductType.KEYCHAIN, None, {}) == ("figure", "product default")
    assert resolve_form(ProductType.MAGNET, None, {}) == ("flat", "product default")
    legacy = {AddOnKind.KEYRING_LOOP: AddOn(kind=AddOnKind.KEYRING_LOOP,
                                            params={"orientation": "flat"})}
    assert resolve_form(ProductType.KEYCHAIN, None, legacy) == ("flat", "add-on orientation")
    assert resolve_form(ProductType.KEYCHAIN, "figure", legacy) == ("figure", "job")


def test_keychain_keeps_the_3d_figure_unless_the_job_chose_flat(standing, library):
    geo = build_product_geometry(standing.vertices, standing.faces, "keychain", _keyring(), 45.0,
                                 library, None)
    assert geo.orientation == "upright" and geo.supports == "tree"
    assert geo.metrics["relief_scale"] == 1.0 and geo.metrics["form"] == "figure"
    assert "relief_back" not in geo.figure.report.removed_faces
    assert not any(p.role == "back" for p in geo.parts)
    ring = geo.metrics["loop"]
    assert ring["thickness_mm"] == 5.0  # the heavier upright ring
    assert geo.size_mm.max() == pytest.approx(45.0, abs=0.5)


def test_loop_area_follows_the_intake_answer():
    def area(**params):
        return loop_area(AddOn(kind=AddOnKind.KEYRING_LOOP, params=params))

    assert area(position="top") == "head" and area(position="head") == "head"
    assert area(position="back") == "back"
    assert area(position="left") == "side" and area(position="side") == "side"
    assert area(position="any") == "any" and area() == "any" and area(position="") == "any"
    assert area(position="back", hang="head") == "head"  # legacy hang: head


def test_top_of_the_head_keeps_the_loop_on_the_head_and_reports_the_tilt(standing, library):
    """The customer answered "Top of the head" (position top): the loop stays on the head even
    though the head is forward of the center of mass, at the least tilted head seat, and the
    report says how it hangs and what 'any' would give."""
    geo = build_product_geometry(standing.vertices, standing.faces, "keychain", _keyring(), 45.0,
                                 library, None)
    loop, hang = geo.metrics["loop"], geo.metrics["hang"]
    bal = loop["balance"]
    assert loop["area"] == "head" and bal["where"] == "head" and bal["on_head"]
    assert bal["tilt_deg"] <= bal["head_best_tilt_deg"] + 1.0  # least tilted head seat
    assert geo.metrics["hang_tilt_deg"] > 20  # a standing dog hangs nose down from its head
    assert hang["area"] == "head" and hang["tilt_deg"] == pytest.approx(
        geo.metrics["hang_tilt_deg"], abs=0.01)
    assert len(hang["pivot_mm"]) == 3 and len(hang["com_mm"]) == 3
    assert hang["best_anywhere_deg"] <= ad.STRAIGHT_TILT_DEG
    assert any("Wherever it hangs best" in n for n in geo.notes)
    assert not any("Hangs tilted" in w for w in geo.warnings)  # the customer's choice: a note
    assert loop["local_thickness_mm"] >= 4.0  # sturdy seat


def test_any_lets_the_loop_move_where_it_hangs_straight(standing, library):
    geo = build_product_geometry(standing.vertices, standing.faces, "keychain",
                                 _keyring(position="any"), 45.0, library, None)
    bal = geo.metrics["loop"]["balance"]
    assert geo.metrics["loop"]["area"] == "any"
    assert geo.metrics["hang_tilt_deg"] <= ad.STRAIGHT_TILT_DEG
    # the head is forward of the center of mass: on the head it would hang tilted
    assert bal["head_best_tilt_deg"] > 20 and bal["where"] == "balance"
    assert any("would hang" in n for n in geo.notes)
    old = build_product_geometry(standing.vertices, standing.faces, "keychain",
                                 _keyring(hang="highest"), 45.0, library, None)
    assert "balance" not in old.metrics["loop"]


def test_on_the_back_sits_on_the_back_over_the_center_of_mass(standing, library):
    geo = build_product_geometry(standing.vertices, standing.faces, "keychain",
                                 _keyring(position="back"), 45.0, library, None)
    loop = geo.metrics["loop"]
    assert loop["area"] == "back" and loop["position"] == "top"  # the top of the back
    assert loop["balance"]["where"] == "back" and not loop["balance"]["on_head"]
    assert geo.metrics["hang_tilt_deg"] <= ad.STRAIGHT_TILT_DEG
    ring = geo.part("keyring")
    assert ring.vertices[:, 2].min() > 0.5 * geo.size_mm[2]  # on top, not out of the rump


def test_sitting_figure_keeps_the_loop_on_the_head(sitting, library):
    addons = [AddOn(kind=AddOnKind.HANGING_LOOP, params={"position": "top"})]
    geo = build_product_geometry(sitting.vertices, sitting.faces, "ornament", addons, 70.0,
                                 library, None)
    bal = geo.metrics["loop"]["balance"]
    assert bal["on_head"] and bal["where"] == "head"
    assert geo.metrics["hang_tilt_deg"] <= ad.STRAIGHT_TILT_DEG + 1.0


def test_flat_charm_is_a_true_relief(sitting, library):
    geo = build_product_geometry(sitting.vertices, sitting.faces, "keychain",
                                 [AddOn(kind=AddOnKind.KEYRING_LOOP)], 45.0, library, None,
                                 form="flat")
    assert geo.orientation == "flat" and geo.supports == "none"
    cut = geo.metrics["relief_cut"]
    assert 0.35 <= cut["kept_depth_frac"] < 1.0 and cut["cap_faces"] > 0
    rep = geo.figure.report
    assert rep.removed_faces["relief_back"] and rep.watertight
    assert (geo.figure.face_source == -1).any()
    # squeezing only the kept front leaves more depth than squashing the whole figure (0.2 to
    # 0.45 before), and the charm stays within its thickness aim
    assert geo.metrics["relief_scale"] > 0.4
    assert geo.metrics["thickness_mm"] <= 10.0 + 0.05
    assert any(n.startswith("True relief") for n in geo.notes)


def test_magnet_forms(sitting, library):
    flat = build_product_geometry(sitting.vertices, sitting.faces, "magnet", [], 60.0, library,
                                  None)
    assert flat.orientation == "flat" and {p.role for p in flat.parts} == {"back"}
    fig = build_product_geometry(sitting.vertices, sitting.faces, "magnet", [], 60.0, library,
                                 None, form="figure")
    assert fig.orientation == "upright" and "base" in {p.role for p in fig.parts}
    assert "magnet_pocket" in fig.metrics and not fig.warnings


@pytest.mark.parametrize("shape", ["rectangle", "rounded", "oval"])
def test_plate_shape_is_the_customers_pick(sitting, library, shape):
    addons = [AddOn(kind=AddOnKind.NAMEPLATE, text=TextSpec(text="Bronco"),
                    params={"shape": shape})]
    geo = build_product_geometry(sitting.vertices, sitting.faces, "nameplate", addons, 120.0,
                                 library, None)
    plate = geo.part("plate")
    info = geo.metrics["plate"]
    assert info["shape"] == shape and not geo.warnings
    w, d = np.ptp(plate.vertices[:, 0]), np.ptp(plate.vertices[:, 1])
    area_ratio = plate.volume_mm3 / (w * d * info["t_mm"])
    corner = np.array([plate.vertices[:, 0].max(), plate.vertices[:, 1].max()])
    gap = float(np.linalg.norm(plate.vertices[:, :2] - corner, axis=1).min())
    if shape == "oval":
        assert 0.72 <= area_ratio <= 0.8, area_ratio  # an ellipse fills pi/4 of its box
    else:  # corner radius 1 mm (gap 0.41 mm) or 5 mm (gap 2.07 mm)
        assert gap == pytest.approx({"rectangle": 0.41, "rounded": 2.07}[shape], abs=0.1)
    # the figure's footprint stays on the plate
    fig = geo.figure.vertices
    low = fig[fig[:, 2] < info["t_mm"] + 0.5]
    poly = ad.plate_polygon(w, d, shape, x0=float(plate.vertices[:, 0].min()))
    import shapely

    assert shapely.contains_xy(poly.buffer(0.5), low[:, 0], low[:, 1]).all()


def _library_with(extra: list[Filament]) -> FilamentLibrary:
    from pathlib import Path

    base = FilamentLibrary.model_validate_json(
        (Path(__file__).resolve().parents[2] / "filaments" / "library.json").read_text())
    return base.model_copy(update={"filaments": list(base.filaments) + extra})


PINK = Filament(id="bambu-pla-matte-sakura-pink", brand="Bambu Lab", line="PLA Matte",
                name="Sakura Pink", hex="#E8AFCF", lab=(77.6, 22.9, -6.9), owned=False)


def test_addon_colors_follow_the_figure_not_the_darkest_spool(sitting, library):
    """The hard figure is mostly white (head brown, ears black): the back of a flat charm is
    white, not black; the loop takes the color it touches."""
    ci = AddonColorInputs(face_rgb=sitting.face_colors)
    geo = build_product_geometry(sitting.vertices, sitting.faces, "keychain",
                                 [AddOn(kind=AddOnKind.KEYRING_LOOP)], 45.0, library, None,
                                 form="flat", colors=ci)
    assert geo.part("back").filament_id == "generic-pla-white"
    rows = {r["role"]: r for r in geo.metrics["addon_colors"]}
    assert rows["back"]["from"] == "figure main color"
    assert rows["keyring"]["from"] == "figure color it touches"
    pal = geo.metrics["figure_palette"]
    assert pal[0]["filament_id"] == "generic-pla-white" and pal[0]["share"] > 0.5
    # without color inputs the old default (darkest loaded PLA) is unchanged
    old = build_product_geometry(sitting.vertices, sitting.faces, "keychain",
                                 [AddOn(kind=AddOnKind.KEYRING_LOOP)], 45.0, library, None,
                                 form="flat")
    assert old.part("back").filament_id == "generic-pla-black"


def test_nameplate_colors_from_the_job_and_the_pet_palette(sitting, library):
    addons = [AddOn(kind=AddOnKind.NAMEPLATE, text=TextSpec(text="Bronco"))]
    auto = build_product_geometry(sitting.vertices, sitting.faces, "nameplate", addons, 120.0,
                                  library, None,
                                  colors=AddonColorInputs(face_rgb=sitting.face_colors))
    plate, text = auto.part("plate").filament_id, auto.part("text").filament_id
    assert plate == "generic-pla-white"  # the coat, not the darkest spool
    assert text != plate and text in {r["filament_id"] for r in auto.metrics["figure_palette"]}
    picked = build_product_geometry(
        sitting.vertices, sitting.faces, "nameplate", addons, 120.0, library, None,
        colors=AddonColorInputs(face_rgb=sitting.face_colors,
                                targets={"plate": "#7A4A10", "text": "#FAFAFA"},
                                target_sources={"plate": "job", "text": "concept"}))
    rows = {r["role"]: r for r in picked.metrics["addon_colors"]}
    assert picked.part("plate").filament_id == "generic-pla-brown" and rows["plate"]["from"] == "job"
    assert picked.part("text").filament_id == "generic-pla-white"
    assert rows["text"]["from"] == "concept"


def test_unowned_catalog_colors_only_with_source_any(sitting):
    lib = _library_with([PINK])
    addons = [AddOn(kind=AddOnKind.NAMEPLATE, text=TextSpec(text="Bronco"))]
    kw = {"face_rgb": sitting.face_colors, "targets": {"plate": "#E8AFCF"}}
    owned = build_product_geometry(sitting.vertices, sitting.faces, "nameplate", addons, 120.0,
                                   lib, None, colors=AddonColorInputs(**kw))
    assert owned.part("plate").filament_id != PINK.id
    anyc = build_product_geometry(sitting.vertices, sitting.faces, "nameplate", addons, 120.0,
                                  lib, None, colors=AddonColorInputs(**kw, source="any"))
    assert anyc.part("plate").filament_id == PINK.id
    assert not {r["role"]: r for r in anyc.metrics["addon_colors"]}["plate"]["owned"]


def test_build_product_passes_form_and_colors(tmp_path, standing, library):
    res = build_product(standing.vertices, standing.faces, "keychain", _keyring(), 45.0, library,
                        None, tmp_path, form="figure",
                        colors=AddonColorInputs(face_rgb=standing.face_colors))
    assert res.orientation == "upright" and res.supports == "tree"
    assert any(n.startswith("Form figure (job)") for n in res.notes)
    assert res.parts[0].filament_id in {"generic-pla-white", "generic-pla-black"}
    assert np.isfinite(res.bbox_max_mm).all()


def _face_up_charm():
    """A Creative Lab style build: a round backing disc lying face up in the print frame with a
    raised figure on it, head toward +Y (the top edge). Returned in the glTF frame."""
    from geo_fixtures import _TO_GLTF, _ellipsoid
    from manifold3d import Manifold, OpType

    disc = Manifold.cylinder(0.06, 0.5, 0.5, 96)
    body = _ellipsoid((0.20, 0.30, 0.05), (0.0, -0.05, 0.06))
    head = _ellipsoid((0.14, 0.12, 0.05), (0.0, 0.30, 0.07))
    mesh = Manifold.batch_boolean([disc, body, head], OpType.Add).to_mesh()
    v = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    return v @ _TO_GLTF.T, np.asarray(mesh.tri_verts, dtype=np.int64)


def test_a_face_up_creative_lab_build_is_laid_as_built(library):
    """live2 run: the flat cat charm (a Creative Lab build, already lying face up) was turned as
    if it stood upright, so its edge became the face and the relief squeeze made a 45 x 12 mm
    sliver. With source_pose face_up it stays a round charm, loop on the top edge."""
    v, f = _face_up_charm()
    lab = {"remove_plinth": "keep", "relief_cut": "off", "source_pose": "face_up",
           "relief_scale": 1.0}
    geo = build_product_geometry(v, f, "keychain", _keyring(**lab), 45.0, library, None,
                                 form="flat")
    fig = geo.figure.vertices
    w, h, t = np.ptp(fig[:, 0]), np.ptp(fig[:, 1]), np.ptp(fig[:, 2])
    assert abs(w - h) / max(w, h) < 0.1, (w, h)  # round, as built
    assert t < 0.3 * min(w, h)  # thin, face up
    ring = geo.part("keyring")
    assert ring is not None and ring.vertices[:, 1].mean() > fig[:, 1].max() - 0.2 * h
    assert geo.orientation == "flat" and any("Creative Lab" in n for n in geo.notes)

    # the old path: turned from a standing pose, the disc ends up on its edge and squeezed
    old = build_product_geometry(v, f, "keychain", _keyring(remove_plinth="keep",
                                                            relief_cut="off"), 45.0, library,
                                 None, form="flat")
    ofig = old.figure.vertices
    assert np.ptp(ofig[:, 1]) < 0.5 * np.ptp(ofig[:, 0])
