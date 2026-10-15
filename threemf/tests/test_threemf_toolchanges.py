"""'Invalid T command' in the Bambu log: only Bambu's own X2D end G-code may cause it."""

import json
import re
import zipfile

import pytest
from p2p_threemf.demo import demo_settings, jackson_palette
from p2p_threemf.gcode_stats import FIRMWARE_TOOL_COMMANDS, invalid_tool_changes
from p2p_threemf.products import PRODUCTS
from p2p_threemf.profiles import get_preset
from p2p_threemf.slicer import BambuStudioBackend, slice_check
from p2p_threemf.writer import write_3mf
from threemf_testutil import MESHY_FIXTURE, needs_bambu, needs_meshy

INVALID_T = re.compile(r"Invalid T command \(T(\d+)\)")


def test_firmware_unload_commands_come_from_bambus_end_gcode():
    end = get_preset("Bambu Lab X2D 0.4 nozzle").config["machine_end_gcode"]
    found = {int(t) for t in re.findall(r"^\s*T(\d+)", end, re.MULTILINE)}
    assert found == FIRMWARE_TOOL_COMMANDS


def test_invalid_tool_change_detection():
    body = "\n".join(["T0", "G1 X1 E1", "T4", "T65535", "; MACHINE_END_GCODE_START",
                      "T65279", "T65535", "T7"])
    bad = invalid_tool_changes(body, n_filaments=5)
    assert [(t, w) for _, t, w in bad] == [(65535, "print"), (65279, "end_gcode"),
                                          (65535, "end_gcode"), (7, "print")]


def _slice_and_check(path, tmp_path, n_filaments):
    raw = BambuStudioBackend().slice(path, tmp_path / "s", 300)
    assert raw.ok, raw.errors
    log = raw.log_path.read_text(errors="replace")
    logged = [int(t) for t in INVALID_T.findall(log)]
    with zipfile.ZipFile(raw.sliced_3mf) as z:
        gcode = z.read("Metadata/plate_1.gcode").decode("utf-8", "replace")
    bad = invalid_tool_changes(gcode, n_filaments)
    # Every out-of-range tool command is Bambu's firmware unload inside the end G-code ...
    assert all(where == "end_gcode" for _, _, where in bad), bad
    # ... and every "Invalid T command" the slicer logged is one of those, nothing else.
    assert set(logged) <= FIRMWARE_TOOL_COMMANDS
    assert sorted(logged) == sorted(t for _, t, _ in bad)


@needs_bambu
@pytest.mark.parametrize("name", ["keychain", "nameplate"])
def test_no_invalid_tool_commands_from_our_files(tmp_path, name):
    p = PRODUCTS[name]()
    path = tmp_path / f"{name}.3mf"
    write_3mf(p.vertices, p.faces, p.face_slots, jackson_palette(), demo_settings(5), path,
              parts=p.parts)
    _slice_and_check(path, tmp_path, 5)


@needs_bambu
@needs_meshy
def test_no_invalid_tool_commands_after_retarget(tmp_path):
    from p2p_contracts import PrintSettings
    from p2p_threemf import retarget_3mf

    ams = [f.model_copy(update={"ams_slot": i + 1}) for i, f in enumerate(jackson_palette())]
    out = tmp_path / "r.3mf"
    retarget_3mf(MESHY_FIXTURE, None, tmp_path, ams, PrintSettings(), out)
    _slice_and_check(out, tmp_path, 5)
    rep = slice_check(out, PrintSettings(), None)
    assert not any("invalid tool change" in e for e in rep.errors)
    with zipfile.ZipFile(out) as z:
        cfg = json.loads(z.read("Metadata/project_settings.config"))
    assert len(cfg["filament_colour"]) == 5
