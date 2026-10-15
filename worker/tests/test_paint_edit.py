"""paint_edit on a finished job: the ops as a new s09 attempt (region fill, face brush, slot
replace), the p2p_slicer preview, s10 through the gate as a new result, and the refusals (stale
base, bad op, closed job), on the fake Meshy keychain with the fake Bambu gate."""

from __future__ import annotations

import json

import numpy as np
from conftest import run
from p2p_contracts import JobStatus, PaintState, StageId, StageStatus
from p2p_contracts.payloads import JobResult, PaintPreview
from test_m3 import _job, make_rt, manifest
from test_palette_edit import notes, tree_hash

WHITE, BLACK = "generic-pla-white", "generic-pla-black"
S09, S10 = StageId.S09_PAINT, StageId.S10_WRITE


def post(rt, job_id: str, **payload) -> str:
    row = rt.store.post_message(job_id, "user", "paint_edit", payload)
    run(rt.orchestrator.advance(job_id))
    return row.id


def state(rt, job_id: str, rel: str) -> PaintState:
    return PaintState.model_validate_json((rt.store.job_dir(job_id) / rel).read_text())


def preview_of(rt, job_id: str) -> PaintPreview:
    m = manifest(rt, job_id, S09)
    rel = next(o.path for o in m.outputs if o.role == "paint_preview")
    return PaintPreview.model_validate_json((rt.store.job_dir(job_id) / rel).read_text())


