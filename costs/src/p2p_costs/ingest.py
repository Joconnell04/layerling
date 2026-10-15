"""Collect test outputs into Test data rows: one row per run.

Sources (all optional; anything missing is skipped):
- Slice reports (p2p_contracts.SliceReport JSON): data/slice_reports/, threemf/_out/,
  geometry/_out/, and jobs/<id>/**/ in the main checkout, this checkout and (by default) every
  worktree under .claude/worktrees/.
- Meshy spend: data/ledger.jsonl (real spend) and jobs/<id>/ledger.jsonl (worker runs, including
  simulated runs on the fake Meshy server).
- Job records: jobs/<id>/job.json gives the product of a job.

Every row ingested is kept in costs/test_data.jsonl, so a run stays in the model after its
source folder is cleaned up. A newer copy of the same run replaces the older one.

Each row gets a config (what it was sliced or run with) and an automatic Use: 1 when that config
is what production does now (production.py), else 0. Jackson's Use column in the workbook
overrides it; ingest never touches that column.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from p2p_contracts import SliceReport

from .defaults import M_TIER, PRODUCTS, SIZE_TIERS_MM, TIERS
from .production import embedded_config, lab_config, production

# Slice files that should not count toward a product by default, with the reason shown in Notes.
DEFAULT_EXCLUDE = {
    "prod_figurine": "40 mm capsule demo; the figurine product is 90 mm",
    "demo_4color": "demo shape, not a product",
    "demo_5color_petg": "demo shape, not a product",
}
# Purge-lab fixtures (<fixture>__<lever>__<variant>.json) and the product each stands for. The
# beagles are real Meshy models scaled to each product size; the others are procedural shapes.
FIXTURE_PRODUCT = {
    "beagle45": "keychain", "beagle75": "large_figure", "beagle90": "figurine",
    "keychain": "keychain", "nameplate": "nameplate", "figurine": "figurine",
}
PROCEDURAL = {"keychain", "nameplate", "figurine", "prod_keychain", "prod_nameplate",
              "prod_figurine", "prod_keychain_too_thin"}
LEDGER_GAP_S = 30 * 60


@dataclass
class RunRow:
    run_key: str
    product: str
    kind: str  # "slice", "meshy spend", "job"
    auto: int = 1  # counts automatically: 1 when the config matches production
    config: str = ""
    credits: float | None = None
    meshy_usd: float | None = None
    pla_bambu_g: float | None = None
    pla_generic_g: float | None = None
    petg_g: float | None = None
    other_g: float | None = None
    total_g: float | None = None
    purge_g: float | None = None
    print_min: float | None = None
    slots: int | None = None
    ok: bool | None = None
    filaments: str = ""
    source: str = ""
    notes: str = ""
    run_at: str = ""
    tier: str = M_TIER  # size tier the run counts toward: xs, s, m, l
    size_mm: float | None = None  # size the run was actually made at (longest dimension)
    exact: bool = True  # made at its tier's size; False = scaled to it from a nearby size
    mtime: float = field(default=0.0, repr=False)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> RunRow:
        known = set(cls.__dataclass_fields__)
        d = dict(d)
        if "auto" not in d and "use" in d:  # archives written before the config rule
            d["auto"] = d["use"]
        if not d.get("config"):
            d["config"] = "not recorded (archived before configs; source no longer scanned)"
        if not d.get("tier"):
            d["tier"] = M_TIER
        return cls(**{k: v for k, v in d.items() if k in known})


# ------------------------------------------------------------------ where to look


def main_checkout(start: Path) -> Path:
    """The main checkout's root, also when running inside a git worktree."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=start, capture_output=True, text=True, check=True,
        ).stdout.strip()
        return Path(out).parent
    except (OSError, subprocess.CalledProcessError):
        return start


def scan_roots(repo_root: Path, include_worktrees: bool = True) -> list[Path]:
    roots = [main_checkout(repo_root), repo_root]
    if include_worktrees:
        wt = roots[0] / ".claude" / "worktrees"
        if wt.is_dir():
            roots += sorted(p for p in wt.iterdir() if p.is_dir())
    seen: list[Path] = []
    for r in roots:
        r = r.resolve()
        if r not in seen:
            seen.append(r)
    return seen


