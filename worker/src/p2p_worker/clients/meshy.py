"""Meshy API client (async httpx).

Rules enforced here, not by callers:
- The worker never sends DELETE to Meshy. A DELETE on an IN_PROGRESS task is not refunded, so the
  transport refuses the method before anything leaves the process.
- MESHY_MODE=test means the fake Meshy server (fakes/meshy.py); Meshy has no free test mode any
  more. MESHY_MODE=live uses MESHY_API_KEY against api.meshy.ai; only the orchestrator sets it.
- The auth header goes only to the API host. Asset downloads (signed URLs) use a separate client
  with no credentials.
- Results arrive by webhook. `get_task` is used once per webhook event to confirm the status
  (webhooks are unsigned) and once per task on resume; never in a loop.

Credit prices: PRICES below, from https://docs.meshy.ai/api/pricing (read 2026-09-23).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from p2p_contracts import MeshyTask

from ..errors import ForbiddenRequest, VendorError
from ..util import atomic_write_bytes, manifest_safe, ms_to_dt, utcnow

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "EXPIRED"}

# One price table, in credits per call. SOURCE: https://docs.meshy.ai/api/pricing, read 2026-09-23.
PRICES: dict[str, dict[str, int]] = {
    "image-to-image": {
        "nano-banana": 3, "nano-banana-2": 6, "nano-banana-pro": 9,
        "gpt-image-2": 12, "gpt-image-2-5-flare": 12, "gpt-image-2-5-sunburst": 12,
    },
    "text-to-image": {
        "nano-banana": 3, "nano-banana-2": 6, "nano-banana-pro": 9,
        "gpt-image-2": 9, "gpt-image-2-5-flare": 9, "gpt-image-2-5-sunburst": 9,
    },
    # Image to 3D and Multi-Image to 3D, Meshy 6 / 7 / 7.1 / latest
    "gen3d": {"untextured": 20, "textured": 30, "textured_8k": 35, "geometry_2k_or_4k": 5},
    # meshy-6-lite and Smart Topology models
    "gen3d_lite": {"untextured": 5, "textured": 15, "textured_8k": 20},
    "retexture": {"default": 10, "8k": 15},
    "print-multi-color": {"default": 10},
    "print-analyze": {"default": 0},
    "print-repair": {"default": 10},
    "print-split": {"default": 10},
    "keychain-prototype": {"default": 6},
    "keychain-build": {"default": 30},
    "magnet-prototype": {"default": 6},
    "magnet-build": {"default": 30},
}
# generate_multi_view on Image to Image bills once per request, the single-image price. SOURCE:
# the live probe in docs/research/meshy-iteration.md section 3 ("Probe result"): nano-banana,
# three images, consumed_credits 3, balance 982 -> 979.
MULTI_VIEW_FACTOR = 1
MESHY7 = {"meshy-7", "meshy-7.1", "latest"}


def _image_credits(table: str) -> Callable[[dict[str, Any]], int]:
    def est(body: dict[str, Any]) -> int:
        price = PRICES[table].get(body.get("ai_model", ""), max(PRICES[table].values()))
        return price * (MULTI_VIEW_FACTOR if body.get("generate_multi_view") else 1)

    return est


def _gen3d_credits(body: dict[str, Any]) -> int:
    model = body.get("ai_model", "latest")
    textured = body.get("should_texture", True)
    tex8k = body.get("texture_resolution") == "8k"
    if model == "meshy-6-lite" or body.get("model_type") in {"lowpoly", "smart-topology"}:
        t = PRICES["gen3d_lite"]
        return t["textured_8k"] if textured and tex8k else t["textured" if textured else "untextured"]
    t = PRICES["gen3d"]
    base = (t["textured_8k"] if tex8k else t["textured"]) if textured else t["untextured"]
    geo = body.get("geometry_resolution") or ("2k" if body.get("ultra_mode") else "standard")
    if model in MESHY7 and geo in {"2k", "4k"}:
        base += t["geometry_2k_or_4k"]
    return base


def _retexture_credits(body: dict[str, Any]) -> int:
    return PRICES["retexture"]["8k" if body.get("texture_resolution") == "8k" else "default"]


def _fixed(name: str) -> Callable[[dict[str, Any]], int]:
    return lambda body: PRICES[name]["default"]


@dataclass(frozen=True)
class Endpoint:
    name: str
    path: str
    task_type: str  # the task object's `type`
    estimate: Callable[[dict[str, Any]], int]


ENDPOINTS: dict[str, Endpoint] = {
    e.name: e
    for e in (
        Endpoint("image-to-image", "openapi/v1/image-to-image", "image-to-image",
                 _image_credits("image-to-image")),
        Endpoint("text-to-image", "openapi/v1/text-to-image", "text-to-image",
                 _image_credits("text-to-image")),
        Endpoint("multi-image-to-3d", "openapi/v1/multi-image-to-3d", "multi-image-to-3d",
                 _gen3d_credits),
        Endpoint("image-to-3d", "openapi/v1/image-to-3d", "image-to-3d", _gen3d_credits),
        Endpoint("retexture", "openapi/v1/retexture", "retexture", _retexture_credits),
        Endpoint("print-multi-color", "openapi/v1/print/multi-color", "print-multi-color",
                 _fixed("print-multi-color")),
        Endpoint("print-analyze", "openapi/v1/print/analyze", "print-analyze",
                 _fixed("print-analyze")),
        Endpoint("print-repair", "openapi/v1/print/repair", "print-repair",
                 _fixed("print-repair")),
        Endpoint("print-split", "openapi/v1/print/split", "print-split", _fixed("print-split")),
        Endpoint("keychain-prototype", "openapi/creative-lab/keychain/v1/prototype",
                 "creative-lab-keychain-prototype", _fixed("keychain-prototype")),
        Endpoint("keychain-build", "openapi/creative-lab/keychain/v1/build",
                 "creative-lab-keychain-build", _fixed("keychain-build")),
        Endpoint("magnet-prototype", "openapi/creative-lab/fridge-magnet/v1/prototype",
                 "creative-lab-fridge-magnet-prototype", _fixed("magnet-prototype")),
        Endpoint("magnet-build", "openapi/creative-lab/fridge-magnet/v1/build",
                 "creative-lab-fridge-magnet-build", _fixed("magnet-build")),
    )
}
ENDPOINT_BY_TYPE = {e.task_type: e for e in ENDPOINTS.values()}


def estimate_credits(endpoint: str, body: dict[str, Any]) -> int:
    return ENDPOINTS[endpoint].estimate(body)


class NoDeleteTransport(httpx.AsyncBaseTransport):
    """Wraps the real transport and refuses DELETE before it is sent."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self.inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method.upper() == "DELETE":
            raise ForbiddenRequest(
                "DELETE to Meshy is forbidden: an in-progress task is not refunded"
            )
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def trim_result(task: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "type", "status", "progress", "ai_model", "image_urls", "model_urls", "thumbnail_url",
        "thumbnail_urls", "alpha_thumbnail_url", "texture_urls", "task_error", "consumed_credits",
        "printability", "part_count", "prompt_ignored", "expires_at", "finished_at", "created_at",
        "name_text",
    )
    return {k: task[k] for k in keep if k in task}


