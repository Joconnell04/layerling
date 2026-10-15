"""The approved product reaches geometry: form, plate shape, add-on colors, palette source
(docs/runs/fidelity/DEEP-DIVE.md items 1, 3, 4)."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import make_rt, run
from p2p_contracts import JobStatus, StageId
from p2p_contracts.geometry import GeometryResult
from p2p_contracts.job import AddOn, AddOnKind
from p2p_worker.local import AutoResponder, run_job
from p2p_worker.product_spec import color_value, read_spec


class _Job:
    id = "j"

    def __init__(self):
        self.addons: list = []


def _job_dir(tmp_path: Path, request: dict | None = None, job: dict | None = None,
             answers: list | None = None) -> Path:
    d = tmp_path / "job"
    (d / "input").mkdir(parents=True)
    (d / "input" / "request.json").write_text(json.dumps(request or {}))
    (d / "job.json").write_text(json.dumps(job or {}))
    if answers is not None:
        a = d / "s01_intake" / "a1"
        a.mkdir(parents=True)
        (a / "answers.json").write_text(json.dumps({"answers": answers}))
    return d


def test_defaults_leave_the_form_to_geometry(tmp_path):
    spec = read_spec(_job_dir(tmp_path), _Job())
    assert spec.form is None and spec.plate_shape is None and spec.colors == {}
    assert spec.palette_source == "owned"


def test_form_from_the_request_then_the_answers(tmp_path):
    s = read_spec(_job_dir(tmp_path, {"form": "3D figure"}), _Job())
    assert (s.form, s.form_source) == ("figure", "input/request.json form")
    s = read_spec(_job_dir(tmp_path / "b", answers=[
        {"question_id": "keychain_form", "option_ids": ["flat_charm"]}]), _Job())
    assert s.form == "flat" and s.form_source.startswith("answer keychain_form")
    s = read_spec(_job_dir(tmp_path / "c", job={"product_spec": {"form": "flat"}},
                           request={"form": "figure"}), _Job())
    assert s.form == "flat"  # the job record wins over the new-job form


def test_form_from_the_job_record_then_product_json(tmp_path):
    job = _Job()
    job.form = "flat"
    d = _job_dir(tmp_path, {"form": "figure"})
    assert read_spec(d, job).form_source == "job form" and read_spec(d, job).form == "flat"
    a = d / "s01_intake" / "a1"
    a.mkdir(parents=True)
    (a / "product.json").write_text(json.dumps({
        "product_type": "nameplate", "form": "figure", "decided_by": {"form": "user"},
        "picks": {"plate_shape": ["oval"]}}))
    s = read_spec(d, _Job())
    assert s.form == "figure" and s.form_source == "s01 product.json (user)"
    assert s.plate_shape == "oval" and s.plate_shape_source == "s01 product.json picks"


def test_plate_shape_colors_and_palette_source(tmp_path):
    job = _Job()
    job.addons = [AddOn(kind=AddOnKind.NAMEPLATE, params={"shape": "oval"})]
    d = _job_dir(tmp_path, {"addon_colors": {"plate": "cream"}, "name_color": "#4d3324",
                            "palette_source": "any"},
                 answers=[{"question_id": "loop_color", "option_ids": ["black"]}])
    s = read_spec(d, job)
    assert s.plate_shape == "oval" and s.plate_shape_source == "nameplate params.shape"
    assert s.colors == {"plate": "#EDE3CC", "text": "#4D3324", "loop": "#161616"}
    assert s.color_sources["loop"] == "answer loop_color"
    assert s.palette_source == "any" and s.palette_source_from == "input/request.json"
    assert color_value("bambu-pla-matte-caramel", {"bambu-pla-matte-caramel"}) == \
        "bambu-pla-matte-caramel"
    assert color_value("mauve-ish") is None


def _job_with_request(rt, photos, product, **extra):
    job = rt.orchestrator.create_job(f"{product} of my dog", photos, product_type=product)
    req = rt.store.job_dir(job.id) / "input" / "request.json"
    req.write_text(json.dumps({**json.loads(req.read_text()), **extra}))
    return job.id


def _run_job(rt, job_id):
    async def go():
        try:
            return await run_job(rt, job_id, AutoResponder([], []))
        finally:
            await rt.fake.drain()
    return run(go())


def _geometry(rt, job_id):
    job = rt.store.load_job(job_id)
    m6 = rt.store.load_manifest(job_id, StageId.S06_PRINTABILITY,
                                job.attempts[StageId.S06_PRINTABILITY])
    ref = next(o for o in m6.outputs if o.role == "geometry")
    geo = GeometryResult.model_validate_json((rt.store.job_dir(job_id) / ref.path).read_text())
    return m6, geo


def test_keychain_keeps_the_3d_figure_by_default(settings, photos):
    rt = make_rt(settings)
    job_id = _job_with_request(rt, photos, "keychain")
    assert _run_job(rt, job_id) == JobStatus.DONE
    m6, geo = _geometry(rt, job_id)
    assert geo.orientation == "upright" and geo.supports == "tree"
    assert m6.data["form"] == "figure"
    roles = {o.role for o in m6.outputs}
    assert {"product_resolved", "concept_read", "geometry"} <= roles
    # every add-on color decision is recorded (the fake figure has no texture: the default)
    assert m6.data["addon_colors"] and m6.data["addon_colors"][0]["from"]
    assert any(d.key == "product.form" for d in m6.decisions)


def test_keychain_form_flat_from_the_request(settings, photos):
    rt = make_rt(settings)
    job_id = _job_with_request(rt, photos, "keychain", form="flat")
    assert _run_job(rt, job_id) == JobStatus.DONE
    m6, geo = _geometry(rt, job_id)
    assert geo.orientation == "flat_face_up" and geo.supports == "none"
    assert m6.data["form"] == "flat" and m6.data["form_source"] == "job form"  # s01 set it
    assert any(n.startswith("Form flat (job)") for n in geo.notes)


def test_new_attempt_skips_folders_already_on_disk(settings, photos):
    """An evidence run left s09_paint/a2 and a3 outside the job record; a new attempt must not
    reuse them (it would load their stale paint state)."""
    rt = make_rt(settings)
    job = rt.orchestrator.create_job("keychain of my dog", photos, product_type="keychain")
    d = rt.store.job_dir(job.id) / StageId.S09_PAINT.value
    for a in ("a2", "a3"):
        (d / a).mkdir(parents=True)
    m = rt.orchestrator._new_attempt(rt.store.load_job(job.id), StageId.S09_PAINT)
    assert m.attempt == 4


def test_concept_read_unwraps_and_rejects_empty_answers():
    import pytest
    from p2p_worker.concept_read import ConceptRead
    from pydantic import ValidationError

    wrapped = {"input": json.dumps({"form": "figure", "figure_colors": [
        {"area": "coat", "hex": "#4A4038", "share": 0.3}], "plate": {"shown": False}})}
    r = ConceptRead.model_validate(wrapped)
    assert r.form == "figure" and r.figure_colors[0].hex == "#4A4038"
    assert r.addon_targets() == {}
    with pytest.raises(ValidationError):  # an empty answer goes to the repair round
        ConceptRead.model_validate({"input": "not json"})
    shown = ConceptRead.model_validate({"form": "figure", "figure_colors": [
        {"area": "coat", "hex": "#FFFFFF", "share": 1}], "plate": {"shown": True,
                                                                  "hex": "#aa8844"}})
    assert shown.addon_targets() == {"plate": "#AA8844"}
