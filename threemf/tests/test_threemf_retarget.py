import json
import zipfile

import numpy as np
import pytest
from p2p_contracts import PrintSettings
from p2p_contracts.geometry import GeometryResult, PartRef
from p2p_contracts.manifest import FileRef
from p2p_threemf.colour import ciede2000, hex6, hex_to_lab
from p2p_threemf.demo import jackson_palette
from p2p_threemf.ply import read_ply, write_ply
from p2p_threemf.products import box, torus
from p2p_threemf.reader import read_3mf_full
from p2p_threemf.retarget import (
    MESHY_Z_UP,
    candidates,
    fit_meshy_frame,
    nearest,
    retarget,
    retarget_3mf,
    retarget_and_check,
)
from p2p_threemf.writer import apply_transform
from threemf_testutil import (
    MESHY_FIXTURE,
    MESHY_GLB,
    needs_bambu,
    needs_glb,
    needs_meshy,
    read_glb,
)

# Sharma, Wu, Dalal (2005) CIEDE2000 test pairs.
SHARMA = [
    ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
    ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
    ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
    ((50.0, 2.5, 0.0), (50.0, 0.0, -2.5), 4.3065),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]


@pytest.mark.parametrize(("a", "b", "expected"), SHARMA)
def test_ciede2000_reference_pairs(a, b, expected):
    assert ciede2000(a, b) == pytest.approx(expected, abs=1e-4)
    assert ciede2000(b, a) == pytest.approx(expected, abs=1e-4)


def test_hex_helpers():
    assert hex6("#999692FF") == "#999692"
    assert hex_to_lab("#FFFFFF") == pytest.approx((100.0, 0.0, 0.0), abs=0.01)
    assert hex_to_lab("#000000") == pytest.approx((0.0, 0.0, 0.0), abs=0.01)


def _ams():
    return [f.model_copy(update={"ams_slot": i + 1}) for i, f in enumerate(jackson_palette())]


def test_nearest_prefers_pla_unless_clearly_blue():
    pool = _ams()
    pick, _, _, _ = nearest("#0B5F86", pool)  # saturated blue: clearly the PETG
    assert pick.id == "generic-petg-0d6284"
    raw, _, _, _ = nearest("#3A4858", pool, material_margin=0)
    pick, _, _, _ = nearest("#3A4858", pool)  # grey-blue: PETG nearest, but not by 10
    assert raw.material == "PETG" and pick.material == "PLA"


def test_candidates_order_and_fallback():
    pool = _ams()[::-1]
    assert [f.ams_slot for f in candidates(pool)] == [1, 2, 3, 4, 5]
    unloaded = [f.model_copy(update={"ams_slot": None}) for f in pool]
    assert len(candidates(unloaded)) == 5


def test_ply_round_trip(tmp_path):
    v, f = torus(4.0, 1.25, (1.0, 2.0, 3.0))
    write_ply(tmp_path / "t.ply", v, f)
    v2, f2 = read_ply(tmp_path / "t.ply")
    assert np.allclose(v2, v, atol=1e-5) and np.array_equal(f2, f)
    (tmp_path / "a.ply").write_text(
        "ply\nformat ascii 1.0\nelement vertex 4\nproperty float x\nproperty float y\n"
        "property float z\nelement face 1\nproperty list uchar int vertex_indices\nend_header\n"
        "0 0 0\n1 0 0\n1 1 0\n0 1 0\n4 0 1 2 3\n")
    _, f3 = read_ply(tmp_path / "a.ply")
    assert f3.tolist() == [[0, 1, 2], [0, 2, 3]]


@needs_meshy
def test_retarget_meshy_fixture(tmp_path):
    out = tmp_path / "x2d.3mf"
    res, maps = retarget(MESHY_FIXTURE, _ams(), PrintSettings(), out, clean=False)
    src = read_3mf_full(MESHY_FIXTURE)
    dst = read_3mf_full(out)
    assert src.application == "BambuStudio-02.03.01.00"
    assert len(dst.faces) == len(src.faces) == 31159
    assert dst.printer_settings_id == "Bambu Lab X2D 0.4 nozzle"
    assert dst.filament_colours == ["#161616", "#7C4B00", "#F72323", "#0D6284", "#FFFFFF"]
    by_src = {m.src_slot: m.filament_id for m in maps}  # 1 grey, 2 black, 3 white, 4 brown, 5 grey
    assert by_src[2] == "generic-pla-161616"
    assert by_src[3] == "generic-pla-ffffff"
    assert by_src[4] == "generic-pla-7c4b00"
    assert by_src[5] == "generic-pla-ffffff"
    assert all(m.filament_id != "generic-petg-0d6284" for m in maps)
    lut = {m.src_slot: m.target_slot for m in maps}
    assert np.array_equal(dst.face_slots, np.vectorize(lut.get)(src.face_slots))
    assert res["slots_used"] == sorted(set(lut.values()))
    with zipfile.ZipFile(out) as z:
        side = json.loads(z.read("Metadata/p2p_threemf.json"))
    assert side["retarget"]["source_printer"] == "Bambu Lab X1 Carbon 0.4 nozzle"