def apply_task_object(task: MeshyTask, obj: dict[str, Any]) -> MeshyTask:
    """Update a recorded MeshyTask from a webhook payload or GET response."""
    status = str(obj.get("status") or task.status).upper()
    if status not in {"PENDING", "IN_PROGRESS", "SUCCEEDED", "FAILED", "CANCELED", "EXPIRED"}:
        status = task.status
    upd: dict[str, Any] = {"status": status, "result": manifest_safe(trim_result(obj))}
    if status in TERMINAL:
        upd["finished_at"] = ms_to_dt(obj.get("finished_at")) or utcnow()
    if status == "SUCCEEDED" and obj.get("consumed_credits") is not None:
        upd["credits"] = int(obj["consumed_credits"])
    if status in {"FAILED", "CANCELED", "EXPIRED"}:
        upd["refunded"] = status == "FAILED" or int(obj.get("consumed_credits") or 0) == 0
        upd["credits"] = 0 if upd["refunded"] else task.credits
    return task.model_copy(update=upd)


class MeshyClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        test_mode: bool,
        base_url: str = "https://api.meshy.ai",
        transport: httpx.AsyncBaseTransport | None = None,
        asset_transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 120.0,
        max_429_retries: int = 3,
    ):
        if not api_key:
            raise ForbiddenRequest("No Meshy key. Live mode needs MESHY_API_KEY in .env.")
        self.test_mode = test_mode
        self.base_url = base_url.rstrip("/")
        inner = transport or httpx.AsyncHTTPTransport(retries=2)
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            transport=NoDeleteTransport(inner),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        self._assets = httpx.AsyncClient(
            transport=NoDeleteTransport(asset_transport or transport or httpx.AsyncHTTPTransport()),
            timeout=timeout,
            follow_redirects=True,
        )
        self.max_429_retries = max_429_retries
        self.calls: list[tuple[str, str]] = []  # (method, path) log, no bodies or headers

    def __repr__(self) -> str:  # never show the key
        return f"MeshyClient(base_url={self.base_url!r}, test_mode={self.test_mode})"

    @classmethod
    def from_settings(cls, settings, transport: httpx.AsyncBaseTransport | None = None) -> MeshyClient:
        """Test mode must be given the fake server's transport; live mode uses the real API."""
        if settings.meshy_mode == "test":
            if transport is None:
                raise ForbiddenRequest("MESHY_MODE=test runs against the fake Meshy server only")
            return cls(api_key="msy_fake_test_key", test_mode=True,
                       base_url="https://api.meshy.ai", transport=transport)
        return cls(api_key=settings.meshy_live_key, test_mode=False,
                   base_url=settings.meshy_base_url, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._assets.aclose()

    # ------------------------------------------------------------------ requests
    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        attempt = 0
        while True:
            self.calls.append((method, path))
            try:
                resp = await self._http.request(method, path, **kw)
            except httpx.TransportError as e:
                raise VendorError(f"Meshy network error on {method} {path}: {type(e).__name__}",
                                  code="vendor_timeout", retryable=True) from e
            if resp.status_code == 429 and attempt < self.max_429_retries:
                attempt += 1
                try:
                    wait = float(resp.headers.get("retry-after", "2"))
                except ValueError:
                    wait = 2.0
                await asyncio.sleep(min(wait, 30.0) * attempt)
                continue
            return resp

    @staticmethod
    def _raise_for(resp: httpx.Response, what: str) -> None:
        if resp.status_code < 400:
            return
        try:
            msg = resp.json().get("message", "")
        except Exception:  # noqa: BLE001
            msg = resp.text[:300]
        code, retryable = "vendor_failed", resp.status_code >= 500 or resp.status_code == 429
        if resp.status_code in (400, 404, 422):
            code = "validation"
        if resp.status_code == 402:
            code, retryable = "budget_exceeded", False
        raise VendorError(f"Meshy {what} returned {resp.status_code}: {msg}", code=code,
                          retryable=retryable, status_code=resp.status_code)

    async def create(self, endpoint: str, body: dict[str, Any], purpose: str) -> MeshyTask:
        ep = ENDPOINTS[endpoint]
        credits = ep.estimate(body)
        resp = await self._request("POST", "/" + ep.path, json=body)
        self._raise_for(resp, f"create {endpoint}")
        task_id = resp.json().get("result")
        if not task_id:
            raise VendorError(f"Meshy create {endpoint} returned no task id", retryable=True)
        return MeshyTask(
            task_id=str(task_id), endpoint=ep.path, purpose=purpose, status="PENDING",
            test_mode=self.test_mode, credits=credits, created_at=utcnow(),
            request=manifest_safe(body),
        )

    async def get_task(self, endpoint: str, task_id: str) -> dict[str, Any]:
        ep = ENDPOINTS[endpoint]
        resp = await self._request("GET", f"/{ep.path}/{task_id}")
        self._raise_for(resp, f"get {endpoint}")
        return resp.json()

    async def list_recent(self, endpoint: str, page_size: int = 10) -> list[dict[str, Any]]:
        """Recent tasks, newest first. Crash recovery only (did a create land before the crash?)."""
        ep = ENDPOINTS[endpoint]
        resp = await self._request(
            "GET", "/" + ep.path, params={"page_size": page_size, "sort_by": "-created_at"}
        )
        self._raise_for(resp, f"list {endpoint}")
        data = resp.json()
        return data if isinstance(data, list) else data.get("result", data.get("data", []))

    async def balance(self) -> int:
        resp = await self._request("GET", "/openapi/v1/balance")
        self._raise_for(resp, "balance")
        return int(resp.json().get("balance", 0))

    async def download(self, url: str, dest: Path) -> int:
        """Fetch a signed asset URL (no credentials sent) and write it atomically."""
        try:
            resp = await self._assets.get(url)
        except httpx.TransportError as e:
            raise VendorError(f"Meshy asset download failed: {type(e).__name__}",
                              code="vendor_timeout", retryable=True) from e
        if resp.status_code >= 400:
            raise VendorError(f"Meshy asset download returned {resp.status_code}",
                              retryable=resp.status_code >= 500, status_code=resp.status_code)
        atomic_write_bytes(dest, resp.content)
        return len(resp.content)
