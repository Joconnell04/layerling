"""Traces: every model, vendor and slicer call of a stage attempt, for the admin trace viewer.

Each call becomes one redacted JSON body at jobs/<id>/<stage>/a<N>/trace/<seq>-<kind>.json
(uploaded to Storage `artifacts` like any other file) plus a small index entry appended to
`manifest.data.trace`. Bodies hold the prompt, system prompt, reference images (job paths, never
base64), the raw reply or the full vendor task object, tokens, credits and $.

- `attempt(ctx)` binds a StageContext for the duration of a stage run (the orchestrator does it).
  With nothing bound, `record` and `link` do nothing.
- `record(kind, ...)` writes one body and its index entry. One entry per (kind, ref).
- `link(ref, rel_path)` adds a downloaded file to the entry of the call that made it.
- `record_meshy(task, obj)` and `record_slicer(report)` build the record for a terminal Meshy
  task and for the slicer gate.
- `redact(obj)`: `util.manifest_safe`, then `logs.Redactor` with the configured secrets, then a
  few extra shapes (partial JWTs, `token=`, long base64), then strings cut at 200,000 chars.
- `backfill_job(...)`: bodies for attempts that ran before tracing, rebuilt from disk
  (`trace-backfill` in the CLI).

Tracing never fails a stage: every error is logged as `trace_failed` and swallowed. No hook
saves the manifest; each hook site is followed by a save that already exists.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import json
import logging
import math
import re
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from p2p_contracts import FileRef, Manifest, StageStatus
from p2p_contracts.job import USD_PER_MESHY_CREDIT
from pydantic import BaseModel

from .logs import Redactor, log_event
from .util import (
    downscale_image,
    manifest_safe,
    read_json,
    sha256_bytes,
    sha256_file,
    utcnow,
    write_json,
)

if TYPE_CHECKING:
    from .stages.base import StageContext

VERSION = 1
MAX_STR = 200_000
MAX_LOG = 50_000
SUMMARY_CHARS = 160
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "EXPIRED"}
KINDS = ("claude", "meshy", "slicer", "runpod")

_CTX: contextvars.ContextVar[StageContext | None] = contextvars.ContextVar(
    "p2p_trace_ctx", default=None
)

# ---------------------------------------------------------------------------- binding


@contextlib.contextmanager
def attempt(ctx: StageContext) -> Iterator[None]:
    """Bind the stage attempt that record() and link() write to."""
    token = _CTX.set(ctx)
    try:
        yield
    finally:
        _CTX.reset(token)


def current() -> StageContext | None:
    return _CTX.get()


def _failed(what: str, kind: str | None, ref: str | None, e: BaseException) -> None:
    with contextlib.suppress(Exception):  # logging must not fail a stage either
        log_event("trace_failed", level=logging.WARNING, what=what, kind=kind, ref=ref,
                  error=f"{type(e).__name__}: {e}"[:300])


# ---------------------------------------------------------------------------- redaction

_EXTRA: list[tuple[re.Pattern[str], str]] = [
    # a JWT cut short (the logs pattern needs all three segments)
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]*){0,2}"), "<jwt>"),
    # `token=<redacted>` and friends still read like a credential; drop the `=`
    (re.compile(r"(?i)\b(token|signature|expires|key-pair-id)=[^\s&\"'<>]*"), r"\1:<redacted>"),
]
_LONG_B64 = re.compile(r"[A-Za-z0-9+/=_-]{2000,}")


def _jsonable(v: Any) -> Any:
    if isinstance(v, BaseModel):
        return v.model_dump(mode="json")
    if isinstance(v, Path):
        return v.as_posix()
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _clean_str(s: str, cap: int | None) -> str:
    for pat, repl in _EXTRA:
        s = pat.sub(repl, s)
    if len(s) >= 2000 and _LONG_B64.fullmatch(s.strip()):
        return f"<base64 {len(s)} chars>"
    if cap is not None and len(s) > cap:
        s = s[:cap] + f"\n[cut: {len(s) - cap} more chars]"
    return s


def _walk_str(v: Any, fn) -> Any:
    if isinstance(v, str):
        return fn(v)
    if isinstance(v, dict):
        return {k: _walk_str(x, fn) for k, x in v.items()}
    if isinstance(v, list):
        return [_walk_str(x, fn) for x in v]
    return v


def redact(obj: Any, secrets: Iterable[str] | None = None, *, cap: int | None = MAX_STR) -> Any:
    """What a trace body, index entry or rewritten manifest may hold: no keys, JWTs, bearer
    tokens, signed URLs, data URIs, base64 images or configured secret values."""
    if secrets is None:
        ctx = _CTX.get()
        secrets = _secrets_of(ctx.settings) if ctx is not None else []
    # manifest_safe string by string; its data-URI form is kept as is (hashing it again
    # would lose the hash)
    safe = _walk_str(_jsonable(obj), lambda s: s if _SAFE_DATA.match(s) else manifest_safe(s))
    safe = Redactor(list(secrets)).value(safe)
    return _walk_str(safe, lambda s: _clean_str(s, cap))


def _secrets_of(settings) -> list[str]:
    from .logs import secrets_of

    return secrets_of(settings)


# ---------------------------------------------------------------------------- job paths

_STORAGE_REF = re.compile(r"^storage:(?:inputs|artifacts)/([0-9A-Fa-f-]{36})/(.+)$")
_SAFE_DATA = re.compile(r"^data:([\w.+/-]+);sha256=([0-9a-f]{16});len=(\d+)$")
_B64_CACHE: dict[tuple[str, int, int, str], tuple[int, str]] = {}


def _b64_sig(p: Path, variant: str) -> tuple[int, str]:
    """(len, sha256[:16]) of the base64 text the worker would have sent for this file."""
    st = p.stat()
    key = (str(p), st.st_mtime_ns, st.st_size, variant)
    if key not in _B64_CACHE:
        if variant == "raw":
            data = p.read_bytes()
        else:
            data, _ = downscale_image(p, max_px=int(variant))
        text = base64.b64encode(data).decode()
        _B64_CACHE[key] = (len(text), sha256_bytes(text.encode())[:16])
    return _B64_CACHE[key]


def resolve_data(job_dir: Path, mime: str, h16: str, n: int) -> str | None:
    """Job-relative path of the file behind a data URI (manifest_safe form: mime, the first
    16 hex of the base64 text's sha256, its length). The worker sends job photos and concept
    PNGs downscaled (1536 px for Meshy, 1024 px for Claude) and GLBs raw."""
    if not job_dir.is_dir():
        return None
    cands = sorted((job_dir / "input").glob("*")) + sorted(job_dir.glob("s*/a*/*"))
    for p in cands:
        ext = p.suffix.lower()
        if not p.is_file() or ext in {".json", ".jsonl", ".log", ".npy", ".npz", ".ply"}:
            continue
        if mime.startswith("image/"):
            if (mime == "image/png") != (ext == ".png") or ext not in IMAGE_EXT | {".heic"}:
                continue
            variants = ["raw", "1536", "1024", "2048"]
        else:
            variants = ["raw"]
        for v in variants:
            if v == "raw" and 4 * math.ceil(p.stat().st_size / 3) != n:
                continue
            try:
                ln, h = _b64_sig(p, v)
            except Exception:  # noqa: BLE001 not an image Pillow can read
                break
            if ln == n and h == h16:
                return p.relative_to(job_dir).as_posix()
    return None


def job_path(job_dir: Path, job_id: str, x: Any) -> str | None:
    """A job-relative path for an image or file reference: an absolute path in the job folder,
    a `storage:<bucket>/<job_id>/<path>` ref, a data URI (matched by hash), an Anthropic
    base64 image block, or an already relative path. Never returns base64."""
    if x is None:
        return None
    if isinstance(x, dict):
        src = x.get("source") if isinstance(x.get("source"), dict) else {}
        if src.get("type") == "base64" and isinstance(src.get("data"), str):
            data = src["data"]
            mime = str(src.get("media_type") or "image/jpeg")
            hit = resolve_data(job_dir, mime, sha256_bytes(data.encode())[:16], len(data))
            return hit or f"unresolved:{mime}"
        if src.get("type") == "url" and isinstance(src.get("url"), str):
            x = src["url"]
        else:
            return None
    if isinstance(x, Path) or (isinstance(x, str) and x.startswith("/")):
        p = Path(x)
        try:
            return p.resolve().relative_to(job_dir.resolve()).as_posix()
        except ValueError:
            return p.name
    s = str(x)
    if s.startswith(("data:", "http://", "https://")) and not _SAFE_DATA.match(s):
        s = manifest_safe(s)
    m = _SAFE_DATA.match(s)
    if m:
        return resolve_data(job_dir, m.group(1), m.group(2), int(m.group(3))) or \
            f"unresolved:{m.group(1)}"
    m = _STORAGE_REF.match(s)
    if m and m.group(1).lower() == job_id.lower():
        return m.group(2)
    return s


def _storage_to_paths(v: Any, job_id: str) -> Any:
    def one(s: str) -> str:
        m = _STORAGE_REF.match(s)
        return m.group(2) if m and m.group(1).lower() == job_id.lower() else s

    return _walk_str(v, one)


# ---------------------------------------------------------------------------- bodies


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    return v.isoformat() if isinstance(v, datetime) else str(v)


def _text(v: Any) -> str | None:
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, BaseModel):
        return v.model_dump_json(indent=2)
    return json.dumps(_jsonable(v), indent=2, default=str)


def build_body(*, job_dir: Path, job_id: str, stage: str, attempt: int, seq: int, kind: str,
               vendor: str, model: str, purpose: str, ref: str | None,
               started_at: Any = None, duration_s: float | None = None, status: str = "ok",
               error: str | None = None, credits: int = 0, usd: float = 0.0,
               usd_list: float = 0.0, billing: str | None = None,
               tokens: dict[str, int] | None = None, input: dict[str, Any] | None = None,
               output: dict[str, Any] | None = None, rounds: list | None = None,
               files: list | None = None, test_mode: bool = False,
               reconstructed: bool = False, secrets: Iterable[str] = ()) -> dict[str, Any]:
    """One redacted trace body (schema v1, see docs/plans/telemetry.md)."""
    inp = dict(input or {})
    images = []
    for x in inp.pop("images", None) or []:
        p = job_path(job_dir, job_id, x)
        if p and p not in images:
            images.append(p)
    body_in: dict[str, Any] = {
        "system": _text(inp.pop("system", None)),
        "text": _text(inp.pop("text", None)),
        "images": images,
        "params": _storage_to_paths(_walk_str(
            _jsonable(inp.pop("params", None) or {}),
            lambda s: s if _SAFE_DATA.match(s) else manifest_safe(s)), job_id),
    }
    body_in.update(_jsonable(inp))
    out = dict(output or {})
    out_files: list[str] = []
    for x in [*(out.pop("files", None) or []), *(files or [])]:
        p = job_path(job_dir, job_id, x)
        if p and p not in out_files:
            out_files.append(p)
    body_out: dict[str, Any] = {"text": _text(out.pop("text", None)),
                                "json": _jsonable(out.pop("json", None)), "files": out_files}
    body_out.update(_jsonable(out))
    tok = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
    tok.update({k: int(v or 0) for k, v in (tokens or {}).items()})
    body = {
        "v": VERSION, "job_id": job_id, "stage": stage, "attempt": attempt, "seq": seq,
        "kind": kind, "vendor": vendor, "model": model, "purpose": purpose, "ref": ref,
        "status": status, "error": error, "started_at": _iso(started_at),
        "duration_s": round(float(duration_s), 3) if duration_s is not None else None,
        "credits": int(credits or 0), "usd": round(float(usd or 0), 6),
        "usd_list": round(float(usd_list or 0), 6), "billing": billing, "test_mode": test_mode,
        "tokens": tok, "input": body_in, "output": body_out, "rounds": _jsonable(rounds or []),
        "reconstructed": reconstructed,
    }
    return redact(body, secrets)


def index_entry(body: dict[str, Any], path: str) -> dict[str, Any]:
    """The ~400 byte entry that goes in manifest.data.trace."""
    text = (body.get("input") or {}).get("text") or ""
    e = {
        "seq": body["seq"], "kind": body["kind"], "ref": body.get("ref"), "path": path,
        "model": body.get("model"), "purpose": body.get("purpose"), "status": body.get("status"),
        "started_at": body.get("started_at"), "duration_s": body.get("duration_s"),
        "credits": body.get("credits", 0), "usd": body.get("usd", 0.0),
        "usd_list": body.get("usd_list", 0.0), "summary": text[:SUMMARY_CHARS],
        "images": list((body.get("input") or {}).get("images") or []),
        "files": list((body.get("output") or {}).get("files") or []),
    }
    if body.get("error"):
        e["error"] = str(body["error"])[:300]
    if body.get("reconstructed"):
        e["reconstructed"] = True
    return e


def _next_seq(attempt_dir: Path, entries: list[dict[str, Any]]) -> int:
    n = max((int(e.get("seq") or 0) for e in entries), default=0)
    tdir = attempt_dir / "trace"
    if tdir.is_dir():
        for p in tdir.glob("*.json"):
            m = re.match(r"^(\d+)-", p.name)
            if m:
                n = max(n, int(m.group(1)))
    return n + 1


# ---------------------------------------------------------------------------- live capture


def record(kind: str, *, vendor: str, model: str, purpose: str, ref: str | None = None,
           started_at: Any = None, duration_s: float | None = None, status: str = "ok",
           error: str | None = None, credits: int = 0, usd: float = 0.0, usd_list: float = 0.0,
           billing: str | None = None, tokens: dict[str, int] | None = None,
           input: dict[str, Any] | None = None, output: dict[str, Any] | None = None,
           rounds: list | None = None, files: list | None = None,
           test_mode: bool = False) -> dict[str, Any] | None:
    """Write one trace body for the bound attempt and append its index entry. Returns the entry,
    or None (nothing bound, a duplicate (kind, ref), or a tracing error)."""
    ctx = _CTX.get()
    if ctx is None:
        return None
    try:
        entries: list[dict[str, Any]] = ctx.m.data.setdefault("trace", [])
        if ref and any(e.get("kind") == kind and e.get("ref") == ref for e in entries):
            return None
        seq = _next_seq(ctx.dir, entries)
        rel = f"{ctx.rel_dir}/trace/{seq:02d}-{kind}.json"
        body = build_body(
            job_dir=ctx.job_dir, job_id=ctx.job.id, stage=ctx.stage.value, attempt=ctx.attempt,
            seq=seq, kind=kind, vendor=vendor, model=model, purpose=purpose, ref=ref,
            started_at=started_at, duration_s=duration_s, status=status, error=error,
            credits=credits, usd=usd, usd_list=usd_list, billing=billing, tokens=tokens,
            input=input, output=output, rounds=rounds, files=files, test_mode=test_mode,
            secrets=_secrets_of(ctx.settings))
        _write_body(ctx.abs(rel), body)
        ctx.store.upload_artifacts(ctx.job.id, [rel])
        entry = index_entry(body, rel)
        entries.append(entry)
        return entry
    except Exception as e:  # noqa: BLE001 tracing never fails a stage
        _failed("record", kind, ref, e)
        return None


def _write_body(path: Path, body: dict[str, Any]) -> None:
    write_json(path, body)


def link(ref: str | None, rel_path: str) -> None:
    """Add a downloaded file to the trace entry (and body) of the call identified by ref."""
    ctx = _CTX.get()
    if ctx is None or not ref:
        return
    try:
        for e in ctx.m.data.get("trace") or []:
            if e.get("ref") != ref:
                continue
            files = e.setdefault("files", [])
            if rel_path in files:
                return
            files.append(rel_path)
            p = ctx.abs(e["path"])
            body = read_json(p)
            if isinstance(body, dict):
                out = body.setdefault("output", {})
                if rel_path not in (out.get("files") or []):
                    out["files"] = [*(out.get("files") or []), rel_path]
                _write_body(p, body)
                ctx.store.upload_artifacts(ctx.job.id, [e["path"]])
            return
    except Exception as e:  # noqa: BLE001
        _failed("link", None, ref, e)


# ---------------------------------------------------------------------------- meshy

MESHY_TEXT_KEYS = ("prompt", "texture_prompt", "text_style_prompt")
MESHY_IMAGE_KEYS = ("reference_image_urls", "image_urls", "image_url", "texture_image_urls",
                    "texture_image_url", "image_style_url", "input_image_url")


def meshy_label(endpoint: str, request: dict[str, Any] | None) -> str:
    """ai_model for image endpoints, else the endpoint's short name (print/multi-color)."""
    short = endpoint.removeprefix("openapi/v1/").removeprefix("openapi/")
    if short in ("image-to-image", "text-to-image"):
        return str((request or {}).get("ai_model") or short)
    return short


def meshy_input(request: dict[str, Any] | None) -> dict[str, Any]:
    """Split a Meshy request body into prompt text, image refs and the other params."""
    req = dict(request or {})
    text = None
    for k in MESHY_TEXT_KEYS:
        if isinstance(req.get(k), str):
            text = req.pop(k)
            break
    images: list[Any] = []
    for k in MESHY_IMAGE_KEYS:
        v = req.pop(k, None)
        if v is not None:
            images.extend(v if isinstance(v, list) else [v])
    return {"text": text, "images": images, "params": req}


def _meshy_error(task) -> str | None:
    if task.status == "SUCCEEDED":
        return None
    err = (task.result or {}).get("task_error") or {}
    msg = err.get("message") if isinstance(err, dict) else str(err)
    return str(msg or task.status)


def _linked_images(ctx: StageContext, task_id: str) -> list[str]:
    """Image files already linked to a task: the concept PNG behind a chained input_task_id,
    or the 3D model's thumbnails behind analyze and multi-color."""
    manifests: list[Manifest] = [ctx.m]
    for sid, n in ctx.job.attempts.items():
        if sid == ctx.stage or not n:
            continue
        m = ctx.store.load_manifest(ctx.job.id, sid, n)
        if m is not None:
            manifests.append(m)
    for m in manifests:
        for e in m.data.get("trace") or []:
            if e.get("ref") == task_id:
                return [f for f in e.get("files") or [] if Path(f).suffix.lower() in IMAGE_EXT]
    return []


def record_meshy(task, obj: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """One trace entry for a Meshy task that reached a terminal status. `obj` is the full
    task object (GET response or webhook payload); signed URLs are stripped on write."""
    ctx = _CTX.get()
    if ctx is None:
        return None
    try:
        if str(task.status) not in TERMINAL:
            return None
        inp = meshy_input(task.request)
        tid = (task.request or {}).get("input_task_id")
        if tid:
            inp["images"] += _linked_images(ctx, str(tid))
        done = task.finished_at
        return record(
            "meshy", vendor="meshy", model=meshy_label(task.endpoint, task.request),
            purpose=task.purpose, ref=task.task_id, started_at=task.created_at,
            duration_s=(done - task.created_at).total_seconds() if done else None,
            status="ok" if task.status == "SUCCEEDED" else "error", error=_meshy_error(task),
            credits=task.credits, usd=round(task.credits * USD_PER_MESHY_CREDIT, 6),
            billing="credits", input=inp,
            output={"json": obj if obj is not None else task.result}, test_mode=task.test_mode)
    except Exception as e:  # noqa: BLE001
        _failed("meshy", "meshy", getattr(task, "task_id", None), e)
        return None


# ---------------------------------------------------------------------------- slicer

_LOG_TS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?)\]")


