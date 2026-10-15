"""Scores of one replayed job against its stored run, and the suite report (JSON plus a short
markdown table under evals/reports/)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from p2p_contracts import STAGES
from p2p_worker.util import read_json, read_jsonl

from .compare import compare_geometry
from .jobs import StoredJob

STAGE_IDS = [s.id.value for s in STAGES]


def _accepted(job: dict[str, Any], stage: str) -> int | None:
    return (job.get("attempts") or {}).get(stage)


def _manifest(root: Path, job: dict[str, Any], stage: str) -> dict[str, Any] | None:
    n = _accepted(job, stage)
    return read_json(root / stage / f"a{n}" / "manifest.json") if n else None


def _grams(result: dict[str, Any] | None) -> float | None:
    if not result or not result.get("filaments"):
        return None
    return round(sum(float(f.get("grams") or 0) for f in result["filaments"]), 2)


def _slots(result: dict[str, Any] | None) -> int | None:
    if not result or not result.get("filaments"):
        return None
    return len({f.get("slot") for f in result["filaments"] if float(f.get("grams") or 0) > 0})


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old in (None, 0):
        return None
    return round(100.0 * (new - old) / old, 2)


def _result_stage(job: dict[str, Any]) -> str | None:
    t = ((job.get("result") or {}).get("threemf") or "")
    return t.split("/", 1)[0] if t else None


def _gate(root: Path, job: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """The slicer gate of the stage that wrote the result (s10 on full, s07 on a thin slice),
    or of the last stage that tried."""
    stage = _result_stage(job)
    if stage is None:
        for s in ("s10_write", "s07_baseline"):
            if _accepted(job, s) and (root / s / f"a{_accepted(job, s)}" /
                                      "slice_report.json").is_file():
                stage = s
                break
    rep = read_json(root / stage / f"a{_accepted(job, stage)}" / "slice_report.json") \
        if stage else None
    if not rep:
        return {"stage": stage, "slicer": None, "ok": None, "summary": "not run"}
    slicer = "Bambu" if rep.get("slicer") == "bambu_studio" else (rep.get("slicer") or "?")
    return {"stage": stage, "slicer": slicer, "ok": rep.get("ok"),
            "errors": (rep.get("errors") or [])[:3], "print_time_s": rep.get("print_time_s"),
            "total_grams": rep.get("total_grams"), "slots_used": rep.get("slots_used"),
            "summary": f"{slicer} {'pass' if rep.get('ok') else 'fail'}"}


def _form(root: Path, job: dict[str, Any]) -> dict[str, Any]:
    n1 = _accepted(job, "s01_intake")
    product = read_json(root / "s01_intake" / f"a{n1}" / "product.json") if n1 else None
    m6 = _manifest(root, job, "s06_printability") or {}
    n6 = _accepted(job, "s06_printability")
    read = (read_json(root / "s06_printability" / f"a{n6}" / "concept_read.json") or {}) \
        if n6 else {}
    n3 = _accepted(job, "s03_concept")
    concept = (read_json(root / "s03_concept" / f"a{n3}" / "concept.json") or {}) if n3 else {}
    built = (m6.get("data") or {}).get("form")
    if not built and n6:  # runs before s06 recorded the form: read it off the print pose
        geo = read_json(root / "s06_printability" / f"a{n6}" / "geometry.json") or {}
        built = {"upright": "figure", "flat_face_up": "flat",
                 "flat_back_down": "flat"}.get(geo.get("orientation"))
    return {"job": job.get("form") or (product or {}).get("form"),
            "concept": concept.get("form"),
            "concept_read": (read.get("read") or {}).get("form"),
            "built": built}


def _concept_check(root: Path, job: dict[str, Any]) -> dict[str, Any]:
    n3 = _accepted(job, "s03_concept")
    c = (read_json(root / "s03_concept" / f"a{n3}" / "concept.json") or {}) if n3 else {}
    ap = c.get("approved") or {}
    cands = c.get("candidates") or []
    return {"approved": ap.get("id"), "passed": (ap.get("check") or {}).get("passed"),
            "candidates": len(cands),
            "candidates_passed": sum(1 for x in cands if (x.get("check") or {}).get("passed"))}


def _final_check(m9: dict[str, Any] | None) -> dict[str, Any]:
    if not m9 or m9.get("status") == "skipped":
        return {"passed": None, "how": "no s09"}
    d = m9.get("data") or {}
    if (d.get("paint_replay") or {}).get("used"):
        return {"passed": d["paint_replay"].get("stored_check_passed"),
                "how": "stored verdict (paint replayed)",
                "approved_anyway": d["paint_replay"].get("stored_review_approved")}
    cc = d.get("concept_check")
    if cc:
        return {"passed": cc.get("passed"), "how": "paint agent",
                "approved_anyway": bool(d.get("review_approved"))}
    return {"passed": None, "how": f"not run ({d.get('agent') or 'stub'} paint)"}


def _stage_times(root: Path, job: dict[str, Any], files: Path, stored_job: dict[str, Any],
                 start: str) -> dict[str, dict[str, Any]]:
    """Per stage: the rerun's wall time (None for a stage copied from the stored run, before
    the start stage) and the stored run's."""
    first = next(i for i, s in enumerate(STAGE_IDS) if s.startswith(start))
    out = {}
    for i, s in enumerate(STAGE_IDS):
        m = _manifest(root, job, s)
        ms = _manifest(files, stored_job, s)
        if m is None and ms is None:
            continue
        rerun = i >= first
        out[s] = {"status": (m or {}).get("status") if rerun else "stored",
                  "attempt": _accepted(job, s),
                  "wall_s": (m or {}).get("duration_s") if rerun else None,
                  "stored_s": (ms or {}).get("duration_s"),
                  "stored_status": (ms or {}).get("status")}
    return out


