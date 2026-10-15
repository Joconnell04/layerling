"""FastAPI routes (webhook token, messages) and the Supabase store against a fake PostgREST."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from conftest import make_rt, run
from fastapi.testclient import TestClient
from p2p_contracts import JobStatus, Manifest, SpendEvent, StageId, StageStatus
from p2p_worker.app import Scheduler, create_app
from p2p_worker.clients.supabase import (
    RealtimeListener,
    SupabaseError,
    SupabaseStore,
    parse_realtime,
)


def test_webhook_requires_token_and_records_event(settings, photos):
    s = settings.with_(webhook_token="t0ken")
    rt = make_rt(s)
    app = create_app(rt, Scheduler(rt))
    with TestClient(app) as client:
        assert client.get("/healthz").json()["ok"] is True
        assert client.post("/webhooks/meshy", json={"id": "x"}).status_code == 401
        assert client.post("/webhooks/meshy?token=nope", json={"id": "x"}).status_code == 401
        r = client.post("/webhooks/meshy?token=t0ken",
                        json={"id": "task-1", "status": "SUCCEEDED", "image_urls": []})
        assert r.status_code == 200
    ev = rt.store.meshy_events("task-1")
    assert ev and ev[0].status == "SUCCEEDED" and ev[0].event["_source"] == "local_webhook"


def test_webhook_without_configured_token_refuses(settings):
    rt = make_rt(settings.with_(webhook_token=None))
    with TestClient(create_app(rt, Scheduler(rt))) as client:
        assert client.post("/webhooks/meshy?token=x", json={"id": "a"}).status_code == 503


def test_local_serve_flow_answers_through_http(settings, photos):
    """Drive a job through the app's message route; the scheduler advances it."""
    rt = make_rt(settings)
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    scheduler = Scheduler(rt)
    app = create_app(rt, scheduler)

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://w") as c:
            await c.post(f"/jobs/{job.id}/advance")
            await scheduler.idle()
            q = [m for m in rt.store.list_messages(job.id) if m.kind == "questions"][-1]
            answers = {"answers": [{"question_id": qq["id"],
                                    "option_ids": [qq["options"][0]["id"]]}
                                   for qq in q.payload["questions"]]}
            bad = await c.post(f"/jobs/{job.id}/messages", json={"kind": "answers",
                                                                  "payload": {"x": 1}})
            assert bad.status_code == 422
            ok = await c.post(f"/jobs/{job.id}/messages", json={"kind": "answers",
                                                                 "payload": answers})
            assert ok.status_code == 200
            await scheduler.idle()
        await rt.fake.drain()

    run(go())
    job2 = rt.store.load_job(job.id)
    assert job2.status == JobStatus.WAITING_USER and job2.stage == StageId.S03_CONCEPT


class FakePostgrest:
    """Just enough PostgREST and Storage to exercise SupabaseStore."""

    def __init__(self):
        self.tables: dict[str, list[dict]] = {
            "jobs": [], "stage_runs": [], "messages": [], "spend": [], "meshy_events": [],
            "workers": [], "filaments": []}
        self.requests: list[tuple[str, str, dict]] = []
        self.objects: dict[str, bytes] = {}

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["apikey"] == "service-key"
        assert req.headers["authorization"] == "Bearer service-key"
        path = req.url.path
        params = dict(req.url.params)
        self.requests.append((req.method, path, params))
        if path.startswith("/storage/v1/object/sign/"):
            return httpx.Response(200, json={"signedURL": "/object/sign/x?token=abc"})
        if path.startswith("/storage/v1/object/"):
            key = path.split("/storage/v1/object/", 1)[1]
            if req.method == "POST":
                assert req.headers.get("x-upsert") == "true"
                self.objects[key] = req.content
                return httpx.Response(200, json={"Key": key})
            if key not in self.objects:
                return httpx.Response(404, json={"error": "not_found"})
            return httpx.Response(200, content=self.objects[key])
        table = path.split("/rest/v1/", 1)[1]
        rows = self.tables[table]
        if req.method == "POST":
            body = json.loads(req.content)
            items = body if isinstance(body, list) else [body]
            out = []
            for it in items:
                it = dict(it)
                it.setdefault("id", len(rows) + 1 if table == "meshy_events" else
                              f"{table}-{len(rows) + 1}")
                it.setdefault("created_at", datetime.now(UTC).isoformat())
                it.setdefault("received_at", datetime.now(UTC).isoformat())
                it.setdefault("consumed_at", None)
                it.setdefault("processed_at", None)
                rows.append(it)
                out.append(it)
            return httpx.Response(201, json=out)
        def match(r):
            for k, v in params.items():
                if k in ("select", "order", "limit", "on_conflict"):
                    continue
                if k == "manifest->meshy_tasks":
                    tid = json.loads(v[3:])[0]["task_id"]
                    if not any(t["task_id"] == tid for t in r["manifest"].get("meshy_tasks", [])):
                        return False
                    continue
                op, _, val = v.partition(".")
                cur = r.get(k)
                cur_s = json.dumps(cur) if isinstance(cur, bool) else str(cur)
                if op == "eq" and cur_s != val:
                    return False
                if op == "in" and cur_s not in val.strip("()").split(","):
                    return False
                if op == "is" and cur is not None:
                    return False
            return True
        hit = [r for r in rows if match(r)]
        if req.method == "PATCH":
            upd = json.loads(req.content)
            for r in hit:
                r.update(upd)
            return httpx.Response(204)
        return httpx.Response(200, json=hit)


