"""Meshy client: price table, DELETE guard, key handling, fake server rules, fixtures."""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import run
from p2p_worker.clients.meshy import MeshyClient, apply_task_object, estimate_credits
from p2p_worker.config import Settings
from p2p_worker.errors import ForbiddenRequest, VendorError
from p2p_worker.fakes.meshy import FakeMeshy


def client(fake: FakeMeshy) -> MeshyClient:
    return MeshyClient(api_key="msy_fake_test_key", test_mode=True, transport=fake.transport())


@pytest.mark.parametrize("endpoint,body,credits", [
    ("image-to-image", {"ai_model": "nano-banana"}, 3),
    ("image-to-image", {"ai_model": "nano-banana-2"}, 6),
    ("image-to-image", {"ai_model": "nano-banana-pro"}, 9),
    ("image-to-image", {"ai_model": "gpt-image-2"}, 12),
    ("multi-image-to-3d", {"ai_model": "latest", "geometry_resolution": "2k"}, 35),
    ("multi-image-to-3d", {"ai_model": "meshy-7.1"}, 30),
    ("multi-image-to-3d", {"ai_model": "latest", "should_texture": False}, 20),
    ("multi-image-to-3d", {"ai_model": "meshy-6-lite"}, 15),
    ("multi-image-to-3d", {"ai_model": "meshy-6-lite", "should_texture": False}, 5),
    ("image-to-3d", {"ai_model": "latest", "geometry_resolution": "4k"}, 35),
    ("retexture", {}, 10),
    ("print-multi-color", {}, 10),
    ("print-analyze", {}, 0),
    ("print-repair", {}, 10),
    ("print-split", {}, 10),
])
def test_price_table(endpoint, body, credits):
    assert estimate_credits(endpoint, body) == credits


def test_delete_is_never_sent():
    fake = FakeMeshy(fixtures_dir=None)
    c = client(fake)

    async def go():
        with pytest.raises(ForbiddenRequest):
            await c._http.delete("/openapi/v1/image-to-image/abc")
        with pytest.raises(ForbiddenRequest):
            await c._http.request("DELETE", "/openapi/v1/print/multi-color/abc")
        with pytest.raises(ForbiddenRequest):
            await c._assets.delete("https://assets.meshy.test/abc/x.png")
        await c.aclose()

    run(go())
    assert fake.deletes == 0
    assert not hasattr(MeshyClient, "delete") and not hasattr(MeshyClient, "delete_task")


def test_test_mode_requires_the_fake_and_live_requires_a_key(tmp_path):
    s = Settings(repo_root=tmp_path, jobs_dir=tmp_path / "jobs")
    with pytest.raises(ForbiddenRequest):
        MeshyClient.from_settings(s)  # test mode never reaches the real API
    with pytest.raises(ForbiddenRequest):
        MeshyClient.from_settings(s.with_(meshy_mode="live", meshy_live_key=None))
    live = MeshyClient.from_settings(s.with_(meshy_mode="live", meshy_live_key="msy_secret_1"),
                                     transport=FakeMeshy(fixtures_dir=None).transport())
    assert live.test_mode is False
    assert "msy_secret_1" not in repr(live) and "msy_secret_1" not in repr(
        s.with_(meshy_live_key="msy_secret_1"))
    run(live.aclose())


def test_create_records_task_and_strips_data_uris():
    fake = FakeMeshy(fixtures_dir=None)
    c = client(fake)
    body = {"ai_model": "nano-banana-pro", "prompt": "a dog",
            "reference_image_urls": ["data:image/png;base64," + "A" * 5000],
            "remove_background": True}

    async def go():
        t = await c.create("image-to-image", body, "concept")
        await fake.drain()
        await c.aclose()
        return t

    task = run(go())
    assert task.status == "PENDING" and task.test_mode and task.credits == 9
    assert task.endpoint == "openapi/v1/image-to-image"
    ref = task.request["reference_image_urls"][0]
    assert ref.startswith("data:image/png;sha256=") and len(ref) < 80
    assert "Bearer" not in json.dumps(task.model_dump(mode="json"))