def _rel(path: Path, roots: list[Path]) -> str:
    for r in sorted(roots, key=lambda p: -len(str(p))):
        try:
            return str(path.resolve().relative_to(r))
        except ValueError:
            continue
    return str(path)


# ------------------------------------------------------------------ slice reports


def filament_bucket(filament_id: str | None) -> str:
    f = (filament_id or "").lower()
    if "petg" in f:
        return "petg_g"
    if "pla" in f:
        return "pla_bambu_g" if f.startswith("bambu") else "pla_generic_g"
    return "other_g"


def product_from_name(stem: str) -> str:
    s = stem.lower()
    fixture = s.split("__", 1)[0]
    if fixture in FIXTURE_PRODUCT:
        return FIXTURE_PRODUCT[fixture]
    if "meshy" in s or "beagle" in s:
        return "large_figure"
    for p in PRODUCTS:
        if p in s:
            return p
    return "demo" if s.startswith("demo") else "unknown"


def _is_procedural(stem: str) -> bool:
    return stem.lower().split("__", 1)[0] in PROCEDURAL


def parse_slice_report(text: str) -> SliceReport:
    """SliceReport, ignoring fields newer writers add (readback_ok, thin_paint, config, ...)."""
    return parse_slice_file(text)[0]


def parse_slice_file(text: str) -> tuple[SliceReport, dict | None]:
    """(SliceReport, the embedded "config" dict if the writer recorded one)."""
    d = json.loads(text)
    if not isinstance(d, dict) or "slicer" not in d:
        raise ValueError("not a slice report")
    rep = SliceReport.model_validate({k: v for k, v in d.items() if k in SliceReport.model_fields})
    cfg = d.get("config")
    return rep, cfg if isinstance(cfg, dict) else None


# Which slices a product is priced on, best first: a geometry production slice (the real
# pipeline end to end), then a purge-lab slice of the real Meshy beagle, then a procedural shape.
# Only the best tier that has a run matching production counts automatically.
TIER_NAMES = {3: "geometry production slice", 2: "purge-lab Meshy beagle", 1: "procedural shape"}


# A slice made within 2% (or 1 mm) of a tier's size (contracts SIZE_TIERS_MM) is at that tier. One
# made within 15% counts toward the nearest tier, scaled to its size with the size exponents in
# Inputs (ASSUMED). Anything further off does not count automatically.
EXACT_MM, EXACT_SHARE, NEAR_SHARE = 1.0, 0.02, 0.15
DEFAULT_EXPONENTS = {"model": 3.0, "purge": 0.91, "time": 1.09}


def tier_label(stem: str = "", cfg: dict | None = None, product: str = "") -> str | None:
    """The tier a writer put on the run (config size_tier, or geometry_<product>_<tier>__...)."""
    cfg = cfg or {}
    for key in ("size_tier", "tier"):
        v = str(cfg.get(key) or "").lower()
        if v in TIERS:
            return v
    head = stem.lower().split("__", 1)[0]
    last = head.rsplit("_", 1)[-1] if "_" in head else ""
    if last in TIERS and head.removesuffix("_" + last).endswith(product):
        return last
    return None


def measured_size(stem: str = "", cfg: dict | None = None) -> float | None:
    """Longest dimension the run was made at: config target_size_mm, else the largest of
    config size_mm, else a lab fixture name (beagle45 = 45 mm)."""
    cfg = cfg or {}
    v = cfg.get("target_size_mm")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    dims = cfg.get("size_mm")
    if isinstance(dims, list) and dims and all(isinstance(x, (int, float)) for x in dims):
        return float(max(dims))
    m = re.match(r"beagle(\d+)", stem.lower())
    return float(m.group(1)) if m else None


def size_tier_of(product: str, stem: str = "", cfg: dict | None = None,
                 size_mm: float | None = None) -> str:
    """The tier a run counts toward: the nearest SIZE_TIERS_MM size to the size it was made at;
    the writer's label when the size is unknown; M when neither is known."""
    size = size_mm if size_mm is not None else measured_size(stem, cfg)
    tiers = SIZE_TIERS_MM.get(product, {})
    if size is not None and tiers:
        return min(tiers, key=lambda t: abs(tiers[t] - size))
    return tier_label(stem, cfg, product) or M_TIER