def test_supabase_store_round_trips(settings):
    fake = FakePostgrest()
    s = settings.with_(supabase_url="https://proj.supabase.co", supabase_service_key="service-key")
    store = SupabaseStore(s, transport=httpx.MockTransport(fake.handler))
    fake.tables["jobs"].append({
        "id": "job-1", "owner_id": "user-1", "prompt": "keychain of my dog",
        "product_type": "keychain", "photos": [{"path": "job-1/photo_1.jpg"}],
        "settings": {}, "status": "queued", "profile": "thin_slice", "budget_usd": 10,
        "budget_credits": 400, "spent_usd": 0, "spent_credits": 0, "addons": []})
    fake.objects["inputs/job-1/input/photo_1.jpg"] = b"\xff\xd8photo"  # contract key

    assert store.list_jobs() == ["job-1"]
    job = store.load_job("job-1")  # ingested: photo downloaded, request.json written
    assert (store.job_dir("job-1") / "input" / "photo_1.jpg").read_bytes() == b"\xff\xd8photo"
    assert job.product_type.value == "keychain" and job.photos[0].path == "input/photo_1.jpg"

    msg = store.post_message("job-1", "assistant", "note", {"text": "hi"},
                             stage=StageId.S01_INTAKE)
    assert fake.tables["messages"][0]["owner_id"] == "user-1"
    store.post_message("job-1", "user", "answers", {"answers": []})
    unconsumed = store.list_messages("job-1", role="user", kinds={"answers"}, unconsumed=True)
    assert len(unconsumed) == 1
    store.consume_message("job-1", unconsumed[0].id)
    assert not store.list_messages("job-1", role="user", unconsumed=True)
    assert msg.kind == "note"
    with pytest.raises(ValueError):
        store.post_message("job-1", "assistant", "answers", {"answers": []})  # wrong role

    m = Manifest(job_id="job-1", stage=StageId.S03_CONCEPT, attempt=1,
                 status=StageStatus.RUNNING, started_at=datetime.now(UTC))
    m.meshy_tasks = []
    store.write_manifest(m)
    store.write_manifest(m.model_copy(update={"status": StageStatus.SUCCEEDED}))
    upserts = [r for r in fake.requests if r[1] == "/rest/v1/stage_runs"]
    assert all(r[2].get("on_conflict") == "job_id,stage,attempt" for r in upserts)

    store.record_spend(SpendEvent(at=datetime.now(UTC), job_id="job-1", vendor="meshy",
                                  kind="image-to-image", credits=12, usd=0.24, test_mode=False),
                       usd_list=0.24, billing="credits")
    store.record_spend(SpendEvent(at=datetime.now(UTC), job_id="job-1", vendor="anthropic",
                                  kind="intake", usd=0.0, ref="sess-1", test_mode=False),
                       usd_list=0.0123, billing="subscription")
    sub = next(r for r in fake.tables["spend"] if r["vendor"] == "anthropic")
    assert sub["billing"] == "subscription" and sub["usd_list"] == 0.0123 and sub["usd"] == 0
    assert fake.tables["spend"][0]["billing"] == "credits"
    assert fake.tables["spend"][0]["owner_id"] == "user-1"
    fake.tables["spend"] = [r for r in fake.tables["spend"] if r["vendor"] == "meshy"]
    store.record_spend(SpendEvent(at=datetime.now(UTC), job_id="job-1", vendor="meshy",
                                  kind="image-to-image", credits=9, usd=0.18, test_mode=True))
    fake.tables["spend"] = [r for r in fake.tables["spend"] if not r["test_mode"]]
    assert store.build_totals() == {"usd": 0.24, "meshy_credits": 12}

    # the job owner's filaments rows become the library (lab_l/a/b -> lab)
    from p2p_worker.stages.adapters import library_for

    fake.tables["filaments"] += [
        {"id": "generic-pla-gray", "owner_id": "user-1", "brand": "Generic", "line": "PLA",
         "name": "Gray", "material": "PLA", "hex": "#808080", "lab_l": 53.6, "lab_a": 0.0,
         "lab_b": 0.0, "owned": True, "spools": 1, "ams_slot": 2, "swatch_path": None,
         "density_g_cm3": 1.24, "td_mm": None},
        {"id": "someone-else", "owner_id": "user-2", "brand": "X", "line": "PLA",
         "name": "Red", "material": "PLA", "hex": "#FF0000", "lab_l": 53.2, "lab_a": 80.1,
         "lab_b": 67.2, "owned": True, "spools": 1, "ams_slot": 1},
        {"id": "studio-black", "owner_id": None, "brand": "Generic", "line": "PLA",
         "name": "Black", "material": "PLA", "hex": "#161616", "lab_l": 7.2, "lab_a": 0.0,
         "lab_b": 0.0, "owned": True, "spools": 1, "ams_slot": 1}]
    lib, source = library_for(store, "job-1", s.repo_root)
    assert source == "supabase"  # the owner's spools plus the shared studio AMS
    assert sorted(f.id for f in lib.filaments) == ["generic-pla-gray", "studio-black"]
    gray = next(f for f in lib.filaments if f.id == "generic-pla-gray")
    assert gray.lab == (53.6, 0.0, 0.0) and gray.ams_slot == 2
    fake.tables["filaments"].clear()
    lib, source = library_for(store, "job-1", s.repo_root)
    assert source == "filaments/library.json" and lib.filaments

    eid = store.insert_meshy_event({"id": "task-9", "status": "SUCCEEDED"})
    assert store.terminal_meshy_event("task-9").id == eid
    store.mark_meshy_processed([eid])
    assert store.terminal_meshy_event("task-9") is None

    (store.job_dir("job-1") / "s03_concept" / "a1").mkdir(parents=True, exist_ok=True)
    (store.job_dir("job-1") / "s03_concept" / "a1" / "c.png").write_bytes(b"png")
    store.upload_artifacts("job-1", ["s03_concept/a1/c.png"])
    assert fake.objects["artifacts/job-1/s03_concept/a1/c.png"] == b"png"
    url = store.signed_url("job-1", "input/photo_1.jpg")
    assert url == "https://proj.supabase.co/storage/v1/object/sign/x?token=abc"
    assert ("POST", "/storage/v1/object/sign/inputs/job-1/input/photo_1.jpg", {}) in fake.requests

    store.heartbeat("w1", version="0.1.0", meshy_mode="test", info={})
    assert fake.tables["workers"][0]["id"] == "w1"
    store.update_job_row("job-1", status="done", pause_reason=None, not_a_column=1)
    assert fake.tables["jobs"][0]["status"] == "done"
    assert "not_a_column" not in fake.tables["jobs"][0]
    assert "service-key" not in repr(store)