def test_documented_request_rules_are_enforced_by_the_fake():
    fake = FakeMeshy(fixtures_dir=None, auto_complete=False)
    c = client(fake)

    async def go():
        t = await c.create("multi-image-to-3d", {"image_urls": ["https://x/a.png"]}, "mesh")
        fake.complete(t.task_id)
        # Retexture does not accept a Multi-Image to 3D task id
        with pytest.raises(VendorError) as e:
            await c.create("retexture", {"input_task_id": t.task_id,
                                         "text_style_prompt": "red"}, "retex")
        assert e.value.code == "validation" and e.value.status_code == 400
        with pytest.raises(VendorError):
            await c.create("multi-image-to-3d", {"image_urls": ["https://x/a.png"],
                                                 "texture_image_urls": ["https://x/a.png"],
                                                 "texture_prompt": "red"}, "bad")
        with pytest.raises(VendorError):
            await c.create("multi-image-to-3d", {"image_urls": ["https://x/a.png"],
                                                 "geometry_resolution": "4k"}, "bad")
        with pytest.raises(VendorError):
            await c.create("image-to-image", {"ai_model": "gpt-image-2", "prompt": "x",
                                              "reference_image_urls": ["https://x/a.png"],
                                              "aspect_ratio": "1:1",
                                              "generate_multi_view": True}, "bad")
        ok = await c.create("print-multi-color", {"input_task_id": t.task_id, "max_colors": 4,
                                                  "style": "cartoon"}, "print")
        await c.aclose()
        return ok

    assert run(go()).credits == 10


def test_apply_task_object_refund_and_adjust():
    fake = FakeMeshy(fixtures_dir=None, auto_complete=False)
    c = client(fake)

    async def go():
        t = await c.create("image-to-image", {"ai_model": "gpt-image-2", "prompt": "x",
                                              "reference_image_urls": ["https://x/a.png"]}, "c")
        await c.aclose()
        return t

    t = run(go())
    failed = apply_task_object(t, {"status": "FAILED", "consumed_credits": 0,
                                   "task_error": {"type": "server_error", "message": "boom"}})
    assert failed.status == "FAILED" and failed.refunded and failed.credits == 0
    ok = apply_task_object(t, {"status": "SUCCEEDED", "consumed_credits": 11,
                               "image_urls": ["https://a/b.png"], "finished_at": 1700000000000})
    assert ok.credits == 11 and ok.result["image_urls"] == ["https://a/b.png"]
    assert ok.finished_at.year == 2023


def test_fixtures_shape_the_fake_results(tmp_path):
    recorded = {
        "id": "0192-real", "type": "image-to-image", "ai_model": "nano-banana", "prompt": "p",
        "status": "SUCCEEDED", "progress": 100, "created_at": 1, "started_at": 2,
        "finished_at": 3, "expires_at": 4, "consumed_credits": 3,
        "image_urls": ["https://assets.meshy.ai/abc/tasks/0192-real/output/image.png?Expires=1"],
        "extra_field_from_real_api": {"kept": True},
    }
    (tmp_path / "image-to-image.json").write_text(json.dumps(recorded))
    events = []
    fake = FakeMeshy(fixtures_dir=tmp_path, auto_complete=False, on_event=events.append)
    c = client(fake)

    async def go():
        t = await c.create("image-to-image", {"ai_model": "nano-banana-2", "prompt": "q",
                                              "reference_image_urls": ["https://x/a.png"]}, "c")
        obj = fake.complete(t.task_id)
        n = await c.download(obj["image_urls"][0], tmp_path / "out.png")
        got = await c.get_task("image-to-image", t.task_id)
        await c.aclose()
        return t, obj, n, got

    t, obj, n, got = run(go())
    assert obj["id"] == t.task_id and obj["ai_model"] == "nano-banana-2"
    assert obj["image_urls"][0].startswith("https://assets.meshy.test/")
    assert obj["extra_field_from_real_api"] == {"kept": True}
    assert n > 0 and (tmp_path / "out.png").read_bytes()[:4] == b"\x89PNG"
    assert got["status"] == "SUCCEEDED" and events[-1]["status"] == "SUCCEEDED"
    assert fake.asset_requests_with_auth == 0


def test_rate_limit_is_retried_with_backoff(monkeypatch):
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"message": "RateLimitExceeded"},
                                  headers={"retry-after": "0"})
        return httpx.Response(202, json={"result": "t1"})

    c = MeshyClient(api_key="k", test_mode=True, transport=httpx.MockTransport(handler))

    async def go():
        t = await c.create("print-analyze", {"model_url": "https://x/a.glb"}, "analyze")
        await c.aclose()
        return t

    assert run(go()).task_id == "t1" and calls["n"] == 2
