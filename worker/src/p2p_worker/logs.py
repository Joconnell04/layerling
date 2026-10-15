"""Structured logs: JSON lines to logs/worker.jsonl, rotated at 10 MB.

Every line carries ts, level, logger, event, msg, plus job_id, stage, attempt from the current
context (set by the orchestrator per stage run) and whatever fields the call adds (duration_ms,
meshy_task_id, status, ...).

Redaction runs on every string before it is written: data URIs, URL query strings (signed asset
URLs, ?token=), bearer tokens, JWTs, Meshy/Anthropic/Supabase key shapes, and the exact values of
the configured secrets. The httpx loggers are held at WARNING because their request lines carry
full URLs.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .util import utcnow

EVENTS = logging.getLogger("p2p_worker.events")
_CTX: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "p2p_log_ctx", default={}  # noqa: B039 never mutated; bind() sets a new dict
)

MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"data:[\w.+/-]+;base64,[A-Za-z0-9+/=_-]+"), "data:<redacted>"),
    (re.compile(r"(https?://[^\s\"'<>?]+)\?[^\s\"'<>]+"), r"\1?<redacted>"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"), "<jwt>"),
    (re.compile(r"\b(?:msy|sk-ant|sb_secret|sb_publishable)[_-][A-Za-z0-9_-]{6,}"), "<key>"),
    (re.compile(r"(?i)\b(token|apikey|api_key|signature|key)=[^\s&\"']+"), r"\1=<redacted>"),
]


class Redactor:
    def __init__(self, secrets: list[str] | None = None):
        self.secrets = sorted({s for s in (secrets or []) if s and len(s) >= 8}, key=len,
                              reverse=True)

    def text(self, s: str) -> str:
        for secret in self.secrets:
            if secret in s:
                s = s.replace(secret, "<secret>")
        for pat, repl in _PATTERNS:
            s = pat.sub(repl, s)
        return s

    def value(self, v: Any) -> Any:
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, dict):
            return {k: self.value(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [self.value(x) for x in v]
        if isinstance(v, (int, float, bool)) or v is None:
            return v
        return self.text(str(v))


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor):
        super().__init__()
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": utcnow().isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": getattr(record, "event", None) or "log",
            "msg": record.getMessage(),
        }
        out.update(getattr(record, "ctx", None) or _CTX.get())
        out.update(getattr(record, "fields", None) or {})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)[-4000:]
        return json.dumps(self.redactor.value(out), default=str, separators=(",", ":"))


def bind(**fields: Any) -> contextvars.Token:
    return _CTX.set({**_CTX.get(), **{k: v for k, v in fields.items() if v is not None}})


@contextlib.contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    token = bind(**fields)
    try:
        yield
    finally:
        _CTX.reset(token)


def log_event(event: str, level: int = logging.INFO, msg: str = "", **fields: Any) -> None:
    EVENTS.log(level, msg or event, extra={"event": event, "ctx": dict(_CTX.get()),
                                           "fields": {k: v for k, v in fields.items()
                                                      if v is not None}})


def secrets_of(settings) -> list[str]:
    return [v for v in (getattr(settings, n, None) for n in (
        "meshy_live_key", "anthropic_turnkey_key", "supabase_service_key", "webhook_token"))
        if isinstance(v, str)]


def setup_logging(settings, log_dir: Path | None = None, *, console: bool = True,
                  level: int = logging.INFO) -> Path:
    """File handler (JSON lines, rotating) plus an optional redacted console handler."""
    log_dir = Path(log_dir or settings.repo_root / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    ignore = log_dir / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")  # logs never get committed
    path = log_dir / "worker.jsonl"
    redactor = Redactor(secrets_of(settings))
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_p2p", False):
            root.removeHandler(h)
    fh = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    fh.setFormatter(JsonFormatter(redactor))
    fh._p2p = True  # type: ignore[attr-defined]
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(_ConsoleFormatter(redactor))
        ch._p2p = True  # type: ignore[attr-defined]
        root.addHandler(ch)
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "websockets", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path


class _ConsoleFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor):
        super().__init__("%(asctime)s %(levelname)s %(message)s")
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        fields = {**(getattr(record, "ctx", None) or {}), **(getattr(record, "fields", None) or {})}
        if fields:
            line += " " + json.dumps(fields, default=str, separators=(",", ":"))
        return self.redactor.text(line)
