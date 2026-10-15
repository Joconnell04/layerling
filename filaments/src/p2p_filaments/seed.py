"""Seed the Supabase `filaments` table with the catalog as shared rows.

Shared rows have owner_id null (readable by every logged-in user, migration 0008), owned false
and spools 0. The seed is idempotent and never changes an existing row:

1. Read every existing id, and snapshot the owned rows (owned true) in full.
2. Drop catalog entries whose id already exists (an owned row with that id covers it).
3. POST the rest with on_conflict=id and resolution=ignore-duplicates, so a row that appears
   between steps 1 and 3 is left alone too.
4. Read the owned rows again and fail loudly if any of them changed.

Credentials: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the environment, else the .env
file (P2P_ENV_FILE, else the repo root .env, else the main checkout's .env for a worktree).
Values are never printed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from p2p_contracts import Filament, FilamentLibrary

from .library import REPO

BATCH = 100
ROW_FIELDS = ("id", "owner_id", "brand", "line", "name", "material", "hex", "lab_l", "lab_a",
              "lab_b", "lab_source", "td_mm", "owned", "spools", "ams_slot", "swatch_path",
              "bambu_filament_id", "slicer_preset", "density_g_cm3", "notes")


class SeedError(RuntimeError):
    pass


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines (same rules as p2p_worker.config). Values are returned, never printed."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.split(" #", 1)[0].strip().strip('"').strip("'")
    return out


def env_files(root: Path = REPO) -> list[Path]:
    if os.environ.get("P2P_ENV_FILE"):
        return [Path(os.environ["P2P_ENV_FILE"])]
    files = [root / ".env"]
    for parent in root.parents:  # a worktree under .claude/worktrees/ shares the main .env
        if (parent / ".git").is_dir():
            files.append(parent / ".env")
            break
    return files


def credentials() -> tuple[str, str]:
    vals: dict[str, str] = {}
    for f in reversed(env_files()):
        vals.update(read_env_file(f))
    url = os.environ.get("SUPABASE_URL") or vals.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or vals.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise SeedError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are not set (env or .env)")
    return url.rstrip("/"), key


def client_for(url: str, key: str) -> httpx.Client:
    return httpx.Client(base_url=f"{url}/rest/v1", timeout=30.0,
                        headers={"apikey": key, "Authorization": f"Bearer {key}"})


def to_row(f: Filament) -> dict[str, Any]:
    """A shared catalog row: owner_id null, owned false, spools 0, never in the AMS."""
    return {"id": f.id, "owner_id": None, "brand": f.brand, "line": f.line, "name": f.name,
            "material": f.material, "hex": f.hex, "lab_l": f.lab[0], "lab_a": f.lab[1],
            "lab_b": f.lab[2], "lab_source": f.lab_source, "td_mm": f.td_mm, "owned": False,
            "spools": 0, "ams_slot": None, "swatch_path": f.swatch,
            "bambu_filament_id": f.bambu_filament_id, "slicer_preset": f.slicer_preset,
            "density_g_cm3": f.density_g_cm3, "notes": f.notes}


def _get(client: httpx.Client, params: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        r = client.get("/filaments", params={**params, "limit": "1000", "offset": str(offset)})
        if r.status_code >= 400:
            raise SeedError(f"GET filaments: HTTP {r.status_code}")
        page = r.json()
        out.extend(page)
        if len(page) < 1000:
            return out
        offset += 1000


def owned_snapshot(client: httpx.Client,
                   protect: frozenset[str] | set[str] = frozenset()) -> dict[str, dict]:
    """Full rows of every owned filament plus the protected ids (library.json's owned ids)."""
    rows = _get(client, {"select": "*", "owned": "is.true", "order": "id.asc"})
    if protect:
        rows += _get(client, {"select": "*", "id": f"in.({','.join(sorted(protect))})"})
    return {r["id"]: r for r in rows}


def seed(catalog: FilamentLibrary, client: httpx.Client, *,
         protect: frozenset[str] | set[str] = frozenset(), dry_run: bool = False) -> dict:
    """Insert catalog rows that are not in the table yet. `protect`: ids snapshotted and
    compared even if their row says owned false. Returns counts; raises SeedError if an owned
    or protected row changed or the insert failed."""
    before = owned_snapshot(client, protect)
    existing = {r["id"] for r in _get(client, {"select": "id"})}
    clash = sorted(f.id for f in catalog.filaments if f.id in before)
    rows = [to_row(f) for f in catalog.filaments if f.id not in existing]
    report: dict[str, Any] = {"catalog": len(catalog.filaments), "existing_rows": len(existing),
                              "owned_rows": len(before), "owned_id_clash": clash,
                              "already_present": len(catalog.filaments) - len(rows),
                              "to_insert": len(rows), "inserted": 0, "dry_run": dry_run}
    if not dry_run:
        for i in range(0, len(rows), BATCH):
            r = client.post("/filaments", params={"on_conflict": "id"}, json=rows[i:i + BATCH],
                            headers={"Prefer": "resolution=ignore-duplicates,return=minimal"})
            if r.status_code >= 400:
                raise SeedError(f"POST filaments: HTTP {r.status_code}: {r.text[:300]}")
            report["inserted"] += len(rows[i:i + BATCH])
    after = owned_snapshot(client, protect)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    report["owned_unchanged"] = not changed
    if changed:
        raise SeedError(f"owned rows changed during the seed: {changed}")
    ids = {f.id for f in catalog.filaments}
    present = {r["id"] for r in _get(client, {"select": "id", "owner_id": "is.null"})} & ids
    report["catalog_rows_in_table"] = len(present)
    return report