def test_paint_edit_on_a_finished_full_job(settings, photos, fake_slicer):
    rt = make_rt(settings)
    job_id, status, _ = _job(rt, photos[:1], product_type="keychain", profile="full")
    assert status == JobStatus.DONE
    jdir = rt.store.job_dir(job_id)
    old = {s: tree_hash(jdir / s.value / "a1") for s in (S09, S10)}
    base = state(rt, job_id, "s09_paint/a1/paint_state.json")
    fr = np.load(jdir / state_rm(rt, job_id, base))
    target = next(int(r) for r, f in base.assignment.items() if f != WHITE)
    stroke = np.nonzero(fr == int(next(iter(base.assignment))))[0][:30]
    faces = ",".join(str(int(i)) for i in stroke)
    credits = rt.store.load_job(job_id).budget.spent_credits
    posts = rt.fake.count("POST")
    calls = len(fake_slicer.calls)

    mid = post(rt, job_id, base_attempt="s09_paint/a1", note="eyes darker", ops=[
        {"op": "fill_region", "region_ids": [target], "filament_id": WHITE},
        {"op": "brush_faces", "faces": faces, "filament_id": BLACK},
    ])
    job = rt.store.load_job(job_id)
    assert job.status == JobStatus.DONE, rt.store.job_row(job_id)
    assert not rt.store.list_messages(job_id, kinds={"paint_edit"}, unconsumed=True)
    assert job.attempts[S09] == 2 and job.attempts[S10] == 2
    for s in (S09, S10):
        assert manifest(rt, job_id, s).status == StageStatus.SUCCEEDED
        assert tree_hash(jdir / s.value / "a1") == old[s]  # old attempts never change
    m9 = manifest(rt, job_id, S09)
    assert m9.data["paint_edit"]["message_id"] == mid
    assert m9.data["paint_edit"]["base"] == "s09_paint/a1" and m9.data["progress"] == "done"
    assert {i.role for i in m9.inputs} >= {"base_paint_state"}
    assert all(i.from_stage != S09 for i in m9.inputs)  # never stale against its own stage

    after = state(rt, job_id, "s09_paint/a2/paint_state.json")
    assert after.assignment[str(target)] == WHITE
    new_rid = max(int(r) for r in after.assignment)
    assert after.assignment[str(new_rid)] == BLACK
    fr2 = np.load(jdir / "s09_paint/a2/face_regions.npy")
    assert (fr2[stroke] == new_rid).all()

    prev = preview_of(rt, job_id)
    assert prev.base_attempt == "s09_paint/a1" and prev.ops == 2 and prev.changed_faces > 0
    assert prev.after is not None and prev.after.slicer == "p2p_slicer"
    assert prev.after.print_time_s > 0 and prev.after.total_grams > 0
    assert prev.before is not None and prev.before.slicer == "p2p_slicer"  # the old result
    idx = json.loads((jdir / prev.toolpaths).read_text())
    assert idx["layers"] and (jdir / "s09_paint/a2/preview" / idx["bin"]).is_file()
    assert len(prev.proof_images) == 16
    roles = {o.role for o in m9.outputs}
    assert {"paint_preview", "paint_state", "region_map", "face_regions", "toolpaths",
            "toolpaths_bin", "preview_3mf", "edit_proof_print", "edit_proof_paint"} <= roles
    assert "proof_print" not in roles  # the result's proof set stays s10's

    m10 = manifest(rt, job_id, S10)
    assert next(i.path for i in m10.inputs if i.role == "paint_state") == \
        "s09_paint/a2/paint_state.json"
    assert m10.data["paint_edit_message"] == mid
    result = JobResult.model_validate(job.result)
    assert result.threemf == "s10_write/a2/final.3mf" and result.slice_ok
    assert len(fake_slicer.calls) == calls + 1  # one gate: s10's
    assert fake_slicer.calls[-1]["filament_ids"] == after.palette
    # the preview sliced the same paint s10 gated: same faces, same slots
    from p2p_threemf import read_3mf_full

    a = read_3mf_full(jdir / "s09_paint/a2/final.3mf")
    b = read_3mf_full(jdir / result.threemf)
    assert np.array_equal(a.face_slots, b.face_slots)
    job = rt.store.load_job(job_id)
    assert job.budget.spent_credits == credits and rt.fake.count("POST") == posts

    # a second advance changes nothing
    assert run(rt.orchestrator.advance(job_id)) == JobStatus.DONE
    assert rt.store.load_job(job_id).attempts[S10] == 2

    # a stale base and a bad op: a note each, nothing changes
    mid = post(rt, job_id, base_attempt="s09_paint/a1",
               ops=[{"op": "fill_region", "region_ids": [target], "slot": 1}])
    assert notes(rt, job_id, mid) == ["Paint changed since s09_paint/a1; reload"]
    mid = post(rt, job_id, base_attempt="s09_paint/a2",
               ops=[{"op": "fill_region", "region_ids": [99999], "filament_id": WHITE}])
    assert notes(rt, job_id, mid)[0].startswith(
        "Paint edit not applied: op 1 (fill_region): no region 99999")
    job = rt.store.load_job(job_id)
    assert job.status == JobStatus.DONE and job.attempts[S09] == 2 and job.attempts[S10] == 2
    assert rt.store.load_manifest(job_id, S09, 3).status == StageStatus.FAILED
    assert job.result["threemf"] == "s10_write/a2/final.3mf"

    # a large stroke from a file, replace a slot, then the preview's before is the last edit's
    (jdir / "input/paint").mkdir(parents=True)
    (jdir / "input/paint/stroke1.txt").write_text(faces)
    k = after.palette.index(BLACK) + 1
    post(rt, job_id, base_attempt="s09_paint/a2", ops=[
        {"op": "brush_faces", "faces_path": "input/paint/stroke1.txt", "slot": 1},
        {"op": "replace_slot", "from_slot": k, "filament_id": "bambu-pla-basic-gray"},
    ], source="any")
    job = rt.store.load_job(job_id)
    assert job.status == JobStatus.DONE and job.attempts[S09] == 4, rt.store.job_row(job_id)
    m9 = manifest(rt, job_id, S09)
    assert "input/paint/stroke1.txt" in {i.path for i in m9.inputs}
    st = state(rt, job_id, "s09_paint/a4/paint_state.json")
    assert "bambu-pla-basic-gray" in st.palette and BLACK not in st.palette
    assert preview_of(rt, job_id).before == prev.after

    # the gate fails in s10: review, as for any s10
    fake_slicer.fail_with = ["filament conflict"]
    post(rt, job_id, ops=[{"op": "fill_region", "region_ids": [target], "slot": 1}])
    job = rt.store.load_job(job_id)
    assert job.status == JobStatus.REVIEW
    assert rt.store.job_row(job_id)["review_reason"].startswith("slicer gate failed")
    fake_slicer.fail_with = None
    # an edit on a job in review after a result is still applied
    post(rt, job_id, ops=[])
    job = rt.store.load_job(job_id)
    assert job.status == JobStatus.DONE and job.attempts[S10] == 5


def state_rm(rt, job_id: str, ps: PaintState) -> str:
    rm = json.loads((rt.store.job_dir(job_id) / ps.region_map.path).read_text())
    return rm["face_regions"]["path"]


def test_paint_edit_on_a_thin_slice_job(settings, photos, fake_slicer):
    rt = make_rt(settings)
    job_id, status, _ = _job(rt, photos[:1], product_type="keychain")
    assert status == JobStatus.DONE
    assert manifest(rt, job_id, S09).status == StageStatus.SKIPPED
    post(rt, job_id, ops=[])  # no paint map yet: start one from the baseline
    job = rt.store.load_job(job_id)
    assert job.status == JobStatus.DONE, rt.store.job_row(job_id)
    assert job.attempts[S09] == 2 and job.attempts[S10] == 2
    m9 = manifest(rt, job_id, S09)
    assert m9.data["paint_edit"]["base"] is None and m9.data["bootstrapped"] is True
    st = state(rt, job_id, "s09_paint/a2/paint_state.json")
    assert st.assignment and set(st.assignment.values()) <= set(st.palette)
    prev = preview_of(rt, job_id)
    assert prev.before is not None and prev.after is not None  # before = s07's 3MF
    assert JobResult.model_validate(job.result).threemf == "s10_write/a2/final.3mf"
    # the thin profile never reruns s09 or s10 on its own
    assert run(rt.orchestrator.advance(job_id)) == JobStatus.DONE
    assert rt.store.load_job(job_id).attempts[S10] == 2


