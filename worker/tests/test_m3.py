"""M3 worker side: achromatic guard, spool suggestions, despeckle, previews, the full profile,
run-stages at 0 credits, and the s09 switch."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest
from conftest import REPO, make_rt, run
from p2p_contracts import (
    FilamentLibrary,
    JobStatus,
    PaintState,
    PrintSettings,
    RegionMap,
    StageId,
    StageStatus,
)
from p2p_contracts.payloads import ColorSpec, JobResult
from p2p_worker import printing as pr
from p2p_worker.local import run_local
from p2p_worker.stages.s10_write import face_slots_for

LIB = FilamentLibrary.model_validate_json((REPO / "filaments/library.json").read_text())
POOL = pr.candidates(LIB)
CATALOG = pr.load_catalog(REPO)


# ---------------------------------------------------------------- color


def test_gray_never_becomes_brown_and_suggests_a_gray_spool():
    """Proof run 1: the Frenchie's gray (#797979) went to brown at dE00 26.9."""
    ids = [f.id for f in POOL]
    assert "generic-pla-brown" in ids and not any("gray" in i for i in ids)
    m = pr.match_colour("#797979", POOL, CATALOG)
    assert m.achromatic and m.filament_id in ("generic-pla-black", "generic-pla-white")
    assert m.unguarded_id != m.filament_id  # plain nearest dE00 picks a colored spool
    assert m.suggestion == "add a gray PLA"
    # the full catalog (filaments/catalog.json) has a closer gray than PLA Basic Gray (dE00 9.2)
    assert m.suggest_id == "bambu-pla-matte-nardo-gray" and m.suggest_de < 2.0
    text = pr.suggestion_text(m, "gray coat", CATALOG)
    assert text.startswith("add a gray PLA (Bambu Lab PLA Matte Nardo Gray): #797979 (gray coat), "
                           "now ")


def test_colored_and_matching_colors():
    brown = pr.match_colour("#7A4A05", POOL, CATALOG)
    assert brown.filament_id == "generic-pla-brown" and brown.suggestion is None
    black = pr.match_colour("#121212", POOL, CATALOG)
    assert black.filament_id == "generic-pla-black" and black.delta_e < 10
    pink = pr.match_colour("#CE9FA3", POOL, CATALOG)  # Frenchie inner ears
    assert not pink.achromatic and pink.suggestion == "add a pink PLA"
    # no gray filament at all: the guard falls back to plain nearest instead of failing
    colored = [f for f in POOL if pr.chroma(f.lab) > pr.ACHROMATIC_FILAMENT_C]
    assert pr.match_colour("#808080", colored).filament_id in {f.id for f in colored}


# ---------------------------------------------------------------- despeckle and previews


def grid(n: int = 40, step: float = 0.5):
    """A flat n x n grid of 2n^2 triangles, `step` mm apart."""
    xs, ys = np.meshgrid(np.arange(n + 1) * step, np.arange(n + 1) * step)
    v = np.c_[xs.ravel(), ys.ravel(), np.zeros(xs.size)]
    f = []
    for j in range(n):
        for i in range(n):
            a, b, c, d = j * (n + 1) + i, j * (n + 1) + i + 1, (j + 1) * (n + 1) + i, \
                (j + 1) * (n + 1) + i + 1
            f += [(a, b, d), (a, d, c)]
    return v, np.array(f)


def test_despeckle_merges_small_islands_and_keeps_big_patches():
    v, f = grid()
    labels = np.ones(len(f), dtype=np.int64)
    cen = v[f].mean(axis=1)
    labels[(np.abs(cen[:, 0] - 5) < 0.3) & (np.abs(cen[:, 1] - 5) < 0.3)] = 2  # 0.5 mm speck
    big = (cen[:, 0] > 12) & (cen[:, 1] > 12)  # 8 x 8 mm patch
    labels[big] = 3
    settings = PrintSettings()
    assert pr.islands(v, f, labels, settings) >= 1
    clean, changed = pr.despeckle(v, f, labels, settings)
    assert 2 not in clean and changed == int((labels == 2).sum())
    assert (clean[big] == 3).all() and pr.islands(v, f, clean, settings) == 0