@needs_meshy
def test_retarget_used_palette(tmp_path):
    res, maps = retarget(MESHY_FIXTURE, _ams(), PrintSettings(), tmp_path / "u.3mf",
                         palette_mode="used")
    assert len(res["palette"]) == len({m.filament_id for m in maps})
    assert res["slots_used"] == list(range(1, len(res["palette"]) + 1))


# ------------------------------------------------------------------ GeometryResult path

S_MESHY = 39.4953  # Meshy 3MF mm per glTF unit for the captured 75 mm figure


def _keychain_transform(gltf_v: np.ndarray, height_mm: float = 45.0) -> np.ndarray:
    """glTF (Y up) -> print frame: Z up, scaled to height, base on z = 0."""
    zup = gltf_v @ MESHY_Z_UP.T
    s = height_mm / np.ptp(zup[:, 2])
    m = np.eye(4)
    m[:3, :3] = s * MESHY_Z_UP
    m[:3, 3] = [0.0, 0.0, -s * zup[:, 2].min()]
    return m


def _geometry(tmp_path, gltf_v, gltf_f, with_cutter=True):
    t = _keychain_transform(gltf_v)
    fig = apply_transform(gltf_v, t)
    job = tmp_path / "job"
    (job / "s06_printability/a1").mkdir(parents=True)
    write_ply(job / "s06_printability/a1/figure.ply", fig, gltf_f)
    # Flat loop sunk 1 mm into the top of the head so it joins the figure (a loop floating
    # above it fails the slice with "empty layer").
    x, y, top = fig[np.argmax(fig[:, 2])]
    lv, lf = torus(4.0, 1.25, (x, y, top - 1.0))
    write_ply(job / "s06_printability/a1/loop.ply", lv, lf)
    parts = [PartRef(name="keyring_loop", role="keyring", filament_id="generic-pla-7c4b00",
                     watertight=True,
                     mesh=FileRef(path="s06_printability/a1/loop.ply", sha256="0"))]
    if with_cutter:
        cv, cf = box((-3, -3, -0.01), (3, 3, 2))
        write_ply(job / "s06_printability/a1/cutter.ply", cv, cf)
        parts.append(PartRef(name="magnet_pocket", role="cutter", filament_id=None,
                             watertight=True,
                             mesh=FileRef(path="s06_printability/a1/cutter.ply", sha256="0")))
    geo = GeometryResult(
        transform=tuple(tuple(float(x) for x in row) for row in t),
        figure=PartRef(name="figure", role="figure", filament_id=None, watertight=True,
                       mesh=FileRef(path="s06_printability/a1/figure.ply", sha256="0")),
        parts=parts, bbox_min_mm=tuple(fig.min(0)), bbox_max_mm=tuple(fig.max(0)))
    return geo, job, fig, (lv, lf)


def _synthetic_gltf():
    """Meshy's 3MF mesh taken back to a glTF frame with the measured Meshy mapping."""
    src = read_3mf_full(MESHY_FIXTURE)
    g = (src.vertices / S_MESHY) @ MESHY_Z_UP  # rows: R^T p, the inverse of Z-up rotation
    return g, src.faces, src


@needs_meshy
def test_fit_meshy_frame_recovers_scale(tmp_path):
    g, _, src = _synthetic_gltf()
    t = _keychain_transform(g)
    fig = apply_transform(g, t)
    fit = fit_meshy_frame(src.vertices, t, fig)
    assert fit.scale == pytest.approx(S_MESHY, rel=1e-6)
    assert fit.axis_ratio_spread < 1e-9
    assert fit.median_gap_mm < 1e-6
    placed = apply_transform(src.vertices, np.array(fit.matrix))
    assert np.allclose(placed, fig, atol=1e-6)