def test_paint_edit_waits_while_the_job_runs_and_is_refused_when_closed(settings):
    rt = make_rt(settings)
    job = rt.orchestrator.create_job("keychain", [], product_type="keychain")
    row = rt.store.post_message(job.id, "user", "paint_edit", {"ops": []})
    job.status = JobStatus.WAITING_USER
    rt.store.save_job(job)
    from p2p_worker.paint_edit import apply_paint_edits

    run(apply_paint_edits(rt.orchestrator, job))
    assert rt.store.list_messages(job.id, kinds={"paint_edit"}, unconsumed=True)
    job.status = JobStatus.CANCELLED
    rt.store.save_job(job)
    run(rt.orchestrator.advance(job.id))
    assert notes(rt, job.id, row.id) == ["Job is closed"]
    job = rt.store.load_job(job.id)
    job.status, job.result = JobStatus.DONE, None
    rt.store.save_job(job)
    row = rt.store.post_message(job.id, "user", "paint_edit", {"ops": []})
    run(apply_paint_edits(rt.orchestrator, job))
    assert notes(rt, job.id, row.id) == ["No result to edit yet"]


def test_paint_edit_cli(settings, photos, fake_slicer, monkeypatch, capsys):
    """Claude's path without the browser: paint-edit posts, advances and prints the preview."""
    import p2p_worker.__main__ as cli

    rt = make_rt(settings)
    job_id, status, _ = _job(rt, photos[:1], product_type="keychain", profile="full")
    assert status == JobStatus.DONE
    monkeypatch.setattr(cli, "_settings", lambda _a: settings)
    monkeypatch.setattr(cli, "_runtime", lambda _a, _s: rt)
    st = state(rt, job_id, "s09_paint/a1/paint_state.json")
    rid = next(int(r) for r, f in st.assignment.items() if f != WHITE)
    ops = json.dumps([{"op": "fill_region", "region_ids": [rid], "filament_id": WHITE}])
    assert cli.main(["paint-edit", "--job", job_id, "--ops", ops, "--note", "cli"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "done" and out["notes"] == []
    assert out["edit"]["attempt"] == "s09_paint/a2" and out["edit"]["accepted"]
    assert out["edit"]["preview"]["after"]["print_time_s"] > 0
    assert out["result"]["threemf"] == "s10_write/a2/final.3mf"
    msg = rt.store.list_messages(job_id, kinds={"paint_edit"})[-1]
    assert msg.payload["base_attempt"] == "s09_paint/a1" and msg.payload["note"] == "cli"
    # a refused edit exits 1 with the note
    bad = json.dumps([{"op": "fill_region", "region_ids": [99999], "slot": 1}])
    assert cli.main(["paint-edit", "--job", job_id, "--ops", bad]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["notes"][0].startswith("Paint edit not applied")
    assert out["edit"]["status"] == "failed" and not out["edit"]["accepted"]


def test_a_worker_stopped_inside_an_edit_finishes_it(settings, photos, fake_slicer):
    """The s09 attempt was made and the worker stopped: the next advance runs the edit, then s10."""
    from p2p_worker.paint_edit import _sub

    rt = make_rt(settings)
    job_id, status, _ = _job(rt, photos[:1], product_type="keychain")  # thin slice
    assert status == JobStatus.DONE
    job = rt.store.load_job(job_id)
    m9 = _sub(rt.orchestrator)._new_attempt(job, S09)
    m9.data["paint_edit"] = {"ops": [], "source": "owned", "message_id": "m-1", "base": None}
    rt.store.write_manifest(m9)
    rt.orchestrator._set_job(rt.store.load_job(job_id), JobStatus.RUNNING, S09)

    assert run(rt.orchestrator.advance(job_id)) == JobStatus.DONE
    job = rt.store.load_job(job_id)
    assert job.attempts[S09] == 2 and job.attempts[S10] == 2
    assert manifest(rt, job_id, S09).status == StageStatus.SUCCEEDED
    assert manifest(rt, job_id, S10).data["paint_edit_message"] == "m-1"
    assert JobResult.model_validate(job.result).threemf == "s10_write/a2/final.3mf"
