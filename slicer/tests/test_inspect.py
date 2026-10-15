"""Inspector: package reading, renders, inspection.json and index.html. Toolpath and estimate
tests need the slicer core (gcode_read, estimate, moves) and skip until it is installed; truth
tests need slicer/calibration/_truth (gitignored)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import zipfile
from pathlib import Path

import numpy as np
import pytest
from p2p_slicer.__main__ import main as cli
from p2p_slicer.inspect import inspect_path
from p2p_slicer.inspect.chart import fmt_s, layer_time_svg
from p2p_slicer.inspect.package import gcode_blocks, open_package, package_check, split_list
from p2p_slicer.inspect.page import render_page
from p2p_slicer.inspect.scene import (
    OrthoCamera,
    load_scene,
    ortho_for_box,
    render_multi,
    render_plate,
    tower_estimate,
)
from p2p_threemf.demo import demo_mesh, demo_settings, jackson_palette
from p2p_threemf.writer import write_3mf
from PIL import Image

HAS_CORE = all(importlib.util.find_spec(f"p2p_slicer.{m}") is not None
               for m in ("gcode_read", "estimate", "moves"))
needs_core = pytest.mark.skipif(not HAS_CORE, reason="slicer core (gcode_read, estimate) "
                                                    "not installed")
REPO = Path(__file__).resolve().parents[2]
TRUTH = Path(os.environ.get("P2P_SLICER_TRUTH") or REPO / "slicer/calibration/_truth")
needs_truth = pytest.mark.skipif(not (TRUTH / "keychain_4c_020" / "sliced.gcode.3mf").exists(),
                                 reason="slicer/calibration/_truth not present")

GCODE = "\n".join([
    "; HEADER_BLOCK_START", "; BambuStudio 02.08.02.61",
    "; model printing time: 5m 0s; total estimated time: 10m 10s",
    "; total layer number: 2", "; filament: 1,3", "; HEADER_BLOCK_END", "",
    "; CONFIG_BLOCK_START", "; filament_colour = #161616;#7C4B00;#F72323",
    ('; filament_settings_id = "Generic PLA @BBL X2D 0.4 nozzle";"Generic PLA @BBL X2D 0.4 '
     'nozzle";"Bambu PLA Basic @BBL X2D 0.4 nozzle"'),
    "; filament_type = PLA;PLA;PLA", "; filament_density = 1.24,1.24,1.26",
    "; filament_diameter = 1.75,1.75,1.75", "; filament_map = 1,1,2",
    "; printable_area = 0x0,256x0,256x256,0x256",
    "; extruder_printable_area = 0x0,256x0,256x256,0x256#20.5x0,256x0,256x256,20.5x256",
    "; printer_settings_id = Bambu Lab X2D 0.4 nozzle",
    "; print_settings_id = 0.20mm Standard @BBL X2D", "; CONFIG_BLOCK_END", "",
    "; EXECUTABLE_BLOCK_START", "M83", "  T0 H-1", "; CHANGE_LAYER", "; Z_HEIGHT: 0.2",
    "; LAYER_HEIGHT: 0.2", "; FEATURE: Outer wall", "G1 X100 Y100 Z.2 F3000",
    "G1 X110 Y100 E1", "G1 X110 Y110 E1", "; CHANGE_LAYER", "; Z_HEIGHT: 0.4",
    "; LAYER_HEIGHT: 0.2", "G1 Z.4", "T2 H-1", "; VFLUSH_START", ";VG1 E41.5 F299",
    "SYNC T5", "; VFLUSH_END", "; FEATURE: Outer wall", "G1 X100 Y100 E1",
    "; MACHINE_END_GCODE_START", "T65535",
])
SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config><header><header_item key="X-BBL-Client-Version" value="02.08.02.61"/></header><plate>
  <metadata key="index" value="1"/><metadata key="prediction" value="610"/>
  <metadata key="weight" value="0.25"/><metadata key="filament_maps" value="1 1 2"/>
  <filament id="1" type="PLA" color="#161616" used_m="0.02" used_g="0.06"/>
  <filament id="3" type="PLA" color="#F72323" used_m="0.06" used_g="0.19"/>
  <warning msg="not_support_traditional_timelapse" level="2" error_code ="10018003" />
  <layer_filament_lists>
    <layer_filament_list filament_list="0" layer_ranges="0 0" />
    <layer_filament_list filament_list="2" layer_ranges="1 1" />
  </layer_filament_lists>
</plate></config>"""


