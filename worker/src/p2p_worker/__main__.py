"""CLI.

uv run python -m p2p_worker run-local --photo a.jpg [--photo b.jpg] --prompt "..." --auto
uv run python -m p2p_worker advance --job <id>          resume a local job
uv run python -m p2p_worker reply --job <id> --kind concept_pick --payload '{...}'
uv run python -m p2p_worker resume --job <id>           clear a pause, then advance
uv run python -m p2p_worker status --job <id>
uv run python -m p2p_worker paint-edit --job <id> --ops '[{...}]'   repaint, preview, gate
uv run python -m p2p_worker paint-preview --job <id>   the newest paint edit's preview
uv run python -m p2p_worker serve [--port 8787]
uv run python -m p2p_worker smoke-supabase             TEST job through serve on real Supabase
uv run python -m p2p_worker trace-backfill --job <id>   traces for jobs run before tracing
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .config import Settings
from .local import AutoResponder, run_job, run_local
from .logs import setup_logging
from .runtime import build_runtime


def _settings(args) -> Settings:
    over = {}
    if getattr(args, "jobs_dir", None):
        over["jobs_dir"] = Path(args.jobs_dir)
    if getattr(args, "dev", False):
        over["cost_profile"] = "dev"
    if args.cmd in ("run-local", "run-stages") and not getattr(args, "claude", False):
        over["mock_claude"] = True  # MockClaude unless --claude
    s = Settings.load(**over)
    setup_logging(s, os.environ.get("P2P_LOG_DIR") or None,
                  console=args.cmd in ("serve", "smoke-supabase"))
    if s.meshy_mode == "live":
        print(f"MESHY_MODE=live: real credits. Build cap {s.meshy_build_credit_cap} credits, "
              f"job cap {s.job_cap_credits} credits / ${s.job_cap_usd:.2f}.", file=sys.stderr)
    return s


def _summary(rt, job_id: str, status) -> None:
    jdir = rt.store.job_dir(job_id)
    job = rt.store.load_job(job_id)
    row = rt.store.job_row(job_id)
    print(f"job      {job_id}")
    print(f"status   {status.value}")
    print(f"stage    {job.stage.value if job.stage else '-'}")
    print(f"spent    {job.budget.spent_credits} credits, ${job.budget.spent_usd:.4f}"
          f"{' (test mode, nothing billed)' if rt.settings.meshy_mode == 'test' else ''}")
    if row.get("pause_reason"):
        print(f"paused   {row['pause_reason']}")
    if row.get("result"):
        print(f"3mf      {jdir / row['result']['threemf']}")
    print(f"folder   {jdir}")


def _runtime(args, s: Settings):
    """Local folder store unless --supabase (serve always uses Supabase when configured)."""
    from .store import LocalStore

    if getattr(args, "supabase", False):
        return build_runtime(s)
    return build_runtime(s, store=LocalStore(s.jobs_dir))


async def _cmd_run_local(args) -> int:
    s = _settings(args)
    rt = _runtime(args, s)
    try:
        job_id, status, responder = await run_local(
            rt, [Path(p) for p in args.photo], args.prompt, auto=args.auto,
            product_type=args.product, ams_slots=args.ams_slots, tweaks=args.tweak,
            mesh_actions=args.mesh_action, profile=args.profile, form=args.form,
        )
        if responder:
            for line in responder.log:
                print(f"auto     {line}")
        _summary(rt, job_id, status)
        return 0 if status.value in ("done", "waiting_user") else 1
    finally:
        await rt.aclose()


async def _cmd_advance(args) -> int:
    s = _settings(args)
    rt = _runtime(args, s)
    try:
        if args.cmd == "resume":
            rt.orchestrator.resume_job(args.job)
        if args.cmd == "rerun":
            from p2p_contracts import StageId

            rt.orchestrator.rerun_stage(args.job, StageId(args.stage))
        status = await run_job(rt, args.job, AutoResponder() if args.auto else None)
        _summary(rt, args.job, status)
        return 0
    finally:
        await rt.aclose()


def stage_ids(spec: str) -> list:
    """"s02,s08" or full ids -> StageId list."""
    from p2p_contracts import StageId

    out = []
    for tok in (t.strip() for t in spec.split(",") if t.strip()):
        hit = [s for s in StageId if s.value == tok or s.value.startswith(tok + "_")]
        if len(hit) != 1:
            raise SystemExit(f"unknown stage {tok!r}")
        out.append(hit[0])
    return out


async def _cmd_run_stages(args) -> int:
    s = _settings(args)
    rt = _runtime(args, s)
    try:
        out = await rt.orchestrator.run_stages(args.job, stage_ids(args.stages),
                                               allow_meshy=args.allow_meshy)
        for k, v in out.items():
            print(f"{k:18} {v}")
        _summary(rt, args.job, rt.store.load_job(args.job).status)
        return 0 if all(v == "succeeded" for v in out.values()) else 1
    finally:
        await rt.aclose()


def _cmd_reply(args) -> int:
    s = _settings(args)
    from .store import LocalStore

    store = LocalStore(s.jobs_dir)
    job = store.load_job(args.job)
    row = store.post_message(args.job, "user", args.kind, json.loads(args.payload or "{}"),
                             stage=job.stage)
    print(row.id)
    return 0


def _latest_preview(store, job_id: str) -> dict | None:
    """The newest s09 paint edit attempt's PaintPreview, with its attempt and status."""
    from p2p_contracts import StageId

    from .util import read_json

    job = store.load_job(job_id)
    d = store.job_dir(job_id) / StageId.S09_PAINT.value
    nums = sorted((int(p.name[1:]) for p in d.glob("a*") if p.name[1:].isdigit()), reverse=True)
    for n in nums:
        m = store.load_manifest(job_id, StageId.S09_PAINT, n)
        if m is None or "paint_edit" not in m.data:
            continue
        rel = next((o.path for o in m.outputs if o.role == "paint_preview"), None)
        return {"attempt": f"s09_paint/a{n}", "status": m.status.value,
                "accepted": job.attempts.get(StageId.S09_PAINT) == n,
                "error": m.error.message if m.error else None,
                "preview": read_json(store.job_dir(job_id) / rel) if rel else None}
    return None


