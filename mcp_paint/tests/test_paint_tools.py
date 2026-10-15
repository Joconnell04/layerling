"""Every paint tool against the procedural figurine, plus checkpoint and undo round trips."""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest
from p2p_contracts.mcp_tools import (
    TOOLS,
    BoundaryEditIn,
    CheckpointIn,
    DespeckleIn,
    LabelRegionsIn,
    ListRegionsIn,
    MergeRegionsIn,
    PaintRegionIn,
    RenderViewsIn,
    SelectByPixelIn,
    SimulatePrintIn,
    SliceCheckIn,
    SnapToCreasesIn,
    SplitRegionIn,
    UndoIn,
    Write3mfIn,
)
from p2p_contracts.regions import PaintState, RegionMap
from p2p_mcp_paint.session import ToolError


def state_of(s):
    return (s.fr.copy(), dict(s.assignment), list(s.palette), sorted(s.regions))


def same(a, b):
    return np.array_equal(a[0], b[0]) and a[1:] == b[1:]


def test_every_contract_tool_is_a_session_method(session):
    for name in TOOLS:
        if name == "open_job":
            continue
        assert callable(getattr(session, name)), name


def test_initial_state_files_follow_contracts(session):
    d = session.dir
    rm = RegionMap.model_validate_json((d / "region_map.json").read_text())
    ps = PaintState.model_validate_json((d / "paint_state.json").read_text())
    fr = np.load(d / "face_regions.npy")
    assert fr.shape == (session.inp.figure.n_faces,)
    assert {r.id for r in rm.regions} == set(np.unique(fr)) - {0}
    assert set(ps.assignment.values()) <= set(ps.palette)
    assert len(ps.palette) <= session.inp.settings.ams_slots
    assert ps.checkpoint_id == "c000"
    # the planted gray specks were split out of s2.body as their own brown region
    specks = [r for r in rm.regions if r.label == "s2.body_generic_pla_brown"
              or r.label.startswith("s2.body_")]
    assert specks, [r.label for r in rm.regions]


def test_get_state_and_list_regions(session):
    st = session.get_state().out
    assert st.n_regions == len(session.regions) and st.iteration == 0
    eyes = session.list_regions(ListRegionsIn(part="eye")).out.regions
    assert {r.label for r in eyes} >= {"s1.eye_left", "s1.eye_right", "s2.eye_left"}
    assert all(r.area_mm2 > 0 and r.min_width_mm for r in eyes)


def test_paint_region_palette_rules(session):
    rid = next(r.id for r in session.regions.values() if r.label == "s2.nose")
    session.paint_region(PaintRegionIn(region_ids=[rid], filament_id="generic-pla-white"))
    assert session.assignment[rid] == "generic-pla-white"
    with pytest.raises(ToolError):  # not loaded in the palette, no free slot
        session.paint_region(PaintRegionIn(region_ids=[rid],
                                           filament_id="bambu-pla-basic-gray",
                                           add_to_palette=True))
    with pytest.raises(ToolError):
        session.paint_region(PaintRegionIn(region_ids=[99999], filament_id="generic-pla-white"))


def test_merge_split_roundtrip(session):
    a = next(r.id for r in session.regions.values() if r.label == "s1.stripe_1")
    b = next(r.id for r in session.regions.values() if r.label == "s1.stripe_2")
    n = len(session.regions)
    area = session.regions[a].area_mm2 + session.regions[b].area_mm2
    session.merge_regions(MergeRegionsIn(region_ids=[a, b], label="s1.stripes"))
    assert len(session.regions) == n - 1 and b not in session.regions
    assert session.regions[a].area_mm2 == pytest.approx(area, rel=1e-6)
    out = session.split_region(SplitRegionIn(region_id=a, method="connected_components")).out
    assert len(out.changed_region_ids) >= 2
    assert all(i not in (b,) for i in out.changed_region_ids)  # ids never reused
    c = session.regions[a].centroid_mm
    out = session.split_region(SplitRegionIn(region_id=a, method="plane", plane_point_mm=c,
                                             plane_normal=(0, 0, 1))).out
    assert out.n_regions > n - 1


