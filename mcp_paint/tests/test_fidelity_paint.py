"""Fidelity rules in the paint stack (docs/runs/fidelity/DEEP-DIVE.md items 6, 7, 10): the full
catalog when the job allows it, face-part colors from the texture, the final print checked
against the approved concept from every side."""

from __future__ import annotations

import asyncio
import io
import json

import numpy as np
import pytest
from p2p_contracts import SliceReport
from p2p_contracts.mcp_tools import PaintRegionIn
from p2p_mcp_paint import checklist
from p2p_mcp_paint.agent import StubDriver, run_paint
from p2p_mcp_paint.concept_check import StubJudge, concept_loop, parse_verdict
from p2p_mcp_paint.job import PartMesh, load_fixture, load_library
from p2p_mcp_paint.session import PaintSession, ToolError
from p2p_sim.fixtures import box_mesh
from PIL import Image

PINK = "bambu-pla-matte-sakura-pink"


def _png(color=(200, 200, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, format="PNG")
    return buf.getvalue()


def _rid(s, label):
    return next(r.id for r in s.regions.values() if r.label == label)


def fake_slice(s):
    return SliceReport(ok=True, slicer="bambu_studio", slicer_version="stub",
                       printer_profile="x", process_profile="y", print_time_s=60.0,
                       total_grams=5.0, purge_grams=1.2)


def test_catalog_is_every_auto_pick_color():
    owned, catalog = load_library()
    ids = {f.id for f in catalog}
    assert PINK in ids and len(catalog) > 40  # was the 16-color example library
    assert not {f.id for f in owned} & ids
    assert all(f.line in ("PLA Basic", "PLA Matte") for f in catalog)


def test_owned_job_never_paints_an_unowned_color(session):
    rid = _rid(session, "s1.nose")
    session.inp.settings = session.inp.settings.model_copy(update={"ams_slots": 8})
    with pytest.raises(ToolError, match="owned spools only"):
        session.paint_region(PaintRegionIn(region_ids=[rid], filament_id=PINK,
                                           add_to_palette=True))
    session.inp.palette_source = "any"
    session.paint_region(PaintRegionIn(region_ids=[rid], filament_id=PINK, add_to_palette=True))
    assert session.assignment[rid] == PINK and PINK in session.palette


def test_face_parts_follow_the_texture_not_a_guessed_spec(session):
    """The spec says the nose is black; the model's texture (the concept) says pink. Painting
    it pink passes; painting it by the guessed spec hex fails."""
    rid = _rid(session, "s1.nose")
    session.inp.palette_source = "any"
    session.inp.settings = session.inp.settings.model_copy(update={"ams_slots": 8})
    session.paint_region(PaintRegionIn(region_ids=[rid], filament_id=PINK, add_to_palette=True))
    sim = set(session.sim_summary()["regions_lost"])
    ok = checklist.check_face_parts(session, sim)
    assert "s1.nose" not in ok.evidence.split(". ")[0]
    session.paint_region(PaintRegionIn(region_ids=[rid], filament_id="generic-pla-black"))
    bad = checklist.check_face_parts(session, set(session.sim_summary()["regions_lost"]))
    assert bad.passed is False and "texture" in bad.evidence and "s1.nose" in bad.evidence


def test_spec_coverage_skips_guessed_face_colors(session):
    session.inp.color_spec.append({"subject_id": "s1", "region": "eyes", "color": "brown",
                                   "hex": "#8B4513", "kind": "feature"})
    item = checklist.check_spec(session)
    assert "eyes" not in item.evidence


def test_purge_evidence_uses_the_slicer_when_known(session):
    summ = session.sim_summary()
    session.last_slice = {"ok": True, "purge_grams": 3.64, "total_grams": 6.51}
    item = checklist.check_purge(session, summ)
    assert item.passed is None and item.evidence.startswith("slicer: purge 3.64 g of 6.51 g")


def test_parse_verdict_is_forgiving():
    v = parse_verdict('thinking...\n{"matches": false, "confidence": 0.4, "summary": "x", '
                      '"discrepancies": [{"what": "ears white", "route": "paint_region"}]}')
    assert not v.passed and v.majors[0].route == "paint_region"
    assert parse_verdict("no json").majors  # unreadable counts as a failure


@pytest.fixture()
def plated(tmp_path, figurine):
    """The fixture figurine plus a black plate part (an add-on the check may recolor)."""
    fx, tex, _ = figurine
    owned, catalog = load_library()
    plate = box_mesh((30.0, 30.0, 3.0), spacing=1.0, center=(0, 0, -1.5))
    job_dir = tmp_path / "job-plated"
    job_dir.mkdir()
    from p2p_contracts.regions import Region
    from p2p_sim.fixtures import REGION_LABELS

    regions = [Region(id=rid, label=name, part="other", source="sam3", n_faces=0, area_mm2=0,
                      centroid_mm=(0, 0, 0), bbox_min_mm=(0, 0, 0), bbox_max_mm=(0, 0, 0))
               for rid, (name, _) in REGION_LABELS.items()]
    inp = load_fixture(job_dir, fx.mesh, tex_rgb=tex,
                       parts=[PartMesh("plate", "plate", "generic-pla-black", plate)],
                       library=owned, catalog=catalog)
    return PaintSession.from_inputs(inp, job_dir / "s09_paint" / "a1",
                                    face_regions=fx.face_regions, regions=regions)


def test_concept_loop_fixes_addon_and_region_colors(plated):
    s = plated
    nose = _rid(s, "s2.nose")
    s.paint_region(PaintRegionIn(region_ids=[nose], filament_id="generic-pla-white"))
    judge = StubJudge([
        {"matches": False, "confidence": 0.5, "summary": "plate and nose wrong",
         "discrepancies": [
             {"view": "front", "what": "plate is black, concept plate is white",
              "route": "addon_color", "part": "plate", "target_hex": "#F8F8F8"},
             {"view": "front", "what": "nose is white", "route": "paint_region",
              "region_ids": [nose], "filament_id": "generic-pla-black"},
             {"view": "top", "what": "tiny shade", "route": "paint_region", "severity": "minor"}]},
        {"matches": True, "confidence": 0.9, "summary": "matches", "discrepancies": []}])

    def reload():
        return PaintSession.resume(s.inp, s.dir, s.attempt)

    res = asyncio.run(concept_loop(reload, judge, None, _png(), [], s.dir / "proof" / "check",
                                   lambda p: s.rel(p), log=lambda m: None))
    assert res.passed and len(res.rounds) == 2 and len(judge.calls) == 2
    after = reload()
    assert after.part_filaments["plate"] == "generic-pla-white"
    assert after.assignment[nose] == "generic-pla-black"
    assert res.rounds[0]["applied"] and not res.rounds[0]["blocked"]
    # the judge saw the concept and the print from eight sides
    texts = [b["text"] for b in judge.calls[0] if b["type"] == "text"]
    assert any("SIMULATED PRINT, eight sides" in t for t in texts)
    assert (s.dir / "proof" / "check" / "round1" / "print_back_3q.png").is_file()


def test_geometry_discrepancy_goes_to_review_with_side_by_side(session):
    judge = StubJudge([{"matches": False, "confidence": 0.3, "summary": "wrong name",
                        "discrepancies": [{"view": "front", "what": "name reads Brnco",
                                           "route": "text"}]}])
    driver = StubDriver(lambda: PaintSession.resume(session.inp, session.dir, session.attempt))
    run = asyncio.run(run_paint(session.inp.job_id, session=session, driver=driver,
                                slice_fn=fake_slice, log=lambda m: None, judge=judge,
                                concept_png=_png(), photos=[]))
    assert run.review and "differs from the approved concept" in run.review_reason
    assert run.confidence <= 0.3
    assert run.concept_check["passed"] is False and len(judge.calls) == 1
    assert run.review_images and (session.job_dir / run.review_images[0]).is_file()
    kinds = {(r["kind"], r["view"]) for r in run.proof}
    assert ("print", "back_3q") in kinds and ("paint", "left_3q") in kinds
    assert ("model", "top") in kinds and len([k for k in kinds if k[1] != "sheet"]) == 24
    saved = json.loads((session.dir / "paint_run.json").read_text())
    assert saved["concept_check"]["rounds"][0]["verdict"]["discrepancies"][0]["route"] == "text"


def test_no_concept_means_no_check(session):
    driver = StubDriver(lambda: PaintSession.resume(session.inp, session.dir, session.attempt))
    run = asyncio.run(run_paint(session.inp.job_id, session=session, driver=driver,
                                slice_fn=fake_slice, log=lambda m: None, concept_png=None))
    assert run.concept_check == {} and run.proof
    assert np.isfinite(run.confidence)
