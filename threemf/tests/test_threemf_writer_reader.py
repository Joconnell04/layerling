import io
import json
import re
import zipfile

import numpy as np
import pytest
from p2p_threemf import read_3mf, read_3mf_full, write_3mf
from p2p_threemf.encoding import PaintNode, decode_tree, encode_state, encode_tree
from p2p_threemf.profiles import load_snapshot
from p2p_threemf.writer import default_slot
from threemf_testutil import BAMBU_SAVED, needs_saved, pla_palette, random_slots, settings


@pytest.mark.parametrize("n_slots", [1, 2, 3, 4, 5, 6, 7, 8])
def test_round_trip(tmp_path, mesh, n_slots):
    v, f = mesh
    s = random_slots(len(f), n_slots, seed=n_slots)
    res = write_3mf(v, f, s, pla_palette(n_slots), settings(n_slots), tmp_path / "m.3mf")
    assert res["n_faces"] == len(f)
    assert res["slots_used"] == sorted(set(s.tolist()))
    assert res["default_slot"] == 1
    v2, f2, s2, colours = read_3mf(tmp_path / "m.3mf")
    assert np.array_equal(f2, f)
    assert np.array_equal(s2, s)
    assert np.allclose(v2, v, atol=1e-5)
    assert colours == [p.hex.upper() for p in pla_palette(n_slots)]


def test_default_slot_is_unpainted(tmp_path, mesh):
    v, f = mesh
    s = np.full(len(f), 3)
    s[:10] = 1
    s[10:20] = 4
    write_3mf(v, f, s, pla_palette(4), settings(4), tmp_path / "m.3mf")
    with zipfile.ZipFile(tmp_path / "m.3mf") as z:
        obj = z.read("3D/Objects/object_1.model").decode()
        ms = z.read("Metadata/model_settings.config").decode()
    codes = re.findall(r'paint_color="([0-9A-F]+)"', obj)
    assert len(codes) == 20
    assert set(codes) == {encode_state(1), encode_state(4)}
    assert '<metadata key="extruder" value="3"/>' in ms
    assert default_slot(s) == 3


def test_deterministic(tmp_path, mesh):
    v, f = mesh
    s = random_slots(len(f), 4)
    a = write_3mf(v, f, s, pla_palette(4), settings(4), tmp_path / "a.3mf")
    b = write_3mf(v, f, s, pla_palette(4), settings(4), tmp_path / "b.3mf")
    assert a["sha256"] == b["sha256"]
    assert (tmp_path / "a.3mf").read_bytes() == (tmp_path / "b.3mf").read_bytes()
    s[0] = 2 if s[0] != 2 else 3
    c = write_3mf(v, f, s, pla_palette(4), settings(4), tmp_path / "c.3mf")
    assert c["sha256"] != a["sha256"]


def test_package_layout(tmp_path, mesh, jackson):
    v, f = mesh
    write_3mf(v, f, random_slots(len(f), 5), jackson, settings(5), tmp_path / "m.3mf")
    with zipfile.ZipFile(tmp_path / "m.3mf") as z:
        names = z.namelist()
        main = z.read("3D/3dmodel.model").decode()
        rels = z.read("_rels/.rels").decode()
    assert names[:5] == ["[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model",
                         "3D/_rels/3dmodel.model.rels", "3D/Objects/object_1.model"]
    assert "Metadata/project_settings.config" in names
    assert "Metadata/model_settings.config" in names
    assert '<metadata name="Application">BambuStudio-02.08.02.61</metadata>' in main
    assert 'p:path="/3D/Objects/object_1.model"' in main
    assert "/3D/3dmodel.model" in rels
    # Base on the bed, centred on the X2D plate (256 x 256).
    xf = re.search(r'<item objectid="2"[^>]*transform="([^"]+)"', main).group(1).split()
    lo, hi = v.min(axis=0), v.max(axis=0)
    assert float(xf[9]) + (lo[0] + hi[0]) / 2 == pytest.approx(128.0, abs=1e-4)
    assert float(xf[10]) + (lo[1] + hi[1]) / 2 == pytest.approx(128.0, abs=1e-4)
    assert float(xf[11]) == pytest.approx(-lo[2], abs=1e-6)


def test_project_config_fields(tmp_path, mesh, jackson):
    v, f = mesh
    write_3mf(v, f, random_slots(len(f), 5), jackson, settings(5), tmp_path / "m.3mf")
    with zipfile.ZipFile(tmp_path / "m.3mf") as z:
        cfg = json.loads(z.read("Metadata/project_settings.config"))
    n = 5
    assert cfg["printer_settings_id"] == "Bambu Lab X2D 0.4 nozzle"
    assert cfg["printer_model"] == "Bambu Lab X2D"
    assert cfg["print_settings_id"] == "0.20mm Standard @BBL X2D"
    assert cfg["layer_height"] == "0.2"
    assert cfg["filament_settings_id"] == [
        "Generic PLA @BBL X2D 0.4 nozzle", "Generic PLA @BBL X2D 0.4 nozzle",
        "Bambu PLA Basic @BBL X2D 0.4 nozzle", "Generic PETG @BBL X2D 0.4 nozzle",
        "Generic PLA @BBL X2D 0.4 nozzle"]
    assert cfg["filament_ids"] == ["GFL99", "GFL99", "GFA00", "GFG99", "GFL99"]
    assert cfg["filament_colour"] == ["#161616", "#7C4B00", "#F72323", "#0D6284", "#FFFFFF"]
    assert cfg["filament_type"] == ["PLA", "PLA", "PLA", "PETG", "PLA"]
    # X2D: two extruders, six extruder variants per filament.
    assert cfg["nozzle_diameter"] == ["0.4", "0.4"]
    assert len(cfg["filament_extruder_variant"]) == n * 6
    assert cfg["filament_self_index"] == [str(i) for i in range(1, n + 1) for _ in range(6)]
    assert len(cfg["nozzle_temperature"]) == n * 6
    assert len(cfg["filament_flow_ratio"]) == n * 6
    assert len(cfg["flush_volumes_matrix"]) == 2 * n * n
    assert cfg["filament_map_mode"] == "Auto For Flush"
    assert cfg["filament_map"] == ["1"] * n
    assert cfg["different_settings_to_system"] == [""] * (n + 2)
    assert cfg["from"] == "project" and cfg["name"] == "project_settings"
    # Same key set Bambu Studio writes for this printer.
    assert set(cfg) == set(load_snapshot()["project_template"]["keys"])
    # PETG slot keeps PETG temperatures.
    assert cfg["nozzle_temperature"][3 * 6] != cfg["nozzle_temperature"][0]