def test_split_geodesic_seed(session):
    r = next(r for r in session.regions.values() if r.label == "s1.body")
    faces = np.nonzero(session.fr == r.id)[0]
    seeds = [int(faces[0]), int(faces[-1])]
    out = session.split_region(SplitRegionIn(region_id=r.id, method="geodesic_seed",
                                             seed_face_ids=seeds)).out
    assert len(out.changed_region_ids) == 2


def test_grow_shrink_smooth_in_mm(session):
    nose = next(r for r in session.regions.values() if r.label == "s2.nose")
    a0 = nose.area_mm2
    session.grow(BoundaryEditIn(region_id=nose.id, mm=0.5))
    a1 = session.regions[nose.id].area_mm2
    assert a1 > a0
    session.shrink(BoundaryEditIn(region_id=nose.id, mm=0.5))
    a2 = session.regions[nose.id].area_mm2
    assert a2 < a1
    with pytest.raises(ToolError):  # would delete the region
        session.shrink(BoundaryEditIn(region_id=nose.id, mm=10))
    body = next(r for r in session.regions.values() if r.label == "s2.body")
    out = session.smooth_boundary(BoundaryEditIn(region_id=body.id, mm=0.5)).out
    assert "changed hands" in out.message


def test_snap_and_despeckle(session):
    from p2p_mcp_paint import regionops

    fig = session.inp.figure
    n0, _ = regionops.islands(fig, session.face_filament(), 1.0)
    out = session.snap_to_creases(SnapToCreasesIn()).out
    assert "on creases" in out.message
    n1, _ = regionops.islands(fig, session.face_filament(), 1.0)
    assert n1 <= n0  # a boundary move never leaves new specks
    out = session.despeckle(DespeckleIn(min_area_mm2=3.0)).out
    assert "->" in out.message
    n, _ = regionops.islands(session.inp.figure, session.face_filament(), 3.0)
    assert n == 0


def test_select_by_pixel(session):
    info = session.select_by_pixel(SelectByPixelIn(view="front", x=512, y=500)).out
    assert info.face_id is not None and info.region_id is not None
    new = session.select_by_pixel(SelectByPixelIn(view="front", x=512, y=500, radius_px=6,
                                                  action="new_region", label="s1.test")).out
    assert new.new_region_id in session.regions
    with pytest.raises(ToolError):
        session.select_by_pixel(SelectByPixelIn(view="front", x=5000, y=5))


def test_render_views_images(session):
    res = session.render_views(RenderViewsIn(kinds=["flat", "overlay", "texture"],
                                             views=["front", "front_3q"], size_px=256))
    assert len(res.out.images) == 6
    # model-facing sheets are JPEG (the PNG stays on disk)
    assert len(res.images) == 3 and all(img[:2] == b"\xff\xd8" for _, img in res.images)
    for meta in res.out.images:
        assert (session.job_dir / meta["path"]).is_file()
    crops = session.render_views(RenderViewsIn(kinds=["crops"], region_ids=[9, 13]))
    assert [m["region_id"] for m in crops.out.images] == [9, 13]


def test_simulate_print_returns_report_and_renders(session):
    res = session.simulate_print(SimulatePrintIn())
    rep = res.out
    assert rep.layers > 0 and rep.purge_pct > 0
    assert (session.job_dir / rep.face_filaments.path).is_file()
    assert len(res.images) == 2 and len(rep.images) == 8  # every proof view
    again = session.simulate_print(SimulatePrintIn(render=False))
    assert again.out.purge_pct == rep.purge_pct  # cached on unchanged paint