async def _cmd_paint_edit(args) -> int:
    """Post a paint_edit and apply it (only it: no other stage runs), then print what came
    back (notes, the preview, the result). A running job keeps the message until it is done."""
    from p2p_mcp_paint.edits import current_paint

    s = _settings(args)
    rt = _runtime(args, s)
    try:
        raw = args.ops
        if raw.startswith("@"):
            raw = Path(raw[1:]).read_text()
        payload = {"ops": json.loads(raw), "source": args.source}
        if args.note:
            payload["note"] = args.note
        if args.base != "none":
            base = current_paint(rt.store.job_dir(args.job)) if args.base == "current" \
                else args.base
            if base:
                payload["base_attempt"] = base
        job = rt.store.load_job(args.job)
        row = rt.store.post_message(args.job, "user", "paint_edit", payload, stage=job.stage)
        # only the edit: a full advance could rerun stale stages (and call Meshy)
        from .paint_edit import apply_paint_edits

        status = (await apply_paint_edits(rt.orchestrator, job)).status
        replies = [m.payload.get("text") for m in rt.store.list_messages(args.job,
                                                                         role="assistant")
                   if m.reply_to == row.id and m.kind == "note"]
        out = {"message_id": row.id, "status": status.value, "notes": replies,
               "edit": _latest_preview(rt.store, args.job),
               "result": rt.store.load_job(args.job).result}
        print(json.dumps(out, indent=1, default=str))
        return 0 if status.value in ("done", "review") and not replies else 1
    finally:
        await rt.aclose()


