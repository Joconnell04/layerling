"""Supabase: PostgREST rows, Storage files, Realtime notifications, behind the JobStore interface.

SupabaseStore extends LocalStore. The job folder under jobs/<job_id>/ stays the working copy
(stages read and write files there); rows go to PostgREST and files to Storage:
- jobs, stage_runs (upsert per attempt), messages (source of truth for the conversation),
  spend, meshy_events (written by the meshy-webhook edge function), workers (heartbeat).
- Storage: inputs/<job_id>/photo_<n>.<ext> (user uploads), artifacts/<job_id>/<path> (outputs).
The worker uses the service role key, which bypasses RLS. The key is never logged or printed.

RealtimeListener subscribes to postgres_changes over the Realtime websocket (Phoenix protocol)
and calls back on inserts and updates, so the worker never polls.

Every write is safe to repeat, and repeated when the connection drops (live2: one dropped
keep-alive failed a stage that was waiting on a paid 3D model):
- PATCH, upserts (stage_runs, workers, Storage with x-upsert) and reads retry on a transport
  error or a 429/502/503/504, RETRIES times with BACKOFF_S between.
- messages: the worker picks the row id (store.next_message_id) and inserts with
  `resolution=ignore-duplicates`, so a repeat is a no-op and the row is read back.
- spend and meshy_events have no id the worker can pick: after a failed insert the worker looks
  for the row it sent (spend by job, time, vendor and kind; a meshy_events row by the `_ref` it
  put in the event) and inserts again only when it is not there.
- consumed_at and processed_at are only set where still null, so a repeat keeps the first time.
After a Realtime reconnect the worker reconciles from the tables (store.pending_work, app.py),
so an insert that happened while the socket was down is not missed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from p2p_contracts import JobRecord, Manifest, SpendEvent
from p2p_contracts.job import Photo, storage_key
from pydantic import BaseModel

from ..errors import is_transient
from ..store import LocalStore, MeshyEventRow, MessageRow, next_message_id, validate_message
from ..util import append_jsonl, mime_for, read_jsonl, sha256_file, utcnow, write_json

log = logging.getLogger("p2p_worker.supabase")

JOB_COLUMNS = {"status", "stage", "spent_usd", "spent_credits", "pause_reason", "result",
               "confidence", "review_reason", "target_size_mm", "addons", "photos", "settings",
               "title", "budget_usd", "budget_credits"}
# Columns added by migration 20260924000011_product_form.sql. Written in their own PATCH so a
# project without the migration keeps working (the first refusal turns them off for the run).
OPTIONAL_JOB_COLUMNS = {"form", "custom_asks"}


class SupabaseError(RuntimeError):
    pass


# live2 run: one GET /rest/v1/meshy_events hit RemoteProtocolError mid-wait and failed the s05
# attempt; the retry attempt then tried to pay for a second 3D model. Requests that are safe to
# repeat are retried here (ASSUMED: about 8 s in all; the client is synchronous, so a longer
# outage fails the stage as transient and the orchestrator replays it after a backoff).
RETRIES = 3
BACKOFF_S = (0.5, 2.0, 5.0)
IDEMPOTENT = {"GET", "HEAD", "PATCH", "PUT", "DELETE"}
RETRY_STATUS = {429, 502, 503, 504}
SPEND_PAGE = 1000  # rows per spend table read (build_totals pages through the table)


def tier_size(row: dict[str, Any]) -> tuple[float | None, str | None]:
    """(target_size_mm, size_tier) from a jobs row: a tier the form picked gives the size when
    the row has none (contracts SIZE_TIERS_MM)."""
    from p2p_contracts.job import SIZE_TIERS_MM, ProductType, SizeTier

    tier = row.get("size_tier")
    size = row.get("target_size_mm")
    if tier and not size:
        try:
            size = SIZE_TIERS_MM[ProductType(row.get("product_type", "figurine"))][SizeTier(tier)]
        except (KeyError, ValueError):
            size = None
    return (float(size) if size else None), tier


class SupabaseStore(LocalStore):
    def __init__(self, settings, transport: httpx.BaseTransport | None = None):
        super().__init__(settings.jobs_dir)
        if not (settings.supabase_url and settings.supabase_service_key):
            raise SupabaseError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
        self.url = settings.supabase_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.url,
            headers={"apikey": settings.supabase_service_key,
                     "Authorization": f"Bearer {settings.supabase_service_key}"},
            transport=transport, timeout=30.0,
        )
        self._owner: dict[str, str] = {}
        self._optional_columns = True

    def __repr__(self) -> str:
        return f"SupabaseStore(url={self.url!r})"

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ http
    def _req(self, method: str, path: str, *, params: dict | None = None, json_body: Any = None,
             headers: dict | None = None, content: bytes | None = None,
             idempotent: bool | None = None) -> httpx.Response:
        """One request; repeated on a dropped connection or a 429/502/503/504 when it is safe to
        repeat (idempotent: reads, PATCH, upserts; a plain insert is not, see _insert_once)."""
        retry = idempotent if idempotent is not None else method in IDEMPOTENT
        tries = RETRIES + 1 if retry else 1
        backoff = BACKOFF_S
        for i in range(tries):
            last = i + 1 >= tries
            try:
                r = self._http.request(method, path, params=params, json=json_body,
                                       headers=headers, content=content)
            except httpx.TransportError as e:
                if not last:  # a dropped keep-alive is common, after a sleep most of all
                    log.warning("Supabase %s %s: %s, retrying", method, path.split("?")[0],
                                type(e).__name__)
                    time.sleep(backoff[min(i, len(backoff) - 1)])
                    continue
                raise SupabaseError(
                    f"Supabase {method} {path.split('?')[0]}: {type(e).__name__}") from e
            if r.status_code in RETRY_STATUS and not last:
                log.warning("Supabase %s %s returned %s, retrying", method, path.split("?")[0],
                            r.status_code)
                time.sleep(backoff[min(i, len(backoff) - 1)])
                continue
            break
        if r.status_code >= 400:
            raise SupabaseError(f"Supabase {method} {path.split('?')[0]} returned "
                                f"{r.status_code}: {r.text[:300]}")
        return r

    def _rest(self, method: str, table: str, *, params: dict | None = None, body: Any = None,
              prefer: str | None = None, idempotent: bool | None = None) -> Any:
        headers = {"Prefer": prefer} if prefer else None
        r = self._req(method, f"/rest/v1/{table}", params=params, json_body=body, headers=headers,
                      idempotent=idempotent)
        return r.json() if r.content else None

    def _insert_once(self, table: str, body: dict[str, Any], match: dict[str, str],
                     prefer: str) -> Any:
        """Insert a row whose table gives the worker no id to pick. After a failure that may
        have landed (a dropped connection, a 5xx), look for the row by `match` and insert again
        only when it is not there."""
        backoff = BACKOFF_S
        for i in range(RETRIES + 1):
            try:
                return self._rest("POST", table, body=body, prefer=prefer, idempotent=False)
            except SupabaseError as e:
                if i >= RETRIES or not is_transient(e):
                    raise
                log.warning("Supabase insert into %s may not have landed (%s); checking",
                            table, str(e)[:120])
                time.sleep(backoff[min(i, len(backoff) - 1)])
                found = self._rest("GET", table, params={**match, "select": "*", "limit": "1"})
                if found:
                    return found
        return None

    # ------------------------------------------------------------------ filaments
    def filament_rows(self, job_id: str) -> list[dict[str, Any]] | None:
        """The owner's spools plus the shared ones (owner_id null, the studio's AMS); an owner
        row wins over a shared row with the same id."""
        owner = self.owner_id(job_id)
        order = {"select": "*", "order": "ams_slot.asc.nullslast,id.asc"}
        rows: dict[str, dict[str, Any]] = {}
        if owner:
            for r in self._rest("GET", "filaments", params={**order,
                                                            "owner_id": f"eq.{owner}"}) or []:
                rows[r["id"]] = r
        for r in self._rest("GET", "filaments", params={**order, "owner_id": "is.null"}) or []:
            rows.setdefault(r["id"], r)
        return list(rows.values()) or None

    # ------------------------------------------------------------------ jobs
    def owner_id(self, job_id: str) -> str | None:
        if job_id not in self._owner:
            rows = self._rest("GET", "jobs", params={"id": f"eq.{job_id}", "select": "owner_id"})
            if rows:
                self._owner[job_id] = rows[0]["owner_id"]
        return self._owner.get(job_id)

    def ingest_job(self, row: dict[str, Any]) -> JobRecord:
        """Build the local job folder for a jobs row the web app created."""
        job_id = row["id"]
        self._owner[job_id] = row.get("owner_id")
        jdir = self.job_dir(job_id)
        (jdir / "input").mkdir(parents=True, exist_ok=True)
        photos = []
        for i, ph in enumerate(row.get("photos") or [], start=1):
            key = str(ph.get("path") or f"photo_{i}.jpg")
            name = key.split("/")[-1]
            local = jdir / "input" / name
            if not local.is_file():
                local.write_bytes(self._download_input(job_id, key))
            photos.append(Photo(path=f"input/{name}", sha256=sha256_file(local),
                                subject_ids=ph.get("subject_ids") or []))
        size, tier = tier_size(row)
        write_json(jdir / "input" / "request.json", {  # what the new-job form set
            "prompt": row["prompt"], "product_type": row.get("product_type", "figurine"),
            "target_size_mm": size, "size_tier": tier, "addons": row.get("addons") or [],
            "form": row.get("form"),
        })
        data = {
            "id": job_id, "created_at": row.get("created_at") or utcnow().isoformat(),
            "prompt": row["prompt"], "product_type": row.get("product_type", "figurine"),
            "target_size_mm": size, "size_tier": tier, "addons": row.get("addons") or [],
            "photos": [p.model_dump(mode="json") for p in photos],
            "settings": row.get("settings") or {},
            "budget": {"cap_usd": float(row.get("budget_usd") or 10),
                       "cap_credits": int(row.get("budget_credits") or 400),
                       "spent_usd": float(row.get("spent_usd") or 0),
                       "spent_credits": int(row.get("spent_credits") or 0)},
            "status": row.get("status", "queued"), "profile": row.get("profile", "thin_slice"),
            "access": row.get("access") or "owner", "pause_reason": row.get("pause_reason"),
            "result": row.get("result"),
        }
        job = JobRecord.model_validate(data)
        super().save_job(job)
        if not (jdir / "ledger.jsonl").is_file() and (data["budget"]["spent_usd"]
                                                        or data["budget"]["spent_credits"]):
            # spend booked before this machine had the job: the base the ledger adds to
            LocalStore.update_job_row(self, job_id, spend_baseline={
                "usd": data["budget"]["spent_usd"], "credits": data["budget"]["spent_credits"]})
        return job

    def _download_input(self, job_id: str, path: str) -> bytes:
        """Contract rule: job-relative input/photo_1.jpg is inputs/<job_id>/input/photo_1.jpg.
        The older key without input/ is still read, for jobs created before the fix."""
        rel = path[len(job_id) + 1:] if path.startswith(f"{job_id}/") else path
        name = rel.split("/")[-1]
        tried = []
        for key in dict.fromkeys([f"{job_id}/{rel}", f"{job_id}/input/{name}", f"{job_id}/{name}"]):
            try:
                return self._req("GET", f"/storage/v1/object/inputs/{quote(key)}").content
            except SupabaseError as e:
                tried.append(str(e)[:80])
        raise SupabaseError(f"photo {path} not found in bucket inputs: {tried}")

    def fetch_input(self, job_id: str, rel_path: str) -> Path:
        """A file the web uploaded to bucket inputs after the job started (input/paint/...)."""
        p = self.job_dir(job_id) / rel_path
        if p.is_file():
            return p
        try:
            data = self._req("GET", f"/storage/v1/object/inputs/"
                                    f"{quote(storage_key(job_id, rel_path))}").content
        except SupabaseError as e:
            raise FileNotFoundError(rel_path) from e
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def load_job(self, job_id: str) -> JobRecord:
        try:
            return super().load_job(job_id)
        except KeyError:
            rows = self._rest("GET", "jobs", params={"id": f"eq.{job_id}", "select": "*"})
            if not rows:
                raise
            return self.ingest_job(rows[0])

    def save_job(self, job: JobRecord) -> None:
        super().save_job(job)
        self._rest("PATCH", "jobs", params={"id": f"eq.{job.id}"}, body={
            "status": job.status.value, "stage": job.stage.value if job.stage else None,
            "spent_usd": job.budget.spent_usd, "spent_credits": job.budget.spent_credits,
            "target_size_mm": job.target_size_mm,
            "addons": [a.model_dump(mode="json") for a in job.addons],
            "photos": [p.model_dump(mode="json") for p in job.photos],
            "settings": job.settings.model_dump(mode="json"),
            "pause_reason": job.pause_reason, "result": job.result,
        })

    def update_job_row(self, job_id: str, **fields: Any) -> None:
        super().update_job_row(job_id, **fields)
        dump = {k: (v.model_dump(mode="json") if isinstance(v, BaseModel) else v)
                for k, v in fields.items()}
        body = {k: v for k, v in dump.items() if k in JOB_COLUMNS}
        if body:
            self._rest("PATCH", "jobs", params={"id": f"eq.{job_id}"}, body=body)
        extra = {k: v for k, v in dump.items() if k in OPTIONAL_JOB_COLUMNS}
        if extra and self._optional_columns:
            try:
                self._rest("PATCH", "jobs", params={"id": f"eq.{job_id}"}, body=extra)
            except SupabaseError as e:
                self._optional_columns = False
                log.warning("jobs.form and custom_asks not written (apply migration "
                            "20260924000011): %s", str(e)[:200])

    def list_jobs(self) -> list[str]:
        rows = self._rest("GET", "jobs", params={
            "select": "id", "status": "in.(queued,running,waiting_user)", "order": "created_at"})
        return [r["id"] for r in rows or []]

    # ------------------------------------------------------------------ stage runs, files
    def write_manifest(self, manifest: Manifest) -> None:
        super().write_manifest(manifest)
        self._rest("POST", "stage_runs", params={"on_conflict": "job_id,stage,attempt"},
                   prefer="resolution=merge-duplicates,return=minimal", idempotent=True, body={
                       "job_id": manifest.job_id, "owner_id": self.owner_id(manifest.job_id),
                       "stage": manifest.stage.value, "attempt": manifest.attempt,
                       "status": manifest.status.value,
                       "started_at": manifest.started_at.isoformat() if manifest.started_at
                       else None,
                       "finished_at": manifest.finished_at.isoformat() if manifest.finished_at
                       else None,
                       "usd": manifest.usd, "credits": manifest.credits,
                       "confidence": manifest.confidence,
                       "manifest": manifest.model_dump(mode="json"),
                   })

    def _bucket_key(self, job_id: str, rel_path: str) -> tuple[str, str]:
        """Storage key = <job_id>/<job-relative path> (contracts job.storage_key)."""
        bucket = "inputs" if rel_path.startswith("input/") else "artifacts"
        return bucket, storage_key(job_id, rel_path)

    def upload_artifacts(self, job_id: str, rel_paths: list[str]) -> None:
        for rel in rel_paths:
            p = self.job_dir(job_id) / rel
            bucket, key = self._bucket_key(job_id, rel)
            if bucket != "artifacts" or not p.is_file():
                continue
            self._req("POST", f"/storage/v1/object/{bucket}/{quote(key)}", content=p.read_bytes(),
                      headers={"x-upsert": "true", "Content-Type": mime_for(p)},
                      idempotent=True)

    def signed_url(self, job_id: str, rel_path: str, expires_s: int = 3600) -> str | None:
        bucket, key = self._bucket_key(job_id, rel_path)
        try:
            r = self._req("POST", f"/storage/v1/object/sign/{bucket}/{quote(key)}",
                          json_body={"expiresIn": expires_s}, idempotent=True)
        except SupabaseError:
            return None
        signed = r.json().get("signedURL") or r.json().get("signedUrl")
        if not signed:
            return None
        return signed if signed.startswith("http") else f"{self.url}/storage/v1{signed}"

    # ------------------------------------------------------------------ messages
    def post_message(self, job_id, role, kind, payload=None, *, stage=None, body=None,
                     reply_to=None, message_id=None):
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        payload = validate_message(role, kind, payload or {})
        mid = message_id or next_message_id()
        # The worker picks the id, so the insert can be repeated: a duplicate is ignored and the
        # existing row read back (a retry after a dropped connection, or a replay after a crash).
        rows = self._rest("POST", "messages", params={"on_conflict": "id"},
                          prefer="resolution=ignore-duplicates,return=representation",
                          idempotent=True, body={
            "id": mid, "job_id": job_id, "owner_id": self.owner_id(job_id), "role": role,
            "kind": kind, "stage": stage.value if stage else None, "body": body,
            "payload": payload, "reply_to": reply_to,
        })
        if not rows:
            rows = self._rest("GET", "messages", params={"id": f"eq.{mid}", "select": "*"})
        row = MessageRow.model_validate(rows[0])
        local = self.job_dir(job_id) / "messages.jsonl"
        if not any(d.get("id") == row.id for d in read_jsonl(local)):
            append_jsonl(local, row.model_dump(mode="json"))
        self._msg_notifier.notify()
        return row

    def list_messages(self, job_id, *, role=None, kinds=None, unconsumed=False):
        params = {"job_id": f"eq.{job_id}", "order": "created_at", "select": "*"}
        if role:
            params["role"] = f"eq.{role}"
        if kinds:
            params["kind"] = f"in.({','.join(sorted(kinds))})"
        if unconsumed:
            params["consumed_at"] = "is.null"
        return [MessageRow.model_validate(r) for r in self._rest("GET", "messages", params=params)]

    def consume_message(self, job_id: str, message_id: str) -> None:
        self._rest("PATCH", "messages", params={"id": f"eq.{message_id}",
                                                "consumed_at": "is.null"},
                   body={"consumed_at": utcnow().isoformat()})

    def notify_message(self) -> None:
        self._msg_notifier.notify()

    # ------------------------------------------------------------------ spend
    def record_spend(self, event: SpendEvent, *, usd_list: float = 0.0,
                     billing: str = "api") -> None:
        super().record_spend(event, usd_list=usd_list, billing=billing)
        body = event.model_dump(mode="json") | {"usd_list": usd_list, "billing": billing}
        body["owner_id"] = self.owner_id(event.job_id) if event.job_id else None
        body["stage"] = event.stage.value if event.stage else None
        match = {"at": f"eq.{body['at']}", "vendor": f"eq.{event.vendor}",
                 "kind": f"eq.{event.kind}"}
        if event.job_id:
            match["job_id"] = f"eq.{event.job_id}"
        self._insert_once("spend", body, match, "return=minimal")

    def build_totals(self) -> dict[str, float]:
        """Live spend from the spend table, read page by page (PostgREST returns at most its
        max-rows per request, 1,000 on Supabase by default, ASSUMED unchanged here; the table
        had 103 rows after live run 2, SOURCE). A table that cannot be read raises
        BudgetExceeded: the build caps are never checked against the local file alone, which
        misses spend made elsewhere (live2: 463 local against 491 in the table)."""
        from ..errors import BudgetExceeded

        rows: list[dict[str, Any]] = []
        try:
            while True:
                page = self._rest("GET", "spend", params={
                    "select": "id,vendor,usd,credits", "test_mode": "eq.false",
                    "order": "id", "limit": str(SPEND_PAGE), "offset": str(len(rows))}) or []
                rows += page
                if len(page) < SPEND_PAGE:
                    break
        except SupabaseError as e:
            raise BudgetExceeded(f"Spend table unreadable, build caps not checked: {e}"[:300],
                                 {"cap": "spend_table_unreadable"}) from e
        usd = sum(float(r.get("usd") or 0) for r in rows)
        credits = sum(int(r.get("credits") or 0) for r in rows if r.get("vendor") == "meshy")
        return {"usd": round(usd, 6), "meshy_credits": credits}

    # ------------------------------------------------------------------ meshy events
    def insert_meshy_event(self, event: dict[str, Any], *, source: str = "webhook") -> int:
        task_id = str(event.get("id") or event.get("task_id") or "")
        if not task_id:
            raise ValueError("Meshy event has no task id")
        ref = str(event.get("_ref") or next_message_id())
        rows = self._insert_once("meshy_events", {
            "task_id": task_id, "status": event.get("status"),
            "event": {**event, "_source": source, "_ref": ref},
        }, {"task_id": f"eq.{task_id}", "event->>_ref": f"eq.{ref}"},
            "return=representation")
        self._meshy_notifier.notify()
        return int(rows[0]["id"])

    def meshy_events(self, task_id: str) -> list[MeshyEventRow]:
        rows = self._rest("GET", "meshy_events", params={
            "task_id": f"eq.{task_id}", "order": "id", "select": "*"})
        return [MeshyEventRow.model_validate(r) for r in rows or []]

    def mark_meshy_processed(self, event_ids: list[int]) -> None:
        if event_ids:
            self._rest("PATCH", "meshy_events",
                       params={"id": f"in.({','.join(str(i) for i in event_ids)})",
                               "processed_at": "is.null"},
                       body={"processed_at": utcnow().isoformat()})

    def notify_meshy(self) -> None:
        self._meshy_notifier.notify()

    def job_for_task(self, task_id: str) -> str | None:
        found = super().job_for_task(task_id)
        if found:
            return found
        rows = self._rest("GET", "stage_runs", params={
            "select": "job_id", "manifest->meshy_tasks": f'cs.[{{"task_id":"{task_id}"}}]',
            "limit": "1"})
        return rows[0]["job_id"] if rows else None

    def pending_work(self) -> set[str]:
        """Jobs with an unconsumed user message or an unprocessed Meshy event, from the tables
        (what a Realtime gap or a restart could have hidden)."""
        out = {r["job_id"] for r in self._rest("GET", "messages", params={
            "select": "job_id", "role": "eq.user", "consumed_at": "is.null"}) or []}
        since = (utcnow() - timedelta(days=3)).isoformat()  # Meshy assets live 3 days
        for r in self._rest("GET", "meshy_events", params={
                "select": "task_id", "processed_at": "is.null",
                "received_at": f"gte.{since}"}) or []:
            job = self.job_for_task(str(r.get("task_id")))
            if job:
                out.add(job)
        return out

    # ------------------------------------------------------------------ workers
    def heartbeat(self, worker_id: str, *, version: str, meshy_mode: str, info: dict) -> None:
        super().heartbeat(worker_id, version=version, meshy_mode=meshy_mode, info=info)
        try:
            self._rest("POST", "workers", params={"on_conflict": "id"}, idempotent=True,
                       prefer="resolution=merge-duplicates,return=minimal",
                       body={"id": worker_id, "last_seen": utcnow().isoformat(),
                             "version": version, "meshy_mode": meshy_mode, "info": info})
        except SupabaseError as e:  # a missed heartbeat must not stop a job
            log.warning("heartbeat failed: %s", e)


Change = Callable[[str, str, dict[str, Any]], None]


def parse_realtime(msg: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    """(table, type, record) from a Realtime postgres_changes message, else None."""
    if msg.get("event") != "postgres_changes":
        return None
    data = (msg.get("payload") or {}).get("data") or {}
    table, typ = data.get("table"), data.get("type")
    if not table or not typ:
        return None
    return table, typ, data.get("record") or {}


class RealtimeListener:
    """Supabase Realtime (Phoenix channels over websockets) for postgres_changes.

    Reconnects with backoff and joins again on every connection. `on_connect` runs after each
    successful join (the first one too): serve reconciles from the tables there, so a row
    inserted while the socket was down is still acted on. `reconnect()` drops the socket now
    (serve calls it on wake from sleep, when the old socket is dead but not yet noticed).
    websockets' own pings (20 s) close a socket whose server stopped answering."""

    def __init__(self, url: str, key: str, tables: list[str], on_change: Change,
                 heartbeat_s: float = 25.0, on_connect: Callable[[], None] | None = None):
        host = url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
        self._ws_url = f"{host}/realtime/v1/websocket?apikey={key}&vsn=1.0.0"
        self._key = key
        self.tables = tables
        self.on_change = on_change
        self.on_connect = on_connect
        self.heartbeat_s = heartbeat_s
        self._ref = 0
        self._join_ref: str | None = None
        self._ws = None
        self.connected = asyncio.Event()
        self.joins = 0
        self.drops = 0
        self.last_message = 0.0  # time.monotonic() of the last frame

    def __repr__(self) -> str:
        return f"RealtimeListener(tables={self.tables})"

    def _next_ref(self) -> str:
        self._ref += 1
        return str(self._ref)

    def join_message(self) -> dict[str, Any]:
        self._join_ref = self._next_ref()
        return {
            "topic": "realtime:p2p-worker", "event": "phx_join", "ref": self._join_ref,
            "payload": {"config": {"broadcast": {"self": False}, "presence": {"key": ""},
                                   "postgres_changes": [
                                       {"event": "*", "schema": "public", "table": t}
                                       for t in self.tables]},
                        "access_token": self._key},
        }

    def status(self) -> dict[str, Any]:
        age = time.monotonic() - self.last_message if self.last_message else None
        return {"connected": self.connected.is_set(), "joins": self.joins, "drops": self.drops,
                "last_message_s": round(age, 1) if age is not None else None}

    def handle(self, raw: str) -> None:
        self.last_message = time.monotonic()
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if msg.get("event") == "phx_reply" and msg.get("topic") == "realtime:p2p-worker":
            ok = ((msg.get("payload") or {}).get("status") or "ok") == "ok"
            first = not self.connected.is_set()
            if ok:
                self.connected.set()
            if ok and first and (msg.get("ref") in (None, self._join_ref)):
                self.joins += 1
                if self.on_connect is not None:
                    try:
                        self.on_connect()
                    except Exception:
                        log.exception("realtime reconcile failed")
        parsed = parse_realtime(msg)
        if parsed:
            try:
                self.on_change(*parsed)
            except Exception:
                log.exception("realtime callback failed")

    def reconnect(self) -> None:
        """Drop the socket now; run() connects and joins again."""
        ws = self._ws
        if ws is not None:
            asyncio.ensure_future(ws.close())

    async def run(self) -> None:
        import websockets

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self._ws_url, open_timeout=20) as ws:
                    self._ws = ws
                    self.connected.clear()
                    await ws.send(json.dumps(self.join_message()))
                    backoff = 1.0

                    async def beat():
                        while True:
                            await asyncio.sleep(self.heartbeat_s)
                            await ws.send(json.dumps({"topic": "phoenix", "event": "heartbeat",
                                                      "payload": {}, "ref": self._next_ref()}))

                    hb = asyncio.create_task(beat())
                    try:
                        async for raw in ws:
                            self.handle(raw if isinstance(raw, str) else raw.decode())
                    finally:
                        hb.cancel()
                raise ConnectionError("socket closed")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 reconnect on anything; never log the URL (key)
                self._ws = None
                self.connected.clear()
                self.drops += 1
                log.warning("realtime disconnected (%s); reconnecting in %.0fs",
                            type(e).__name__, backoff,
                            extra={"event": "realtime_drop", "fields": {"drops": self.drops}})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
