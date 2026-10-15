"""Worker settings from the environment and the repo .env file.

Secrets are read, never printed: they are excluded from repr and never logged.
Environment variables override .env values. Set P2P_ENV_FILE to point at another .env.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from p2p_contracts.job import JOB_CAP_CREDITS, JOB_CAP_USD


def find_repo_root(start: Path | None = None) -> Path:
    env = os.environ.get("P2P_REPO_ROOT")
    if env:
        return Path(env).resolve()
    here = (start or Path(__file__)).resolve()
    for p in [here, *here.parents]:
        pp = p / "pyproject.toml"
        if pp.is_file() and "[tool.uv.workspace]" in pp.read_text():
            return p
    return Path.cwd()


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Values are returned, never printed."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0].strip().strip('"').strip("'")
        out[k.strip()] = v
    return out


def _env_files(root: Path) -> list[Path]:
    if os.environ.get("P2P_ENV_FILE"):
        return [Path(os.environ["P2P_ENV_FILE"])]
    files = [root / ".env"]
    # Worktrees under .claude/worktrees/<name> share the main checkout's .env.
    for parent in root.parents:
        if (parent / ".git").is_dir():
            files.append(parent / ".env")
            break
    return files


def _default_cli() -> str:
    import shutil

    found = shutil.which("claude")
    if found:
        return found
    home = Path.home() / ".local" / "bin" / "claude"
    return str(home) if home.exists() else "claude"


def _floats(v: str | None) -> tuple[float, ...]:
    out = []
    for x in (v or "").split(","):
        try:
            out.append(float(x))
        except ValueError:
            continue
    return tuple(out)


def _flag(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    jobs_dir: Path
    # test = the fake Meshy server (Meshy has no free test mode). live = api.meshy.ai, real credits.
    # replay = the stored paid outputs (fakes/replay.py, evals/fixtures), 0 credits, no network.
    meshy_mode: Literal["test", "live", "replay"] = "test"
    meshy_base_url: str = "https://api.meshy.ai"
    # claude_code = Jackson's Claude Code OAuth login via `claude -p` (default, no API billing).
    # api = Anthropic SDK with ANTHROPIC_API_KEY_TURNKEY, only when set on purpose. Never a fallback.
    claude_backend: Literal["claude_code", "api", "mock"] = "claude_code"
    mock_claude: bool = False  # MOCK_CLAUDE=1 forces MockClaude (tests, run-local default)
    claude_cli: str = "claude"
    claude_vision: Literal["stream", "read"] = "stream"
    # product = PLAN.md defaults. dev = one nano-banana-2 concept, standard geometry (cheap live runs).
    cost_profile: Literal["product", "dev"] = "product"
    build_ceiling_usd: float = 150.0
    meshy_build_credit_cap: int = 500  # live Meshy credits across the whole build (ledger)
    job_cap_usd: float = JOB_CAP_USD
    job_cap_credits: int = JOB_CAP_CREDITS
    # Reuse a succeeded Meshy task of another job with identical inputs (credit_ledger.py).
    # None: on for live Meshy, off for the fake (its task ids restart in every process).
    meshy_reuse_across_jobs: bool | None = None
    # GET /openapi/v1/balance (free) before every paid create; below the price, pause.
    meshy_balance_check: bool = True
    vendor_wait_s: float = 900.0  # in-process wait for a webhook before a stage returns WAITING_VENDOR
    missed_webhook_s: float = 600.0  # backup status GET for a task older than this, once per
    meshy_sweep_s: float = 600.0  # serve kicks jobs due a backup status read this often
    baseline_styles: tuple[str, ...] = ("cartoon",)  # realistic is optional (10 more credits)
    worker_id: str = "local"
    supabase_url: str | None = None
    fake_meshy_delay_s: float = 0.05
    # False: the fake server finishes tasks silently and a meshy_events row (edge function, or
    # the smoke test acting as it) must deliver the webhook.
    fake_meshy_webhooks: bool = True
    only_jobs: tuple[str, ...] = ()  # serve handles only these job ids when set (smoke runs)
    heartbeat_s: float = 15.0  # workers row cadence in serve (contract: 15 s)
    # s09: stub keeps s08's first pass; agent runs stages/s09_paint.py (the paint workstream's)
    paint_agent: Literal["stub", "agent"] = "stub"
    # s02 part crops: SAM 3 on the photos. mock (tests), local (MPS), runpod, or off
    sam3_backend: str = "mock"
    # Where s06, s07, s08, s10 builds and the s09 paint run go (offload.py): process keeps the
    # event loop free of GIL-holding native code (P2P_OFFLOAD, default process in load());
    # thread is the dataclass default so tests keep their in-process slicer stub.
    offload: Literal["process", "thread"] = "thread"
    # Stage lifecycle (lifecycle.py). Every stage limit times this (P2P_STAGE_LIMIT_SCALE).
    stage_limit_scale: float = 1.0
    # Wait before retry n (the last value repeats). The dataclass default retries at once (tests);
    # Settings.load uses P2P_RETRY_BACKOFF_S, default 10,60,300,900 (ASSUMED).
    retry_backoff_s: tuple[float, ...] = (0.0,)
    progress_log_s: float = 60.0  # one stage_running line per job this often (P2P_PROGRESS_LOG_S)
    # serve holds `caffeinate -i -w <pid>` while a job is active (KEEP_AWAKE, default on in
    # Settings.load on macOS), and keeps it this long after a job starts waiting on the customer
    keep_awake: bool = False
    keep_awake_user_s: float = 1800.0
    # secrets: never in repr
    meshy_live_key: str | None = field(default=None, repr=False)
    anthropic_turnkey_key: str | None = field(default=None, repr=False)
    supabase_service_key: str | None = field(default=None, repr=False)
    webhook_token: str | None = field(default=None, repr=False)

    @property
    def meshy_test_mode(self) -> bool:
        return self.meshy_mode == "test"

    @property
    def use_supabase(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_key)

    def with_(self, **kw) -> Settings:
        return replace(self, **kw)

    @classmethod
    def load(cls, **overrides) -> Settings:
        root = find_repo_root()
        env: dict[str, str] = {}
        for f in _env_files(root):
            for k, v in read_env_file(f).items():
                env.setdefault(k, v)
        env.update({k: v for k, v in os.environ.items()})

        def get(k: str, default: str | None = None) -> str | None:
            v = env.get(k)
            return v if v not in (None, "") else default

        mode = (get("MESHY_MODE", "test") or "test").lower()
        if mode not in ("test", "live", "replay"):
            raise ValueError("MESHY_MODE must be test, live or replay")
        backend = (get("CLAUDE_BACKEND", "claude_code") or "claude_code").lower()
        if backend not in ("claude_code", "api", "mock"):
            raise ValueError("CLAUDE_BACKEND must be claude_code, api or mock")
        styles = tuple(
            s.strip() for s in (get("BASELINE_STYLES", "cartoon") or "").split(",") if s.strip()
        )
        s = cls(
            repo_root=root,
            jobs_dir=Path(get("P2P_JOBS_DIR") or root / "jobs"),
            meshy_mode=mode,  # type: ignore[arg-type]
            meshy_base_url=get("MESHY_BASE_URL", "https://api.meshy.ai") or "https://api.meshy.ai",
            claude_backend=backend,  # type: ignore[arg-type]
            mock_claude=_flag(get("MOCK_CLAUDE")),
            claude_cli=get("CLAUDE_CLI") or _default_cli(),
            claude_vision="read" if (get("CLAUDE_VISION") or "").lower() == "read" else "stream",
            cost_profile="dev" if (get("P2P_COST_PROFILE", "product") or "").lower() == "dev"
            else "product",
            build_ceiling_usd=float(get("BUILD_SPEND_CEILING_USD", "150") or 150),
            meshy_build_credit_cap=int(get("MESHY_BUILD_CREDIT_CAP", "500") or 500),
            job_cap_usd=float(get("JOB_CAP_USD", str(JOB_CAP_USD)) or JOB_CAP_USD),
            job_cap_credits=int(get("JOB_CAP_CREDITS", str(JOB_CAP_CREDITS)) or JOB_CAP_CREDITS),
            meshy_reuse_across_jobs=(None if get("MESHY_REUSE_ACROSS_JOBS") is None
                                     else _flag(get("MESHY_REUSE_ACROSS_JOBS"))),
            meshy_balance_check=(get("MESHY_BALANCE_CHECK", "1") or "1") not in ("0", "false"),
            vendor_wait_s=float(get("VENDOR_WAIT_S", "900") or 900),
            missed_webhook_s=float(get("MESHY_MISSED_WEBHOOK_S", "600") or 600),
            meshy_sweep_s=float(get("MESHY_SWEEP_S", "600") or 600),
            baseline_styles=styles or ("cartoon",),
            worker_id=get("WORKER_ID", "local") or "local",
            heartbeat_s=float(get("WORKER_HEARTBEAT_S", "15") or 15),
            paint_agent="agent" if (get("PAINT_AGENT") or "").lower() == "agent" else "stub",
            sam3_backend=(get("SAM3_BACKEND", "mock") or "mock").lower(),
            offload="thread" if (get("P2P_OFFLOAD") or "").lower() == "thread" else "process",
            stage_limit_scale=float(get("P2P_STAGE_LIMIT_SCALE", "1") or 1),
            retry_backoff_s=_floats(get("P2P_RETRY_BACKOFF_S", "10,60,300,900")) or (10.0,),
            progress_log_s=float(get("P2P_PROGRESS_LOG_S", "60") or 60),
            keep_awake=(get("KEEP_AWAKE", "1") or "1") not in ("0", "false")
            and sys.platform == "darwin",
            keep_awake_user_s=float(get("KEEP_AWAKE_USER_S", "1800") or 1800),
            fake_meshy_webhooks=(get("FAKE_MESHY_WEBHOOKS", "1") or "1") not in ("0", "false"),
            only_jobs=tuple(j.strip() for j in (get("P2P_ONLY_JOBS") or "").split(",")
                            if j.strip()),
            supabase_url=get("SUPABASE_URL"),
            meshy_live_key=get("MESHY_API_KEY"),
            anthropic_turnkey_key=get("ANTHROPIC_API_KEY_TURNKEY"),
            supabase_service_key=get("SUPABASE_SERVICE_ROLE_KEY"),
            webhook_token=get("MESHY_WEBHOOK_TOKEN"),
        )
        return replace(s, **overrides) if overrides else s