def _scale_run(row: RunRow, r: float, exps: dict[str, float]) -> None:
    total, purge = row.total_g, row.purge_g
    if total is not None:
        pg = purge or 0.0
        new_purge = pg * r ** exps["purge"]
        new_total = (total - pg) * r ** exps["model"] + new_purge
        f = new_total / total if total else 0.0
        for k in ("pla_bambu_g", "pla_generic_g", "petg_g", "other_g"):
            v = getattr(row, k)
            if v is not None:
                setattr(row, k, round(v * f, 3))
        row.total_g = round(new_total, 3)
        if purge is not None:
            row.purge_g = round(new_purge, 3)
    if row.print_min is not None:
        row.print_min = round(row.print_min * r ** exps["time"], 2)


def place_on_tier(row: RunRow, stem: str = "", cfg: dict | None = None,
                  size_mm: float | None = None, exps: dict[str, float] | None = None) -> None:
    """Set the run's tier from its measured size (contracts tiers, not the writer's label);
    scale a near-size slice to its tier, and stop a far-off one counting."""
    exps = exps or DEFAULT_EXPONENTS
    product = row.product
    label = tier_label(stem, cfg, product)
    size = size_mm if size_mm is not None else measured_size(stem, cfg)
    tiers = SIZE_TIERS_MM.get(product, {})
    row.size_mm = size
    if size is None or not tiers:
        row.tier, row.exact = label or M_TIER, True
        return
    t = min(tiers, key=lambda k: abs(tiers[k] - size))
    mm = tiers[t]
    row.tier = t
    gap = abs(mm - size)
    was = f", labelled {label.upper()}" if label and label != t else ""
    sizes = "/".join(f"{tiers[k]:g}" for k in TIERS if k in tiers)
    if gap <= max(EXACT_MM, EXACT_SHARE * mm):
        row.exact = True
        if was:
            row.config += f"; {size:g} mm is {t.upper()} now{was}"
    elif gap / mm <= NEAR_SHARE:
        row.exact = False
        if row.total_g is not None or row.print_min is not None:
            _scale_run(row, mm / size, exps)
        row.config += f"; made at {size:g} mm, scaled to {t.upper()} {mm:g} mm (ASSUMED){was}"
    else:
        row.exact = False
        if row.total_g is not None:
            row.auto = 0
        row.config += (f"; differs: {size:g} mm is not a current {product} size "
                       f"({sizes} mm){was}")


def _tier(stem: str, has_config: bool) -> int:
    if has_config:
        return 3
    return 1 if _is_procedural(stem) else 2


def slice_row(report: SliceReport, *, run_key: str, product: str, source: str, mtime: float,
              notes: str = "", auto: int | None = None, config: str = "") -> RunRow:
    grams = defaultdict(float)
    parts = []
    for f in report.filaments:
        grams[filament_bucket(f.filament_id)] += f.grams
        parts.append(f"slot {f.slot} {f.filament_id or '?'} {f.grams:g} g")
    total = round(sum(grams.values()), 3) if report.filaments else report.total_grams
    default_auto = 1 if (report.ok and product in PRODUCTS) else 0
    if not report.ok:
        notes = "; ".join(x for x in [notes, "slice failed the gate"] if x)
    return RunRow(
        run_key=run_key, product=product, kind="slice",
        auto=default_auto if auto is None else (auto if report.ok else 0), config=config,
        pla_bambu_g=round(grams["pla_bambu_g"], 3), pla_generic_g=round(grams["pla_generic_g"], 3),
        petg_g=round(grams["petg_g"], 3), other_g=round(grams["other_g"], 3), total_g=total,
        purge_g=report.purge_grams,
        print_min=round(report.print_time_s / 60, 2) if report.print_time_s is not None else None,
        slots=report.slots_used, ok=report.ok, filaments="; ".join(parts), source=source,
        notes=notes, run_at=datetime.fromtimestamp(mtime, UTC).isoformat(timespec="seconds"),
        mtime=mtime,
    )


