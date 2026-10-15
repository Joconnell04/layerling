"""Studio paint edits on the procedural figurine: face ranges, fill, brush, replace slot, the
palette rules, seeding a session from a finished paint, and the print preview (sim, 3MF,
p2p_slicer numbers and toolpaths, proof views)."""

from __future__ import annotations

import json

import numpy as np
import pytest
from p2p_contracts.mcp_tools import PaintOpsIn, PreviewPrintIn
from p2p_contracts.payloads import PaintEdit, PaintOp, PaintPreview
from p2p_contracts.regions import PaintState, RegionMap
from p2p_mcp_paint.edits import (
    apply_ops,
    decode_faces,
    encode_faces,
    preview,
    seed,
)
from p2p_mcp_paint.session import PaintSession, ToolError

BLACK, WHITE, BROWN = "generic-pla-black", "generic-pla-white", "generic-pla-brown"
GRAY = "bambu-pla-basic-gray"


def region(s, label: str) -> int:
    return next(r.id for r in s.regions.values() if r.label == label)


def test_face_ranges_round_trip():
    ids = [0, 1, 2, 3, 7, 9, 10, 11, 40]
    text = encode_faces(ids)
    assert text == "0-3,7,9-11,40"
    assert np.nonzero(decode_faces(text, 50))[0].tolist() == ids
    assert encode_faces([5, 5, 4]) == "4-5" and encode_faces([]) == ""
    assert not decode_faces("", 10).any()
    for bad in ("1-", "a", "3-1", "1,,2", "1 ,2"):
        with pytest.raises(ToolError):
            decode_faces(bad, 10)
    with pytest.raises(ToolError, match="outside the figure"):
        decode_faces("5-10", 10)


def test_contract_rules():
    with pytest.raises(ValueError, match="exactly one of filament_id and slot"):
        PaintOp(op="fill_region", region_ids=[1])
    with pytest.raises(ValueError):
        PaintOp(op="fill_region", region_ids=[1], slot=1, filament_id=BLACK)
    with pytest.raises(ValueError, match="needs region_ids"):
        PaintOp(op="fill_region", slot=1)
    with pytest.raises(ValueError, match="faces and faces_path"):
        PaintOp(op="brush_faces", slot=1)
    with pytest.raises(ValueError):
        PaintOp(op="brush_faces", slot=1, faces="1-2, 3")
    with pytest.raises(ValueError, match="from_slot"):
        PaintOp(op="replace_slot", filament_id=BLACK)
    with pytest.raises(ValueError):
        PaintEdit(ops=[], base_attempt="s07_baseline/a1")
    PaintEdit(ops=[], base_attempt="s09_paint/a12")


def test_fill_brush_and_replace(session):
    s = session
    nose = region(s, "s2.nose")
    body = region(s, "s2.body")
    nxt = s.next_id
    fr0 = s.fr.copy()
    fil0 = s.face_filament().copy()
    stroke = np.nonzero(fr0 == body)[0][:40]
    ops = [
        PaintOp(op="fill_region", region_ids=[nose], filament_id=WHITE),
        PaintOp(op="brush_faces", faces=encode_faces(stroke), filament_id=BROWN,
                label="s2.patch_brushed"),
        PaintOp(op="fill_region", region_ids=[nxt], slot=s.palette.index(BLACK) + 1),
    ]
    res = apply_ops(s, ops)
    assert s.assignment[nose] == WHITE
    assert res.new_regions == [nxt] and s.next_id == nxt + 1
    new = s.regions[nxt]
    assert new.label == "s2.patch_brushed" and new.source == "manual" and body in new.parent_ids
    assert (s.fr[stroke] == nxt).all() and s.assignment[nxt] == BLACK  # the fill used slot
    assert res.changed_faces == int((s.face_filament() != fil0).sum()) > 40
    assert set(res.changed_regions) >= {nose, nxt}
    # the saved files are the edited paint
    ps = PaintState.model_validate_json((s.dir / "paint_state.json").read_text())
    assert ps.assignment[str(nxt)] == BLACK
    assert np.array_equal(np.load(s.dir / "face_regions.npy"), s.fr)

    # replace a slot: every region painted brown takes gray (unowned -> source any)
    brown_regions = [r for r, f in s.assignment.items() if f == BROWN]
    k = s.palette.index(BROWN) + 1
    with pytest.raises(ToolError, match="op 1 .replace_slot.*not an owned spool"):
        apply_ops(s, [PaintOp(op="replace_slot", from_slot=k, filament_id=GRAY)])
    res = apply_ops(s, [PaintOp(op="replace_slot", from_slot=k, filament_id=GRAY)],
                    source="any")
    assert s.palette[k - 1] == GRAY and BROWN not in s.palette
    assert all(s.assignment[r] == GRAY for r in brown_regions)
    # replacing into a filament already in the palette drops the slot
    n = len(s.palette)
    apply_ops(s, [PaintOp(op="replace_slot", from_slot=s.palette.index(GRAY) + 1,
                          filament_id=BLACK)])
    assert len(s.palette) == n - 1 and GRAY not in s.palette