def log_span_s(text: str) -> float | None:
    """Seconds between the first and last timestamped line of a Bambu Studio log."""
    stamps = []
    for line in text.splitlines():
        m = _LOG_TS.match(line)
        if m:
            with contextlib.suppress(ValueError):
                stamps.append(datetime.fromisoformat(m.group(1)))
    if len(stamps) < 2:
        return None
    return round((stamps[-1] - stamps[0]).total_seconds(), 3)


def scrub_text_file(path: Path, secrets: Iterable[str] = ()) -> str:
    """Redact a text file in place (a slicer log before it goes to Storage); return the text."""
    text = path.read_text(errors="replace")
    clean = redact(text, secrets, cap=None)
    if clean != text:
        path.write_text(clean)
    return clean


def find_slice_log(attempt_dir: Path, job_dir: Path, report: dict[str, Any]) -> Path | None:
    rel = report.get("log")
    if isinstance(rel, str) and rel and (job_dir / rel).is_file():
        return job_dir / rel
    logs = sorted(attempt_dir.glob("*.slice.log"))
    return logs[0] if logs else None


def slicer_fields(report: dict[str, Any], log_text: str | None,
                  overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"printer_profile": report.get("printer_profile"),
                              "process_profile": report.get("process_profile"),
                              "expected_slots": report.get("expected_slots")}
    if overrides:
        params["process_overrides"] = overrides
    out_json = {k: report.get(k) for k in (
        "ok", "print_time_s", "total_grams", "purge_grams", "slots_used", "errors", "warnings",
        "slicer_version", "filaments")}
    text = log_text
    if log_text and len(log_text) > MAX_LOG:  # the end of the log carries the errors
        text = f"[first {len(log_text) - MAX_LOG} chars cut]\n" + log_text[-MAX_LOG:]
    return {
        "model": f"{report.get('slicer') or 'slicer'} {report.get('slicer_version') or ''}".strip(),
        "status": "ok" if report.get("ok") else "error",
        "error": "; ".join(report.get("errors") or [])[:300] or (None if report.get("ok")
                                                                 else "gate failed"),
        "duration_s": log_span_s(log_text or ""),
        "input": {"params": params}, "output": {"json": out_json, "text": text},
    }


