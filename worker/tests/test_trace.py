"""Traces: redaction, live capture through the pipeline, idempotence, failure isolation, and the
backfill (disk only and --upload against a fake Supabase)."""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import httpx
from conftest import FailFirstComposite, make_rt, run
from p2p_contracts import JobStatus, Manifest, StageId, StageStatus
from p2p_worker import trace
from p2p_worker.clients.supabase import SupabaseStore
from p2p_worker.local import run_local
from p2p_worker.store import LocalStore

THIN = [StageId.S01_INTAKE, StageId.S03_CONCEPT, StageId.S05_MESH, StageId.S06_PRINTABILITY,
        StageId.S07_BASELINE]
SECRET_RE = re.compile(r"Signature=|Expires=|token=|eyJ[A-Za-z0-9_-]{10,}|msy_|sk-ant|sb_secret|"
                       r";base64,|supersecretvalue")
JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJzdG9yYWdlIn0.c2lnbmF0dXJlLXZhbHVl"  # gitleaks:allow (test fake)


class RecordingStore(LocalStore):
    def __init__(self, jobs_dir: Path):
        super().__init__(jobs_dir)
        self.uploaded: list[str] = []

    def upload_artifacts(self, job_id: str, rel_paths: list[str]) -> None:
        self.uploaded += [f"{job_id}/{p}" for p in rel_paths]


async def _local(rt, photos, prompt="keychain of my dog and cat hugging", **kw):
    try:
        return await run_local(rt, photos, prompt, auto=True, **kw)
    finally:
        await rt.fake.drain()


def entries(rt, job_id) -> dict[StageId, list[dict]]:
    job = rt.store.load_job(job_id)
    out = {}
    for sid, n in job.attempts.items():
        m = rt.store.load_manifest(job_id, sid, n)
        out[sid] = list(m.data.get("trace") or [])
    return out


def body(rt, job_id, entry) -> dict:
    return json.loads((rt.store.job_dir(job_id) / entry["path"]).read_text())


# ------------------------------------------------------------------ redaction


class FakeCtx:
    def __init__(self, tmp: Path, settings):
        self.job_id = "11111111-2222-3333-4444-555555555555"
        self.job = SimpleNamespace(id=self.job_id, attempts={})
        self.stage = StageId.S03_CONCEPT
        self.attempt = 1
        self.rel_dir = "s03_concept/a1"
        self.store = RecordingStore(tmp / "jobs")
        self.job_dir = self.store.job_dir(self.job_id)
        self.dir = self.job_dir / self.rel_dir
        self.dir.mkdir(parents=True)
        self.settings = settings
        self.m = Manifest(job_id=self.job_id, stage=self.stage, attempt=1,
                          status=StageStatus.RUNNING)

    def abs(self, rel: str) -> Path:
        return self.job_dir / rel


def test_redaction_keeps_no_secret_and_maps_refs(settings, tmp_path):
    s = settings.with_(meshy_live_key="supersecretvalue123")
    ctx = FakeCtx(tmp_path, s)
    (ctx.job_dir / "input").mkdir()
    (ctx.job_dir / "input" / "photo_1.jpg").write_bytes(b"x")
    job = ctx.job_id
    supa = (f"https://abc.supabase.co/storage/v1/object/sign/artifacts/{job}/s03_concept/a1/"
            f"c.png?token={JWT}")
    meshy = "https://assets.meshy.ai/t/1/model.glb?Expires=1790&Signature=abc123~x&Key-Pair-Id=K"
    text = ("keys sk-ant-api03-abcdefghijklmnop msy_abcdefghijklmnop sb_secret_abcdefghijklmnop "
            f"jwt {JWT} auth Bearer x and supersecretvalue123 url {supa} and {meshy} "
            "img data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB")
    with trace.attempt(ctx):
        e = trace.record(
            "meshy", vendor="meshy", model="gpt-image-2", purpose="concept r1c1",
            ref="task-1", input={"text": text, "images": [supa, ctx.abs("input/photo_1.jpg")],
                                 "params": {"model_url": supa, "k": "supersecretvalue123"}},
            output={"json": {"image_urls": [meshy], "texture": supa, "note": text}})
    assert e is not None
    raw = (ctx.job_dir / e["path"]).read_text()
    assert not SECRET_RE.search(raw), SECRET_RE.search(raw)
    for bad in ("sk-ant", "msy_abc", "sb_secret", "Bearer x", "abc123", "base64"):
        assert bad not in raw
    b = json.loads(raw)
    assert b["input"]["images"] == ["s03_concept/a1/c.png", "input/photo_1.jpg"]
    assert b["input"]["params"]["model_url"] == "s03_concept/a1/c.png"
    assert b["output"]["json"]["texture"] == f"storage:artifacts/{job}/s03_concept/a1/c.png"
    assert b["output"]["json"]["image_urls"] == ["https://assets.meshy.ai/t/1/model.glb"]
    assert not SECRET_RE.search(json.dumps(e))
    assert ctx.store.uploaded == [f"{job}/{e['path']}"]
    # one entry per (kind, ref)
    with trace.attempt(ctx):
        assert trace.record("meshy", vendor="meshy", model="m", purpose="p", ref="task-1") is None
    assert len(ctx.m.data["trace"]) == 1
    # nothing bound: a no-op
    assert trace.record("meshy", vendor="meshy", model="m", purpose="p", ref="task-2") is None


