"""RunpodSegmenter against a mocked HTTP transport. The fake endpoint runs the real handler
(validation, output assembly, RLE) with a fake model, so the wire format is tested end to end."""

import json

import httpx
import numpy as np
import pytest
from p2p_contracts import SpendEvent, StageId
from p2p_sam3.client import BudgetExceeded, RunpodSegmenter, Sam3Error


class FakeModel:
    """Stands in for TorchSam3: one square mask per prompt, score depends on prompt length."""

    def __init__(self):
        self.info = {
            "id": "fake/sam3",
            "source": "test",
            "revision": None,
            "dtype": "float32",
            "device": "cpu",
            "torch": None,
            "transformers": None,
        }

    def vision(self, image, prompts=None):
        return np.asarray(image).shape[:2]

    def prompt(self, state, text, score_threshold, mask_threshold):
        h, w = state
        masks, scores = [], []
        for i, s in enumerate((0.9, 0.3, 0.7)[: len(text) % 3 + 1]):
            m = np.zeros((h, w), bool)
            m[i : i + 4, i : i + 5] = True
            masks.append(m)
            scores.append(s)
        keep = [i for i, s in enumerate(scores) if s > score_threshold]
        boxes = np.array([[i, i, i + 5, i + 4] for i in range(len(scores))], float)
        return np.array(scores)[keep], boxes[keep], np.array(masks)[keep]


@pytest.fixture
def fake_worker(handler_mod, monkeypatch):
    monkeypatch.setattr(handler_mod, "_STATE", {})
    handler_mod.load_model(factory=FakeModel)
    handler_mod._STATE["load_s"] = 12.5
    return handler_mod


class FakeRunpod:
    """Minimal /runsync, /run, /status, /cancel over the real handler."""

    def __init__(self, handler_mod, pending_polls=0, http_errors=0, fail=False):
        self.h = handler_mod
        self.pending_polls = pending_polls
        self.http_errors = http_errors
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self.jobs: dict[str, dict] = {}
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        assert request.headers["authorization"] == "Bearer test-key"
        if self.http_errors:
            self.http_errors -= 1
            return httpx.Response(503, text="busy")
        if path.endswith(("/runsync", "/run")):
            body = json.loads(request.content)
            self.bodies.append(body)
            job_id = f"job-{len(self.jobs) + 1}"
            out = (
                {"error": "CUDA out of memory"}
                if self.fail
                else self.h.handler({"id": job_id, "input": body["input"]})
            )
            status = "FAILED" if "error" in out else "COMPLETED"
            done = {"id": job_id, "status": status, "delayTime": 800, "executionTime": 2500}
            done.update({"error": out["error"]} if "error" in out else {"output": out})
            self.jobs[job_id] = {"done": done, "polls": self.pending_polls}
            if self.pending_polls or path.endswith("/run"):
                return httpx.Response(200, json={"id": job_id, "status": "IN_QUEUE"})
            return httpx.Response(200, json=done)
        if "/status/" in path:
            job = self.jobs[path.rsplit("/", 1)[1]]
            if job["polls"] > 0:
                job["polls"] -= 1
                return httpx.Response(
                    200, json={"id": path.rsplit("/", 1)[1], "status": "IN_PROGRESS"}
                )
            return httpx.Response(200, json=job["done"])
        if "/cancel/" in path:
            return httpx.Response(200, json={"status": "CANCELLED"})
        return httpx.Response(404)


def make_client(fake, **kw):
    http = httpx.Client(transport=httpx.MockTransport(fake))
    sleeps = []
    seg = RunpodSegmenter("ep123", "test-key", http=http, sleep=sleeps.append, **kw)
    return seg, sleeps


def images(n, size=(40, 30)):
    return [np.full((*size, 3), 50 + i, np.uint8) for i in range(n)]


def test_runsync_round_trip(fake_worker):
    fake = FakeRunpod(fake_worker)
    seg, _ = make_client(fake)
    events = []
    guard_calls = []
    res = seg.segment(
        images(2),
        ["eye", "nose"],
        score_threshold=0.5,
        max_masks_per_prompt=5,
        budget_guard=guard_calls.append,
        on_spend=events.append,
        job_id="J",
        stage=StageId.S07_BASELINE,
    )
    assert fake.calls[0] == ("POST", "/v2/ep123/runsync")
    body = fake.bodies[0]["input"]
    assert body["prompts"] == ["eye", "nose"] and len(body["images"]) == 2
    assert len(guard_calls) == 1 and guard_calls[0] > 0

    assert [im.image_index for im in res.images] == [0, 1]
    eye = res.images[0].get("eye")  # len 3 -> 1 mask, score 0.9
    assert eye.scores == [0.9] and eye.areas == [20]
    nose = res.images[1].get("nose")  # len 4 -> 2 masks, 0.3 dropped by threshold
    assert nose.scores == [0.9] and nose.boxes == [(0.0, 0.0, 5.0, 4.0)]
    m = eye.decoded()[0]
    assert m.shape == (40, 30) and m[:4, :5].all() and m.sum() == 20

    rec = res.requests[0]
    assert rec.ref == "job-1" and rec.mode == "runsync" and rec.cold_start
    # 2.5 s execution + 12.5 s model load (cold) + 5 s idle timeout
    assert rec.gpu_seconds == pytest.approx(20.0)
    assert res.usd == pytest.approx(20.0 * 0.58 / 3600, rel=1e-4)
    assert len(events) == 1 and isinstance(events[0], SpendEvent)
    ev = events[0]
    assert (ev.vendor, ev.kind, ev.ref, ev.job_id, ev.stage) == (
        "runpod",
        "sam3",
        "job-1",
        "J",
        StageId.S07_BASELINE,
    )
    assert not ev.test_mode and ev.usd == pytest.approx(res.usd, abs=1e-6)
    calls = res.model_calls()
    assert calls[0].vendor == "runpod" and calls[0].request_id == "job-1"

    # Second call on the same warm worker: no model load in the bill.
    res2 = seg.segment(images(1), ["eye"])
    assert not res2.requests[0].cold_start
    assert res2.gpu_seconds == pytest.approx(7.5)