def record_slicer(report) -> dict[str, Any] | None:
    """After slice_report.json: keep the slicer log as an output (so it reaches Storage) and
    record one slicer entry with the report numbers and the log text."""
    ctx = _CTX.get()
    if ctx is None:
        return None
    try:
        rep = _jsonable(report)
        log = find_slice_log(ctx.dir, ctx.job_dir, rep)
        files = []
        for name in ("baseline_x2d.3mf", "debug_x2d.3mf", "slice_report.json"):
            if ctx.path(name).is_file():
                # a failed gate keeps the project only as debug_x2d.3mf (moved right after this)
                files.append(ctx.rel("debug_x2d.3mf" if name == "baseline_x2d.3mf"
                                     and not rep.get("ok") else name))
        log_text = None
        if log is not None:
            log_text = scrub_text_file(log, _secrets_of(ctx.settings))
            if log.parent == ctx.dir:
                ctx.add_output(log.name, role="slice_log", kind="log")
            files.append(log.relative_to(ctx.job_dir).as_posix())
        gate = ctx.steps.get("gate") if isinstance(ctx.steps.get("gate"), dict) else {}
        f = slicer_fields(rep, log_text, ((gate or {}).get("retarget") or {}).get(
            "process_overrides"))
        dur = f["duration_s"]
        return record(
            "slicer", vendor="local", model=f["model"], purpose="slicer gate",
            ref=ctx.rel("slice_report.json"),
            started_at=utcnow() - timedelta(seconds=dur or 0), duration_s=dur,
            status=f["status"], error=f["error"], billing="free", input=f["input"],
            output=f["output"], files=files)
    except Exception as e:  # noqa: BLE001
        _failed("slicer", "slicer", None, e)
        return None