def _cmd_paint_preview(args) -> int:
    from .store import LocalStore

    s = _settings(args)
    print(json.dumps(_latest_preview(LocalStore(s.jobs_dir), args.job), indent=1, default=str))
    return 0


def _cmd_status(args) -> int:
    s = _settings(args)
    from .store import LocalStore

    store = LocalStore(s.jobs_dir)
    job = store.load_job(args.job)
    out = {"job": job.model_dump(mode="json"), "row": store.job_row(args.job), "stages": {}}
    for sid, n in job.attempts.items():
        m = store.load_manifest(args.job, sid, n)
        if m:
            out["stages"][sid.value] = {"attempt": n, "status": m.status.value,
                                        "gate": m.gate.kind if m.gate else None,
                                        "credits": m.credits, "usd": m.usd,
                                        "pause_reason": m.pause_reason}
    print(json.dumps(out, indent=2, default=str))
    return 0


def _cmd_trace_backfill(args) -> int:
    """Trace bodies and data.trace for attempts that ran before tracing. Disk only unless
    --upload (then trace files and slicer logs go to Storage and stage_runs.manifest is
    rewritten). Never calls Meshy or Claude."""
    import re

    from . import trace
    from .logs import secrets_of

    s = _settings(args)
    bad = [j for j in args.job if not re.fullmatch(r"[0-9a-fA-F-]{36}", j)]
    if bad:
        print(f"not a job id: {', '.join(bad)}", file=sys.stderr)
        return 2
    store = None
    if args.upload:
        from .clients.supabase import SupabaseStore

        store = SupabaseStore(s)
    try:
        for job_id in args.job:
            jdir = s.jobs_dir / job_id
            if not (jdir / "job.json").is_file():
                print(f"job {job_id}: no folder under {s.jobs_dir}", file=sys.stderr)
                return 1
            st = trace.backfill_job(jdir, store=store, force=args.force, secrets=secrets_of(s))
            print(f"job {job_id}: {st['attempts']} attempts, {st['built']} traced "
                  f"({st['entries']} calls), {st['rewritten']} manifests rewritten on disk")
            if store is not None:
                print(f"  supabase: {st['patched']} stage_runs manifests updated, "
                      f"{st['uploaded']} files uploaded"
                      + (f", only on disk: {', '.join(st['local_only'])}"
                         if st["local_only"] else ""))
    finally:
        if store is not None:
            store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p2p_worker")
    ap.add_argument("--jobs-dir", help="default: <repo>/jobs")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run-local", help="create a job from photos and run the thin slice")
    r.add_argument("--photo", action="append", required=True)
    r.add_argument("--prompt", required=True)
    r.add_argument("--product", default="figurine",
                   choices=["figurine", "keychain", "nameplate", "ornament", "magnet", "other"])
    r.add_argument("--ams-slots", type=int, default=None, help="default: contract (5)")
    r.add_argument("--form", choices=["flat", "figure"], default=None,
                   help="keychain or magnet form, as the order form would set it")
    r.add_argument("--auto", action="store_true", help="answer every gate automatically")
    r.add_argument("--profile", default="thin_slice", choices=["thin_slice", "full"])
    r.add_argument("--tweak", action="append", default=[], help="scripted concept tweak")
    r.add_argument("--mesh-action", action="append", default=[],
                   choices=["retexture", "regenerate"], help="scripted mesh review action")
    r.add_argument("--dev", action="store_true", help="cheap dev cost profile")
    r.add_argument("--claude", action="store_true",
                   help="use CLAUDE_BACKEND (default claude_code) instead of MockClaude")
    r.add_argument("--supabase", action="store_true", help="mirror rows and files to Supabase")

    for name in ("advance", "resume"):
        a = sub.add_parser(name)
        a.add_argument("--job", required=True)
        a.add_argument("--auto", action="store_true")
        a.add_argument("--dev", action="store_true")
        a.add_argument("--supabase", action="store_true")

    rr = sub.add_parser("rerun", help="fresh attempt of one stage of a local job, then advance")
    rr.add_argument("--job", required=True)
    rr.add_argument("--stage", required=True, help="e.g. s07_baseline")
    rr.add_argument("--auto", action="store_true")
    rr.add_argument("--dev", action="store_true")
    rr.add_argument("--supabase", action="store_true")

    rs = sub.add_parser("run-stages", help="fresh attempts of the given stages of a local job, "
                                           "whatever its profile; Meshy off (0 credits)")
    rs.add_argument("--job", required=True)
    rs.add_argument("--stages", required=True, help="e.g. s02,s08,s09,s10")
    rs.add_argument("--allow-meshy", action="store_true")
    rs.add_argument("--claude", action="store_true",
                    help="use CLAUDE_BACKEND (default claude_code) instead of MockClaude")

    rp = sub.add_parser("reply", help="post a user message to a local job")
    rp.add_argument("--job", required=True)
    rp.add_argument("--kind", required=True)
    rp.add_argument("--payload")

    st = sub.add_parser("status")
    st.add_argument("--job", required=True)

    pe = sub.add_parser("paint-edit", help="repaint a finished job (paint_edit): apply it, print "
                                           "the notes, the p2p_slicer preview and the result")
    pe.add_argument("--job", required=True)
    pe.add_argument("--ops", required=True,
                    help='JSON list of PaintOp, or @file.json, e.g. [{"op": "fill_region", '
                         '"region_ids": [4], "filament_id": "generic-pla-white"}]')
    pe.add_argument("--base", default="current",
                    help="current (default), an attempt folder such as s09_paint/a2, or none")
    pe.add_argument("--source", default="owned", choices=["owned", "any"])
    pe.add_argument("--note", default=None)
    pe.add_argument("--supabase", action="store_true")

    pp = sub.add_parser("paint-preview", help="print the newest paint edit's preview")
    pp.add_argument("--job", required=True)

    sv = sub.add_parser("serve", help="FastAPI app plus the job loop")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8787)

    sm = sub.add_parser("smoke-supabase",
                        help="one TEST job through serve against the real Supabase, then clean up")
    sm.add_argument("--keep", action="store_true", help="leave the TEST rows and files")
    sm.add_argument("--timeout", type=float, default=240.0)

    tb = sub.add_parser("trace-backfill",
                        help="trace files for jobs run before tracing (disk only unless --upload)")
    tb.add_argument("--job", action="append", required=True)
    tb.add_argument("--jobs-dir", dest="jobs_dir", default=argparse.SUPPRESS,
                    help="default: <repo>/jobs or P2P_JOBS_DIR")
    tb.add_argument("--upload", action="store_true",
                    help="upload trace files and slicer logs, rewrite stage_runs.manifest")
    tb.add_argument("--force", action="store_true", help="rebuild backfilled traces")

    args = ap.parse_args(argv)
    if args.cmd == "run-local":
        return asyncio.run(_cmd_run_local(args))
    if args.cmd in ("advance", "resume", "rerun"):
        return asyncio.run(_cmd_advance(args))
    if args.cmd == "run-stages":
        return asyncio.run(_cmd_run_stages(args))
    if args.cmd == "reply":
        return _cmd_reply(args)
    if args.cmd == "status":
        return _cmd_status(args)
    if args.cmd == "paint-edit":
        return asyncio.run(_cmd_paint_edit(args))
    if args.cmd == "paint-preview":
        return _cmd_paint_preview(args)
    if args.cmd == "serve":
        from .app import serve

        return serve(_settings(args), host=args.host, port=args.port)
    if args.cmd == "trace-backfill":
        return _cmd_trace_backfill(args)
    if args.cmd == "smoke-supabase":
        from .smoke import main as smoke_main

        return smoke_main(_settings(args), keep=args.keep, timeout_s=args.timeout)
    return 2


if __name__ == "__main__":
    sys.exit(main())