def test_redact_caps_long_strings(settings):
    out = trace.redact({"t": "a " * 150_000}, [])
    assert len(out["t"]) < 200_100 and out["t"].endswith("more chars]")


# ------------------------------------------------------------------ live capture


def test_pipeline_records_every_call(settings, photos):
    store = RecordingStore(settings.jobs_dir)
    # each code composite fails its check, so the paid loop edits (and their traces) run
    rt = make_rt(settings, store=store, claude=FailFirstComposite())
    job_id, status, _ = run(_local(rt, photos, product_type="keychain"))
    assert status == JobStatus.DONE
    tr = entries(rt, job_id)
    photo_paths = ["input/photo_1.jpg", "input/photo_2.jpg"]

    [intake] = tr[StageId.S01_INTAKE]
    b = body(rt, job_id, intake)
    assert intake["kind"] == "claude" and b["input"]["system"].startswith("You run intake")
    assert "keychain of my dog and cat hugging" in b["input"]["text"]
    assert b["input"]["images"] == photo_paths
    assert b["output"]["json"]["subjects"] and b["output"]["text"]
    m1 = rt.store.load_manifest(job_id, StageId.S01_INTAKE, 1)
    assert intake["ref"] == m1.model_calls[0].request_id

    concept = tr[StageId.S03_CONCEPT]
    # two figure renders, two loop edits chained from them after the composites failed, two
    # concept checks per candidate (the composite, then the edit)
    assert sorted(e["kind"] for e in concept) == ["claude"] * 4 + ["meshy"] * 4
    figures = [e for e in concept if e["kind"] == "meshy"
               and re.search(r"figure_r1c\d\.png$", e["files"][0])]
    assert sorted(e["model"] for e in figures) == ["gpt-image-2", "nano-banana-pro"]
    m3 = rt.store.load_manifest(job_id, StageId.S03_CONCEPT, 1)
    prompts = {t.task_id: t.request["prompt"] for t in m3.meshy_tasks}
    for e in figures:
        b = body(rt, job_id, e)
        assert b["input"]["text"] == prompts[e["ref"]] and e["summary"] == prompts[e["ref"]][:160]
        assert b["input"]["images"] == photo_paths
        assert b["input"]["params"]["remove_background"] is True
        assert len(e["files"]) == 1 and re.search(r"figure_r1c\d\.png$", e["files"][0])
        assert b["output"]["files"] == e["files"] and b["output"]["json"]["status"] == "SUCCEEDED"
        assert e["credits"] in (12, 9) and e["usd"] == round(e["credits"] * 0.02, 6)
    edits = [e for e in concept if e["kind"] == "meshy" and e not in figures]
    assert all(e["model"] == "nano-banana-2" and e["credits"] == 6 for e in edits)
    assert all(re.search(r"edit_r1c\d_1\.png$", e["files"][0]) for e in edits)
    checks = [e for e in concept if e["kind"] == "claude"]
    assert all(e["model"] == "claude-haiku-4-5-20251001" for e in checks)

    views = tr[StageId.S04_MULTIVIEW]
    assert [e["kind"] for e in views] == ["meshy", "claude"]
    assert len(views[0]["files"]) == 3  # three views from one task

    [mesh] = tr[StageId.S05_MESH]
    assert mesh["model"] == "multi-image-to-3d"
    names = [f.rsplit("/", 1)[-1] for f in mesh["files"]]
    assert names[0] == "mesh_m1.glb" and len([n for n in names if n.startswith("thumb_")]) == 4
    assert mesh["images"][0] == "s04_multiview/a1/view_concept.png" and len(mesh["images"]) == 4

    [analyze] = tr[StageId.S06_PRINTABILITY]
    assert analyze["model"] == "print/analyze" and analyze["credits"] == 0

    baseline = tr[StageId.S07_BASELINE]
    assert [e["kind"] for e in baseline] == ["meshy", "slicer"]
    assert baseline[0]["model"] == "print/multi-color"
    assert baseline[0]["files"] == ["s07_baseline/a1/baseline_cartoon.3mf"]
    sb = body(rt, job_id, baseline[1])
    assert sb["output"]["json"]["ok"] is True and sb["output"]["json"]["print_time_s"] == 4321.0
    assert sb["input"]["params"]["printer_profile"] == "Bambu Lab X2D 0.4 nozzle"

    jdir = rt.store.job_dir(job_id)
    all_entries = [e for es in tr.values() for e in es]
    assert len(all_entries) == 15  # two more checks: each code composite, then its edit
    for e in all_entries:
        assert (jdir / e["path"]).is_file()
        assert f"{job_id}/{e['path']}" in store.uploaded
        assert re.fullmatch(r"s\d\d_[a-z0-9_]+/a\d+/trace/\d\d-[a-z]+\.json", e["path"])
    for f in jdir.rglob("trace/*.json"):
        assert not SECRET_RE.search(f.read_text()), f