# ---------------------------------------------------------------------------- backfill

_MESHY_FILE = re.compile(r"^(concept_r\d+c\d+\.png|mesh_m\d+\.glb|thumb_m\d+_[a-z]+\.png|"
                         r"baseline_(?!x2d)[a-z]+\.3mf)$")
_ATTEMPT = re.compile(r"^(s\d\d_[a-z0-9_]+)/a(\d+)/manifest\.json$")


def _attempt_manifests(job_dir: Path) -> list[tuple[Path, Manifest]]:
    out = []
    for p in job_dir.glob("s*/a*/manifest.json"):
        rel = p.relative_to(job_dir).as_posix()
        m = _ATTEMPT.match(rel)
        data = read_json(p)
        if not m or not isinstance(data, dict):
            continue
        out.append((p, Manifest.model_validate(data)))
    out.sort(key=lambda x: (x[1].stage.value, x[1].attempt))
    return out


def _task_files(m: Manifest) -> dict[str, list[str]]:
    """Meshy task id -> files downloaded from it, from what the attempt recorded."""
    out: dict[str, list[str]] = {}
    steps = m.data.get("steps") or {}
    keys = m.data.get("meshy_keys") or {}
    for k, v in steps.items():
        if re.match(r"^r\d+\.candidates$", k) and isinstance(v, list):
            for c in v:
                if isinstance(c, dict) and c.get("meshy_task_id") and c.get("image"):
                    out.setdefault(c["meshy_task_id"], []).append(c["image"])
        if re.match(r"^m\d+\.mesh$", k) and isinstance(v, dict) and keys.get(v.get("key")):
            tid = keys[v["key"]]
            out.setdefault(tid, []).extend([v["glb"], *(v.get("thumbnails") or [])])
    if len(m.meshy_tasks) == 1 and m.meshy_tasks[0].task_id not in out:
        names = [o.path for o in m.outputs if _MESHY_FILE.match(o.path.rsplit("/", 1)[-1])]
        if names:
            out[m.meshy_tasks[0].task_id] = names
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}


