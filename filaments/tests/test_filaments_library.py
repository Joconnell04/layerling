import json
import shutil
from datetime import UTC, datetime

import pytest
from p2p_contracts import FilamentLibrary
from p2p_filaments.library import DEFAULT_LIBRARY, _dump, ams_palette, load, write_measured


@pytest.fixture
def lib_copy(tmp_path):
    dst = tmp_path / "library.json"
    shutil.copyfile(DEFAULT_LIBRARY, dst)
    return dst


def test_real_library_loads_and_dumps_byte_identical():
    text = DEFAULT_LIBRARY.read_text()
    assert _dump(json.loads(text)) == text  # our writer keeps Jackson's formatting
    lib = load(DEFAULT_LIBRARY)
    assert [f.ams_slot for f in ams_palette(lib)] == [1, 2, 3, 4, 5]


def test_write_measured_touches_only_lab_source_td(lib_copy):
    before = json.loads(lib_copy.read_text())
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    changes = write_measured(lib_copy, {
        "bambu-pla-basic-red": {"lab": (46.123, 58.456, 38.0), "td_mm": 1.7654},
        "generic-pla-white": {"lab": (91.5, -0.004, 2.5), "td_mm": None},
    }, now=now)
    after = json.loads(lib_copy.read_text())
    FilamentLibrary.model_validate(after)
    assert after["updated_at"] == "2026-09-24T12:00:00Z"
    assert {k: v for k, v in after.items() if k not in ("updated_at", "filaments")} == \
        {k: v for k, v in before.items() if k not in ("updated_at", "filaments")}
    for b, a in zip(before["filaments"], after["filaments"], strict=True):
        assert list(a) == list(b)  # same keys, same order
        for key in b:
            if key in ("lab", "lab_source", "td_mm") and b["id"] in changes:
                continue
            assert a[key] == b[key], (b["id"], key)
    red = next(f for f in after["filaments"] if f["id"] == "bambu-pla-basic-red")
    assert red["lab"] == [46.12, 58.46, 38.0]
    assert red["lab_source"] == "measured" and red["td_mm"] == 1.77
    white = next(f for f in after["filaments"] if f["id"] == "generic-pla-white")
    assert white["lab"] == [91.5, 0.0, 2.5] and white["td_mm"] is None  # None leaves td alone
    assert changes["bambu-pla-basic-red"]["before"]["lab_source"] == "nominal"


def test_write_measured_rejects_unknown_ids_and_keeps_file(lib_copy):
    text = lib_copy.read_text()
    with pytest.raises(KeyError):
        write_measured(lib_copy, {"no-such-filament": {"lab": (1, 2, 3)}})
    assert lib_copy.read_text() == text


def test_second_write_round_trips(lib_copy):
    write_measured(lib_copy, {"generic-pla-black": {"lab": (22.0, 0.5, -0.8), "td_mm": 0.19}})
    first = lib_copy.read_text()
    assert _dump(json.loads(first)) == first
    lib = load(lib_copy)
    black = next(f for f in lib.filaments if f.id == "generic-pla-black")
    assert black.lab == (22.0, 0.5, -0.8) and black.td_mm == 0.19