def test_render_outputs_exact_face_colors(tmp_path):
    import trimesh

    v, f = grid(20, 1.0)
    v[:, 2] = 0.2 * np.sin(v[:, 0])  # not perfectly flat
    slots = np.where(v[f].mean(axis=1)[:, 0] < 10, 1, 5)  # black | white
    out = tmp_path / "t.3mf"
    res = pr.write_print(out, v, f, slots, POOL, None, PrintSettings(), tmp_path)
    assert res["slots_used"] == [1, 5] and res["palette"] == [x.id for x in POOL]
    info = pr.render_outputs(out, tmp_path / "sheet.png", tmp_path / "p.glb", "test")
    assert (tmp_path / "sheet.png").read_bytes()[:4] == b"\x89PNG"
    g = trimesh.load(tmp_path / "p.glb", force="mesh", process=False)
    cols = {tuple(c) for c in np.asarray(g.visual.vertex_colors)[:, :3]}
    # black and white only (no blended boundary colors), white stays 255 in linear RGB
    assert cols == {tuple(pr._linear_u8(np.array([[0x16, 0x16, 0x16]]))[0]), (255, 255, 255)}
    assert info["glb_vertices"] > len(v)  # boundary vertices doubled, nothing blended
    # glTF frame: Y up, metres
    assert np.ptp(np.asarray(g.vertices)[:, 1]) < 0.001 and np.ptp(g.vertices[:, 0]) > 0.019


def test_spool_payloads_follow_the_contract():
    from p2p_contracts.payloads import SpoolSuggestion

    items = [{"share": 0.05, "regions": [f"s1.marking_gray_{i}"],
              **pr.asdict_matches([pr.match_colour(h, POOL, CATALOG)])[0]}
             for i, h in enumerate(["#797979", "#7A7A7A", "#CE9FA3", "#121212"])]
    out = [SpoolSuggestion.model_validate(p) for p in pr.spool_payloads(items)]
    assert [p.suggestion for p in out] == ["gray PLA", "pink PLA"]  # black is loaded
    gray = out[0]
    assert gray.target_hex == "#797979" and gray.delta_e_now == 33.3
    assert gray.delta_e_with is not None and gray.delta_e_with < pr.SUGGEST_DE
    assert gray.regions == ["s1.marking_gray_0", "s1.marking_gray_1"] and gray.area_pct == 10.0


def test_face_slots_for_default_and_assignment():
    state = PaintState(region_map={"path": "x", "sha256": "0"}, palette=["a", "b", "c"],
                       assignment={"1": "b", "2": "c"}, default_filament="a")
    assert face_slots_for(state, np.array([0, 1, 2, 2, 7])).tolist() == [1, 2, 3, 3, 1]
    with pytest.raises(ValueError):
        face_slots_for(state.model_copy(update={"assignment": {"1": "zz"}}), np.array([1]))


# ---------------------------------------------------------------- pipeline


def _job(rt, photos, prompt="keychain of my dog", **kw):
    async def go():
        try:
            return await run_local(rt, photos, prompt, auto=True, **kw)
        finally:
            await rt.fake.drain()

    return run(go())


def manifest(rt, job_id, stage):
    job = rt.store.load_job(job_id)
    return rt.store.load_manifest(job_id, stage, job.attempts[stage])


