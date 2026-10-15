import pytest
from p2p_threemf.flush import flush_matrix, flush_volume, formula
from p2p_threemf.profiles import (
    AppProfiles,
    ProfileError,
    build_template,
    filament_preset_name,
    get_preset,
    load_snapshot,
    process_name,
)
from threemf_testutil import needs_profiles


def test_snapshot_has_x2d_presets():
    snap = load_snapshot()
    assert snap["app_version"] == "02.08.02.61"
    for name in ["Bambu Lab X2D 0.4 nozzle", "0.20mm Standard @BBL X2D",
                 "Bambu PLA Basic @BBL X2D 0.4 nozzle", "Generic PLA @BBL X2D 0.4 nozzle",
                 "Generic PETG @BBL X2D 0.4 nozzle", "Bambu PLA Matte @BBL X2D 0.4 nozzle"]:
        assert name in snap["presets"], name
    m = get_preset("Bambu Lab X2D 0.4 nozzle").config
    assert m["printer_model"] == "Bambu Lab X2D"
    assert m["nozzle_diameter"] == ["0.4", "0.4"]
    assert m["extruder_type"] == ["Direct Drive", "Bowden"]
    assert get_preset("Bambu PLA Basic @BBL X2D 0.4 nozzle").filament_id == "GFA00"
    assert get_preset("Generic PETG @BBL X2D 0.4 nozzle").filament_id == "GFG99"


def test_name_helpers():
    assert process_name(0.2) == "0.20mm Standard @BBL X2D"
    assert process_name(0.16) == "0.16mm Standard @BBL X2D"
    with pytest.raises(ProfileError):
        process_name(0.21)
    assert filament_preset_name("Bambu PLA Basic @BBL X2D") == "Bambu PLA Basic @BBL X2D 0.4 nozzle"
    assert filament_preset_name(None) == "Bambu PLA Basic @BBL X2D 0.4 nozzle"
    with pytest.raises(ProfileError):
        filament_preset_name("Nope PLA @BBL X2D")


@needs_profiles
def test_snapshot_matches_installed_app():
    """Fails when Bambu Studio updates its profiles: rerun refresh-profiles."""
    app = AppProfiles()
    snap = load_snapshot()
    assert snap["vendor_version"] == app.vendor_version
    for name, p in snap["presets"].items():
        assert app.resolve(name).config == p["config"], name


@needs_profiles
def test_flattening_follows_inherits_and_includes():
    app = AppProfiles()
    raw = app.raw("Bambu PLA Basic @BBL X2D 0.4 nozzle")
    assert raw["inherits"] == "Bambu PLA Basic @base"
    cfg = app.resolve("Bambu PLA Basic @BBL X2D 0.4 nozzle").config
    assert cfg["filament_type"] == ["PLA"]  # from fdm_filament_pla, three levels up
    assert len(cfg["filament_extruder_variant"]) == 6  # from the include template
    assert cfg["nozzle_temperature"] == raw["nozzle_temperature"]  # own keys win
    assert "inherits" not in cfg and "name" not in cfg


def test_template_split():
    ref = {"filament_colour": ["#1", "#2", "#3"], "nozzle_diameter": ["0.4", "0.4"],
           "printer_extruder_variant": ["a"] * 6, "pressure_advance": ["0.02"] * 3,
           "curr_bed_type": "Textured PEI Plate", "extruder_ams_count": ["x", "y"],
           "layer_height": "0.2", "filament_type": ["PLA"] * 3}
    t = build_template(ref, preset_keys={"layer_height", "filament_type", "nozzle_diameter",
                                         "printer_extruder_variant"},
                       filament_keys={"filament_type"})
    assert t["per_filament"] == {"pressure_advance": ["0.02"]}
    assert t["fixed"] == {"curr_bed_type": "Textured PEI Plate", "extruder_ams_count": ["x", "y"]}
    assert set(t["keys"]) == set(ref)


# Bambu Studio 02.08.02.61 GUI matrix for Jackson's five filaments (X2D 0.4, dual standard
# dataset), read from a project it saved. Rows: from; columns: to.
GUI_COLOURS = ["#161616", "#7C4B00", "#F72323", "#0D6284", "#FFFFFF"]
GUI_IDS = ["GFL99", "GFL99", "GFA00", "GFG99", "GFL99"]
GUI_MATRIX = [0, 367, 467, 352, 900, 153, 0, 270, 209, 585, 192, 215, 0, 248, 612,
              154, 328, 430, 0, 591, 90, 238, 311, 235, 0]


def test_flush_matrix_matches_bambu_gui():
    m = flush_matrix(GUI_COLOURS, min_flush=92, dataset=1, filament_ids=GUI_IDS)
    assert [int(x) for x in m] == GUI_MATRIX


def test_flush_volume_paths():
    assert flush_volume("#FFFFFF", "#FFFFFF") == 0
    # Measured table (dual standard): black to white 900, white to black 90.
    assert flush_volume("#000000", "#FFFFFF", min_flush=92) == 900
    assert flush_volume("#FFFFFF", "#000000", min_flush=92) == 90
    # No table colour within Delta E 5 of this brown: formula plus the nozzle volume.
    assert flush_volume("#161616", "#7C4B00", min_flush=92) == int(formula("#161616", "#7C4B00") + 92)
    assert 60 <= formula("#FFFFFF", "#000000") < formula("#000000", "#FFFFFF")
    # PETG Basic after PLA Basic has a measured floor of 650.
    assert flush_volume("#FFFFFF", "#F4EE2A", 92, 1, "GFA00", "GFG00") >= 650