def _intake_text(job_dir: Path, m: Manifest, billing: str) -> str | None:
    """The intake user text, rebuilt with today's code from input/request.json."""
    try:
        from p2p_contracts.job import AddOn

        from .clients.claude import IntakeRequest
        from .stages.prompts import bank_for
        from .stages.s01_intake import skipped_questions

        req = read_json(job_dir / "input" / "request.json", {}) or {}
        product = str(req.get("product_type") or "figurine")
        prompt = str(req.get("prompt") or "")
        addons = []
        for a in req.get("addons") or []:
            with contextlib.suppress(ValueError):
                addons.append(AddOn.model_validate(a))
        size = req.get("target_size_mm")
        bank = bank_for(product)
        photos = [i.path for i in m.inputs if i.role == "photo"]
        ir = IntakeRequest(prompt=prompt, product_type=product, photos=[], question_bank=bank,
                           form_target_size_mm=size, form_addons=addons,
                           skip_questions=skipped_questions(bank, size, addons))
        n = len(photos)
        tail = (f"There are {n} photos, indices 0 to {n - 1}." if billing == "subscription"
                else f"Photos above, in order, indices 0 to {n - 1}.")
        return (f"Request: {prompt}\nProduct type chosen in the app: {product}\n"
                f"{ir.form_lines()}\n{tail}")
    except Exception:  # noqa: BLE001 best effort
        return None