def test_thin_slice_result_shows_what_prints(settings, photos):
    rt = make_rt(settings)
    job_id, status, _ = _job(rt, photos[:1], product_type="keychain")
    assert status == JobStatus.DONE
    jdir = rt.store.job_dir(job_id)
    result = JobResult.model_validate(rt.store.load_job(job_id).result)
    assert result.render_sheet == "s07_baseline/a1/render_sheet.png"
    assert (jdir / result.render_sheet).read_bytes()[:4] == b"\x89PNG"
    assert result.preview_glb == "s07_baseline/a1/preview.glb"
    m7 = manifest(rt, job_id, StageId.S07_BASELINE)
    assert m7.data["despeckle"]["islands_after"] <= m7.data["despeckle"]["islands_before"]
    assert {m["src_hex"] for m in m7.data["mapping"]} == {"#BE8C50", "#F5F5F0", "#191919"}


def test_full_profile_end_to_end_with_the_stub_paint(settings, photos, fake_slicer):
    rt = make_rt(settings)
    job_id, status, _ = _job(rt, photos[:1], product_type="keychain", profile="full")
    assert status == JobStatus.DONE, rt.store.job_row(job_id)
    job = rt.store.load_job(job_id)
    jdir = rt.store.job_dir(job_id)
    for sid in StageId:
        assert manifest(rt, job_id, sid).status == StageStatus.SUCCEEDED, sid
    # s02: Sonnet color spec (mock) with LAB from hex
    m2 = manifest(rt, job_id, StageId.S02_COLOR_SPEC)
    spec = ColorSpec.model_validate_json((jdir / next(
        o.path for o in m2.outputs if o.role == "color_spec")).read_text())
    parts = spec.subjects[0].parts
    assert any(p.part == "marking" for p in parts) and all(p.lab for p in parts)
    assert m2.model_calls[0].model == "claude-sonnet-5"
    # s07 in the full profile: baseline project and render, no gate
    m7 = manifest(rt, job_id, StageId.S07_BASELINE)
    assert m7.data["slice_ok"] is None and any(o.role == "baseline_3mf" for o in m7.outputs)
    assert all(c["path"].endswith("final.3mf") for c in fake_slicer.calls)
    # s08: region map on the geometry figure, paint state in AMS order
    m8 = manifest(rt, job_id, StageId.S08_PALETTE)
    rmap = RegionMap.model_validate_json((jdir / next(
        o.path for o in m8.outputs if o.role == "region_map")).read_text())
    fr = np.load(jdir / rmap.face_regions.path)
    assert len(fr) == rmap.mesh.n_faces and fr.min() >= 1
    assert rmap.regions[0].label.endswith("body") and rmap.regions[0].source == "meshy_color"
    geo_fig = next(i.path for i in m8.inputs if i.role == "geometry")
    assert rmap.mesh.path.startswith("s06_printability/") and geo_fig
    state = PaintState.model_validate_json((jdir / next(
        o.path for o in m8.outputs if o.role == "paint_state")).read_text())
    assert state.palette == [f.id for f in POOL]
    assert set(state.assignment.values()) <= set(state.palette)
    assert state.part_filaments  # keyring and back parts keep their filament
    # s09 stub, s10 ships
    assert manifest(rt, job_id, StageId.S09_PAINT).data["agent"] == "stub"
    result = JobResult.model_validate(job.result)
    assert result.threemf == "s10_write/a1/final.3mf" and not result.baseline_only
    assert (jdir / result.render_sheet).is_file() and (jdir / result.preview_glb).is_file()
    assert result.slice_ok and result.filaments
    kinds = [m.kind for m in rt.store.list_messages(job_id, role="assistant")]
    assert kinds.count("result") == 1


