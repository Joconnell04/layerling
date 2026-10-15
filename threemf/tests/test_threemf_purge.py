"""Round 2: process overrides, nozzle maps, variable layers, despeckle, change counts."""

import json
import zipfile

import numpy as np
from p2p_contracts import PrintSettings
from p2p_threemf.audit import audit_paint, despeckle
from p2p_threemf.demo import demo_settings, jackson_palette
from p2p_threemf.gcode_stats import parse_gcode
from p2p_threemf.lab import max_cut_map
from p2p_threemf.layers import adaptive_profile
from p2p_threemf.products import PRODUCTS, figurine, keychain_too_thin
from p2p_threemf.profiles import coerce_value
from p2p_threemf.slicer import slice_check
from p2p_threemf.writer import DEFAULT_PROCESS_OVERRIDES, write_3mf
from threemf_testutil import needs_bambu


def _cfg(path):
    with zipfile.ZipFile(path) as z:
        return (json.loads(z.read("Metadata/project_settings.config")),
                z.read("Metadata/model_settings.config").decode(), z.namelist())


def test_coerce_value():
    assert coerce_value("0.8", ["1", "1"]) == ["0.8", "0.8"]
    assert coerce_value("1,2,1", ["1", "1", "1"]) == ["1", "2", "1"]
    assert coerce_value("20", "35") == "20"


def test_process_overrides_reach_the_project(tmp_path):
    p = figurine()
    s = demo_settings(5).model_copy(update={"process_overrides": {
        "prime_tower_width": "20", "flush_multiplier": "0.9",
        "filament_map_mode": "Manual", "filament_map": "1,1,2,1,2"}})
    write_3mf(p.vertices, p.faces, p.face_slots, jackson_palette(), s, tmp_path / "o.3mf")
    cfg, ms, _ = _cfg(tmp_path / "o.3mf")
    assert cfg["prime_tower_width"] == "20"
    assert cfg["flush_multiplier"] == ["0.9", "0.9"]
    assert cfg["filament_map"] == ["1", "1", "2", "1", "2"]
    assert "prime_tower_width" in cfg["different_settings_to_system"][0]
    assert '<metadata key="filament_map_mode" value="Manual"/>' in ms
    assert '<metadata key="filament_maps" value="1 1 2 1 2"/>' in ms


def test_writer_defaults_apply_and_can_be_overridden(tmp_path):
    p = figurine()
    write_3mf(p.vertices, p.faces, p.face_slots, jackson_palette(), demo_settings(5),
              tmp_path / "d.3mf")
    cfg, _, _ = _cfg(tmp_path / "d.3mf")
    for k, v in DEFAULT_PROCESS_OVERRIDES.items():
        assert cfg[k] == coerce_value(v, cfg[k]) or cfg[k] == v
    if DEFAULT_PROCESS_OVERRIDES:
        k = next(iter(DEFAULT_PROCESS_OVERRIDES))
        s = demo_settings(5).model_copy(update={"process_overrides": {k: "Default"}})
        write_3mf(p.vertices, p.faces, p.face_slots, jackson_palette(), s, tmp_path / "e.3mf")
        assert _cfg(tmp_path / "e.3mf")[0][k] in ("Default", ["Default"] * 2)


def test_adaptive_profile_and_writer(tmp_path):
    p = figurine()
    prof = adaptive_profile(p.vertices, p.faces, p.face_slots)
    assert prof[0] == (0.0, 0.2)  # Bambu drops profiles whose first height is not the first layer
    assert prof[-1][0] == 40.0
    assert {h for _, h in prof} == {0.2, 0.28}
    write_3mf(p.vertices, p.faces, p.face_slots, jackson_palette(), demo_settings(5),
              tmp_path / "a.3mf", layer_profile=prof)
    with zipfile.ZipFile(tmp_path / "a.3mf") as z:
        text = z.read("Metadata/layer_heights_profile.txt").decode()
    assert text.startswith("object_id=1|0.000000;0.200000;")


def test_despeckle_removes_only_unprintable_paint():
    p = keychain_too_thin()
    before = audit_paint(p.vertices, p.faces, p.face_slots)
    s2, merged, changed = despeckle(p.vertices, p.faces, p.face_slots)
    after = audit_paint(p.vertices, p.faces, s2)
    assert merged == len(before.thin) and after.thin == []
    assert changed == int((s2 != p.face_slots).sum())
    k = PRODUCTS["keychain"]()
    s3, merged3, _ = despeckle(k.vertices, k.faces, k.face_slots)
    assert merged3 == 0 and np.array_equal(s3, k.face_slots)  # 1.2 mm eyes stay


def test_change_counts_from_gcode():
    g = "\n".join([
        "; filament: 1,3", "M83", "; CHANGE_LAYER", "T0", "G1 X1 E1", "T2", "G1 X2 E1",
        "; CHANGE_LAYER", "T2", "G1 X3 E1", "T0", "G1 X4 E1", "T2", "; CHANGE_LAYER",
        "T65535", "G1 E1"])
    st = parse_gcode(g, filament_map=[1, 2, 2])
    assert st.layers == 3
    assert st.filament_changes == 3
    assert st.max_changes_per_layer == 2 and st.layers_with_change == 2
    assert st.nozzle_changes == 3


def test_max_cut_map():
    assert max_cut_map({"3-5": 42, "1-5": 4, "1-3": 1}, 5) in ("1,1,1,1,2", "2,2,2,2,1")


@needs_bambu
def test_adaptive_layers_cut_layers_not_slots(tmp_path):
    p = figurine()
    base = tmp_path / "b.3mf"
    write_3mf(p.vertices, p.faces, p.face_slots, jackson_palette(), demo_settings(5), base)
    ad = tmp_path / "a.3mf"
    write_3mf(p.vertices, p.faces, p.face_slots, jackson_palette(), demo_settings(5), ad,
              layer_profile=adaptive_profile(p.vertices, p.faces, p.face_slots))
    rb = slice_check(base, demo_settings(5), 4)
    ra = slice_check(ad, demo_settings(5), 4)
    assert rb.ok and ra.ok
    assert ra.print_time_s < rb.print_time_s
    assert ra.slots_used == 4 and ra.readback_ok


@needs_bambu
def test_report_fields(tmp_path):
    p = keychain_too_thin()
    path = tmp_path / "t.3mf"
    write_3mf(p.vertices, p.faces, p.face_slots, jackson_palette(), PrintSettings(), path,
              parts=p.parts)
    r = slice_check(path, PrintSettings(), 4, filament_ids=[f.id for f in jackson_palette()])
    assert r.readback_ok is True and r.readback_slots == 4
    assert {t["slot"] for t in r.thin_paint} == {1, 3}
    assert all(t["min_width_mm"] < 0.8 for t in r.thin_paint)
    assert r.thin_paint[0]["filament_id"] in {f.id for f in jackson_palette()}


def test_clean_mesh():
    from p2p_threemf.meshfix import clean_mesh

    v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 0, 0]], dtype=float)
    f = np.array([[0, 1, 2], [0, 2, 3], [2, 1, 3], [0, 1, 1], [2, 1, 0], [0, 4, 3]])
    s = np.array([1, 2, 3, 4, 5, 2])
    v2, f2, s2, rep = clean_mesh(v, f, s)
    assert rep.vertices_welded == 1  # vertex 4 equals vertex 1
    assert rep.repeated_index == 1 and rep.duplicate == 1 and rep.faces_out == 4
    assert len(v2) == 4 and s2.tolist() == [1, 2, 3, 2]