def _backfill_calls(job_dir: Path, m: Manifest, task_files: dict[str, list[str]]
                    ) -> list[dict[str, Any]]:
    """Keyword arguments for build_body, one per call, in call order."""
    calls: list[tuple[Any, dict[str, Any]]] = []
    photos = [i.path for i in m.inputs if i.role == "photo"]
    steps = m.data.get("steps") or {}
    route_steps = [v for k, v in sorted(steps.items(), key=lambda kv: kv[0])
                   if k.startswith("route.g")]
    n_route = 0
    for mc in m.model_calls:
        inp: dict[str, Any] = {"system": None, "system_note": "not captured", "images": []}
        out: dict[str, Any] = {"text": None, "json": None, "text_note": "not captured"}
        if mc.purpose.startswith("intake"):
            inp["text"] = _intake_text(job_dir, m, mc.billing)
            inp["images"] = photos
            out["json"] = steps.get("intake")
        elif mc.purpose.startswith("route.tweak"):
            out["json"] = route_steps[n_route] if n_route < len(route_steps) else None
            n_route += 1
        calls.append((mc.started_at, {
            "kind": "claude", "vendor": mc.vendor, "model": mc.model, "purpose": mc.purpose,
            "ref": mc.request_id, "started_at": mc.started_at, "duration_s": mc.duration_s,
            "credits": mc.credits, "usd": mc.usd, "usd_list": mc.usd_list,
            "billing": mc.billing,
            "tokens": {"in": mc.input_tokens, "out": mc.output_tokens,
                       "cache_read": mc.cache_read_tokens, "cache_write": mc.cache_write_tokens},
            "input": inp, "output": out}))
    for t in m.meshy_tasks:
        inp = meshy_input(t.request)
        refs = []
        for x in inp["images"]:
            p = job_path(job_dir, m.job_id, x)
            refs.append(p)
        if any(r and r.startswith("unresolved:image/jpeg") for r in refs) and photos:
            # data URIs of the job photos (runs without Supabase), in the order sent
            it = iter(photos)
            refs = [next(it, r) if r and r.startswith("unresolved:image/jpeg") else r
                    for r in refs]
        inp["images"] = [r for r in refs if r]
        tid = (t.request or {}).get("input_task_id")
        if tid:
            inp["images"] += [f for f in task_files.get(str(tid), [])
                              if Path(f).suffix.lower() in IMAGE_EXT]
        done = t.finished_at
        status = "ok" if t.status == "SUCCEEDED" else (
            "error" if t.status in TERMINAL else "pending")
        calls.append((t.created_at, {
            "kind": "meshy", "vendor": "meshy", "model": meshy_label(t.endpoint, t.request),
            "purpose": t.purpose, "ref": t.task_id, "started_at": t.created_at,
            "duration_s": (done - t.created_at).total_seconds() if done else None,
            "status": status, "error": _meshy_error(t) if t.status in TERMINAL else None,
            "credits": t.credits, "usd": round(t.credits * USD_PER_MESHY_CREDIT, 6),
            "billing": "credits", "test_mode": t.test_mode, "input": inp,
            "output": {"json": t.result, "json_note": "trimmed result; full task not captured",
                       "files": task_files.get(t.task_id, [])}}))
    adir = job_dir / m.stage.value / f"a{m.attempt}"
    rep = read_json(adir / "slice_report.json")
    if isinstance(rep, dict):
        log = find_slice_log(adir, job_dir, rep)
        log_text = log.read_text(errors="replace") if log else None
        gate = steps.get("gate") if isinstance(steps.get("gate"), dict) else {}
        f = slicer_fields(rep, log_text, ((gate or {}).get("retarget") or {}).get(
            "process_overrides"))
        files = [f"{m.stage.value}/a{m.attempt}/{n}" for n in
                 ("baseline_x2d.3mf", "debug_x2d.3mf", "slice_report.json")
                 if (adir / n).is_file()]
        if log is not None:
            files.append(log.relative_to(job_dir).as_posix())
        calls.append((None, {
            "kind": "slicer", "vendor": "local", "model": f["model"], "purpose": "slicer gate",
            "ref": f"{m.stage.value}/a{m.attempt}/slice_report.json", "started_at": None,
            "duration_s": f["duration_s"], "status": f["status"], "error": f["error"],
            "billing": "free", "input": f["input"], "output": f["output"], "files": files}))

    def when(c):
        t = c[0]
        return (t is None, t.timestamp() if isinstance(t, datetime) else 0)

    return [kw for _, kw in sorted(calls, key=when)]


