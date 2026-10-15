"""Fake Meshy server for MESHY_MODE=test and the test suite (httpx MockTransport).

Built from documented request rules and response shapes (docs/research/meshy.md), and replays
real recordings when they exist in worker/tests/fixtures/meshy/:

  <NN_name>/request.json          {"path": "/openapi/v1/<endpoint>", "body": {...}}
  <NN_name>/create_response.json  {"status": 202, "body": {"result": "<id>"}}
  <NN_name>/webhook_events.json   meshy_events rows, in the order they arrived
  <NN_name>/task.json             one GET after the terminal webhook
  <endpoint>.json                 (older flat form) a SUCCEEDED task object

For an endpoint with a recording, the SUCCEEDED task object is the recorded task.json and the
webhooks replay the recorded events in their recorded order (Meshy can deliver SUCCEEDED before
IN_PROGRESS). Ids are replaced, request fields (ai_model, prompt, style, max_colors) are kept from
the actual request, and every https://assets.meshy.ai/... URL is rewritten to the fake asset host
with the same file name so downloads work offline. Multi-Image to 3D reuses the Image to 3D
recording when it has none of its own (same result shape).

Webhooks: on each delivery the fake calls `on_event(task_object)`, which in the worker is
`store.insert_meshy_event`, the same row the Supabase meshy-webhook edge function writes.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import json
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..clients.meshy import ENDPOINTS, PRICES
from . import assets

ASSET_HOST = "assets.meshy.test"
# Asset URLs are signed like CloudFront's; the fake refuses a URL without the signature, so a
# URL read back from a manifest (signatures stripped) cannot download anything.
SIGNED_QUERY = "Expires=9999999999&Signature=fake-signature&Key-Pair-Id=FAKE"
API_HOST = "api.meshy.ai"
DEFAULT_FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "meshy"

IMAGE_TYPES = {"text-to-image", "image-to-image"}
PROTOTYPES = {"keychain-prototype": "creative-lab-keychain-prototype",
              "magnet-prototype": "creative-lab-fridge-magnet-prototype"}
BUILDS = {"keychain-build": "creative-lab-keychain-prototype",
          "magnet-build": "creative-lab-fridge-magnet-prototype"}
BADGES = {"circle", "rounded-rect", "hexagon", "shield", "star"}
# generate_multi_view returns three separate images (RGBA, transparent background, cropped to
# the subject), in the order three-quarter, front, back. SOURCE: the live probe in
# docs/research/meshy-iteration.md section 3 ("Probe result").
MULTI_VIEW_COUNT = 3
GEN3D_TYPES = {"text-to-3d", "image-to-3d", "multi-image-to-3d"}
RETEXTURE_INPUT_TYPES = {"text-to-3d", "image-to-3d", "remesh"}
MULTICOLOR_INPUT_TYPES = {"image-to-3d", "multi-image-to-3d", "text-to-3d", "remesh", "retexture"}
ANALYZE_INPUT_TYPES = MULTICOLOR_INPUT_TYPES
GEN3D_MODELS = {"meshy-6-lite", "meshy-6", "meshy-7", "meshy-7.1", "latest"}
RETEXTURE_MODELS = {"meshy-6-lite", "meshy-6", "meshy-7", "latest"}
NANO_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4"}
GPT_RATIOS = NANO_RATIOS | {"3:2", "2:3"}


class BadRequest(Exception):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _rewrite_urls(obj: Any, task_id: str) -> Any:
    if isinstance(obj, str) and obj.startswith("https://assets.meshy.ai/"):
        name = urlparse(obj).path.rsplit("/", 1)[-1] or "asset.bin"
        return f"https://{ASSET_HOST}/{task_id}/{name}?{SIGNED_QUERY}"
    if isinstance(obj, dict):
        return {k: _rewrite_urls(v, task_id) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rewrite_urls(v, task_id) for v in obj]
    return obj


class FakeMeshy:
    def __init__(
        self,
        *,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
        delay_s: float = 0.02,
        auto_complete: bool = True,
        webhooks: bool = True,
        fixtures_dir: Path | None = DEFAULT_FIXTURES,
        balance: int = 1_000_000,
    ):
        """balance: the account's credits (ASSUMED large for tests; the real account started
        at 1,142, FROM JACKSON). A create takes its price off and a failed task gives it back,
        like Meshy (consumed_credits 0 on FAILED); a create the balance cannot cover gets 402."""
        self.on_event = on_event
        self.delay_s = delay_s
        self.auto_complete = auto_complete
        self.webhooks = webhooks
        self.balance = balance
        self.tasks: dict[str, dict[str, Any]] = {}
        self.endpoint_of: dict[str, str] = {}
        self.requests: list[tuple[str, str]] = []
        self.bodies: dict[str, dict[str, Any]] = {}
        self.fail_next: Counter[str] = Counter()
        self.asset_requests_with_auth = 0
        self.unsigned_asset_requests = 0
        self._bg: set[asyncio.Task] = set()
        self._ids = itertools.count(1)
        self.fixtures: dict[str, dict[str, Any]] = {}
        self.fixture_events: dict[str, list[dict[str, Any]]] = {}
        self.multi_view_count = MULTI_VIEW_COUNT
        self._images: dict[tuple[str, int], bytes] = {}
        if fixtures_dir and Path(fixtures_dir).is_dir():
            self._load_fixtures(Path(fixtures_dir))

    def _load_fixtures(self, root: Path) -> None:
        by_path = {"/" + e.path: e.name for e in ENDPOINTS.values()}
        for f in root.glob("*.json"):
            try:
                self.fixtures[f.stem] = json.loads(f.read_text())
            except json.JSONDecodeError:
                continue
        for d in sorted(x for x in root.iterdir() if x.is_dir()):
            try:
                req = json.loads((d / "request.json").read_text())
                task = json.loads((d / "task.json").read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            ep = by_path.get(req.get("path", ""))
            if not ep:
                continue
            self.fixtures[ep] = task
            ev_file = d / "webhook_events.json"
            if ev_file.is_file():
                rows = json.loads(ev_file.read_text())
                self.fixture_events[ep] = [r.get("event", r) for r in rows]
        if "image-to-3d" in self.fixtures and "multi-image-to-3d" not in self.fixtures:
            self.fixtures["multi-image-to-3d"] = self.fixtures["image-to-3d"]
            if "image-to-3d" in self.fixture_events:
                self.fixture_events["multi-image-to-3d"] = self.fixture_events["image-to-3d"]

    # ------------------------------------------------------------------ inspection
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def count(self, method: str, endpoint: str | None = None, task_id: str | None = None) -> int:
        n = 0
        for m, path in self.requests:
            if m != method:
                continue
            if endpoint and f"/{ENDPOINTS[endpoint].path}" not in path:
                continue
            if task_id and not path.rstrip("/").endswith(task_id):
                continue
            n += 1
        return n

    @property
    def deletes(self) -> int:
        return sum(1 for m, _ in self.requests if m == "DELETE")

    async def drain(self) -> None:
        while self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    # ------------------------------------------------------------------ lifecycle
    def _emit(self, task_id: str) -> None:
        if self.webhooks and self.on_event is not None:
            self.on_event(copy.deepcopy(self.tasks[task_id]))

    def set_status(self, task_id: str, status: str) -> dict[str, Any]:
        t = self.tasks[task_id]
        ep = self.endpoint_of[task_id]
        if status == "IN_PROGRESS":
            t.update(status="IN_PROGRESS", progress=50, started_at=_now_ms())
        elif status == "SUCCEEDED":
            t.update(self._result(ep, task_id, self.bodies[task_id]))
            t.update(status="SUCCEEDED", progress=100, finished_at=_now_ms())
        elif status == "FAILED":
            fx = self.fixtures.get(f"{ep}.failed")
            err = (fx or {}).get("task_error") or {
                "type": "server_error", "message": "An internal error occurred. Please retry."
            }
            self.balance += int(t.get("consumed_credits") or 0)  # refunded
            t.update(status="FAILED", progress=0, finished_at=_now_ms(), consumed_credits=0,
                     task_error=err)
        self._emit(task_id)
        return t

    def complete(self, task_id: str, status: str = "SUCCEEDED") -> dict[str, Any]:
        return self.set_status(task_id, status)

    def complete_pending(self) -> list[str]:
        """Finish every unfinished task (simulates Meshy finishing while the worker was down)."""
        done = []
        for tid, t in self.tasks.items():
            if t["status"] in {"PENDING", "IN_PROGRESS"}:
                self.set_status(tid, "SUCCEEDED")
                done.append(tid)
        return done

    async def _run(self, task_id: str, final: str) -> None:
        ep = self.endpoint_of[task_id]
        recorded = self.fixture_events.get(ep)
        if final == "SUCCEEDED" and recorded:
            await self._replay(task_id, recorded)
            return
        await asyncio.sleep(self.delay_s)
        self.set_status(task_id, "IN_PROGRESS")
        await asyncio.sleep(self.delay_s)
        self.set_status(task_id, final)

    async def _replay(self, task_id: str, recorded: list[dict[str, Any]]) -> None:
        """Deliver the recorded webhook sequence; the GET state follows the terminal event."""
        t = self.tasks[task_id]
        for ev in recorded:
            await asyncio.sleep(self.delay_s)
            status = str(ev.get("status", "")).upper()
            if status == "SUCCEEDED" and t["status"] != "SUCCEEDED":
                t.update(self._result(self.endpoint_of[task_id], task_id, self.bodies[task_id]))
                t.update(status="SUCCEEDED", progress=100, finished_at=_now_ms())
            elif status == "IN_PROGRESS" and t["status"] == "PENDING":
                t.update(status="IN_PROGRESS", progress=50, started_at=_now_ms())
            if self.webhooks and self.on_event is not None:
                body = _rewrite_urls(copy.deepcopy(ev), task_id)
                body.update(id=task_id, type=t["type"])
                for k in ("ai_model", "prompt", "style", "max_colors", "consumed_credits"):
                    if k in t:
                        body[k] = t[k]
                if status == "SUCCEEDED":
                    body.update({k: v for k, v in t.items() if k not in body})
                    if "image_urls" in t:
                        body["image_urls"] = t["image_urls"]
                self.on_event(body)

    # ------------------------------------------------------------------ results
    def _asset(self, task_id: str, name: str) -> str:
        return f"https://{ASSET_HOST}/{task_id}/{name}?{SIGNED_QUERY}"

    def _result(self, ep: str, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        a = lambda n: self._asset(task_id, n)
        fx = self.fixtures.get(ep)
        out: dict[str, Any] = {}
        if fx:
            out = _rewrite_urls(copy.deepcopy(fx), task_id)
            for k in ("id", "status", "progress", "created_at", "started_at", "finished_at",
                      "task_error", "preceding_tasks", "consumed_credits", "ai_model", "prompt",
                      "expires_at", "type"):
                out.pop(k, None)
        if ep in IMAGE_TYPES or ep in PROTOTYPES:
            # the recorded shape, with images this fake draws (see _image_bytes)
            n = self.multi_view_count if body.get("generate_multi_view") else 1
            out["image_urls"] = [a(f"image_{i}.png") for i in range(n)]
            if ep == "keychain-prototype" and body.get("name_text"):
                out["name_text"] = str(body["name_text"]).strip()
            return out
        if fx:
            return out
        if ep in ("image-to-image", "text-to-image"):
            n = 3 if body.get("generate_multi_view") else 1
            return {"image_urls": [a(f"image_{i}.png") for i in range(n)]}
        if ep in ("multi-image-to-3d", "image-to-3d"):
            out: dict[str, Any] = {
                "model_urls": {"glb": a("model.glb")},
                "thumbnail_url": a("preview.png"),
                "texture_urls": [{"base_color": a("texture_0.png")}],
            }
            if body.get("multi_view_thumbnails"):
                out["thumbnail_urls"] = {v: a(f"thumb_{v}.png") for v in ("front", "right", "back", "left")}
            if body.get("geometry_resolution", "standard") != "standard":
                out["geometry_resolution"] = body["geometry_resolution"]
            return out
        if ep == "retexture":
            return {"model_urls": {"glb": a("model.glb")}, "thumbnail_url": a("preview.png"),
                    "texture_urls": [{"base_color": a("texture_0.png")}]}
        if ep == "print-multi-color":
            return {"model_urls": {"3mf": a(f"model_{body.get('max_colors', 4)}c.3mf")}}
        if ep == "print-analyze":
            return {"printability": {"status": "ok", "issue_count": 0, "error_count": 0,
                                     "warning_count": 0,
                                     "metrics": {"is_watertight": True, "volume": 1.0,
                                                 "non_manifold_edges": 0, "degenerate_faces": 0,
                                                 "holes": 0}}}
        if ep == "print-repair":
            return {"model_urls": {"glb": a("model.glb")}, "thumbnail_url": a("preview.png")}
        if ep == "print-split":
            return {"model_urls": {"glb": a("model.glb")}, "thumbnail_url": a("preview.png"),
                    "part_count": 2, "prompt_ignored": False}
        return {"model_urls": {"glb": a("model.glb")}}

    # ------------------------------------------------------------------ validation
    def _source(self, task_id: str, allowed: set[str]) -> dict[str, Any]:
        t = self.tasks.get(task_id)
        if t is None:
            raise BadRequest("NotFound")
        if t["type"] not in allowed:
            raise BadRequest(f"Invalid input task: type {t['type']} is not accepted here")
        if t["status"] != "SUCCEEDED":
            raise BadRequest("Invalid input task: task has not succeeded")
        return t

    def _validate(self, ep: str, b: dict[str, Any]) -> None:
        def need_one(*keys: str) -> None:
            if not any(b.get(k) for k in keys):
                raise BadRequest(f"Missing parameter: one of {', '.join(keys)} is required")

        if ep in PROTOTYPES:
            if not b.get("image_url"):
                raise BadRequest("Missing parameter: image_url is required")
            if ep == "keychain-prototype" and len(str(b.get("name_text") or "").strip()) > 10:
                raise BadRequest("Engraving too long: name_text is longer than 10 characters")
            if ep == "magnet-prototype" and "name_text" in b:
                raise BadRequest("Unknown parameter: name_text")
        elif ep in BUILDS:
            need_one("input_task_id")
            self._source(b["input_task_id"], {BUILDS[ep]})
            opts = b.get("options") or {}
            if opts.get("badge_shape", "circle") not in BADGES:
                raise BadRequest("Options out of range: badge_shape")
            if not 0 < float(opts.get("size_mm", 40)) <= 400:
                raise BadRequest("Options out of range: size_mm")
            if not 0 <= float(opts.get("base_thickness_mm", 0.1)) <= 20:
                raise BadRequest("Options out of range: base_thickness_mm")
        elif ep == "image-to-image":
            model = b.get("ai_model")
            if model not in PRICES["image-to-image"]:
                raise BadRequest(f"Invalid ai_model: {model}")
            if b.get("generate_multi_view") and b.get("remove_background"):
                raise BadRequest("remove_background is supported for single images only")
            if not b.get("prompt"):
                raise BadRequest("Missing parameter: prompt")
            need_one("input_task_id", "reference_image_urls")
            if b.get("input_task_id"):
                self._source(b["input_task_id"], IMAGE_TYPES)
            elif not 1 <= len(b["reference_image_urls"]) <= 5:
                raise BadRequest("reference_image_urls must have 1 to 5 images")
            if b.get("aspect_ratio"):
                if b.get("generate_multi_view"):
                    raise BadRequest("Conflict: generate_multi_view and aspect_ratio")
                ok = GPT_RATIOS if model.startswith("gpt-image") else NANO_RATIOS
                if b["aspect_ratio"] not in ok:
                    raise BadRequest(f"Invalid aspect_ratio for {model}")
        elif ep == "multi-image-to-3d":
            need_one("input_task_id", "image_urls")
            if b.get("input_task_id"):
                self._source(b["input_task_id"], IMAGE_TYPES)
            elif not 1 <= len(b["image_urls"]) <= 4:
                raise BadRequest("Invalid image count: image_urls must have 1 to 4 images")
            model = b.get("ai_model", "latest")
            if model not in GEN3D_MODELS:
                raise BadRequest(f"Invalid ai_model: {model}")
            geo = b.get("geometry_resolution", "standard")
            if geo not in ("standard", "2k"):
                raise BadRequest(f"Unsupported resolution for multi-image input: {geo}")
            if geo == "2k" and model not in ("meshy-7.1", "latest"):
                raise BadRequest("geometry_resolution 2k requires meshy-7.1 or latest")
            if b.get("texture_image_urls") is not None:
                if b.get("texture_image_url") or b.get("texture_prompt"):
                    raise BadRequest("Conflicting texture inputs")
                if model not in ("meshy-7.1", "latest"):
                    raise BadRequest("texture_image_urls requires meshy-7.1 or latest")
                if not b.get("should_texture", True):
                    raise BadRequest("texture_image_urls requires should_texture: true")
                if not 1 <= len(b["texture_image_urls"]) <= 4:
                    raise BadRequest("texture_image_urls must have 1 to 4 images")
        elif ep == "image-to-3d":
            need_one("input_task_id", "image_url")
            if b.get("input_task_id"):
                self._source(b["input_task_id"], IMAGE_TYPES)
        elif ep == "retexture":
            need_one("input_task_id", "model_url")
            if b.get("input_task_id"):
                self._source(b["input_task_id"], RETEXTURE_INPUT_TYPES)
            styles = [k for k in ("text_style_prompt", "image_style_url", "multiview_image_urls")
                      if b.get(k)]
            if not styles:
                raise BadRequest("Missing style input")
            if "multiview_image_urls" in styles:
                if len(styles) > 1:
                    raise BadRequest("multiview_image_urls cannot be combined with other styles")
                if b.get("ai_model") != "meshy-7":
                    raise BadRequest("multiview_image_urls requires ai_model meshy-7")
            if b.get("ai_model", "latest") not in RETEXTURE_MODELS:
                raise BadRequest(f"Invalid ai_model: {b.get('ai_model')}")
        elif ep == "print-multi-color":
            need_one("input_task_id", "model_url")
            if b.get("input_task_id"):
                self._source(b["input_task_id"], MULTICOLOR_INPUT_TYPES)
            elif not (b["model_url"].startswith("data:")
                      or urlparse(b["model_url"]).path.lower().endswith((".glb", ".fbx"))):
                raise BadRequest("Invalid model format")
            if not 1 <= int(b.get("max_colors", 4)) <= 16:
                raise BadRequest("Invalid max_colors")
            if b.get("style", "realistic") not in ("realistic", "cartoon"):
                raise BadRequest("Invalid style")
        elif ep in ("print-analyze", "print-repair"):
            need_one("input_task_id", "model_url")
            if b.get("input_task_id"):
                self._source(b["input_task_id"], ANALYZE_INPUT_TYPES)
        elif ep == "print-split":
            need_one("input_task_id")
            self._source(b["input_task_id"], GEN3D_TYPES | {"remesh", "convert", "resize"})

    # ------------------------------------------------------------------ http
    def _json(self, status: int, obj: Any) -> httpx.Response:
        return httpx.Response(status, json=obj)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.requests.append((request.method, url.path))
        if url.host == ASSET_HOST:
            if request.headers.get("authorization"):
                self.asset_requests_with_auth += 1
            if "Signature" not in url.params or "Expires" not in url.params:
                self.unsigned_asset_requests += 1
                return httpx.Response(403, text="missing signature")
            return self._asset_response(url.path)
        if url.host != API_HOST:
            return self._json(404, {"message": "unknown host"})
        if request.headers.get("authorization", "") in ("", "Bearer "):
            return self._json(401, {"message": "Invalid API key"})
        if request.method == "DELETE":
            return self._json(409, {"message": "Task is IN_PROGRESS and cannot be deleted."})
        if url.path == "/openapi/v1/balance":
            return self._json(200, {"balance": self.balance})
        for ep in sorted(ENDPOINTS.values(), key=lambda e: -len(e.path)):
            base = "/" + ep.path
            if url.path == base or url.path.startswith(base + "/"):
                rest = url.path[len(base):].strip("/")
                return await self._endpoint(request, ep.name, rest)
        return self._json(404, {"message": "Not found"})

    async def _endpoint(self, request: httpx.Request, ep: str, rest: str) -> httpx.Response:
        if request.method == "POST" and not rest:
            try:
                body = json.loads(request.content or b"{}")
                self._validate(ep, body)
            except BadRequest as e:
                return self._json(404 if str(e) == "NotFound" else 400, {"message": str(e)})
            except json.JSONDecodeError:
                return self._json(400, {"message": "Invalid JSON"})
            price = ENDPOINTS[ep].estimate(body)
            if price > self.balance:
                return self._json(402, {"message": "Insufficient credits"})
            self.balance -= price
            return self._json(202, {"result": self._create(ep, body)})
        if request.method == "GET" and not rest:
            items = [t for tid, t in self.tasks.items() if self.endpoint_of[tid] == ep]
            items.sort(key=lambda t: t["created_at"], reverse=True)
            size = int(request.url.params.get("page_size", 10))
            return self._json(200, copy.deepcopy(items[:size]))
        if request.method == "GET":
            tid = rest.split("/")[0]
            if tid not in self.tasks or self.endpoint_of[tid] != ep:
                return self._json(404, {"message": "Task not found"})
            return self._json(200, copy.deepcopy(self.tasks[tid]))
        return self._json(405, {"message": "Method not allowed"})

    def _create(self, ep: str, body: dict[str, Any]) -> str:
        n = next(self._ids)
        task_id = str(uuid.UUID(int=(0x0199 << 112) | n))
        e = ENDPOINTS[ep]
        task: dict[str, Any] = {
            "id": task_id, "type": e.task_type, "status": "PENDING", "progress": 0,
            "created_at": _now_ms(), "started_at": 0, "finished_at": 0,
            "expires_at": _now_ms() + 3 * 86400 * 1000, "preceding_tasks": 0,
            "consumed_credits": e.estimate(body),
        }
        for k in ("ai_model", "prompt", "texture_prompt", "style", "max_colors"):
            if k in body:
                task[k] = body[k]
        self.tasks[task_id] = task
        self.endpoint_of[task_id] = ep
        self.bodies[task_id] = body
        final = "SUCCEEDED"
        if self.fail_next[ep] > 0:
            self.fail_next[ep] -= 1
            final = "FAILED"
        if self.auto_complete:
            t = asyncio.get_running_loop().create_task(self._run(task_id, final))
            self._bg.add(t)
            t.add_done_callback(self._bg.discard)
        return task_id

    def _image_bytes(self, task_id: str, index: int = 0) -> bytes:
        """What an image task returns, drawn from its inputs: a figure from photos, an edit of an
        input figure (add-ons or a tweak), views of it, or a Creative Lab charm around it."""
        key = (task_id, index)
        if key in self._images:
            return self._images[key]
        ep, body = self.endpoint_of[task_id], self.bodies[task_id]
        src = None
        if body.get("input_task_id") in self.tasks:
            src = self._image_bytes(body["input_task_id"], 0)
        elif body.get("reference_image_urls"):
            src = assets.image_from_ref(body["reference_image_urls"][0])
        elif body.get("image_url"):
            src = assets.image_from_ref(body["image_url"])
        figure = src if src is not None and assets.has_alpha(src) else None
        if ep in PROTOTYPES:
            data = assets.charm_image(figure or assets._png(assets.figure_image(task_id)))
        elif body.get("generate_multi_view"):
            views = assets.view_images(figure or assets._png(assets.figure_image(task_id)),
                                       self.multi_view_count)
            data = views[min(index, len(views) - 1)]
        elif figure is not None:
            prompt = str(body.get("prompt", ""))
            kind = ("name plate" if "name plate" in prompt else "loop tab" if "loop tab" in prompt
                    else "smooth disc" if "smooth disc" in prompt else "tweak")
            data = assets.edit_image(figure, {"name plate": prompt, "loop tab": "loop",
                                              "smooth disc": "base"}.get(kind, "tweak"))
        else:  # a first render from the customer's photos
            data = assets._png(assets.figure_image(task_id))
        self._images[key] = data
        return data

    def _asset_response(self, path: str) -> httpx.Response:
        parts = path.strip("/").split("/")
        if len(parts) < 2 or parts[0] not in self.tasks:
            return httpx.Response(404)
        name = parts[-1]
        seed = f"{parts[0]}/{name}"
        ep = self.endpoint_of[parts[0]]
        m = re.fullmatch(r"image_(\d+)\.png", name)
        if m and (ep in IMAGE_TYPES or ep in PROTOTYPES):
            return httpx.Response(200, content=self._image_bytes(parts[0], int(m.group(1))))
        if name.endswith(".png"):
            data = assets.png(seed)
        elif name.endswith(".glb"):
            data = assets.glb()
        elif name.endswith(".3mf"):
            n = int(self.bodies[parts[0]].get("max_colors", 4))
            data = assets.threemf(n)
        else:
            data = b"fake-asset"
        return httpx.Response(200, content=data)
