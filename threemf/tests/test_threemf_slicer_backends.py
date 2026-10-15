"""slice_check backends: result.json numbers, SLICER_BIN, the p2p_slicer dispatch. No slicer runs
here: backends are fakes that hand slice_check a prepared package."""

import io
import json
import math
import os
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from p2p_contracts import PrintSettings
from p2p_threemf import slicer as sl
from p2p_threemf.demo import demo_mesh, demo_settings, jackson_palette
from p2p_threemf.gcode_stats import hms_to_s, parse_gcode
from p2p_threemf.slicer import (
    BambuStudioBackend,
    NativeSlicerBackend,
    RawSlice,
    backend_available,
    bambu_binary,
    get_backend,
    native_numbers,
    slice_check,
)
from p2p_threemf.writer import write_3mf

PER_MM = math.pi * 0.875**2 * 1.24 / 1000  # grams per mm of 1.75 mm filament at 1.24 g/cm3

GCODE = "\n".join([
    "; HEADER_BLOCK_START",
    "; BambuStudio 02.08.02.61",
    "; model printing time: 5m 0s; total estimated time: 10m 10s",
    "; total filament weight [g] : 0.25,0.30",
    "; filament_density: 1.24,1.24,1.24",
    "; filament_diameter: 1.75,1.75,1.75",
    "; filament: 1,3",
    "; HEADER_BLOCK_END",
    "M83",
    "  T0 H-1",
    "; CHANGE_LAYER",
    "; FEATURE: Outer wall",
    "G1 X10 Y10 E5",
    "; FEATURE: Prime tower",
    "G1 X20 Y20 E2",
    "G1 E-0.8",
    "G1 E0.8",
    "; NOZZLE_CHANGE_START OF1 NF3 ON0 NN1",
    "T2 H-1",
    "; VFLUSH_START",
    ";VG1 E41.5 F299",
    ";VG1 E10 F299",
    "SYNC T5",
    "; VFLUSH_END",
    "; FEATURE: Prime tower",
    "G1 X21 Y20 E3",
    "; FEATURE: Inner wall",
    "G1 X30 Y30 E20",
    "; MACHINE_END_GCODE_START",
    "T65535",
])

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config><plate>
  <metadata key="index" value="1"/>
  <metadata key="prediction" value="610"/>
  <metadata key="weight" value="0.55"/>
  <metadata key="filament_maps" value="1 1 2 1 1"/>
  <filament id="1" type="PLA" color="#161616" used_m="0.08" used_g="0.25"/>
  <filament id="3" type="PLA" color="#F72323" used_m="0.10" used_g="0.30"/>