def _add_slice_log_output(job_dir: Path, m: Manifest, secrets: Iterable[str]) -> str | None:
    adir = job_dir / m.stage.value / f"a{m.attempt}"
    logs = sorted(adir.glob("*.slice.log"))
    if not logs:
        return None
    scrub_text_file(logs[0], secrets)
    rel = logs[0].relative_to(job_dir).as_posix()
    ref = FileRef(path=rel, sha256=sha256_file(logs[0]), bytes=logs[0].stat().st_size,
                  kind="log", role="slice_log")
    m.outputs = [o for o in m.outputs if o.path != rel] + [ref]
    return rel


def backfill_job(job_dir: Path, *, store=None, force: bool = False,
                 secrets: Iterable[str] = ()) -> dict[str, Any]:
    """Trace bodies for every attempt of one job that has no `data.trace`, rebuilt from disk.

    - Claude: call metadata, the parsed reply from data.steps, the user text rebuilt from
      input/request.json and the photos; the system prompt and raw reply are marked not
      captured. Every body says `reconstructed: true`.
    - Meshy: the request, the trimmed result, and the files matched through the attempt's steps
      (concept candidates, mesh rounds) or its single task.
    - Slicer: slice_report.json and the .slice.log (also added as an output).

    Every manifest on disk is rewritten through redact() (manifest_safe plus the log patterns),
    which also removes old signed URLs. With `store` (a SupabaseStore), upload_job() then
    brings the traces to the stage_runs rows Supabase already has. `force` rebuilds backfilled
    traces; traces captured live are never replaced.
    """
    secrets = list(secrets)
    job_id = job_dir.name
    stats: dict[str, Any] = {"attempts": 0, "built": 0, "entries": 0, "rewritten": 0}
    manifests = _attempt_manifests(job_dir)
    task_files: dict[str, list[str]] = {}
    for _, m in manifests:
        task_files.update(_task_files(m))
    for path, m in manifests:
        stats["attempts"] += 1
        before = json.dumps(read_json(path), sort_keys=True, default=str)
        if m.status != StageStatus.SKIPPED:
            _add_slice_log_output(job_dir, m, secrets)
            existing = m.data.get("trace") or []
            live = any(not e.get("reconstructed") for e in existing)
            if not existing or (force and not live):
                adir = path.parent
                for old in (adir / "trace").glob("*.json") if (adir / "trace").is_dir() else []:
                    old.unlink()
                entries = []
                for seq, kw in enumerate(_backfill_calls(job_dir, m, task_files), start=1):
                    rel = f"{m.stage.value}/a{m.attempt}/trace/{seq:02d}-{kw['kind']}.json"
                    body = build_body(job_dir=job_dir, job_id=job_id, stage=m.stage.value,
                                      attempt=m.attempt, seq=seq, reconstructed=True,
                                      secrets=secrets, **kw)
                    _write_body(job_dir / rel, body)
                    entries.append(index_entry(body, rel))
                if entries:
                    m.data["trace"] = entries
                    stats["built"] += 1
                    stats["entries"] += len(entries)
        safe = Manifest.model_validate(redact(m.model_dump(mode="json"), secrets, cap=None))
        if json.dumps(safe.model_dump(mode="json"), sort_keys=True, default=str) != before:
            write_json(path, safe.model_dump(mode="json"))
            stats["rewritten"] += 1
    if store is not None:
        stats.update(upload_job(job_dir, store, secrets=secrets))
    return stats


