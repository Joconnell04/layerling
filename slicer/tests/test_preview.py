"""Toolpath preview files (contracts PaintPreview toolpaths, version 1) from a real native slice."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from p2p_slicer.moves import ROLE_NAMES, Role

INPUTS = Path(__file__).resolve().parents[1] / "calibration" / "inputs"
CASE = INPUTS / "nameplate_5c_020.3mf"


@pytest.fixture(scope="module")
def sliced():
    from p2p_slicer.api import slice_project

    return slice_project(CASE)


@pytest.mark.skipif(not CASE.exists(), reason="calibration input missing")
def test_toolpaths_round_trip(sliced, tmp_path):
    from p2p_slicer.preview import extrusion_segments, read_toolpaths, write_toolpaths

    slots = [{"slot": s, "filament_id": f"f{s}", "hex": "#808080"} for s in range(1, 6)]
    index = write_toolpaths(sliced.moves, tmp_path, slots=slots,
                            per_layer_s=sliced.analysis.time.per_layer_s)
    assert json.loads((tmp_path / "toolpaths.json").read_text()) == index
    index, pts, chains = read_toolpaths(tmp_path / "toolpaths.json")
    assert index["version"] == 1 and index["roles"] == list(ROLE_NAMES)
    assert index["slots"] == slots
    # one entry per printed layer, in order, chains back to back
    assert len(index["layers"]) == sliced.analysis.layer_count
    zs = [lay["z"] for lay in index["layers"]]
    assert zs == sorted(zs)
    first = 0
    for lay in index["layers"]:
        assert lay["chains"][0] == first and lay["chains"][1] > 0
        first += lay["chains"][1]
    assert first == len(chains)
    # every extrusion is in: the drawn length matches the segments within the 0.01 mm grid
    seg = extrusion_segments(sliced.moves)
    want = float(np.hypot(seg["x1"] - seg["x0"], seg["y1"] - seg["y0"]).sum())
    got = 0.0
    for p0, n, _ in chains:
        p = pts[p0:p0 + n]
        got += float(np.hypot(*np.diff(p, axis=0).T).sum())
    assert got == pytest.approx(want, rel=2e-3)
    # points stay on the plate and inside the bbox
    x0, y0, x1, y1 = index["bbox_mm"]
    assert pts[:, 0].min() >= x0 - 0.01 and pts[:, 0].max() <= x1 + 0.01
    assert pts[:, 1].min() >= y0 - 0.01 and pts[:, 1].max() <= y1 + 0.01
    # slot and role packed per chain; the prime tower is in, layer slots leave it out
    slot = chains[:, 2] >> 8
    role = chains[:, 2] & 0xFF
    assert set(slot.tolist()) <= {1, 2, 3, 4, 5}
    assert Role.PRIME_TOWER in set(role.tolist())
    used = sorted({s for lay in index["layers"] for s in lay["slots"]})
    assert used == sliced.analysis.usage.slots_used
    # per-layer seconds add up to the estimate minus the start G-code
    per = sum(lay["time_s"] for lay in index["layers"])
    assert per == pytest.approx(float(sliced.analysis.time.per_layer_s[1:].sum()), rel=1e-3)
    assert sum(lay["changes"] for lay in index["layers"]) == sliced.analysis.usage.filament_changes


@pytest.mark.skipif(not CASE.exists(), reason="calibration input missing")
def test_print_numbers_take_our_filament_ids(sliced):
    from p2p_slicer.preview import print_numbers

    ids = ["a", "b", "c", "d", "e"]
    n = print_numbers(sliced.report, ids, layers=sliced.analysis.layer_count)
    assert n.slicer == "p2p_slicer" and n.layers == sliced.analysis.layer_count
    assert n.print_time_s == sliced.report.print_time_s
    assert n.purge_grams == sliced.report.purge_grams
    assert [f.filament_id for f in n.filaments] == [ids[f.slot - 1] for f in n.filaments]
