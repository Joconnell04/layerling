"""filaments/library.json: read it, pick the AMS palette, write measured values back.

Writing touches only `lab`, `lab_source` and `td_mm` of the measured filaments (plus the
file's `updated_at`). Everything else, including key order and number formatting, is kept.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from p2p_contracts import Filament, FilamentLibrary

REPO = Path(__file__).resolve().parents[3]
DEFAULT_LIBRARY = REPO / "filaments" / "library.json"
WRITABLE = ("lab", "lab_source", "td_mm")


def load(path: str | Path = DEFAULT_LIBRARY) -> FilamentLibrary:
    return FilamentLibrary.model_validate_json(Path(path).read_text())


def ams_palette(lib: FilamentLibrary) -> list[Filament]:
    """Filaments loaded in the AMS, in slot order (project slot = index + 1)."""
    loaded = sorted((f for f in lib.filaments if f.ams_slot is not None),
                    key=lambda f: f.ams_slot)
    slots = [f.ams_slot for f in loaded]
    if not loaded:
        raise ValueError("no filament in the library has an ams_slot")
    if len(set(slots)) != len(slots):
        raise ValueError(f"two filaments share an AMS slot: {slots}")
    return loaded


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def write_measured(path: str | Path, updates: dict[str, dict], now: datetime | None = None
                   ) -> dict[str, dict]:
    """updates: filament id -> {"lab": (L, a, b), "td_mm": float | None}. Sets lab_source to
    "measured"; a td_mm of None leaves the stored value alone. Returns id -> before/after of
    the written fields. Validates against FilamentLibrary before replacing the file."""
    path = Path(path)
    data = json.loads(path.read_text())
    by_id = {f["id"]: f for f in data["filaments"]}
    missing = sorted(set(updates) - set(by_id))
    if missing:
        raise KeyError(f"not in {path.name}: {missing}")
    changes: dict[str, dict] = {}
    for fid, u in updates.items():
        entry = by_id[fid]
        before = {k: entry.get(k) for k in WRITABLE}
        entry["lab"] = [round(float(x), 2) + 0.0 for x in u["lab"]]
        entry["lab_source"] = "measured"
        if u.get("td_mm") is not None:
            entry["td_mm"] = round(float(u["td_mm"]), 2)
        changes[fid] = {"before": before, "after": {k: entry.get(k) for k in WRITABLE}}
    stamp = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    data["updated_at"] = stamp
    text = _dump(data)
    FilamentLibrary.model_validate_json(text)
    fd, tmp = tempfile.mkstemp(prefix=".library.", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return changes