def standalone_slice_files(roots: list[Path]) -> list[Path]:
    pats = ["data/slice_reports/*.json", "data/slice_reports/*/*.json",
            "threemf/_out/*.slice_report.json", "geometry/_out/*.slice_report.json",
            "geometry/_out/**/*slice_report*.json"]
    files: list[Path] = []
    for r in roots:
        for pat in pats:
            files += [p for p in r.glob(pat) if p.is_file()]
    return files


def lab_index(files: list[Path]) -> dict[Path, dict]:
    """Slice report path -> its purge-lab row (the settings it was sliced with)."""
    out: dict[Path, dict] = {}
    for d in {f.parent for f in files}:
        for row in _read_jsonl(d / "purge_lab.jsonl"):
            if row.get("report"):
                out[(d / row["report"]).resolve()] = row
    return out


def ingest_slices(roots: list[Path], exps: dict[str, float] | None = None) -> list[RunRow]:
    """One row per slice file. Automatic Use is 1 only for a slice whose recorded settings and
    orientation match production, preferring real Meshy models over procedural shapes."""
    prod = production()
    files = standalone_slice_files(roots)
    lab = lab_index(files)
    best: dict[str, tuple[RunRow, int]] = {}
    for path in files:
        try:
            report, cfg = parse_slice_file(path.read_text())
        except (OSError, ValueError):
            continue
        stem = path.name.removesuffix(".json").removesuffix(".slice_report")
        product = product_from_name(stem)
        if cfg and cfg.get("product") in PRODUCTS:
            product = cfg["product"]
        procedural = _is_procedural(stem)
        lrow = lab.get(path.resolve())
        notes = DEFAULT_EXCLUDE.get(stem, "")
        if cfg is not None:
            match, config = embedded_config(cfg, product, prod)
            auto = 1 if match and product in PRODUCTS else 0
            if cfg.get("source"):
                notes = "; ".join(x for x in [str(cfg["source"]), notes] if x)
        elif lrow is not None:
            match, config = lab_config(lrow, product, procedural, prod)
            auto = 1 if match and product in PRODUCTS else 0
            if lrow.get("quality_risk"):
                notes = "; ".join(x for x in [notes, f"quality risk: {lrow['quality_risk']}"] if x)
        else:
            auto, config = 0, "settings not recorded (not in a purge_lab.jsonl)"
        if stem.startswith(("beagle", "meshy")):
            notes = "; ".join(x for x in [f"real Meshy beagle, {product.replace('_', ' ')} size",
                                          notes] if x)
        row = slice_row(report, run_key=f"slice:{stem}", product=product,
                        source=_rel(path, roots), mtime=path.stat().st_mtime, notes=notes,
                        auto=auto, config=config)
        place_on_tier(row, stem, cfg, exps=exps)
        old = best.get(row.run_key)
        if old is None or row.mtime > old[0].mtime:
            best[row.run_key] = (row, _tier(stem, cfg is not None))
    rows = [r for r, _ in best.values()]
    # Per product and tier, only the best source counts: geometry production > purge-lab beagle >
    # procedural shape, and within a source a slice at the tier's size beats a scaled one.
    top: dict[tuple[str, str], tuple[int, bool]] = {}
    for r, t in best.values():
        if r.auto == 1:
            top[(r.product, r.tier)] = max(top.get((r.product, r.tier), (0, False)), (t, r.exact))
    for r, t in best.values():
        if r.auto == 1 and (t, r.exact) < top[(r.product, r.tier)]:
            r.auto = 0
            src, exact = top[(r.product, r.tier)]
            why = (f"superseded by the {TIER_NAMES[src]} of this product at "
                   f"{r.tier.upper()}" + (" made at that size" if exact and t == src else ""))
            r.notes = "; ".join(x for x in [r.notes, why] if x)
    return rows


# ------------------------------------------------------------------ Meshy spend and jobs


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _event_key(e: dict) -> tuple:
    return (e.get("vendor"), e.get("kind"), e.get("ref"), e.get("at"), e.get("credits"))