def test_checkpoint_undo_roundtrip(session):
    s0 = state_of(session)
    body = next(r for r in session.regions.values() if r.label == "s2.body")
    session.grow(BoundaryEditIn(region_id=body.id, mm=1.0))
    session.paint_region(PaintRegionIn(region_ids=[body.id], filament_id="generic-pla-black"))
    s1 = state_of(session)
    assert not same(s0, s1)
    out = session.undo(UndoIn()).out  # dirty: back to the last checkpoint (c000)
    assert out.checkpoint_id == "c000" and same(state_of(session), s0)
    # checkpoint an edit, edit again, undo twice
    session.grow(BoundaryEditIn(region_id=body.id, mm=1.0))
    cp = session.checkpoint(CheckpointIn(note="grown")).out
    assert cp.checkpoint_id == "c001" and cp.iteration == 1
    s_c1 = state_of(session)
    session.paint_region(PaintRegionIn(region_ids=[body.id], filament_id="generic-pla-black"))
    session.undo(UndoIn())
    assert same(state_of(session), s_c1)
    session.undo(UndoIn())  # clean: the checkpoint before
    assert same(state_of(session), s0)
    session.undo(UndoIn(checkpoint_id="c001"))
    assert same(state_of(session), s_c1)
    with pytest.raises(ToolError):
        session.undo(UndoIn(checkpoint_id="c999"))
    # the files on disk follow the state
    assert np.array_equal(np.load(session.dir / "face_regions.npy"), session.fr)
    assert json.loads((session.dir / "session.json").read_text())["checkpoints"][1]["id"] == "c001"


def test_resume_from_disk(session):
    from p2p_mcp_paint.session import PaintSession

    body = next(r for r in session.regions.values() if r.label == "s2.body")
    session.grow(BoundaryEditIn(region_id=body.id, mm=0.5))
    session.checkpoint(CheckpointIn(note="x"))
    again = PaintSession.resume(session.inp, session.dir, session.attempt)
    assert same(state_of(again), state_of(session))
    assert again.checkpoint_id == session.checkpoint_id


def test_run_checklist_items(session):
    cl = session.run_checklist().out
    ids = [i.id for i in cl.items]
    assert ids == ["spec_coverage", "face_parts", "markings", "islands", "creases",
                   "slot_count", "sim_matches_intent", "purge_under_40pct"]
    purge = cl.items[-1]
    assert purge.passed is None and "not a gate" in purge.evidence
    assert next(i for i in cl.items if i.id == "sim_matches_intent").by == "model"
    assert cl.passed_all is False  # the model item is open until sign-off


def test_write_3mf_reads_back(session):
    from p2p_threemf import read_3mf

    out = session.write_3mf(Write3mfIn(name="final")).out
    p = session.job_dir / out.path
    _v, f, slots, _colours = read_3mf(p)
    assert out.n_faces == len(f) == session.inp.figure.n_faces
    assert np.array_equal(slots, session.face_slots())
    assert out.slots_used == len(np.unique(session.face_slots()))


def test_slice_check_needs_a_file(session):
    with pytest.raises(ToolError):
        session.slice_check(SliceCheckIn())


def test_label_regions_mock_refines(session):
    before = set(session.regions)
    out = session.label_regions(LabelRegionsIn(views=["front", "front_3q"])).out
    assert {r.id for r in out.regions} >= before - {0} or out.regions


def test_mcp_server_lists_contract_tools_and_returns_images(session):
    from p2p_mcp_paint.server import Holder, build_server

    holder = Holder(None, "mock")
    holder.session = session
    server = build_server(holder)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == set(TOOLS)
    for name, (in_model, _) in TOOLS.items():
        props = set(tools[name].input_schema.get("properties", {}))
        want = set(in_model.model_fields) if in_model else set()
        assert props == want, name
    res = asyncio.run(server.call_tool("render_views", {"kinds": ["flat"], "views": ["front"],
                                                        "size_px": 128}))
    content = res.content if hasattr(res, "content") else res
    kinds = [c.type for c in content]
    assert "image" in kinds and kinds[0] == "text"
    res = asyncio.run(server.call_tool("paint_region", {"region_ids": [99999],
                                                        "filament_id": "generic-pla-white"}))
    content = res.content if hasattr(res, "content") else res
    assert content[0].text.startswith("refused:")
