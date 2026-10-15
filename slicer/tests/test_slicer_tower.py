"""Prime tower: size and place the tower from the changes in Bambu's own G-code, and compare the
bounding box (with brim) with Bambu's plate_1.json, then check the emitted rows."""

import json
import zipfile

import numpy as np
import pytest
from p2p_slicer import tower as T
from p2p_slicer.config import load_project_config, slice_config
from p2p_slicer.gcode_read import read_gcode
from p2p_slicer.layers import LayerPlan
from p2p_slicer.moves import Flag, Kind, MoveBuilder, PathWriter, Role
from slicer_testutil import CASES, REPO, TRUTH_DIR, needs_truth

TOWER_CASES = ["keychain_4c_020", "keychain_4c_008", "nameplate_5c_020", "geo_keychain_3c_020"]  # gitleaks:allow (case id, not a secret)


def _load(cid):
    c = next(c for c in CASES if c["id"] == cid)
    folder = TRUTH_DIR / cid
    gc = json.loads((folder / "gcode_config.json").read_text())
    fmap = [int(v) for v in gc["filament_map"].split(",")]
    cfg = slice_config(load_project_config(REPO / c["input"]), filament_map=fmap)
    mt = read_gcode((folder / "plate_1.gcode").read_bytes())
    lz = np.array(mt.meta["layer_z"], float)
    lh = np.array(mt.meta["layer_h"], float)
    plan = LayerPlan(print_z=lz, height=lh, slice_z=lz - lh / 2)
    pj = json.loads(zipfile.ZipFile(folder / "sliced.gcode.3mf").read("Metadata/plate_1.json"))
    bb = next(o["bbox"] for o in pj["bbox_objects"] if o["name"] == "wipe_tower")
    return cfg, mt, plan, bb


def test_cross_section_is_bambus_rounded_rectangle():
    assert T.area(0.5, 0.2) == pytest.approx(0.2 * (0.5 - 0.2 * (1 - np.pi / 4)))
    assert T.e_per_mm(0.5, 0.2) == pytest.approx(T.area(0.5, 0.2) / T.FILAMENT_AREA)


@needs_truth
@pytest.mark.parametrize("cid", TOWER_CASES)
def test_tower_bbox_matches_bambu(cid):
    cfg, mt, plan, bb = _load(cid)
    tp = T.plan_tower(T.changes_from_moves(mt), plan, cfg,
                      layer_slots=T.layer_slots_from_moves(mt))
    assert tp.enabled
    assert max(abs(a - b) for a, b in zip(tp.bbox, bb)) < 0.05


@needs_truth
def test_tower_rows_prime_every_change():
    cid = "keychain_4c_020"
    cfg, mt, plan, _ = _load(cid)
    ch = T.changes_from_moves(mt)
    tp = T.plan_tower(ch, plan, cfg, layer_slots=T.layer_slots_from_moves(mt))
    noz = mt.meta["nozzle_of"]
    mb = MoveBuilder(meta={"nozzle_of": dict(noz)})
    pw = PathWriter(mb, noz)
    first = ch[0].old
    pw.st.slot, pw.st.nozzle = first, noz.get(first, 0)
    by_layer: dict[int, list] = {}
    for c in ch:
        by_layer.setdefault(c.layer, []).append(c)
    seen = []

    def template(c):
        seen.append(c)
        pw.st.slot, pw.st.nozzle = c.new, noz.get(c.new, 0)

    for L in range(tp.last_layer + 1):
        pw.new_layer(float(plan.print_z[L]), float(plan.height[L]))
        T.tower_layer(tp, L, by_layer.get(L, []), pw, cfg, slot=pw.st.slot, template=template)
    r = mb.build().rows
    assert len(seen) == len(ch)
    tower = (r["flags"] & Flag.TOWER) != 0
    ext = tower & (r["kind"] == Kind.EXTRUDE) & (r["role"] == Role.PRIME_TOWER)
    assert ext.any()
    # every tower extrusion stays inside the tower's bounding box
    x0, y0, x1, y1 = tp.bbox
    assert (r["x"][ext] >= x0 - 0.01).all() and (r["x"][ext] <= x1 + 0.01).all()
    assert (r["y"][ext] >= y0 - 0.01).all() and (r["y"][ext] <= y1 + 0.01).all()
