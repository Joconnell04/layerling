"""Replay Meshy backend (MESHY_MODE=replay): answers every Meshy call from the fixture store of
paid outputs (evals/fixtures), at 0 credits, and fails loudly on a miss.

The store (built by `python -m p2p_evals fixtures build`, format in evals/README.md) holds,
per recorded job, every Meshy task the job paid for: the request as the manifest kept it, the
full task object, and the files the worker downloaded from it. `manifest.json` (committed)
indexes them with sha256; `store/` (gitignored) has the bytes.

A create is matched, in this order:
1. input hash: the endpoint plus the body with every image or model reference replaced by the
   sha256 of the job file behind it (a data URI, a signed Storage URL and a storage: ref of the
   same file all hash the same), among the tasks of the job's source job not yet served;
2. job and stage: the same source job, stage, step key (the key the stage is creating, read from
   the attempt's `meshy_pending`) and endpoint; the inputs differ (a new prompt, say) and the
   answer records which fields did;
3. an exact input hash already served in this run (a retry attempt re-sending the same body).
Anything else raises ReplayMiss (VendorError, validation, not retryable): the stage fails and
says which call had no recording. Nothing ever leaves the process.

The answer keeps the recorded task id (so chained `input_task_id`s line up), reports 0
consumed credits (the stage books a refund of its estimate, so the job nets 0), is stamped as
just finished (so chaining rules that look at task age behave as they did live), and serves the
recorded files at signed-looking asset URLs. The terminal webhook goes to `on_event`, like the
fake server's.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx

from ..clients.meshy import ENDPOINTS, TERMINAL
from ..errors import VendorError
from ..util import manifest_safe, sha256_file

API_HOST = "api.meshy.ai"
ASSET_HOST = "assets.meshy.replay"
SIGNED_QUERY = "Expires=9999999999&Signature=replay&Key-Pair-Id=REPLAY"
REF_PREFIXES = ("data:", "storage:", "http://", "https://")
ENDPOINT_BY_PATH = {"/" + e.path: e.name for e in ENDPOINTS.values()}
MANIFEST_NAME = "manifest.json"
STORE_ENV = "P2P_REPLAY_STORE"  # folder holding manifest.json and store/
JOBS_ENV = "P2P_REPLAY_JOBS"  # "<running job id>=<source job id>,..." when ids differ


class ReplayMiss(VendorError):
    """A Meshy call the fixture store has no recording for."""

    def __init__(self, message: str, detail: dict[str, Any] | None = None):
        super().__init__(f"Meshy replay miss: {message}", code="validation", retryable=False,
                         detail=detail or {})


# ---------------------------------------------------------------------------- hashing


def canonical(obj: Any, job_dir: Path | None, job_id: str | None) -> Any:
    """The body with each file reference replaced by "sha256:<hex>" of the job file behind it
    (data URIs are matched back to job files the way traces do it). A reference with no file
    behind it keeps its manifest_safe form."""
    from ..trace import job_path

    def one(s: str) -> str:
        if job_dir is not None and job_id:
            rel = job_path(job_dir, job_id, s)
            if rel and not rel.startswith(("unresolved:", *REF_PREFIXES)):
                p = job_dir / rel
                if p.is_file():
                    return "sha256:" + sha256_file(p)
        return manifest_safe(s)

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return one(v) if v.startswith(REF_PREFIXES) else v
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(obj)


def request_hash(endpoint: str, canon_body: Any) -> str:
    return hashlib.sha256(json.dumps({"endpoint": endpoint, "body": canon_body},
                                     sort_keys=True, default=str).encode()).hexdigest()


def field_hashes(canon_body: Any) -> dict[str, str]:
    """A short hash per top-level field of a canonical body, so a key match can say which
    fields differ from the recording without the store keeping every prompt twice."""
    if not isinstance(canon_body, dict):
        return {"body": request_hash("", canon_body)[:16]}
    return {k: hashlib.sha256(json.dumps(v, sort_keys=True, default=str).encode()
                              ).hexdigest()[:16] for k, v in canon_body.items()}


def body_diff(recorded: dict[str, str], live: dict[str, str]) -> list[str]:
    return sorted(k for k in set(recorded) | set(live) if recorded.get(k) != live.get(k))


def get_field(obj: dict[str, Any], dotted: str) -> Any:
    cur: Any = obj
    for part in dotted.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def set_field(obj: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur: Any = obj
    for part in parts[:-1]:
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


# ---------------------------------------------------------------------------- the store


@dataclass
class Recorded:
    task_id: str
    job_id: str
    stage: str
    key: str | None
    endpoint: str
    purpose: str
    status: str
    credits: int  # what the live call cost (for reports; replay books 0)
    request: dict[str, Any]
    request_hash: str
    fields: dict[str, str]  # field_hashes of the canonical recorded body
    task_path: Path
    assets: dict[str, Path]  # field ("image_urls.0", "model_urls.glb") -> stored file
    asset_sha: dict[str, str]


@dataclass
class Answer:
    """One create the replay answered, for the eval report."""

    job_id: str
    stage: str | None
    key: str | None
    endpoint: str
    task_id: str | None
    match: str  # hash, key, reused, miss
    request_hash: str
    diff: list[str] = field(default_factory=list)
    credits_live: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "stage": self.stage, "key": self.key,
                "endpoint": self.endpoint, "task_id": self.task_id, "match": self.match,
                "request_hash": self.request_hash[:16], "diff": self.diff,
                "credits_live": self.credits_live}


def default_store_dir(repo_root: Path | None = None) -> Path:
    env = os.environ.get(STORE_ENV)
    if env:
        return Path(env)
    if repo_root is None:
        from ..config import find_repo_root

        repo_root = find_repo_root()
    return repo_root / "evals" / "fixtures"


def load_recordings(fixtures_dir: Path, *, verify: bool = False) -> dict[str, list[Recorded]]:
    """Recorded tasks per source job id, from manifest.json and store/. verify=True re-hashes
    every stored file against the manifest (a changed or missing file raises)."""
    man_path = fixtures_dir / MANIFEST_NAME
    if not man_path.is_file():
        raise ReplayMiss(f"no fixture manifest at {man_path}; run "
                         "`uv run python -m p2p_evals fixtures build`")
    man = json.loads(man_path.read_text())
    store = fixtures_dir / "store"
    out: dict[str, list[Recorded]] = {}
    for job in man.get("jobs", []):
        jid = job["job_id"]
        base = store / jid
        recs = []
        for t in job.get("meshy", []):
            task_path = base / t["task"]
            assets = {a["field"]: base / "files" / a["path"] for a in t.get("assets", [])}
            sha = {a["field"]: a["sha256"] for a in t.get("assets", [])}
            if verify:
                for p, want in [(task_path, t.get("task_sha256")),
                                *[(assets[f], sha[f]) for f in assets]]:
                    if not p.is_file():
                        raise ReplayMiss(f"fixture file missing: {p}")
                    if want and sha256_file(p) != want:
                        raise ReplayMiss(f"fixture file changed: {p}")
            recs.append(Recorded(
                task_id=t["task_id"], job_id=jid, stage=t["stage"], key=t.get("key"),
                endpoint=t["endpoint"], purpose=t.get("purpose", ""),
                status=t.get("status", "SUCCEEDED"), credits=int(t.get("credits") or 0),
                request=t.get("request") or {}, request_hash=t["request_hash"],
                fields=t.get("request_fields") or {}, task_path=task_path, assets=assets,
                asset_sha=sha))
        out[jid] = recs
    return out


def _parse_jobs_env(v: str | None) -> dict[str, str]:
    out = {}
    for pair in (v or "").split(","):
        if "=" in pair:
            a, b = pair.split("=", 1)
            out[a.strip()] = b.strip()
    return out


# ---------------------------------------------------------------------------- the server


def _now_ms() -> int:
    return int(time.time() * 1000)


class ReplayMeshy:
    """httpx transport that plays Meshy from the fixture store. Same surface the runtime uses
    for the fake server: transport(), on_event, drain()."""

    def __init__(self, fixtures_dir: Path | None = None, *,
                 job_map: dict[str, str] | None = None,
                 on_event: Callable[[dict[str, Any]], Any] | None = None,
                 delay_s: float = 0.01, verify: bool = False,
                 recordings: dict[str, list[Recorded]] | None = None):
        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir else default_store_dir()
        self.recordings = recordings if recordings is not None else load_recordings(
            self.fixtures_dir, verify=verify)
        self.by_id: dict[str, Recorded] = {r.task_id: r for rs in self.recordings.values()
                                           for r in rs}
        self.job_map = {**_parse_jobs_env(os.environ.get(JOBS_ENV)), **(job_map or {})}
        self.on_event = on_event
        self.delay_s = delay_s
        self.tasks: dict[str, dict[str, Any]] = {}  # served task objects, by task id
        self.served: dict[str, set[str]] = {}  # running job id -> recorded task ids served
        self.answers: list[Answer] = []
        self.misses: list[Answer] = []
        self.asset_fetches = 0
        self.requests: list[tuple[str, str]] = []
        self._bg: set[asyncio.Task] = set()

    @classmethod
    def from_settings(cls, settings) -> ReplayMeshy:
        return cls(default_store_dir(settings.repo_root),
                   delay_s=getattr(settings, "fake_meshy_delay_s", 0.01))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    async def drain(self) -> None:
        while self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    def source_job(self, job_id: str) -> str:
        return self.job_map.get(job_id, job_id)

    # ------------------------------------------------------------------ matching
    @staticmethod
    def _context() -> tuple[Any, str | None, str | None, Path | None]:
        """(stage context, running job id, stage, job dir) of the stage making the call."""
        from .. import trace

        ctx = trace.current()
        if ctx is None:
            return None, None, None, None
        return ctx, ctx.job.id, ctx.stage.value, ctx.job_dir

    @staticmethod
    def _pending_key(ctx: Any, endpoint: str) -> str | None:
        if ctx is None:
            return None
        pending = ctx.m.data.get("meshy_pending") or {}
        done = ctx.m.data.get("meshy_keys") or {}
        keys = [(v.get("at", ""), k) for k, v in pending.items()
                if v.get("endpoint") == endpoint and k not in done]
        return max(keys)[1] if keys else None

    def match(self, endpoint: str, body: dict[str, Any]) -> Answer:
        ctx, job_id, stage, job_dir = self._context()
        job_id = job_id or ""
        src = self.source_job(job_id) if job_id else None
        canon = canonical(body, job_dir, job_id or None)
        h = request_hash(endpoint, canon)
        key = self._pending_key(ctx, endpoint)
        served = self.served.setdefault(job_id, set())
        pool = self.recordings.get(src, []) if src else \
            [r for rs in self.recordings.values() for r in rs]
        fresh = [r for r in pool if r.task_id not in served and r.endpoint == endpoint]
        hit = next((r for r in fresh if r.request_hash == h
                    and (stage is None or r.stage == stage)), None)
        kind = "hash"
        if hit is None:
            hit = next((r for r in fresh if r.request_hash == h), None)
        if hit is None and stage is not None and key is not None:
            hit = next((r for r in fresh if r.stage == stage and r.key == key), None)
            kind = "key"
        if hit is None:
            hit = next((r for r in pool if r.request_hash == h and r.endpoint == endpoint),
                       None)
            kind = "reused"
        ans = Answer(job_id=job_id, stage=stage, key=key, endpoint=endpoint,
                     task_id=hit.task_id if hit else None, match=kind if hit else "miss",
                     request_hash=h, credits_live=hit.credits if hit else 0)
        if hit is None:
            self.misses.append(ans)
            left = sorted({f"{r.stage}:{r.key}" for r in fresh if r.endpoint == endpoint})
            raise ReplayMiss(
                f"no stored {endpoint} task for job {job_id[:8] or '-'} (source "
                f"{(src or '-')[:8]}), stage {stage or '-'}, key {key or '-'}, request "
                f"{h[:12]}; unserved {endpoint} recordings: {', '.join(left) or 'none'}",
                ans.to_json())
        if kind == "key":
            ans.diff = body_diff(hit.fields, field_hashes(canon))
        served.add(hit.task_id)
        self.answers.append(ans)
        return ans

    # ------------------------------------------------------------------ tasks
    def _served_object(self, rec: Recorded) -> dict[str, Any]:
        """The recorded task, stamped as created now, 0 credits, asset URLs replayable."""
        obj = json.loads(rec.task_path.read_text())
        obj["id"] = rec.task_id
        obj.setdefault("type", ENDPOINTS[rec.endpoint].task_type)
        now = _now_ms()
        obj.update(created_at=now, started_at=now, finished_at=0, expires_at=now + 3 * 86400_000,
                   progress=0, status="PENDING", consumed_credits=0)
        for f in rec.assets:
            if get_field(obj, f) is not None:
                name = Path(urlparse(str(get_field(obj, f))).path).name or "asset.bin"
                set_field(obj, f, f"https://{ASSET_HOST}/{rec.task_id}/{quote(f)}/{name}"
                                  f"?{SIGNED_QUERY}")
        return _unsign_other_urls(obj, rec.task_id)

    def _create(self, endpoint: str, body: dict[str, Any]) -> str:
        ans = self.match(endpoint, body)
        rec = self.by_id[ans.task_id]  # type: ignore[index]
        self.tasks[rec.task_id] = self._served_object(rec)
        t = asyncio.get_running_loop().create_task(self._finish(rec))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return rec.task_id

    async def _finish(self, rec: Recorded) -> None:
        await asyncio.sleep(self.delay_s)
        t = self.tasks[rec.task_id]
        status = rec.status if rec.status in TERMINAL else "SUCCEEDED"
        t.update(status=status, progress=100 if status == "SUCCEEDED" else 0,
                 finished_at=_now_ms(), consumed_credits=0)
        if self.on_event is not None:
            self.on_event(copy.deepcopy(t))

    # ------------------------------------------------------------------ http
    @staticmethod
    def _json(status: int, obj: Any) -> httpx.Response:
        return httpx.Response(status, json=obj)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.requests.append((request.method, url.path))
        if url.host == ASSET_HOST:
            return self._asset(url)
        if url.host != API_HOST:
            raise ReplayMiss(f"request to {url.host} (only Meshy is replayed)")
        if request.method == "DELETE":
            return self._json(409, {"message": "Task is IN_PROGRESS and cannot be deleted."})
        if url.path == "/openapi/v1/balance":
            return self._json(200, {"balance": 0})
        for path, name in sorted(ENDPOINT_BY_PATH.items(), key=lambda kv: -len(kv[0])):
            if url.path == path or url.path.startswith(path + "/"):
                rest = url.path[len(path):].strip("/")
                return self._endpoint(request, name, rest)
        return self._json(404, {"message": "Not found"})

    def _endpoint(self, request: httpx.Request, ep: str, rest: str) -> httpx.Response:
        if request.method == "POST" and not rest:
            body = json.loads(request.content or b"{}")
            return self._json(202, {"result": self._create(ep, body)})
        if request.method == "GET" and not rest:
            items = [t for tid, t in self.tasks.items() if self.by_id[tid].endpoint == ep]
            items.sort(key=lambda t: t.get("created_at", 0), reverse=True)
            return self._json(200, copy.deepcopy(
                items[: int(request.url.params.get("page_size", 10))]))
        if request.method == "GET":
            tid = rest.split("/")[0]
            if tid not in self.tasks:
                return self._json(404, {"message": "Task not found"})
            return self._json(200, copy.deepcopy(self.tasks[tid]))
        return self._json(405, {"message": "Method not allowed"})

    def _asset(self, url: httpx.URL) -> httpx.Response:
        if "Signature" not in url.params:
            return httpx.Response(403, text="missing signature")
        parts = url.path.strip("/").split("/")
        rec = self.by_id.get(parts[0]) if parts else None
        fld = unquote(parts[1]) if len(parts) > 1 else ""
        path = rec.assets.get(fld) if rec else None
        if path is None or not path.is_file():
            raise ReplayMiss(f"asset {fld or url.path} of task {parts[0] if parts else '-'} "
                             "is not in the fixture store")
        self.asset_fetches += 1
        return httpx.Response(200, content=path.read_bytes())

    # ------------------------------------------------------------------ report
    def summary(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for a in self.answers:
            kinds[a.match] = kinds.get(a.match, 0) + 1
        return {"answered": len(self.answers), "by_match": kinds, "misses": len(self.misses),
                "asset_fetches": self.asset_fetches, "credits_charged": 0,
                "credits_live": sum(a.credits_live for a in self.answers),
                "calls": [a.to_json() for a in self.answers + self.misses]}


def _unsign_other_urls(obj: Any, task_id: str) -> Any:
    """Recorded Meshy asset URLs with no stored file point at the replay host too, under
    `missing/`, so a fetch fails loudly instead of reaching the network."""
    def walk(v: Any, path: str) -> Any:
        if isinstance(v, str) and v.startswith("https://assets.meshy.ai/"):
            name = Path(urlparse(v).path).name or "asset.bin"
            return f"https://{ASSET_HOST}/{task_id}/missing/{name}?{SIGNED_QUERY}"
        if isinstance(v, dict):
            return {k: walk(x, f"{path}.{k}") for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x, f"{path}.{i}") for i, x in enumerate(v)]
        return v

    return walk(obj, "")