def score_files(sj: StoredJob, opt, job_dir: Path, files: Path) -> dict[str, Any]:
    """Everything a score reads from the rerun's job folder and the store."""
    job = read_json(job_dir / "job.json") or {}
    row = read_json(job_dir / "job_row.json") or {}
    stored = read_json(files / "job.json") or {}
    stored_row = read_json(files / "job_row.json") or {}
    res, sres = job.get("result") or row.get("result"), stored.get("result")
    gate = _gate(job_dir, job, row)
    sgate = _gate(files, stored, stored_row)
    form = _form(job_dir, job)
    sform = _form(files, stored)
    forms = {v for v in (form["job"], form["concept"], form["built"]) if v}
    consistent = len(forms) <= 1 and form["concept_read"] in (None, "unclear", *forms)
    n6, n6s = _accepted(job, "s06_printability"), _accepted(stored, "s06_printability")
    geometry = compare_geometry(files / "s06_printability" / f"a{n6s}",
                                job_dir / "s06_printability" / f"a{n6}") if n6 and n6s else \
        {"compared": False, "ok": None, "reason": "s06 did not run"}
    m9 = _manifest(job_dir, job, "s09_paint")
    return {
        "pause_reason": row.get("pause_reason") or job.get("pause_reason"),
        "review_reason": row.get("review_reason"),
        "stages": _stage_times(job_dir, job, files, stored, opt.start),
        "gate": {**gate, "stored": sgate.get("summary"), "stored_ok": sgate.get("ok")},
        "form": {**form, "stored_built": sform["built"], "consistent": consistent,
                 "same_as_stored": form["built"] == sform["built"]},
        "concept_check": {**_concept_check(job_dir, job),
                          "stored": _concept_check(files, stored).get("passed"),
                          "rerun": opt.start == "s01"},
        "final_check": {**_final_check(m9),
                        "stored": _final_check(_manifest(files, stored, "s09_paint"))
                        .get("passed")},
        "slots": {"rerun": _slots(res), "stored": _slots(sres)},
        "print_time_s": {"rerun": (res or {}).get("print_time_s"),
                         "stored": (sres or {}).get("print_time_s"),
                         "delta_pct": _pct((res or {}).get("print_time_s"),
                                           (sres or {}).get("print_time_s"))},
        "grams": {"rerun": _grams(res), "stored": _grams(sres),
                  "delta_pct": _pct(_grams(res), _grams(sres))},
        "paint_replay": ((m9 or {}).get("data") or {}).get("paint_replay"),
        "geometry_vs_stored": geometry,
        "meshy_credits_booked": sum(int(e.get("credits") or 0)
                                    for e in read_jsonl(job_dir / "ledger.jsonl")
                                    if e.get("vendor") == "meshy"),
    }