def _sliced(path: Path, with_result: bool = True) -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/slice_info.config", SLICE_INFO)
        z.writestr("Metadata/plate_1.gcode", GCODE)
        import hashlib

        z.writestr("Metadata/plate_1.gcode.md5", hashlib.md5(GCODE.encode()).hexdigest().upper())
        z.writestr("Metadata/filament_sequence.json",
                   json.dumps({"plate_1": {"sequence": [1, 3], "nozzle_sequence": [0, 1]}}))
    path.write_bytes(buf.getvalue())
    if with_result:
        (path.parent / "result.json").write_text(json.dumps({"sliced_plates": [{
            "id": 1, "total_predication": 610.2, "filament_change_times": 1,
            "feature_type_times": {"Outer wall": 20.0, "Flush": 64.0, "Travel": 3.0},
            "filaments": [{"id": 1, "main_used_g": 0.06, "total_used_g": 0.06},
                          {"id": 3, "main_used_g": 0.01, "total_used_g": 0.19}]}]}))
    return path


@pytest.fixture(scope="module")
def project(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("proj")
    v, f, s = demo_mesh([5, 1, 3, 2])
    out = d / "demo.3mf"
    write_3mf(v, f, s, jackson_palette(), demo_settings(5), out)
    return out


def test_split_list_and_blocks():
    assert split_list("a,b") == ["a", "b"]
    assert split_list("#1;#2") == ["#1", "#2"]
    assert split_list('"x y";"z"') == ["x y", "z"]
    hdr, cfg = gcode_blocks(GCODE)
    assert hdr["generator"] == "BambuStudio 02.08.02.61"
    assert hdr["model printing time"] == "5m 0s" and hdr["total layer number"] == "2"
    assert cfg["filament_map"] == "1,1,2" and "EXECUTABLE" not in "".join(cfg)


def test_open_sliced_package(tmp_path):
    pkg = open_package(_sliced(tmp_path / "s.gcode.3mf"))
    assert pkg.kind == "gcode.3mf" and pkg.sliced and pkg.gcode_md5_ok
    assert pkg.cfg_list("filament_colour") == ["#161616", "#7C4B00", "#F72323"]
    assert pkg.cfg_float("filament_density", index=2) == 1.26
    assert pkg.slice_info.layer_filament_lists == [{"first": 0, "last": 0, "slots": [1]},
                                                   {"first": 1, "last": 1, "slots": [3]}]
    assert pkg.slice_info.client_version == "02.08.02.61"
    assert pkg.result_path == tmp_path / "result.json" and pkg.sequence["sequence"] == [1, 3]
    pc = package_check(pkg)
    assert pc["gcode_md5_ok"] and "Metadata/plate_1.png" in pc["missing"]


def test_open_project_package(project):
    pkg = open_package(project)
    assert pkg.kind == "3mf" and not pkg.sliced
    assert pkg.objects and pkg.objects[0]["parts"]
    assert package_check(pkg)["has_p2p_sidecar"]


def test_ortho_camera_projects_box_inside(tmp_path):
    cam = ortho_for_box((0, 0, 0), (100, 50, 10), (-0.5, -0.5, 0.707), 400, 300, "t")
    assert isinstance(cam.resized(800), OrthoCamera)
    xy, depth = cam.project(np.array([[0, 0, 0], [100, 50, 10], [50, 25, 5]], float))
    assert (xy[:, 0] >= 0).all() and (xy[:, 0] <= 400).all()
    assert (xy[:, 1] >= 0).all() and (xy[:, 1] <= 300).all() and (depth > 0).all()


def test_scene_renders(project, tmp_path):
    sc = load_scene(project)
    assert len(sc.faces) == len(sc.face_slots) and sc.bbox[0][2] >= -1e-6
    views = render_multi(sc, tmp_path, size=160)
    assert set(views) == {"front", "front_3q", "right", "back_3q", "back", "left", "top"}
    im = np.asarray(Image.open(tmp_path / views["front"]))
    assert im.shape == (160, 160, 3) and len(np.unique(im.reshape(-1, 3), axis=0)) > 20
    plate = render_plate(sc, tmp_path, tower=([10, 200, 40, 230], 5.0))
    assert set(plate) == {"plate_iso", "plate_top", "model_iso"}
    top = np.asarray(Image.open(tmp_path / plate["plate_top"]))
    assert top.shape[:2] == (512, 512)
    # the tower box is drawn at its bed position: x 10..40, y 200..230 -> rows 52..112
    assert tuple(top[80, 50]) != tuple(top[300, 300])


def test_tower_estimate():
    assert tower_estimate(1, 5, 0.2, 35) == (0.0, 0.0)
    w, d = tower_estimate(4, 5.0, 0.2, 35.0)
    assert 15 <= w <= 40 and d >= 5  # ASSUMED sizing, loose bounds


def test_chart_and_time_format():
    assert fmt_s(3725) == "1h 02m" and fmt_s(95) == "1m 35s" and fmt_s(4.2) == "4.2s"
    layers = [{"index": i, "z": 0.2 * (i + 1), "time_s": 10.0 + i, "elapsed_s": 300 + 10 * i,
               "changes": i % 2} for i in range(12)]
    cat = np.zeros((13, 4))
    cat[1:, 0] = 8
    cat[1:, 1] = 2
    svg = layer_time_svg(layers, cat)
    assert svg.startswith("<svg") and svg.count('class="bar"') == 12
    assert "Layer 12, z 2.40 mm" in svg


def test_project_without_slicing(project, tmp_path):
    insp = inspect_path(project, tmp_path, slice_mode="none")
    assert (tmp_path / "inspection.json").is_file() and (tmp_path / "index.html").is_file()
    data = json.loads((tmp_path / "inspection.json").read_text())
    assert data["source"]["kind"] == "3mf" and data["source"]["sliced_by"]["slicer"] is None
    assert data["totals"]["layers"] is None and not data["layers"]
    assert set(data["renders"]["model"]) >= {"front", "back_3q", "top"}
    for rel in list(data["renders"]["model"].values()) + list(data["renders"]["plate"].values()):
        assert (tmp_path / rel).is_file()
    used = [f for f in data["filaments"] if f["faces"]]
    assert {f["slot"] for f in used} == {1, 2, 3, 5}
    assert data["plate"]["tower"]["basis"].startswith("estimate")
    assert data["printer"]["nozzles"][1]["x_range_mm"][0] == 20.5
    html = (tmp_path / "index.html").read_text()
    assert "<script" not in html and "Front 3/4" in html and "Not sliced" in html
    assert insp["version"] == 1


def test_sliced_package_reference_numbers(tmp_path):
    insp = inspect_path(_sliced(tmp_path / "s.gcode.3mf"), tmp_path / "out")
    ref = insp["totals"]["reference"]
    assert ref["time_s"] == 610.2 and ref["model_s"] == 300 and ref["prepare_s"] == 310.2
    assert ref["ams_swaps"] == 1 and ref["grams"] == 0.25 and ref["layers"] == 2
    f3 = next(f for f in insp["filaments"] if f["slot"] == 3)
    assert f3["reference"]["model_g"] == 0.01 and f3["nozzle"] == 1
    assert any(w["source"] == "slice_info" and w["level"] == "warning"
               for w in insp["warnings"])
    if not HAS_CORE:
        assert any("slicer core" in w["text"] for w in insp["warnings"])


def test_bare_gcode_and_kept_result(tmp_path):
    g = tmp_path / "job.gcode"
    g.write_text(GCODE)
    pkg = open_package(g)
    assert pkg.kind == "gcode" and pkg.result == {}
    (tmp_path / "job.slice_result.json").write_text('{"sliced_plates": []}')
    assert open_package(g).result_path == tmp_path / "job.slice_result.json"
    insp = inspect_path(g, tmp_path / "out")
    assert insp["printer"]["nozzles"][1]["x_range_mm"] == [20.5, 256.0]
    assert not any("Right nozzle" in w["text"] for w in insp["warnings"])


def test_page_renders_minimal_inspection():
    insp = {"source": {"path": "/x/a.gcode.3mf", "sliced_by": {"slicer": None}},
            "printer": {"preset": "Bambu Lab X2D 0.4 nozzle", "nozzles": []},
            "process": {"preset": None}, "plate": {"bed_type": None}, "objects": [],
            "filaments": [], "totals": {}, "roles": [], "travel": {}, "layers": [],
            "switches": [], "grouping": {"left": [], "right": [], "mode": None},
            "warnings": [{"level": "info", "text": "note <b>", "source": "inspector"}],
            "package": {"kind": "3mf", "entries": 3, "missing": [], "gcode_md5_ok": None},
            "parity": [], "renders": {}}
    html = render_page(insp)
    assert "note &lt;b&gt;" in html and "No filament changes" in html


@needs_core
def test_measure_on_synthetic_gcode(tmp_path):
    from p2p_slicer.estimate import estimate
    from p2p_slicer.gcode_read import read_gcode
    from p2p_slicer.inspect.measure import measure

    mt = read_gcode(GCODE)
    m = measure(mt, estimate(mt))
    assert m.layers == 2 and m.filament_changes == 1
    assert [s["from"] for s in m.switches] == [1] and m.switches[0]["to"] == 3
    assert m.per_slot[1]["model"] == pytest.approx(2 * 2.4053, rel=1e-3)
    assert m.per_slot[3]["flush"] == pytest.approx(41.5 * 2.4053, rel=1e-3)
    assert m.extruded_sets == [(1,), (3,)]


@needs_core
@needs_truth
def test_bambu_truth_keychain(tmp_path):
    src = TRUTH / "keychain_4c_020" / "sliced.gcode.3mf"
    insp = inspect_path(src, tmp_path)
    t, ref = insp["totals"], insp["totals"]["reference"]
    assert t["layers"] == ref["layers"] == 25
    assert t["time_s"] == pytest.approx(ref["time_s"], rel=0.01)
    assert t["ams_swaps"] == ref["ams_swaps"] == 6
    assert t["filament_changes"] == 23 and t["nozzle_switches"] == 18
    for f in insp["filaments"]:
        if f["reference"]:
            assert f["usage"]["total_g"] == pytest.approx(f["reference"]["total_g"], abs=0.011)
            assert f["usage"]["flush_g"] == pytest.approx(f["reference"]["flush_g"], abs=0.005)
    assert insp["renders"]["toolpaths"] and 1 <= len(insp["renders"]["layers"]) <= 12
    assert insp["renders"]["layer_time"].endswith(".svg")
    planned = [r for r in insp["layers"] if r["planned"] is not None]
    assert all(sorted(r["planned"]) == r["filaments"] for r in planned)
    assert not [w for w in insp["warnings"] if w["level"] == "error"]


def test_cli(project, tmp_path, capsys):
    assert cli(["inspect", str(project), "--out", str(tmp_path), "--slice", "none"]) == 0
    assert str(tmp_path / "index.html") in capsys.readouterr().out


@needs_core
def test_native_slice_path(project, tmp_path, monkeypatch):
    """A project 3MF sliced by p2p_slicer (faked until the planner lands) goes through the same
    panels as a Bambu file."""
    from types import SimpleNamespace

    from p2p_slicer.estimate import estimate
    from p2p_slicer.gcode_read import read_gcode
    from p2p_slicer.inspect import build

    mt = read_gcode(GCODE)
    res = SimpleNamespace(moves=mt, time=estimate(mt), filament_maps=[1, 1, 2, 1, 1],
                          warnings=["It seems object figure has floating regions."])
    calls = []

    def slice_project(path, *, out_dir=None, gcode=False):
        calls.append(Path(path))
        return res

    monkeypatch.setattr(build, "_native_api", lambda: SimpleNamespace(slice_project=slice_project))
    insp = inspect_path(project, tmp_path)
    assert calls == [project.resolve()]
    assert insp["source"]["sliced_by"]["slicer"] == "p2p_slicer"
    assert insp["totals"]["layers"] == 2 and insp["totals"]["reference"] is None
    assert insp["grouping"]["filament_maps"] == [1, 1, 2, 1, 1]
    assert any(w["source"] == "p2p_slicer" and "floating regions" in w["text"]
               for w in insp["warnings"])
    assert insp["renders"]["toolpaths"] and insp["renders"]["layers"]


@needs_core
@needs_truth
def test_gate_reads_a_core_result(tmp_path):
    """threemf's p2p_slicer backend reads what the core returns: a SliceReport plus an Analysis
    (api.slice_project's result), or a bare Analysis."""
    from types import SimpleNamespace

    from p2p_slicer.report import analyze_gcode, slice_report
    from p2p_threemf.slicer import native_numbers

    a = analyze_gcode(TRUTH / "keychain_4c_020" / "plate_1.gcode")
    rep = slice_report(a)
    for res in (SimpleNamespace(report=rep, analysis=a), SimpleNamespace(analysis=a)):
        n = native_numbers(res)
        assert n.print_time_s == pytest.approx(1864.4, abs=2)
        assert (n.filament_changes, n.ams_swaps, n.nozzle_changes) == (23, 6, 18)
        assert [f.slot for f in n.filaments] == [1, 2, 3, 5]
        assert sum(f.grams for f in n.filaments) == pytest.approx(7.31, abs=0.015)
        assert n.feature_time_s["Flush"] == pytest.approx(663.3, abs=1)