def test_library_preset_names_get_nozzle_suffix(tmp_path, mesh):
    v, f = mesh
    write_3mf(v, f, random_slots(len(f), 2), pla_palette(2), settings(2), tmp_path / "m.3mf")
    r = read_3mf_full(tmp_path / "m.3mf")
    assert r.filament_settings_id == ["Bambu PLA Basic @BBL X2D 0.4 nozzle"] * 2
    assert r.printer_settings_id == "Bambu Lab X2D 0.4 nozzle"
    assert r.sidecar["palette"][0]["filament_id"] == "bambu-pla-basic-0"


def test_input_validation(tmp_path, mesh):
    v, f = mesh
    s = random_slots(len(f), 2)
    with pytest.raises(ValueError):
        write_3mf(v, f, s * 0, pla_palette(2), settings(2), tmp_path / "m.3mf")
    with pytest.raises(ValueError):
        write_3mf(v, f, s + 5, pla_palette(2), settings(2), tmp_path / "m.3mf")
    with pytest.raises(ValueError):
        write_3mf(v, f[:, :2], s, pla_palette(2), settings(2), tmp_path / "m.3mf")
    with pytest.raises(ValueError):  # palette bigger than the AMS
        write_3mf(v, f, s, pla_palette(6), settings(4).model_copy(update={"ams_slots": 4}),
                  tmp_path / "m.3mf")


def _replace_codes(path, new_codes: dict[int, str], out):
    """Rewrite paint_color on chosen triangles (by index) to test the reader on split paint."""
    with zipfile.ZipFile(path) as z:
        items = [(i, z.read(i.filename)) for i in z.infolist()]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zo:
        for info, data in items:
            if info.filename == "3D/Objects/object_1.model":
                lines = data.decode().split("\n")
                tri = [k for k, ln in enumerate(lines) if "<triangle " in ln]
                for idx, code in new_codes.items():
                    ln = re.sub(r' paint_color="[^"]*"', "", lines[tri[idx]])
                    lines[tri[idx]] = ln.replace("/>", f' paint_color="{code}"/>')
                data = "\n".join(lines).encode()
            zo.writestr(info, data)
    out.write_bytes(buf.getvalue())


def test_reader_split_triangles(tmp_path, mesh):
    v, f = mesh
    s = np.full(len(f), 2)  # default slot 2
    write_3mf(v, f, s, pla_palette(4), settings(4), tmp_path / "m.3mf")
    three_quarters_4 = PaintNode(split_sides=3, special_side=0, children=[
        PaintNode(state=4), PaintNode(state=4), PaintNode(state=1), PaintNode(state=4)])
    half_unpainted = PaintNode(split_sides=1, special_side=2, children=[
        PaintNode(state=0), PaintNode(state=3)])
    mostly_unpainted = PaintNode(split_sides=2, special_side=1, children=[
        PaintNode(state=3), PaintNode(state=0), PaintNode(state=0)])
    codes = {0: encode_tree(three_quarters_4), 1: encode_tree(half_unpainted),
             2: encode_tree(mostly_unpainted), 3: "0C442"}
    _replace_codes(tmp_path / "m.3mf", codes, tmp_path / "split.3mf")
    r = read_3mf_full(tmp_path / "split.3mf")
    assert r.face_slots[0] == 4
    assert r.face_slots[1] == 2  # 50/50 tie between default 2 and 3 goes to the lower slot
    assert r.face_slots[2] == 2  # 3/4 unpainted -> default slot
    assert r.face_slots[3] == 1  # Bambu string: 3/4 slot 1, 1/4 slot 3
    assert r.split[:4].all() and not r.split[4:].any()
    assert (r.face_slots[4:] == 2).all()
    assert r.codes[0] == codes[0]


def test_reader_states_beyond_palette_are_unpainted(tmp_path, mesh):
    v, f = mesh
    write_3mf(v, f, np.full(len(f), 1), pla_palette(2), settings(2), tmp_path / "m.3mf")
    _replace_codes(tmp_path / "m.3mf", {0: encode_state(9)}, tmp_path / "x.3mf")
    r = read_3mf_full(tmp_path / "x.3mf")
    assert r.face_slots[0] == 1  # Bambu clamps unknown filaments to the object's extruder


@needs_saved
def test_reads_bambu_saved_project():
    r = read_3mf_full(BAMBU_SAVED)
    assert r.application == "BambuStudio-02.08.02.61"
    assert r.printer_settings_id == "Bambu Lab X2D 0.4 nozzle"
    assert len(r.faces) == 1_499_030
    assert r.object_extruders == {2: 5}
    assert set(np.unique(r.face_slots).tolist()) <= {1, 2, 3, 4, 5}
    assert r.split.sum() > 1000
    # Every distinct paint string Bambu wrote re-encodes to the same string.
    for code in set(r.codes) - {""}:
        assert encode_tree(decode_tree(code)) == code