def test_runsync_falls_back_to_status_polling(fake_worker):
    fake = FakeRunpod(fake_worker, pending_polls=2)
    seg, sleeps = make_client(fake)
    res = seg.segment(images(1), ["eye"])
    assert [c[0] for c in fake.calls] == ["POST", "GET", "GET", "GET"]
    assert fake.calls[1][1] == "/v2/ep123/status/job-1"
    assert len(sleeps) == 3
    assert res.images[0].get("eye").scores == [0.9]


def test_large_jobs_use_run(fake_worker):
    fake = FakeRunpod(fake_worker)
    seg, _ = make_client(fake, runsync_max_est_s=0.1)
    res = seg.segment(images(1), ["eye"])
    assert fake.calls[0] == ("POST", "/v2/ep123/run")
    assert res.requests[0].mode == "run"


def test_chunking_keeps_global_indices(fake_worker):
    fake = FakeRunpod(fake_worker)
    seg, _ = make_client(fake, max_images_per_request=2)
    spends = []
    res = seg.segment(images(5), ["eye"], on_spend=spends.append)
    assert len(fake.bodies) == 3 and [len(b["input"]["images"]) for b in fake.bodies] == [2, 2, 1]
    assert [im.image_index for im in res.images] == [0, 1, 2, 3, 4]
    assert [r.image_indices for r in res.requests] == [[0, 1], [2, 3], [4]]
    assert len(spends) == 3
    # Idle timeout billed once, after the last request.
    assert [r.gpu_seconds for r in res.requests] == pytest.approx([15.0, 2.5, 7.5])


def test_retries_on_5xx(fake_worker):
    fake = FakeRunpod(fake_worker, http_errors=2)
    seg, sleeps = make_client(fake)
    res = seg.segment(images(1), ["eye"])
    assert sleeps == [1.0, 2.0]
    assert res.images[0].get("eye").scores == [0.9]


def test_gives_up_after_retries(fake_worker):
    fake = FakeRunpod(fake_worker, http_errors=10)
    seg, _ = make_client(fake, max_retries=2)
    with pytest.raises(Sam3Error, match="failed after 3 tries"):
        seg.segment(images(1), ["eye"])


def test_failed_job_raises(fake_worker):
    seg, _ = make_client(FakeRunpod(fake_worker, fail=True))
    with pytest.raises(Sam3Error, match="FAILED: CUDA out of memory"):
        seg.segment(images(1), ["eye"])


def test_validation_error_from_worker_raises(fake_worker):
    seg, _ = make_client(FakeRunpod(fake_worker))
    with pytest.raises(Sam3Error, match="validation: images\\[0\\] is 8x8"):
        seg.segment([np.zeros((8, 8, 3), np.uint8)], ["eye"])


def test_timeout_cancels(fake_worker):
    fake = FakeRunpod(fake_worker, pending_polls=10**6)
    seg, _ = make_client(fake, timeout_s=0.0)
    with pytest.raises(Sam3Error, match="timed out"):
        seg.segment(images(1), ["eye"])
    assert ("POST", "/v2/ep123/cancel/job-1") in fake.calls


def test_budget_guard_blocks_before_request(fake_worker):
    fake = FakeRunpod(fake_worker)
    seg, _ = make_client(fake)

    def guard(usd):
        raise BudgetExceeded(f"over cap by {usd}")

    with pytest.raises(BudgetExceeded):
        seg.segment(images(1), ["eye"], budget_guard=guard)
    with pytest.raises(BudgetExceeded):
        seg.segment(images(1), ["eye"], budget_guard=lambda usd: False)
    assert fake.calls == []


def test_http_4xx_is_not_retried(fake_worker):
    def reject(request):
        return httpx.Response(401, text="bad key")

    seg = RunpodSegmenter(
        "ep",
        "test-key",
        http=httpx.Client(transport=httpx.MockTransport(reject)),
        sleep=lambda s: None,
    )
    with pytest.raises(Sam3Error, match="HTTP 401"):
        seg.segment(images(1), ["eye"])


def test_local_backend_uses_shared_handler_code():
    from p2p_sam3.local import LocalSegmenter

    seg = LocalSegmenter(model=FakeModel())
    res = seg.segment(images(2), ["eye", "nose"], score_threshold=0.5)
    assert res.backend == "local" and res.usd == 0.0 and res.spend == []
    assert res.images[0].get("eye").scores == [0.9]
    assert res.requests[0].mode == "local" and res.requests[0].cold_start
    assert not seg.segment(images(1), ["eye"]).requests[0].cold_start  # stays warm
    assert res.model_calls()[0].vendor == "local"
    with pytest.raises(Sam3Error, match="validation"):
        seg.segment([np.zeros((8, 8, 3), np.uint8)], ["eye"])