def _meshy_totals(events: list[dict]) -> tuple[float, float, str, bool]:
    meshy = [e for e in events if e.get("vendor") == "meshy"]
    credits = sum(float(e.get("credits") or 0) for e in meshy)
    usd = sum(float(e.get("usd") or 0) for e in meshy)
    kinds = ", ".join(f"{e.get('kind')} {e.get('credits', 0)}" for e in meshy)
    test = any(e.get("test_mode") for e in meshy)
    return credits, round(usd, 4), kinds, test


def _job_is_dev(job_dir: Path) -> bool:
    for m in job_dir.glob("s05_mesh/*/manifest.json"):
        try:
            if "meshy-6-lite" in m.read_text():
                return True
        except OSError:
            continue
    return False


def _job_slice(job_dir: Path) -> tuple[SliceReport, Path] | None:
    found = []
    for p in job_dir.glob("**/*slice_report*.json"):
        try:
            found.append((p.stat().st_mtime, parse_slice_report(p.read_text()), p))
        except (OSError, ValueError):
            continue
    if not found:
        return None
    _, rep, path = max(found, key=lambda t: t[0])
    return rep, path


def job_dirs(roots: list[Path], extra: list[Path]) -> list[Path]:
    dirs: list[Path] = []
    for base in [r / "jobs" for r in roots] + list(extra):
        if base.is_dir():
            dirs += [d for d in base.iterdir() if (d / "job.json").is_file()]
    return dirs


def ingest_jobs(roots: list[Path], extra_jobs: list[Path],
                global_events: dict[str, list[dict]],
                exps: dict[str, float] | None = None) -> list[RunRow]:
    rows: dict[str, RunRow] = {}
    for d in job_dirs(roots, extra_jobs):
        try:
            job = json.loads((d / "job.json").read_text())
        except (OSError, ValueError):
            continue
        job_id = job.get("id") or d.name
        product = job.get("product_type") or "unknown"
        events = {_event_key(e): e for e in _read_jsonl(d / "ledger.jsonl")}
        for e in global_events.pop(job_id, []):
            events.setdefault(_event_key(e), e)
        credits, usd, kinds, test = _meshy_totals(list(events.values()))
        dev = _job_is_dev(d)
        notes = []
        if test:
            notes.append("simulated on the fake Meshy server (test mode, nothing billed)")
        if dev:
            notes.append("dev cost profile (nano-banana-2, meshy-6-lite)")
        status = job.get("status") or ""
        if status != "done":
            notes.append(f"job not finished (status {status or 'unknown'})")
        notes.append(kinds)
        auto = 1 if (product in PRODUCTS and not dev and status == "done") else 0
        config = (f"job | {'dev' if dev else 'production'} cost profile | "
                  f"{job.get('profile') or 'thin_slice'} | status {status or 'unknown'}")
        mtime = (d / "job.json").stat().st_mtime
        row = RunRow(run_key=f"job:{job_id}", product=product, kind="job", auto=auto,
                     config=config,
                      credits=credits, meshy_usd=usd, source=_rel(d / "ledger.jsonl", roots),
                      notes="; ".join(n for n in notes if n), run_at=job.get("created_at", ""),
                      mtime=mtime)
        sl = _job_slice(d)
        if sl is not None:
            rep, path = sl
            srow = slice_row(rep, run_key=row.run_key, product=product,
                             source=_rel(path, roots), mtime=mtime, auto=auto)
            for k in ("pla_bambu_g", "pla_generic_g", "petg_g", "other_g", "total_g", "purge_g",
                      "print_min", "slots", "ok", "filaments"):
                setattr(row, k, getattr(srow, k))
            row.source = f"{row.source}; {srow.source}"
        size = job.get("target_size_mm")
        place_on_tier(row, cfg={"size_tier": job.get("size_tier")},
                      size_mm=float(size) if isinstance(size, (int, float)) else None, exps=exps)
        old = rows.get(row.run_key)
        if old is None or row.mtime > old.mtime:
            rows[row.run_key] = row
    return list(rows.values())


def ledger_files(roots: list[Path]) -> list[Path]:
    return [r / "data" / "ledger.jsonl" for r in roots if (r / "data" / "ledger.jsonl").is_file()]


