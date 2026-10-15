"""python -m p2p_evals <command>

  fixtures build [--job LABEL ...] [--jobs-dir DIR]   copy the stored jobs' paid Meshy outputs
  fixtures verify                                     re-hash the store against the manifest
  replay [--from s06|s01] [--claude recorded|login|mock] [--paint replay|stub|agent]
         [--job LABEL ...] [--work DIR] [--out DIR]   the regression suite, 0 credits
  report [--from s06|s01] [--claude ...] [--paint ...]  rescore a finished suite from its folders
  bench-s06 [--job LABEL ...] [--impl before,after]   s06 build before and after the speedup

Long runs: start them with nohup and read the log (the suite checkpoints its report after
every job).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .jobs import DEFAULT_JOBS_DIR, FIXTURES, REPO, REPORTS, pick


def _say(msg: str) -> None:
    print(msg, flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p2p_evals")
    sub = ap.add_subparsers(dest="cmd", required=True)

    fx = sub.add_parser("fixtures")
    fx.add_argument("action", choices=("build", "verify"))
    fx.add_argument("--job", action="append", default=[])
    fx.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS_DIR)
    fx.add_argument("--out", type=Path, default=FIXTURES)

    rp = sub.add_parser("replay")
    rp.add_argument("--from", dest="start", choices=("s06", "s01"), default="s06")
    rp.add_argument("--claude", choices=("recorded", "login", "mock"), default="recorded")
    rp.add_argument("--paint", choices=("replay", "stub", "agent"), default="replay")
    rp.add_argument("--job", action="append", default=[])
    rp.add_argument("--work", type=Path, default=REPO / "evals" / "_work")
    rp.add_argument("--out", type=Path, default=REPORTS)
    rp.add_argument("--fixtures", type=Path, default=FIXTURES)

    rs = sub.add_parser("report")
    rs.add_argument("--from", dest="start", choices=("s06", "s01"), default="s06")
    rs.add_argument("--claude", choices=("recorded", "login", "mock"), default="recorded")
    rs.add_argument("--paint", choices=("replay", "stub", "agent"), default="replay")
    rs.add_argument("--work", type=Path, default=REPO / "evals" / "_work")
    rs.add_argument("--out", type=Path, default=REPORTS)
    rs.add_argument("--fixtures", type=Path, default=FIXTURES)

    bn = sub.add_parser("bench-s06")
    bn.add_argument("--job", action="append", default=[])
    bn.add_argument("--impl", default="before,after")
    bn.add_argument("--work", type=Path, default=REPO / "evals" / "_work")
    bn.add_argument("--out", type=Path, default=REPORTS)

    a = ap.parse_args(argv)
    if a.cmd == "fixtures":
        from . import fixtures

        if a.action == "build":
            fixtures.build(pick(a.job), a.jobs_dir, a.out, log=_say)
            problems = fixtures.verify(a.out)
        else:
            problems = fixtures.verify(a.out)
        for p in problems[:20]:
            _say(p)
        _say("store ok" if not problems else f"{len(problems)} problems")
        return 1 if problems else 0
    if a.cmd == "replay":
        from .replay import Options, report_name, run_suite

        opt = Options(start=a.start, claude=a.claude, paint=a.paint, work=a.work,
                      fixtures=a.fixtures)
        report = run_suite(pick(a.job), opt, a.out, say=_say)
        _say(f"report: {a.out / report_name(opt)}.md")
        bad = [j["job"] for j in report["jobs"] if j["status"] != "done"]
        return 1 if bad else 0
    if a.cmd == "report":
        import json
        import time

        from .replay import Options, report_name
        from .score import markdown, rescore

        opt = Options(start=a.start, claude=a.claude, paint=a.paint, work=a.work,
                      fixtures=a.fixtures)
        path = a.out / f"{report_name(opt)}.json"
        report = rescore(path, opt, a.work, a.fixtures)
        path.write_text(json.dumps(report, indent=1, default=str) + "\n")
        path.with_suffix(".md").write_text(
            markdown(report["jobs"], opt, time.time() - float(report.get("wall_s") or 0)))
        _say(f"rewrote {path.with_suffix('.md')}")
        return 0
    if a.cmd == "bench-s06":
        from .bench import bench_job, write_bench

        impls = [i.strip() for i in a.impl.split(",") if i.strip()]
        results = []
        for sj in pick(a.job or ["sombrero"]):
            results.append(bench_job(sj, impls, a.work, say=_say))
            write_bench(results, a.out)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
