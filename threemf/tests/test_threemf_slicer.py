import io
import json
import time
import zipfile

import pytest
from p2p_threemf.__main__ import main as cli_main
from p2p_threemf.demo import demo_mesh, demo_settings, jackson_palette
from p2p_threemf.gcode_stats import parse_gcode
from p2p_threemf.slicer import (
    BambuStudioBackend,
    OrcaSlicerBackend,
    _log_lines,
    _run,
    parse_slice_info,
    slice_check,
)
from p2p_threemf.verify import readback_check
from p2p_threemf.writer import write_3mf
from threemf_testutil import needs_bambu, settings

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="prediction" value="4237"/>
    <metadata key="weight" value="18.75"/>
    <filament id="1" type="PLA" color="#161616" used_m="0.89" used_g="2.67"/>
    <filament id="5" type="PLA" color="#FFFFFF" used_m="3.95" used_g="11.79"/>
    <warning msg="floating regions" level="1" error_code="1000C001"/>
  </plate>
</config>
"""


def _zip(path, files: dict[str, str]):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    path.write_bytes(buf.getvalue())


def test_parse_slice_info(tmp_path):
    _zip(tmp_path / "s.gcode.3mf", {"Metadata/slice_info.config": SLICE_INFO})
    info = parse_slice_info(tmp_path / "s.gcode.3mf")
    assert info.print_time_s == 4237
    assert info.weight_g == 18.75
    assert [f["id"] for f in info.filaments] == ["1", "5"]
    assert info.warnings == ["floating regions"]


def test_gcode_purge_estimate():
    g = "\n".join([
        "; total filament weight [g] : 1.00,2.00",
        "; filament: 1,3",
        "; filament_density: 1.24,1.24,1.24",
        "; filament_diameter: 1.75,1.75,1.75",
        "M83", "T0", "; FEATURE: Outer wall", "G1 X1 E100", "G1 E-2", "G1 E2",
        "; VFLUSH_START", "G1 E50", "; VFLUSH_END",
        "T2", "; FEATURE: Prime tower", "G1 X2 E30", "; FEATURE: Inner wall", "G1 X3 E200",
        "T65535", "G1 E5",
    ])
    s = parse_gcode(g)
    per_mm = 3.14159265 * 0.875**2 * 1.24 / 1000
    assert s.model_g[1] == pytest.approx(100 * per_mm, abs=1e-3)
    assert s.model_g[3] == pytest.approx(205 * per_mm, abs=1e-3)  # T65535 is not a tool change
    assert s.total_g == {1: 1.0, 3: 2.0}
    assert s.purge_g[1] == pytest.approx(1.0 - 100 * per_mm, abs=1e-3)


def test_log_filter(tmp_path):
    log = tmp_path / "l.txt"
    log.write_text(
        "[2026] [0x1] [error]   Invalid T command (T65535).\n"
        "[2026] [0x1] [error]   real problem\n"
        "[2026] [0x1] [warning] cli mode, Current BambuStudio Version 02.08.02.61\n"
        "[2026] [0x1] [warning] something new\n")
    errors, warnings = _log_lines(log)
    assert errors == ["real problem"]
    assert warnings == ["something new"]


def test_run_timeout_kills(tmp_path):
    t = time.monotonic()
    rc, timed_out = _run(["/bin/sleep", "30"], tmp_path / "log", 0.5, tmp_path)
    assert timed_out and rc is None
    assert time.monotonic() - t < 10


def test_missing_file_and_backend(tmp_path):
    r = slice_check(tmp_path / "nope.3mf", settings(4), 4)
    assert not r.ok and "file not found" in r.errors[0]
    _zip(tmp_path / "x.3mf", {"a": "b"})
    r = slice_check(tmp_path / "x.3mf", settings(4), 4,
                    backend=BambuStudioBackend(tmp_path / "no_such_binary"))
    assert not r.ok and "not installed" in r.errors[0]


def test_orca_backend_slices_patched_copy(tmp_path):
    orca = OrcaSlicerBackend()
    if not orca.available() or not orca.has_machine("Bambu Lab X2D 0.4 nozzle"):
        pytest.skip("OrcaSlicer with X2D profiles not installed")
    v, f, s = demo_mesh([5, 1, 3, 2])
    write_3mf(v, f, s, jackson_palette(), demo_settings(5), tmp_path / "d.3mf")
    r = slice_check(tmp_path / "d.3mf",
                    demo_settings(5).model_copy(update={"slicer": "orcaslicer"}), 4)
    assert r.slicer == "orcaslicer" and r.slicer_version.startswith("2.")
    assert r.ok, r.errors
    assert r.slots_used == 4
    assert any("Orca's own" in w for w in r.warnings)


def test_orca_backend_without_x2d_profile(tmp_path):
    orca = OrcaSlicerBackend(profiles=tmp_path / "no_profiles")
    if not orca.available():
        pytest.skip("OrcaSlicer not installed")
    v, f, s = demo_mesh([5, 1, 3, 2])
    write_3mf(v, f, s, jackson_palette(), demo_settings(5), tmp_path / "d.3mf")
    r = slice_check(tmp_path / "d.3mf", demo_settings(5), 4, backend=orca)
    assert not r.ok and "no machine profile" in r.errors[0]


@pytest.fixture(scope="module")
def demo_file(tmp_path_factory):
    d = tmp_path_factory.mktemp("demo")
    out = d / "demo_4color.3mf"
    assert cli_main(["demo", str(out)]) == 0
    return out


@needs_bambu
def test_slicer_gate_on_demo(demo_file):
    fids = [f.id for f in jackson_palette()]
    r = slice_check(demo_file, demo_settings(5), 4, filament_ids=fids, keep_gcode=True)
    assert r.ok, r.errors
    assert r.slicer == "bambu_studio" and r.slicer_version.startswith("02.08")
    assert r.printer_profile == "Bambu Lab X2D 0.4 nozzle"
    assert r.process_profile == "0.20mm Standard @BBL X2D"
    assert r.slots_used == 4 and r.expected_slots == 4
    assert [f.slot for f in r.filaments] == [1, 2, 3, 5]
    assert [f.filament_id for f in r.filaments] == [fids[0], fids[1], fids[2], fids[4]]
    assert all(f.grams > 0 for f in r.filaments)
    assert r.print_time_s and r.print_time_s > 60
    assert r.total_grams and r.purge_grams is not None and 0 < r.purge_grams < r.total_grams
    assert r.output and r.output.path.endswith(".gcode.3mf")
    assert not r.errors


@needs_bambu
def test_slicer_gate_fails_on_wrong_slot_count(demo_file):
    r = slice_check(demo_file, demo_settings(5), 3)
    assert not r.ok
    assert any("slots used 4 != expected 3" in e for e in r.errors)


@needs_bambu
def test_bambu_load_and_save_reads_our_paint(demo_file, tmp_path):
    raw = BambuStudioBackend().load_and_save(demo_file, tmp_path)
    assert raw.ok, raw.errors
    rb = readback_check(demo_file, raw.sliced_3mf)
    assert rb.ok, rb.errors
    assert rb.faces_same_slot == rb.faces_in > 0
    assert rb.presets_match and rb.modified_settings == []
    with zipfile.ZipFile(raw.sliced_3mf) as z:
        cfg = json.loads(z.read("Metadata/project_settings.config"))
    # Bambu keeps the presets as unmodified system presets.
    assert cfg["different_settings_to_system"] == [""] * 7
    assert cfg["filament_settings_id"][3] == "Generic PETG @BBL X2D 0.4 nozzle"


@needs_bambu
def test_mixed_pla_petg_slices(tmp_path):
    v, f, s = demo_mesh([5, 1, 4, 2])  # disc in the PETG slot
    write_3mf(v, f, s, jackson_palette(), demo_settings(5), tmp_path / "petg.3mf")
    r = slice_check(tmp_path / "petg.3mf", demo_settings(5), 4)
    assert r.ok, r.errors
    assert 4 in [f.slot for f in r.filaments]