def test_spool_suggestion_note_for_a_gray_dog(settings, photos, monkeypatch):
    """A gray Meshy color with no gray spool loaded: s08 records a suggestion and posts a note."""
    from p2p_worker.fakes import assets

    monkeypatch.setattr(assets, "MESHY_COLORS", (("Gray", "#797979", (51.0, 0.0, 0.0)),
                                                 *assets.MESHY_COLORS[1:]))
    assets.threemf.cache_clear()
    try:
        rt = make_rt(settings)
        job_id, status, _ = _job(rt, photos[:1], product_type="keychain", profile="full")
    finally:
        assets.threemf.cache_clear()
    assert status == JobStatus.DONE
    m8 = manifest(rt, job_id, StageId.S08_PALETTE)
    assert "add a gray PLA" in m8.data["spool_suggestions"]
    gray = next(m for m in m8.data.get("steps", {}).get("first_pass", {}).get("mapping", [])
                if m["src_hex"] == "#797979")
    assert gray["filament_id"] in ("generic-pla-black", "generic-pla-white")
    spools = [m.payload for m in rt.store.list_messages(job_id)
              if m.kind == "spool_suggestion"]
    gray = next(p for p in spools if p["suggestion"] == "gray PLA")
    assert gray["target_hex"] == "#797979" and gray["delta_e_now"] > 10
    assert gray["regions"] and all(r.startswith("s1.") for r in gray["regions"])


def test_run_stages_reruns_print_stages_at_zero_credits(settings, photos):
    rt = make_rt(settings)
    job_id, status, _ = _job(rt, photos[:1], product_type="keychain")
    assert status == JobStatus.DONE
    posts = rt.fake.count("POST")
    credits = rt.store.load_job(job_id).budget.spent_credits
    out = run(rt.orchestrator.run_stages(job_id, [StageId.S07_BASELINE, StageId.S02_COLOR_SPEC,
                                                  StageId.S08_PALETTE, StageId.S09_PAINT,
                                                  StageId.S10_WRITE]))
    assert set(out.values()) == {"succeeded"}, out
    job = rt.store.load_job(job_id)
    assert job.budget.spent_credits == credits and job.status == JobStatus.DONE
    assert rt.fake.count("POST") == posts
    assert job.result["threemf"] == "s10_write/a2/final.3mf"  # a1 was the skipped marker
    m7 = manifest(rt, job_id, StageId.S07_BASELINE)
    assert m7.attempt == 2 and any(d.key == "baseline.reused.cartoon" for d in m7.decisions)
    # s05 again: the model already paid for comes back from the credit ledger (no Meshy
    # request) and the stage waits for its review
    out = run(rt.orchestrator.run_stages(job_id, [StageId.S05_MESH]))
    assert out == {"s05_mesh": "waiting_user"} and rt.fake.count("POST") == posts
    m5 = manifest(rt, job_id, StageId.S05_MESH)
    assert any(d.key == "meshy.reused.m1.gen" for d in m5.decisions)
    # a stage that needs a new paid call fails and says so, spending nothing
    from p2p_worker.credit_ledger import CreditLedger

    CreditLedger(rt.store, job_id).unusable(m5.meshy_tasks[0].task_id, "test: files gone")
    out = run(rt.orchestrator.run_stages(job_id, [StageId.S05_MESH]))
    assert out == {"s05_mesh": "failed"}
    m5 = manifest(rt, job_id, StageId.S05_MESH)
    assert "Meshy is off for run-stages" in m5.error.message
    assert rt.fake.count("POST") == posts


def test_paint_switch_runs_the_agent_module_when_asked(settings, photos, monkeypatch):
    from p2p_worker.stages.base import Stage
    from p2p_worker.stages.s09_stub import PaintStub, agent_stage

    assert agent_stage() is None or agent_stage().id == StageId.S09_PAINT
    ran = []

    class FakeAgent(Stage):
        id = StageId.S09_PAINT

        async def run(self, ctx):
            ran.append(ctx.job.id)
            await PaintStub().run(ctx)
            ctx.m.data["agent"] = "fake-opus"

    mod = types.ModuleType("p2p_worker.stages.s09_paint")
    FakeAgent.__module__ = mod.__name__
    mod.FakeAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "p2p_worker.stages.s09_paint", mod)
    rt = make_rt(settings.with_(paint_agent="agent"))
    job_id, status, _ = _job(rt, photos[:1], product_type="keychain", profile="full")
    assert status == JobStatus.DONE and ran == [job_id]
    assert manifest(rt, job_id, StageId.S09_PAINT).data["agent"] == "fake-opus"