@needs_meshy
def test_retarget_3mf_with_geometry(tmp_path):
    g, f, src = _synthetic_gltf()
    geo, job, _, (_, lf) = _geometry(tmp_path, g, f)
    out = tmp_path / "keychain.3mf"
    res = retarget_3mf(MESHY_FIXTURE, geo, job, _ams(), PrintSettings(), out)
    for key in ("path", "sha256", "n_faces", "slots_used", "palette"):
        assert key in res
    assert res["frame_fit"]["median_gap_mm"] < 1e-3
    r = read_3mf_full(out)
    assert (r.face_part == 1).sum() == len(src.faces) - res["cleaned"]["duplicate"]
    assert (r.face_part == 2).sum() == len(lf)
    assert (r.face_slots[r.face_part == 2] == 2).all()  # brown loop, AMS slot 2
    assert 3 not in set(np.unique(r.face_part))  # the cutter is not printed geometry
    fig_read = r.vertices[r.faces[r.face_part == 1]].reshape(-1, 3)
    assert fig_read[:, 2].max() == pytest.approx(45.0, abs=1e-3)
    with zipfile.ZipFile(out) as z:
        ms = z.read("Metadata/model_settings.config").decode()
    assert 'subtype="negative_part"' in ms
    assert [p["name"] for p in res["parts"]] == ["meshy_multicolor", "keyring_loop",
                                                 "magnet_pocket"]
    # Same result with the geometry figure and paint copied face by face.
    res2 = retarget_3mf(MESHY_FIXTURE, geo, job, _ams(), PrintSettings(), tmp_path / "g.3mf",
                        figure_source="geometry")
    r2 = read_3mf_full(tmp_path / "g.3mf")
    assert np.array_equal(r2.face_slots, r.face_slots)
    assert res2["slots_used"] == res["slots_used"]


@needs_meshy
def test_retarget_3mf_rejects_mismatched_frames(tmp_path):
    g, f, _ = _synthetic_gltf()
    geo, job, _, _ = _geometry(tmp_path, g, f, with_cutter=False)
    # The figure PLY was made with a different transform than the one reported: taken back
    # through the reported transform it is stretched in x, so it cannot be Meshy's mesh.
    bad = np.array(geo.transform)
    bad[0, :3] *= 1.3
    write_ply(job / geo.figure.mesh.path, apply_transform(g, bad), f)
    with pytest.raises(ValueError, match="frames do not match"):
        retarget_3mf(MESHY_FIXTURE, geo, job, _ams(), PrintSettings(), tmp_path / "b.3mf")
    write_ply(job / geo.figure.mesh.path, apply_transform(g, np.array(geo.transform)), f)
    geo_x = geo.model_copy(update={"parts": [geo.parts[0].model_copy(
        update={"filament_id": "not-loaded"})]})
    with pytest.raises(ValueError, match="not loaded"):
        retarget_3mf(MESHY_FIXTURE, geo_x, job, _ams(), PrintSettings(), tmp_path / "c.3mf")


@needs_meshy
@needs_glb
def test_meshy_3mf_matches_its_gltf_source(tmp_path):
    """The real glTF from the same Meshy job: the fitted frame is a clean similarity."""
    gv, gf = read_glb(MESHY_GLB)
    assert len(gf) == 31159  # same face count as the multi-color 3MF
    geo, job, fig, _ = _geometry(tmp_path, gv, gf, with_cutter=False)
    src = read_3mf_full(MESHY_FIXTURE)
    fit = fit_meshy_frame(src.vertices, np.array(geo.transform), fig)
    assert fit.scale == pytest.approx(S_MESHY, rel=1e-4)
    assert fit.axis_ratio_spread < 1e-4
    assert fit.median_gap_mm < 0.1  # vertices moved slightly by Meshy's colour segmentation
    res = retarget_3mf(MESHY_FIXTURE, geo, job, _ams(), PrintSettings(), tmp_path / "k.3mf")
    assert res["warnings"] == []


@needs_meshy
@needs_bambu
def test_retarget_slices(tmp_path):
    out = tmp_path / "x2d.3mf"
    res, report, supports = retarget_and_check(MESHY_FIXTURE, _ams(), PrintSettings(), out)
    assert report.ok, report.errors
    assert report.slots_used == len(res["slots_used"])
    assert report.print_time_s > 0 and report.total_grams > 0
    assert supports is True  # the Meshy figure has overhangs; auto mode turns supports on
    assert any("enable_support" in w for w in report.warnings)


@needs_meshy
@needs_bambu
def test_retarget_keychain_with_addons_slices(tmp_path):
    g, f, _ = _synthetic_gltf()
    geo, job, _, _ = _geometry(tmp_path, g, f)
    res, report, _ = retarget_and_check(MESHY_FIXTURE, _ams(), PrintSettings(),
                                        tmp_path / "kc.3mf", geometry=geo, job_dir=job)
    assert report.ok, report.errors
    assert report.slots_used == len(res["slots_used"])
    assert 2 in [fl.slot for fl in report.filaments]  # the loop prints in brown


@needs_meshy
def test_retarget_drops_degenerate_and_duplicate_triangles(tmp_path):
    res, _ = retarget(MESHY_FIXTURE, _ams(), PrintSettings(), tmp_path / "c.3mf")
    assert res["cleaned"]["duplicate"] == 1  # the captured Meshy file repeats one triangle
    r = read_3mf_full(tmp_path / "c.3mf")
    f = r.faces
    assert not ((f[:, 0] == f[:, 1]) | (f[:, 1] == f[:, 2]) | (f[:, 0] == f[:, 2])).any()
    assert len(np.unique(np.sort(f, axis=1), axis=0)) == len(f)
    assert len(f) == 31158