def test_palette_rules_and_errors(session):
    s = session
    nose = region(s, "s2.nose")
    with pytest.raises(ToolError, match="op 1 .fill_region.: no region 99999"):
        apply_ops(s, [PaintOp(op="fill_region", region_ids=[99999], filament_id=BLACK)])
    with pytest.raises(ToolError, match="unknown filament"):
        apply_ops(s, [PaintOp(op="fill_region", region_ids=[nose], filament_id="nope")])
    with pytest.raises(ToolError, match="slot 9"):
        apply_ops(s, [PaintOp(op="fill_region", region_ids=[nose], slot=9)])
    # the palette is full (5 of 5): painting every red region white frees a slot
    assert len(s.palette) == s.inp.settings.ams_slots
    red = [r for r, f in s.assignment.items() if f == "bambu-pla-basic-red"]
    apply_ops(s, [PaintOp(op="fill_region", region_ids=red, filament_id=WHITE)])
    assert len(s.palette) == s.inp.settings.ams_slots - 1
    # a catalog color outside the auto-pick lines is still a filament the editor may use
    silk = "bambu-pla-silk-gold"
    apply_ops(s, [PaintOp(op="fill_region", region_ids=[nose], filament_id=silk)],
              source="any")
    assert s.assignment[nose] == silk and silk in s.palette
    # every AMS slot taken: a sixth color is refused
    free = s.inp.settings.ams_slots - len(s.palette)
    extra = ["bambu-pla-basic-orange", "bambu-pla-basic-cyan", "bambu-pla-basic-purple",
             "bambu-pla-basic-pink", "bambu-pla-basic-yellow"][:free + 1]
    ids = sorted(s.regions)[:len(extra)]
    ops = [PaintOp(op="fill_region", region_ids=[r], filament_id=f)
           for r, f in zip(ids, extra, strict=True)]
    with pytest.raises(ToolError, match="no free AMS slot"):
        apply_ops(s, ops, source="any")
    # an empty or all-out-of-range stroke
    with pytest.raises(ToolError, match="outside the figure"):
        apply_ops(s, [PaintOp(op="brush_faces", faces=f"{s.inp.figure.n_faces}",
                              filament_id=BLACK)])


def test_unused_filaments_leave_the_palette(session):
    s = session
    used_brown = [r for r, f in s.assignment.items() if f == BROWN]
    assert used_brown and BROWN in s.palette
    apply_ops(s, [PaintOp(op="fill_region", region_ids=used_brown, filament_id=WHITE)])
    assert BROWN not in s.palette


