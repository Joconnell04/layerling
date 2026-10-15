"""Filament catalog in the worker: owned-first matching with the unowned penalty, source any,
spool suggestions with a catalog id, and palette_edit on a finished job (recolor, refit,
rejections, the slicer gate)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from conftest import REPO, run
from p2p_contracts import (
    Filament,
    FilamentLibrary,
    JobStatus,
    PaintState,
    StageId,
    StageStatus,
)
from p2p_contracts.payloads import JobResult, SpoolSuggestion
from p2p_worker import printing as pr
from p2p_worker.stages.s08_palette import palette_source
from test_m3 import _job, make_rt, manifest

LIB = FilamentLibrary.model_validate_json((REPO / "filaments/library.json").read_text())
POOL = pr.candidates(LIB)
CATALOG = pr.load_catalog(REPO)


def fil(fid: str, hx: str, owned: bool) -> Filament:
    return Filament(id=fid, brand="Bambu Lab", line="PLA Basic", name=fid, hex=hx,
                    lab=pr.hex_to_lab(hx), owned=owned, spools=1 if owned else 0)


# ---------------------------------------------------------------- matching


def test_catalog_is_the_full_list_and_never_reuses_an_owned_id():
    assert len(CATALOG) > 200 and all(not f.owned for f in CATALOG)
    assert not {f.id for f in CATALOG} & {f.id for f in LIB.filaments}
    auto = pr.auto_pick(CATALOG)
    assert auto and {f.line for f in auto} == set(pr.AUTO_PICK_LINES)


def test_owned_spool_wins_a_near_tie():
    owned = fil("owned-orange", "#FF6A13", True)
    # an unowned color about 1 dE00 closer loses: the penalty is 3 dE00
    target = "#FF6C18"
    near = fil("cat-orange", "#FF6C17", False)
    d_owned = pr.de2000(pr.hex_to_lab(target), owned.lab)
    d_cat = pr.de2000(pr.hex_to_lab(target), near.lab)
    assert d_cat < d_owned < d_cat + pr.UNOWNED_PENALTY_DE
    m = pr.match_colour(target, [owned], None, [near])
    assert m.filament_id == "owned-orange" and not m.unowned
    assert m.delta_e == round(d_owned, 2)  # the real dE00, not the penalized cost


def test_catalog_color_wins_when_clearly_closer():
    owned = [fil("owned-black", "#161616", True), fil("owned-white", "#FFFFFF", True)]
    tan = fil("cat-tan", "#BE8C50", False)
    m = pr.match_colour("#BF8D52", owned, None, [tan])
    assert m.filament_id == "cat-tan" and m.unowned and m.delta_e < 2
    # without the catalog (source owned) nothing changes
    assert pr.match_colour("#BF8D52", owned).filament_id in {"owned-black", "owned-white"}


def test_gray_guard_holds_for_catalog_candidates():
    brownish = fil("cat-brown", "#7C4B00", False)
    m = pr.match_colour("#797979", POOL, None, [brownish])
    assert m.filament_id in ("generic-pla-black", "generic-pla-white")


def test_reduce_palette_caps_the_filament_count():
    pool = [fil("a", "#161616", True), fil("b", "#FFFFFF", True), fil("c", "#F72323", True)]
    hexes = {1: "#171717", 2: "#FEFEFE", 3: "#F52525"}
    share = {1: 0.6, 2: 0.3, 3: 0.1}
    matches = {s: pr.match_colour(h, pool) for s, h in hexes.items()}
    assert {m.filament_id for m in matches.values()} == {"a", "b", "c"}
    out = pr.reduce_palette(matches, share, pool, [], 2)
    assert {m.filament_id for m in out.values()} == {"a", "b"}  # red (10%) merged away
    kept = pr.reduce_palette(matches, share, pool, [], 2, keep={"c"})
    assert {m.filament_id for m in kept.values()} == {"a", "c"}  # parts keep their filament


def test_spool_suggestion_carries_the_catalog_id():
    m = pr.match_colour("#797979", POOL, pr.auto_pick(CATALOG))
    items = [{"source": "meshy", "share": 0.2, "regions": ["s1.body"], **pr.asdict_matches([m])[0]}]
    p = SpoolSuggestion.model_validate(pr.spool_payloads(items)[0])
    assert p.filament_id == m.suggest_id and p.filament_id in {f.id for f in CATALOG}


def test_job_level_source_reads_the_request(tmp_path: Path):
    (tmp_path / "input").mkdir()
    ctx = SimpleNamespace(abs=lambda rel: tmp_path / rel, job=SimpleNamespace(id="j"),
                          store=SimpleNamespace(job_row=lambda _id: {}))
    assert palette_source(ctx, None) == "owned"
    (tmp_path / "input/request.json").write_text(json.dumps({"palette_source": "any"}))
    assert palette_source(ctx, None) == "any"
    assert palette_source(ctx, {"source": "owned"}) == "owned"  # the edit wins
    (tmp_path / "input/request.json").write_text("{}")
    ctx.store = SimpleNamespace(job_row=lambda _id: {"palette_source": "any"})
    assert palette_source(ctx, None) == "any"


# ---------------------------------------------------------------- palette_edit


def tree_hash(d: Path) -> dict[str, str]:
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*")) if p.is_file()}


def edit(rt, job_id: str, **payload) -> str:
    row = rt.store.post_message(job_id, "user", "palette_edit", payload)
    run(rt.orchestrator.advance(job_id))
    return row.id


def notes(rt, job_id: str, reply_to: str) -> list[str]:
    return [m.payload["text"] for m in rt.store.list_messages(job_id, role="assistant")
            if m.kind == "note" and m.reply_to == reply_to]


def state_of(rt, job_id: str, stage: StageId) -> PaintState:
    m = manifest(rt, job_id, stage)
    rel = next(o.path for o in m.outputs if o.role == "paint_state")
    return PaintState.model_validate_json((rt.store.job_dir(job_id) / rel).read_text())


def test_palette_edit_on_a_finished_full_job(settings, photos, fake_slicer):
    rt = make_rt(settings)
    job_id, status, _ = _job(rt, photos[:1], product_type="keychain", profile="full")
    assert status == JobStatus.DONE
    jdir = rt.store.job_dir(job_id)
    old = {s: tree_hash(jdir / s.value / "a1")
           for s in (StageId.S08_PALETTE, StageId.S09_PAINT, StageId.S10_WRITE)}
    before = state_of(rt, job_id, StageId.S09_PAINT)
    credits = rt.store.load_job(job_id).budget.spent_credits
    posts = rt.fake.count("POST")

    # recolor: brown's slot takes a catalog color; regions keep their slots
    slots = [("bambu-pla-matte-desert-tan" if f == "generic-pla-brown" else f)
             for f in before.palette]
    mid = edit(rt, job_id, mode="recolor", slots=slots)
    job = rt.store.load_job(job_id)
    assert job.status == JobStatus.DONE, rt.store.job_row(job_id)
    assert not rt.store.list_messages(job_id, kinds={"palette_edit"}, unconsumed=True)
    for s in (StageId.S08_PALETTE, StageId.S09_PAINT, StageId.S10_WRITE):
        assert job.attempts[s] == 2 and manifest(rt, job_id, s).status == StageStatus.SUCCEEDED
        assert tree_hash(jdir / s.value / "a1") == old[s]  # old attempts never change
    m8 = manifest(rt, job_id, StageId.S08_PALETTE)
    assert m8.data["palette_edit"]["message_id"] == mid
    assert m8.data["palette_edit"]["previous_paint_state"] == "s09_paint/a1/paint_state.json"
    after = state_of(rt, job_id, StageId.S09_PAINT)
    assert after.palette == slots
    swap = dict(zip(before.palette, slots, strict=True))
    assert after.assignment == {r: swap[f] for r, f in before.assignment.items()}
    assert after.region_map == before.region_map
    result = JobResult.model_validate(job.result)
    assert result.threemf == "s10_write/a2/final.3mf" and result.slice_ok
    assert fake_slicer.calls[-1]["filament_ids"] == slots
    kinds = [m.kind for m in rt.store.list_messages(job_id, role="assistant")]
    assert kinds.count("result") == 2
    snap = FilamentLibrary.model_validate_json((jdir / "s08_palette/a2/filaments.json").read_text())
    assert "bambu-pla-matte-desert-tan" in {f.id for f in snap.filaments}
    job = rt.store.load_job(job_id)
    assert job.budget.spent_credits == credits and rt.fake.count("POST") == posts  # no spend

    # a second advance changes nothing (the new attempts are consistent)
    assert run(rt.orchestrator.advance(job_id)) == JobStatus.DONE
    assert rt.store.load_job(job_id).attempts[StageId.S10_WRITE] == 2

    # rejections: a note, the message consumed, no new attempts
    for payload, text in (
        ({"mode": "recolor", "slots": slots[:2]}, f"Recolor needs {len(slots)} slots, got 2"),
        ({"mode": "refit", "slots": ["bambu-pla-basic-nope"]},
         "Unknown filaments: bambu-pla-basic-nope"),
        ({"mode": "refit", "slots": [slots[0], slots[0]]}, "Each filament can take one slot"),
    ):
        mid = edit(rt, job_id, **payload)
        assert notes(rt, job_id, mid) == [text]
        assert rt.store.load_job(job_id).attempts[StageId.S08_PALETTE] == 2
    assert not rt.store.list_messages(job_id, kinds={"palette_edit"}, unconsumed=True)
    assert rt.store.load_job(job_id).status == JobStatus.DONE

    # refit, auto-pick from owned plus catalog, at most 2 colors (keyring part included)
    edit(rt, job_id, mode="refit", source="any", max_colors=2)
    job = rt.store.load_job(job_id)
    assert job.status == JobStatus.DONE and job.attempts[StageId.S08_PALETTE] == 3
    st = state_of(rt, job_id, StageId.S08_PALETTE)
    assert len(st.palette) <= 2
    assert set(st.assignment.values()) | set(st.part_filaments.values()) <= set(st.palette)
    assert job.result["threemf"] == "s10_write/a3/final.3mf"

    # refit to explicit slots that fail the slicer gate: review, as today
    fake_slicer.fail_with = ["filament conflict"]
    edit(rt, job_id, mode="refit", slots=["generic-pla-black", "bambu-pla-matte-desert-tan",
                                          "generic-pla-white"])
    job = rt.store.load_job(job_id)
    assert job.status == JobStatus.REVIEW
    assert rt.store.job_row(job_id)["review_reason"].startswith("slicer gate failed")
    st = state_of(rt, job_id, StageId.S08_PALETTE)
    assert st.palette == ["generic-pla-black", "bambu-pla-matte-desert-tan", "generic-pla-white"]
    assert job.result["threemf"] == "s10_write/a3/final.3mf"  # the last good result stays
    # an edit on a job in review after a result is still applied
    fake_slicer.fail_with = None
    edit(rt, job_id, mode="recolor", slots=["generic-pla-black", "generic-pla-brown",
                                            "generic-pla-white"])
    job = rt.store.load_job(job_id)
    assert job.status == JobStatus.DONE and job.result["threemf"] == "s10_write/a5/final.3mf"


def test_palette_edit_on_a_thin_slice_job(settings, photos, fake_slicer):
    rt = make_rt(settings)
    job_id, status, _ = _job(rt, photos[:1], product_type="keychain")
    assert status == JobStatus.DONE
    assert manifest(rt, job_id, StageId.S08_PALETTE).status == StageStatus.SKIPPED
    palette = [f.id for f in pr.candidates(LIB)]
    slots = [("bambu-pla-basic-cocoa-brown" if f == "generic-pla-brown" else f) for f in palette]
    edit(rt, job_id, mode="recolor", slots=slots)
    job = rt.store.load_job(job_id)
    assert job.status == JobStatus.DONE, rt.store.job_row(job_id)
    assert job.attempts[StageId.S08_PALETTE] == 2 and job.attempts[StageId.S10_WRITE] == 2
    assert job.attempts[StageId.S09_PAINT] == 1  # not in the thin-slice profile: untouched
    st = state_of(rt, job_id, StageId.S08_PALETTE)
    assert st.palette == slots and "generic-pla-brown" not in st.assignment.values()
    assert JobResult.model_validate(job.result).threemf == "s10_write/a2/final.3mf"
    assert fake_slicer.calls[-1]["filament_ids"] == slots


def test_palette_edit_waits_while_the_job_runs_and_is_refused_when_closed(settings):
    rt = make_rt(settings)
    job = rt.orchestrator.create_job("keychain", [], product_type="keychain")
    row = rt.store.post_message(job.id, "user", "palette_edit", {"mode": "refit"})
    job.status = JobStatus.WAITING_USER
    rt.store.save_job(job)
    run(rt.orchestrator._apply_palette_edits(job))
    assert rt.store.list_messages(job.id, kinds={"palette_edit"}, unconsumed=True)
    job.status = JobStatus.FAILED
    rt.store.save_job(job)
    run(rt.orchestrator.advance(job.id))
    assert not rt.store.list_messages(job.id, kinds={"palette_edit"}, unconsumed=True)
    assert notes(rt, job.id, row.id) == ["Job is closed"]
    job = rt.store.load_job(job.id)
    job.status, job.result = JobStatus.DONE, None
    rt.store.save_job(job)
    row = rt.store.post_message(job.id, "user", "palette_edit", {"mode": "refit"})
    run(rt.orchestrator._apply_palette_edits(job))
    assert notes(rt, job.id, row.id) == ["No result to edit yet"]