def test_realtime_message_parsing():
    seen = []
    lst = RealtimeListener("https://proj.supabase.co", "k", ["messages", "meshy_events"],
                           lambda *a: seen.append(a))
    join = lst.join_message()
    assert join["event"] == "phx_join"
    assert [c["table"] for c in join["payload"]["config"]["postgres_changes"]] == [
        "messages", "meshy_events"]
    lst.handle(json.dumps({"event": "phx_reply", "topic": "realtime:p2p-worker",
                           "payload": {"status": "ok"}}))
    assert lst.connected.is_set()
    lst.handle(json.dumps({"event": "postgres_changes", "topic": "realtime:p2p-worker",
                           "payload": {"data": {"table": "meshy_events", "type": "INSERT",
                                                "record": {"task_id": "t1"}}}}))
    lst.handle("not json")
    assert seen == [("meshy_events", "INSERT", {"task_id": "t1"})]
    assert parse_realtime({"event": "presence_state"}) is None
    assert "k" not in repr(lst) or "apikey" not in repr(lst)


def test_size_tier_sets_the_target_size():
    from p2p_worker.clients.supabase import tier_size

    assert tier_size({"product_type": "keychain", "size_tier": "xs"}) == (40.0, "xs")
    assert tier_size({"product_type": "keychain", "size_tier": "l",
                      "target_size_mm": 58}) == (58.0, "l")  # an explicit size stays
    assert tier_size({"product_type": "nameplate"}) == (None, None)


def test_supabase_reads_retry_a_dropped_connection_and_writes_do_not(settings, monkeypatch):
    """live2 run: GET /rest/v1/meshy_events raised RemoteProtocolError once, failing s05 while
    it waited for a paid 3D model."""
    from p2p_worker.clients import supabase as sb

    monkeypatch.setattr(sb, "BACKOFF_S", (0.0, 0.0, 0.0))
    fake = FakePostgrest()
    drops = {"GET": 1, "POST": 1}

    def handler(req: httpx.Request) -> httpx.Response:
        if drops.get(req.method, 0) > 0:
            drops[req.method] -= 1
            raise httpx.RemoteProtocolError("Server disconnected", request=req)
        return fake.handler(req)

    s = settings.with_(supabase_url="https://proj.supabase.co", supabase_service_key="service-key")
    store = SupabaseStore(s, transport=httpx.MockTransport(handler))
    fake.tables["meshy_events"].append({"id": 1, "task_id": "t1", "status": "SUCCEEDED",
                                        "event": {}, "processed_at": None,
                                        "received_at": "2026-01-01T00:00:00+00:00"})
    assert [e.id for e in store.meshy_events("t1")] == [1]  # one drop, then the read works
    assert drops["GET"] == 0
    with pytest.raises(SupabaseError, match="RemoteProtocolError"):
        store._rest("POST", "messages", body={"job_id": "j"})  # a write is never repeated
    assert not fake.tables["messages"]