def upload_job(job_dir: Path, store, *, secrets: Iterable[str] = ()) -> dict[str, Any]:
    """Bring a job's traces to Supabase without rewriting its history.

    For every stage_runs row Supabase already has: the row's own manifest gets the local
    `data.trace` (unless it already holds a live one) and the slicer log output, goes through
    redact() (which also drops old signed URLs), and is written back with a PATCH of the
    `manifest` column only; status, times and money columns stay as they are. The trace bodies
    and slicer logs of those attempts go to Storage `artifacts`. Attempts that exist only on disk
    (a local rerun) are reported and left out: their outputs were never uploaded.
    `store` is a SupabaseStore (PostgREST and Storage through its service-role client).
    """
    secrets = list(secrets)
    job_id = job_dir.name
    local = {(m.stage.value, m.attempt): m for _, m in _attempt_manifests(job_dir)}
    rows = store._rest("GET", "stage_runs", params={
        "job_id": f"eq.{job_id}", "select": "stage,attempt,status,manifest"}) or []
    out: dict[str, Any] = {"patched": 0, "uploaded": 0, "local_only": []}
    remote_keys = set()
    for row in rows:
        key = (row["stage"], int(row["attempt"]))
        remote_keys.add(key)
        m = local.get(key)
        remote = row.get("manifest") or {}
        if m is None or not remote:
            continue
        merged = json.loads(json.dumps(remote))
        data = merged.setdefault("data", {})
        entries = m.data.get("trace") or []
        if entries and not any(not e.get("reconstructed") for e in data.get("trace") or []):
            data["trace"] = entries
        uploads = [e["path"] for e in data.get("trace") or [] if (job_dir / e["path"]).is_file()]
        for o in m.outputs:
            if o.role == "slice_log" and (job_dir / o.path).is_file():
                uploads.append(o.path)
                if not any(x.get("path") == o.path for x in merged.get("outputs") or []):
                    merged.setdefault("outputs", []).append(o.model_dump(mode="json"))
        safe = redact(merged, secrets, cap=None)
        with contextlib.suppress(ValueError):  # a manifest from an older schema stays a dict
            safe = Manifest.model_validate(safe).model_dump(mode="json")
        if uploads:
            store.upload_artifacts(job_id, uploads)
            out["uploaded"] += len(uploads)
        if json.dumps(safe, sort_keys=True, default=str) != json.dumps(remote, sort_keys=True,
                                                                       default=str):
            store._rest("PATCH", "stage_runs", params={
                "job_id": f"eq.{job_id}", "stage": f"eq.{key[0]}", "attempt": f"eq.{key[1]}"},
                body={"manifest": safe}, prefer="return=minimal")
            out["patched"] += 1
    out["local_only"] = [f"{st}/a{n}" for (st, n), m in sorted(local.items())
                         if (st, n) not in remote_keys and m.status != StageStatus.SKIPPED]
    return out