def ingest_ledgers(roots: list[Path]) -> tuple[list[RunRow], dict[str, list[dict]]]:
    """Global ledgers: events with a job_id go to that job; the rest are grouped into runs by time
    (a gap over 30 minutes starts a new run)."""
    by_job: dict[str, list[dict]] = defaultdict(list)
    loose: dict[tuple, tuple[dict, Path]] = {}
    for f in ledger_files(roots):
        for e in _read_jsonl(f):
            if e.get("job_id"):
                by_job[e["job_id"]].append(e)
            else:
                loose.setdefault(_event_key(e), (e, f))
    events = sorted(loose.values(), key=lambda t: t[0].get("at", ""))
    groups: list[list[tuple[dict, Path]]] = []
    last = None
    for e, f in events:
        try:
            at = datetime.fromisoformat(e["at"])
        except (KeyError, ValueError):
            continue
        if last is None or (at - last).total_seconds() > LEDGER_GAP_S:
            groups.append([])
        groups[-1].append((e, f))
        last = at
    rows = []
    for g in groups:
        evs = [e for e, _ in g]
        if not any(e.get("vendor") == "meshy" for e in evs):
            continue
        credits, usd, kinds, test = _meshy_totals(evs)
        first = evs[0]["at"]
        note = ("real Meshy spend, live key; no job id, so no product" if not test
                else "test mode; no job id")
        rows.append(RunRow(
            run_key=f"ledger:{first}", product="unknown", kind="meshy spend", auto=0,
            config="Meshy spend with no job id",
            credits=credits, meshy_usd=usd, source=_rel(g[0][1], roots),
            notes=f"{note}; {kinds}", run_at=first,
            mtime=datetime.fromisoformat(first).timestamp()))
    return rows, dict(by_job)


def ingest_all(repo_root: Path, extra_jobs: list[Path] | None = None,
               include_worktrees: bool = True,
               exps: dict[str, float] | None = None) -> list[RunRow]:
    """exps: size exponents (model, purge, time) for scaling near-size slices to their tier."""
    roots = scan_roots(repo_root, include_worktrees)
    ledger_rows, by_job = ingest_ledgers(roots)
    job_rows = ingest_jobs(roots, list(extra_jobs or []), by_job, exps)
    for job_id, evs in by_job.items():  # job ids in the ledger with no job folder found
        credits, usd, kinds, _ = _meshy_totals(evs)
        if credits:
            job_rows.append(RunRow(run_key=f"job:{job_id}", product="unknown", kind="job", auto=0,
                                    config="job folder not found",
                                    credits=credits, meshy_usd=usd, source="data/ledger.jsonl",
                                    notes=f"job folder not found; {kinds}",
                                    run_at=evs[0].get("at", "")))
    return ingest_slices(roots, exps) + ledger_rows + job_rows


# ------------------------------------------------------------------ archive


def load_archive(path: Path) -> dict[str, RunRow]:
    out: dict[str, RunRow] = {}
    if path.is_file():
        for d in _read_jsonl(path):
            row = RunRow.from_json(d)
            out[row.run_key] = row
    return out


REMOVED = "slice file removed (superseded); set Use to 1 to keep counting it"


def merge_archive(archive: dict[str, RunRow], fresh: list[RunRow]) -> dict[str, RunRow]:
    """Newer copies replace older ones. A standalone slice file that is gone was removed on
    purpose (a writer replaced it), so its archived row stops counting automatically; job runs
    keep counting after their folder is cleaned up."""
    merged = dict(archive)
    seen = {r.run_key for r in fresh}
    for row in fresh:
        old = merged.get(row.run_key)
        if old is None or row.mtime >= old.mtime:
            merged[row.run_key] = row
    for key, row in merged.items():
        if key not in seen and row.kind == "slice" and row.auto == 1:
            row.auto = 0
            if REMOVED not in row.notes:
                row.notes = "; ".join(x for x in [row.notes, REMOVED] if x)
    return merged


def save_archive(path: Path, rows: dict[str, RunRow]) -> None:
    ordered = sorted(rows.values(), key=lambda r: (r.kind, r.product, r.run_key))
    path.write_text("".join(json.dumps(r.to_json(), sort_keys=True) + "\n" for r in ordered))