</plate></config>
"""

RESULT = {
    "return_code": 0, "error_string": "Success.",
    "sliced_plates": [{
        "id": 1, "total_predication": 610.4, "main_predication": 250.0,
        "filament_change_times": 1,
        "feature_type_times": {"Custom": 290.1, "Flush": 64.0, "Outer wall": 12.5},
        "filaments": [{"id": 1, "filament_id": "GFL99", "main_used_g": 0.0149,
                       "total_used_g": 0.25},
                      {"id": 3, "filament_id": "GFA00", "main_used_g": 0.0597,
                       "total_used_g": 0.30}],
    }],
}


def _package(path: Path, gcode: str = GCODE, slice_info: str = SLICE_INFO) -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/slice_info.config", slice_info)
        z.writestr("Metadata/plate_1.gcode", gcode)
    path.write_bytes(buf.getvalue())
    return path


class ReplayBackend:
    """Hands slice_check a prepared .gcode.3mf and result.json, as the Bambu backend would."""

    name = "bambu_studio"
    resaves_model = True

    def __init__(self, sliced: Path, result: dict | None, log: str = ""):
        self.sliced, self.result, self.log = sliced, result, log

    def available(self) -> bool:
        return True

    def version(self) -> str:
        return "unknown"

    def slice(self, model, workdir, timeout_s):
        workdir.mkdir(parents=True, exist_ok=True)
        log = workdir / "slicer.log"
        log.write_text(self.log)
        out = workdir / "sliced.gcode.3mf"
        out.write_bytes(self.sliced.read_bytes())
        return RawSlice(ok=True, returncode=0, result=dict(self.result or {}), sliced_3mf=out,
                        log_path=log)


@pytest.fixture(scope="module")
def project(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("proj")
    v, f, s = demo_mesh([5, 1, 3, 2])
    out = d / "demo.3mf"
    write_3mf(v, f, s, jackson_palette(), demo_settings(5), out)
    return out


def test_hms():
    assert hms_to_s("25m 55s") == 1555
    assert hms_to_s("1d 2h 3m 4s") == 93784


def test_gcode_stats_exact_buckets():
    st = parse_gcode(GCODE)
    assert st.tower_g_slot[1] == pytest.approx(2 * PER_MM, abs=1e-4)  # unretract not counted
    assert st.tower_g_slot[3] == pytest.approx(3 * PER_MM, abs=1e-4)
    assert st.flush_g_slot == {3: pytest.approx(51.5 * PER_MM, abs=1e-4)}
    assert st.flushes == 1 and st.nozzle_switches == 1 and st.filament_changes == 1
    assert st.model_time_s == 300 and st.total_time_s == 610


def test_result_json_fills_the_report(project, tmp_path):
    pkg = _package(tmp_path / "s.gcode.3mf")
    backend = ReplayBackend(pkg, RESULT, "[x] [warning] cli mode, Current BambuStudio Version "
                                         "02.08.02.61\n")
    out = tmp_path / "out"
    r = slice_check(project, demo_settings(5), 2, backend=backend, out_dir=out,
                    verify_readback=False, min_paint_width_mm=0)
    assert r.ok, r.errors
    assert r.slicer_version == "02.08.02.61"  # read from the log when no Info.plist
    assert r.print_time_s == 610 and r.model_time_s == 300 and r.prepare_time_s == 310
    assert r.ams_swaps == 1 and r.filament_changes == 1 and r.nozzle_changes == 1
    # exact purge from result.json: total minus model per filament
    assert r.purge_grams == pytest.approx(round(0.25 - 0.0149 + 0.30 - 0.0597, 2))
    assert r.tower_grams == pytest.approx(round(5 * PER_MM, 2))
    f1, f3 = r.filaments
    assert (f1.slot, f3.slot) == (1, 3)
    assert f1.model_grams == pytest.approx(0.0149, abs=1e-3)
    assert f3.model_grams == pytest.approx(0.0597, abs=1e-3)
    assert f1.tower_grams == pytest.approx(2 * PER_MM, abs=1e-3)
    assert f3.flush_grams == pytest.approx(0.30 - 0.0597 - 3 * PER_MM, abs=1e-3)
    for f in r.filaments:
        assert f.model_grams + f.flush_grams + f.tower_grams == pytest.approx(
            {1: 0.25, 3: 0.30}[f.slot], abs=2e-3)
    kept = json.loads((out / "demo.slice_result.json").read_text())
    assert kept["sliced_plates"][0]["feature_type_times"]["Flush"] == 64.0


def test_without_result_json_falls_back_to_gcode(project, tmp_path):
    pkg = _package(tmp_path / "s.gcode.3mf")
    r = slice_check(project, demo_settings(5), 2, backend=ReplayBackend(pkg, None),
                    out_dir=tmp_path, verify_readback=False, min_paint_width_mm=0)
    assert r.ok, r.errors
    assert r.ams_swaps == 1  # VFLUSH blocks that purge
    f3 = next(f for f in r.filaments if f.slot == 3)
    assert f3.model_grams is None
    assert f3.flush_grams == pytest.approx(51.5 * PER_MM, abs=1e-3)
    assert r.purge_grams is not None and 0 < r.purge_grams < r.total_grams
    assert not (tmp_path / "demo.slice_result.json").exists()


def test_invalid_tool_still_fails(project, tmp_path):
    bad = GCODE.replace("T2 H-1", "T9 H-1")
    pkg = _package(tmp_path / "s.gcode.3mf", gcode=bad)
    r = slice_check(project, demo_settings(5), 2, backend=ReplayBackend(pkg, RESULT),
                    out_dir=tmp_path, verify_readback=False, min_paint_width_mm=0)
    assert not r.ok and any("invalid tool change" in e for e in r.errors)


def _fake_binary(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_slicer_bin_override(tmp_path, monkeypatch):
    fake = _fake_binary(tmp_path / "bambu-studio")
    monkeypatch.setenv("SLICER_BIN", str(fake))
    assert bambu_binary() == fake
    assert BambuStudioBackend().binary == fake and BambuStudioBackend().available()
    assert backend_available("bambu_studio")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SLICER_BIN", "bambu-studio")  # a name on PATH
    assert bambu_binary() == fake
    monkeypatch.setenv("SLICER_BIN", str(tmp_path / "missing"))
    assert not BambuStudioBackend().available()
    r = slice_check(tmp_path / "x.3mf", PrintSettings(), 1)
    assert not r.ok  # the file is missing; the backend is resolved first without error
    monkeypatch.delenv("SLICER_BIN")
    assert bambu_binary() == sl.BAMBU_BIN
    assert BambuStudioBackend(tmp_path / "given").binary == tmp_path / "given"


def test_p2p_slicer_without_core(project, tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "_native_api", lambda: None)
    assert not backend_available("p2p_slicer")
    assert not backend_available("no_such_slicer")
    s = demo_settings(5).model_copy(update={"slicer": "p2p_slicer"})
    r = slice_check(project, s, 4)
    assert not r.ok and r.slicer == "p2p_slicer"
    assert r.errors == ["p2p_slicer is not installed"]
    assert isinstance(get_backend("p2p_slicer"), NativeSlicerBackend)


def _slice_result():
    """A SliceResult shaped like docs/research/native-slicer.md 13.4 (time, usage, warnings)."""
    return SimpleNamespace(
        time=SimpleNamespace(total_s=1864.4, prepare_s=308.7,
                             per_role_s={"Flush": 663.3, "Prime tower": 294.8}),
        usage=SimpleNamespace(
            per_slot={
                1: {"model_g": 0.002, "support_g": 0.0, "tower_g": 0.178, "flush_g": 0.666,
                    "total_g": 0.846, "total_m": 0.28},
                2: {"total_g": 0.0},
                5: {"model": {"g": 2.877}, "tower": {"g": 1.001}, "flush": {"g": 0.0},
                    "total": {"g": 3.878, "m": 1.30}},
            },
            filament_changes=23, ams_swaps=6, nozzle_switches=18),
        warnings=[SimpleNamespace(text="It seems object keychain has floating regions. "
                                       "Please re-orient the object or enable support.")])


def test_native_numbers_from_slice_result():
    n = native_numbers(_slice_result())
    assert n.print_time_s == 1864.4 and n.prepare_time_s == 308.7
    assert n.model_time_s == pytest.approx(1555.7)
    assert [f.slot for f in n.filaments] == [1, 5]  # slot 2 printed nothing
    assert n.filaments[1].model_grams == 2.877 and n.filaments[1].meters == 1.3
    assert n.tower_grams == pytest.approx(1.18) and n.purge_grams == pytest.approx(1.845, abs=0.01)
    assert (n.filament_changes, n.ams_swaps, n.nozzle_changes) == (23, 6, 18)
    assert n.feature_time_s["Flush"] == 663.3
    assert "floating regions" in n.warnings[0]


def test_p2p_slicer_dispatch(project, tmp_path, monkeypatch):
    calls = []

    def slice_project(path, *, out_dir=None, gcode=False, overrides=None):
        calls.append((Path(path), out_dir, gcode))
        return _slice_result()

    monkeypatch.setattr(sl, "_native_api", lambda: SimpleNamespace(slice_project=slice_project))
    s = demo_settings(5).model_copy(update={"slicer": "p2p_slicer"})
    fids = [f.id for f in jackson_palette()]
    r = slice_check(project, s, 2, filament_ids=fids, out_dir=tmp_path, min_paint_width_mm=0)
    assert calls and calls[0][0] == project and calls[0][2] is True
    assert r.ok, r.errors
    assert r.slicer == "p2p_slicer"
    assert r.readback_ok is None  # nothing re-saved, nothing to read back
    assert r.print_time_s == 1864.4 and r.ams_swaps == 6
    assert [f.filament_id for f in r.filaments] == [fids[0], fids[4]]
    assert any("floating regions" in w for w in r.warnings)
    assert (tmp_path / "demo.slice.log").read_text().count("[warning]") == 1


def test_p2p_slicer_crash_is_a_failed_gate(project, tmp_path, monkeypatch):
    def boom(path, **kw):
        raise RuntimeError("open mesh")

    monkeypatch.setattr(sl, "_native_api", lambda: SimpleNamespace(slice_project=boom))
    s = demo_settings(5).model_copy(update={"slicer": "p2p_slicer"})
    r = slice_check(project, s, 2, out_dir=tmp_path, min_paint_width_mm=0)
    assert not r.ok and "p2p_slicer failed: RuntimeError: open mesh" in r.errors[0]


# ---------------------------------------------------------------- truth set (gitignored)

REPO = Path(__file__).resolve().parents[2]
TRUTH = Path(os.environ.get("P2P_SLICER_TRUTH") or REPO / "slicer/calibration/_truth")
TRUTH_JSON = REPO / "slicer/calibration/truth.json"
needs_truth = pytest.mark.skipif(not (TRUTH / "keychain_4c_020" / "sliced.gcode.3mf").exists(),
                                 reason="slicer/calibration/_truth not present")


@needs_truth
@pytest.mark.parametrize("case", ["keychain_4c_020", "keychain_1c_020", "nameplate_5c_020",  # gitleaks:allow (case id, not a secret)
                                  "figurine_4c_adaptive", "keychain_thin_3c_020"])
def test_truth_replay(case, project, tmp_path):
    t = json.loads(TRUTH_JSON.read_text())["cases"][case]
    d = TRUTH / case
    backend = ReplayBackend(d / "sliced.gcode.3mf", json.loads((d / "result.json").read_text()))
    r = slice_check(project, demo_settings(5), None, backend=backend, out_dir=tmp_path,
                    verify_readback=False, min_paint_width_mm=0)
    tot = t["totals"]
    assert r.ok, r.errors
    assert r.print_time_s == tot["print_time_s_slice_info"]
    assert r.model_time_s == tot["model_time_s_header"]
    assert r.ams_swaps == tot["filament_change_times_result_json"]
    assert r.filament_changes == tot["filament_changes"]
    assert r.nozzle_changes == tot["nozzle_changes"]
    assert r.tower_grams == pytest.approx(tot["tower_g"], abs=0.01)
    assert r.purge_grams == pytest.approx(tot["purge_g"], abs=0.01)
    assert sum(f.flush_grams for f in r.filaments) == pytest.approx(tot["flush_g"], abs=0.01)
    for f in r.filaments:
        assert f.model_grams == pytest.approx(t["filaments"][str(f.slot)]["model_g"], abs=1e-3)
        assert f.tower_grams == pytest.approx(t["filaments"][str(f.slot)]["gcode_tower_g"],
                                              abs=2e-3)