def test_seed_from_a_finished_paint(session, tmp_path):
    s = session
    nose = region(s, "s2.nose")
    apply_ops(s, [PaintOp(op="fill_region", region_ids=[nose], filament_id=WHITE)])
    s2 = PaintSession(s.inp, s.dir.parent / "a2", 2)
    seed(s2, s.dir / "paint_state.json")
    assert np.array_equal(s2.fr, s.fr) and s2.assignment == s.assignment
    assert s2.palette == s.palette and s2.next_id == s.next_id
    assert s2.checkpoints[0]["note"].startswith("base: ")
    rm = RegionMap.model_validate_json((s2.dir / "region_map.json").read_text())
    assert rm.face_regions.path.endswith("a2/face_regions.npy")
    # a paint on another mesh is refused
    bad = tmp_path / "bad"
    bad.mkdir()
    np.save(bad / "face_regions.npy", np.zeros(10, np.int32))
    rm2 = rm.model_copy(update={"face_regions": rm.face_regions.model_copy(
        update={"path": str(bad / "face_regions.npy")})})
    (bad / "region_map.json").write_text(rm2.model_dump_json())
    ps = PaintState.model_validate_json((s.dir / "paint_state.json").read_text())
    ps2 = ps.model_copy(update={"region_map": ps.region_map.model_copy(
        update={"path": str(bad / "region_map.json")})})
    (bad / "paint_state.json").write_text(ps2.model_dump_json())
    with pytest.raises(ToolError, match="indexes"):
        seed(PaintSession(s.inp, s.dir.parent / "a3", 3), bad / "paint_state.json")


def test_preview_numbers_toolpaths_and_proofs(session):
    s = session
    out = s.dir / "preview"
    first = preview(s, out, proof=False)
    assert first.after is not None and first.after.slicer == "p2p_slicer"
    assert first.after.print_time_s > 0 and first.after.total_grams > 0
    assert [f.filament_id for f in first.after.filaments] == [
        s.palette[f.slot - 1] for f in first.after.filaments]
    assert first.sim.purge_g_est >= 0 and first.threemf.endswith("final.3mf")
    idx = json.loads((s.job_dir / first.toolpaths).read_text())
    assert idx["version"] == 1 and idx["layers"] and len(idx["slots"]) == len(s.palette)
    assert first.proof_images == []

    body = region(s, "s2.body")
    res = apply_ops(s, [PaintOp(op="fill_region", region_ids=[body], filament_id=BLACK)])
    second = preview(s, out, edit=res, before=first.after, base_attempt="s09_paint/a1")
    assert second.before == first.after and second.ops == 1
    assert second.changed_faces == res.changed_faces > 0
    kinds = {(p.kind, p.view) for p in second.proof_images}
    assert {k for k, _ in kinds} == {"paint", "print"} and len(kinds) == 16
    for p in second.proof_images:
        assert (s.job_dir / p.path).is_file() and p.path.endswith(".jpg")
    PaintPreview.model_validate_json(second.model_dump_json())


def test_mcp_tools_apply_and_preview(session):
    s = session
    nose = region(s, "s2.nose")
    st = s.apply_paint_ops(PaintOpsIn(ops=[PaintOp(op="fill_region", region_ids=[nose],
                                                   filament_id=WHITE)])).out
    assert nose in st.changed_region_ids and "faces changed" in st.message
    res = s.preview_print(PreviewPrintIn(proof=False))
    out: PaintPreview = res.out
    assert out.ops == 1 and out.after is not None
    assert (s.dir / "preview" / "paint_preview.json").is_file()
    again = s.preview_print(PreviewPrintIn(proof=False, toolpaths=False)).out
    assert again.before == out.after and again.ops == 0 and again.toolpaths is None


def test_bootstrap_makes_a_region_per_patch(session):
    """A thin-slice job has no paint map: every connected same-filament patch of the baseline
    becomes a region, the largest first."""
    from p2p_mcp_paint.edits import bootstrap

    s = PaintSession(session.inp, session.dir.parent / "a5", 5)
    bootstrap(s)
    base = np.asarray(s.inp.baseline_filaments).astype(str)
    fil = s.face_filament().astype(str)
    assert (fil == base).all()  # the paint is the baseline, face for face
    assert s.regions[1].label == "s1.body"
    areas = [s.regions[r].area_mm2 for r in sorted(s.regions)]
    assert areas == sorted(areas, reverse=True)
    n_fils = len(set(base.tolist()))
    assert len(s.regions) > n_fils  # patches, not one region per color
    for rid in s.regions:
        assert len(set(base[s.fr == rid].tolist())) == 1
    assert set(s.palette) == set(base.tolist())