def test_second_advance_adds_no_entries(settings, photos):
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos, product_type="keychain"))
    assert status == JobStatus.DONE
    before = entries(rt, job_id)
    files = sorted(rt.store.job_dir(job_id).rglob("trace/*.json"))
    assert run(rt.orchestrator.advance(job_id)) == JobStatus.DONE
    assert entries(rt, job_id) == before
    assert sorted(rt.store.job_dir(job_id).rglob("trace/*.json")) == files


def test_trace_errors_never_fail_a_stage(settings, photos, monkeypatch, caplog):
    def boom(path, body):
        raise OSError("disk full")

    monkeypatch.setattr(trace, "_write_body", boom)
    rt = make_rt(settings)
    with caplog.at_level(logging.INFO, logger="p2p_worker.events"):
        job_id, status, _ = run(_local(rt, photos, product_type="keychain"))
    assert status == JobStatus.DONE
    for s in THIN:
        m = rt.store.load_manifest(job_id, s, rt.store.load_job(job_id).attempts[s])
        assert m.status == StageStatus.SUCCEEDED
        assert not m.data.get("trace")
    failed = [r for r in caplog.records if getattr(r, "event", None) == "trace_failed"]
    assert failed and "disk full" in failed[0].fields["error"]


# ------------------------------------------------------------------ backfill


def _m1_shaped_job(settings, photos) -> tuple[str, Path]:
    """A finished thin-slice job with its traces removed and old-style signed URLs put back
    into the manifests, like the Bronco run."""
    rt = make_rt(settings)
    job_id, status, _ = run(_local(rt, photos, prompt="nameplate for my dog named Rex",
                                   product_type="nameplate"))
    assert status == JobStatus.DONE
    jdir = rt.store.job_dir(job_id)
    for d in jdir.rglob("trace"):
        shutil.rmtree(d)
    supa = (f"https://abc.supabase.co/storage/v1/object/sign/artifacts/{job_id}/"
            f"s03_concept/a1/approved_concept.png?token={JWT}")
    for p in jdir.glob("s*/a*/manifest.json"):
        d = json.loads(p.read_text())
        d["data"].pop("trace", None)
        d["outputs"] = [o for o in d["outputs"] if o["role"] != "slice_log"]
        for t in d["meshy_tasks"]:
            if t["endpoint"].endswith("multi-image-to-3d"):
                t["request"]["texture_image_urls"] = [supa]
                t["result"]["model_urls"]["glb"] = (
                    "https://assets.meshy.ai/x/model.glb?Expires=1790225754&Signature=AbC~d_e&"
                    "Key-Pair-Id=KXYZ")
        p.write_text(json.dumps(d))
    log = jdir / "s07_baseline" / "a1" / "baseline_x2d.slice.log"
    log.write_text("[2026-09-24 09:09:32.061990] [0x1] [trace] start\n"
                   f"[2026-09-24 09:09:35.561990] [0x1] [info] fetched {supa}\n")
    return job_id, jdir