def score_job(sj: StoredJob, opt, job_dir: Path, files: Path, status, wall: float,
              replay, claude, responder, error: str | None) -> dict[str, Any]:
    out = {
        "job": sj.label, "job_id": sj.job_id, "what": sj.what,
        "start": opt.start, "claude": opt.claude, "paint": opt.paint,
        "status": status.value if status is not None else "crashed",
        "error": error, "wall_s": round(wall, 1),
        **score_files(sj, opt, job_dir, files),
        "meshy": replay.summary(),
        "claude_calls": claude.summary() if hasattr(claude, "summary") else
        {"backend": type(claude).__name__},
    }
    if responder is not None:
        out["gates"] = {"replies": responder.log, "diverged": responder.diverged,
                        "stuck": responder.stuck}
    return out


def rescore(report_path: Path, opt, work: Path, fixtures: Path) -> dict[str, Any]:
    """Re-read a finished suite's job folders (kept under the work folder) and rewrite its
    report with the current scoring code; call counts and wall times stay as they ran."""
    from .jobs import pick
    from .replay import work_root

    report = json.loads(report_path.read_text())
    for r in report["jobs"]:
        sj = pick([r["job"]])[0]
        job_dir = work_root(opt, sj) / "jobs" / sj.job_id
        r.update(score_files(sj, opt, job_dir, fixtures / "store" / sj.job_id / "files"))
    return report


# ---------------------------------------------------------------------------- report


def _fmt_s(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v / 60:.1f} min" if v >= 120 else f"{v:.0f} s"


def _fmt_time(v: float | None) -> str:
    if v is None:
        return "-"
    h, m = divmod(round(v / 60), 60)
    return f"{h} h {m:02d} min"


def _yn(v: Any) -> str:
    return {True: "pass", False: "fail", None: "-"}.get(v, str(v))


def _geo_cell(geo: dict[str, Any]) -> str:
    if not geo.get("compared"):
        return "-"
    if geo.get("ok") and all(v.get("identical") for v in geo.get("meshes", {}).values()):
        return "identical"
    if geo.get("ok"):
        return f"within {geo.get('tol_mm')} mm"
    return "differs"


def _form_cell(form: dict[str, Any]) -> str:
    out = form["built"] or form["job"] or "-"
    if not form["consistent"]:
        out += " (job, concept and build disagree)"
    if not form.get("same_as_stored"):
        out += f" (stored {form.get('stored_built') or '-'})"
    return out


def _row(r: dict[str, Any]) -> str:
    s6 = r["stages"].get("s06_printability", {})
    cc, fc, form, meshy = r["concept_check"], r["final_check"], r["form"], r["meshy"]
    match = ", ".join(f"{v} {k}" for k, v in sorted(meshy["by_match"].items())) or "none"
    cells = [
        r["job"], r["status"], r["gate"]["summary"],
        _form_cell(form),
        _yn(cc["passed"]) + ("" if cc["rerun"] else " (stored)"),
        _yn(fc["passed"]) + (", approved" if fc.get("approved_anyway") else "")
        + (" (stored)" if "stored" in fc.get("how", "") else ""),
        f"{r['slots']['rerun'] or '-'} ({r['slots']['stored'] or '-'})",
        f"{_fmt_time(r['print_time_s']['rerun'])} ({_fmt_time(r['print_time_s']['stored'])})",
        f"{r['grams']['rerun'] or '-'} ({r['grams']['stored'] or '-'})",
        f"{_fmt_s(s6.get('wall_s'))} ({_fmt_s(s6.get('stored_s'))})",
        _geo_cell(r["geometry_vs_stored"]),
        (f"{meshy['answered']} ({match}), {meshy['misses']} missed, "
         f"{r['meshy_credits_booked']} cr"),
    ]
    return "| " + " | ".join(cells) + " |"


def _notes(results: list[dict[str, Any]]) -> list[str]:
    notes = []
    for r in results:
        for k in ("error", "pause_reason", "review_reason"):
            if r.get(k):
                notes.append(f"- {r['job']}: {k.replace('_', ' ')}: {str(r[k])[:300]}")
        for c in r["meshy"]["calls"]:
            if c["match"] == "miss":
                notes.append(f"- {r['job']}: Meshy miss at {c['stage']} key {c['key']} "
                             f"({c['endpoint']})")
        for c in (r.get("claude_calls") or {}).get("calls") or []:
            if c["match"] == "miss":
                notes.append(f"- {r['job']}: Claude miss at {c['stage']} ({c['purpose']})")
        filled = (r.get("claude_calls") or {}).get("check_items_filled")
        if filled:
            notes.append(f"- {r['job']}: {filled} concept check items had no recorded answer "
                         "(passed by the stub)")
        if (r.get("paint_replay") or {}).get("used") is False:
            notes.append(f"- {r['job']}: paint not replayed: {r['paint_replay']['reason']}")
        gates = r.get("gates") or {}
        if gates.get("stuck"):
            notes.append(f"- {r['job']}: stuck at a gate: {gates['stuck']}")
        notes += [f"- {r['job']}: gate reply adjusted: {d}" for d in gates.get("diverged") or []]
    return notes


def markdown(results: list[dict[str, Any]], opt, started: float) -> str:
    lines = [
        f"# Replay from {opt.start}", "",
        (f"Claude {opt.claude}, s09 paint {opt.paint}, Meshy replay (0 credits). Stored run "
         "numbers in brackets are SOURCE: the job's manifests and slice reports; rerun numbers "
         "are SOURCE: this run."), "",
        ("| Job | Status | Gate | Form | Concept check | Final check | Slots | Print time | "
         "Grams | s06 wall | s06 output | Meshy calls |"),
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        *[_row(r) for r in results],
        "", "Stage wall times, rerun (stored):", "",
    ]
    stages = [s for s in STAGE_IDS if any(s in r["stages"] for r in results)]
    lines.append("| Job | " + " | ".join(s.split("_")[0] for s in stages) + " | Total |")
    lines.append("|---|" + "---|" * (len(stages) + 1))
    for r in results:
        cells = []
        for s in stages:
            st = r["stages"].get(s)
            if not st or st.get("status") == "stored" or (
                    st.get("wall_s") is None and st.get("stored_s") is None):
                cells.append("-")
            else:
                cells.append(f"{_fmt_s(st.get('wall_s'))} ({_fmt_s(st.get('stored_s'))})")
        lines.append(f"| {r['job']} | " + " | ".join(cells) + f" | {_fmt_s(r['wall_s'])} |")
    notes = _notes(results)
    if notes:
        lines += ["", "Notes:", "", *notes]
    lines += ["", (f"Suite wall time {_fmt_s(time.time() - started)}. Stages before the start "
                   "are the stored run's (-). Stored s01 to s05 wall times include customer "
                   "gate waits and Meshy queues; stored s09 is the paint agent.")]
    return "\n".join(lines) + "\n"


def write_report(results: list[dict[str, Any]], opt, out_dir: Path, name: str,
                 started: float, *, partial: bool) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"suite": name, "partial": partial, "start": opt.start, "claude": opt.claude,
              "paint": opt.paint, "wall_s": round(time.time() - started, 1), "jobs": results}
    (out_dir / f"{name}.json").write_text(json.dumps(report, indent=1, default=str) + "\n")
    (out_dir / f"{name}.md").write_text(markdown(results, opt, started))
    return report