def test_backfill_builds_traces_and_removes_signed_urls(settings, photos):
    _, jdir = _m1_shaped_job(settings, photos)
    assert SECRET_RE.search((jdir / "s05_mesh/a1/manifest.json").read_text())
    stats = trace.backfill_job(jdir)
    assert stats["built"] == 6 and stats["entries"] == 11  # the plates drawn in code
    kinds = {}
    for p in jdir.glob("s*/a*/manifest.json"):
        text = p.read_text()
        assert not SECRET_RE.search(text), p
        m = Manifest.model_validate_json(text)
        kinds[m.stage] = [e["kind"] for e in m.data.get("trace") or []]
        for e in m.data.get("trace") or []:
            assert e["reconstructed"] is True and (jdir / e["path"]).is_file()
    assert kinds[StageId.S01_INTAKE] == ["claude"]
    assert kinds[StageId.S03_CONCEPT] == ["meshy"] * 2 + ["claude"] * 2
    assert kinds[StageId.S04_MULTIVIEW] == ["meshy", "claude"]
    assert kinds[StageId.S07_BASELINE] == ["meshy", "slicer"]
    for f in jdir.rglob("trace/*.json"):
        assert not SECRET_RE.search(f.read_text()), f
    mesh = json.loads(next(jdir.glob("s05_mesh/a1/trace/01-meshy.json")).read_text())
    assert "s03_concept/a1/approved_concept.png" in mesh["input"]["images"]
    assert mesh["output"]["files"][0].endswith("/mesh_m1.glb")
    concept = json.loads(next(jdir.glob("s03_concept/a1/trace/01-meshy.json")).read_text())
    assert concept["input"]["images"] == ["input/photo_1.jpg", "input/photo_2.jpg"]
    slicer = json.loads(next(jdir.glob("s07_baseline/a1/trace/02-slicer.json")).read_text())
    assert slicer["duration_s"] == 3.5 and "fetched storage:" not in slicer["output"]["text"]
    m7 = Manifest.model_validate_json((jdir / "s07_baseline/a1/manifest.json").read_text())
    assert any(o.role == "slice_log" for o in m7.outputs)
    # a second run changes nothing
    again = trace.backfill_job(jdir)
    assert again["built"] == 0 and again["rewritten"] == 0


class FakeSupabase:
    """PostgREST and Storage as the upload sees them: stage_runs rows already there."""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.requests: list[tuple[str, str, str, bytes]] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append((req.method, req.url.path, str(req.url.query, "utf-8")
                              if isinstance(req.url.query, bytes) else str(req.url.query),
                              req.content))
        if req.url.path == "/rest/v1/stage_runs" and req.method == "GET":
            return httpx.Response(200, json=self.rows)
        return httpx.Response(201 if req.method == "POST" else 204)


def test_backfill_upload_adds_traces_to_existing_rows_only(settings, photos):
    job_id, jdir = _m1_shaped_job(settings, photos)
    remote = []
    for p in sorted(jdir.glob("s*/a*/manifest.json")):
        m = json.loads(p.read_text())
        remote.append({"stage": m["stage"], "attempt": m["attempt"], "status": m["status"],
                       "manifest": m})
    # a local rerun Supabase never saw (like Bronco's s07 a2): not uploaded, reported
    local_only = jdir / "s07_baseline" / "a2"
    local_only.mkdir()
    m7 = json.loads((jdir / "s07_baseline/a1/manifest.json").read_text())
    (local_only / "manifest.json").write_text(json.dumps({**m7, "attempt": 2}))
    fake = FakeSupabase(remote)
    s = settings.with_(supabase_url="https://fake.supabase.co",
                       supabase_service_key="sb_secret_fake_service_key_123")
    store = SupabaseStore(s, transport=httpx.MockTransport(fake.handler))
    stats = trace.backfill_job(jdir, store=store, secrets=["sb_secret_fake_service_key_123"])
    assert stats["local_only"] == ["s07_baseline/a2"]
    uploads = {p.split(f"/storage/v1/object/artifacts/{job_id}/", 1)[1]
               for m, p, _, _ in fake.requests if m == "POST" and "/storage/v1/object/" in p}
    traces = {p.relative_to(jdir).as_posix() for p in jdir.glob("s*/a1/trace/*.json")}
    assert traces and traces == uploads - {"s07_baseline/a1/baseline_x2d.slice.log"}
    assert "s07_baseline/a1/baseline_x2d.slice.log" in uploads
    assert not any("/a2/" in u for u in uploads)
    assert stats["uploaded"] == len(uploads)
    patches = [(q, json.loads(b)) for m, p, q, b in fake.requests
               if m == "PATCH" and p == "/rest/v1/stage_runs"]
    assert stats["patched"] == len(patches) == 6  # the six thin-slice attempts
    by_stage = {q: body for q, body in patches}
    s03 = next(body for q, body in by_stage.items() if "s03_concept" in q)
    assert list(s03) == ["manifest"]  # only the manifest column changes
    assert [e["kind"] for e in s03["manifest"]["data"]["trace"]] == ["meshy"] * 2 + ["claude"] * 2
    s07 = next(body for q, body in by_stage.items() if "s07_baseline" in q)
    assert any(o["role"] == "slice_log" for o in s07["manifest"]["outputs"])
    assert s07["manifest"]["status"] == m7["status"]
    for _, _, _, b in fake.requests:
        if b:
            assert not SECRET_RE.search(b.decode(errors="replace"))
    assert not any(m in ("DELETE", "PUT") for m, _, _, _ in fake.requests)
    assert not any(m == "POST" and "/rest/v1/" in p for m, p, _, _ in fake.requests)
