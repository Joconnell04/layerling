"""s06 geometry benchmark: the stage's build on a stored job, with the current code and with
the code before the silhouette change (the union of every triangle, and np.unique on edge rows),
same machine, same inputs. Reports wall and CPU seconds and checks the outputs are the same.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

import numpy as np

from .compare import compare_geometry
from .jobs import FIXTURES, REPO, StoredJob


def union_silhouette(vertices: np.ndarray, faces: np.ndarray):
    """projected_silhouette before the change: every triangle, unioned by Clipper2."""
    from manifold3d import CrossSection, FillRule

    tri = np.asarray(vertices)[np.asarray(faces)][:, :, :2]
    area = 0.5 * ((tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1])
                  - (tri[:, 2, 0] - tri[:, 0, 0]) * (tri[:, 1, 1] - tri[:, 0, 1]))
    keep = np.abs(area) > 1e-9
    tri = tri[keep]
    flip = area[keep] < 0
    tri[flip] = tri[flip][:, ::-1]
    return CrossSection([t for t in tri], FillRule.Positive)


def row_edge_table(faces: np.ndarray):
    """repair._edge_table before the change: np.unique over (lo, hi) rows."""
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    fid = np.tile(np.arange(len(faces)), 3)
    es = np.sort(e, axis=1)
    uniq, inv, counts = np.unique(es, axis=0, return_inverse=True, return_counts=True)
    return e, fid, uniq, inv.reshape(-1), counts


@contextlib.contextmanager
def before_change():
    """The geometry code as it was before this branch's speedup."""
    from p2p_geometry import meshio, plinth, product, repair

    saved = [(meshio, "projected_silhouette"), (product, "projected_silhouette"),
             (plinth, "projected_silhouette"), (repair, "_edge_table")]
    old = [getattr(m, n) for m, n in saved]
    for m, n in saved:
        setattr(m, n, row_edge_table if n == "_edge_table" else union_silhouette)
    try:
        yield
    finally:
        for (m, n), f in zip(saved, old, strict=True):
            setattr(m, n, f)


def _inputs(job_dir: Path, files: Path):
    from p2p_worker.config import Settings
    from p2p_worker.product_spec import read_spec
    from p2p_worker.stages.s06_printability import GeometryStage
    from p2p_worker.store import LocalStore
    from p2p_worker.util import read_json

    store = LocalStore(job_dir.parent)
    job = store.load_job(job_dir.name)

    class _C:
        settings = Settings(repo_root=REPO, jobs_dir=job_dir.parent)

    known = GeometryStage._known(_C)
    spec = read_spec(job_dir, job, store, set(known))
    stored = read_json(files / "job.json") or {}
    n5, n6 = job.attempts.get("s05_mesh"), (stored.get("attempts") or {}).get("s06_printability")
    mesh = read_json(job_dir / "s05_mesh" / f"a{n5}" / "mesh.json") or {}
    glb = job_dir / (mesh.get("glb") or f"s05_mesh/a{n5}/mesh_m1.glb")
    cr = read_json(files / "s06_printability" / f"a{n6}" / "concept_read.json") or {}
    return job, spec, cr.get("read"), known, glb, not mesh.get("chainable", True), n6


def bench_job(sj: StoredJob, impls: list[str], work: Path, fixtures: Path = FIXTURES,
              say=print) -> dict[str, Any]:
    from p2p_worker.offload import WorkCtx, _Paths, _Rows
    from p2p_worker.stages.s06_printability import GeometryStage

    from .replay import seed_from_s06

    files = fixtures / "store" / sj.job_id / "files"
    root = work / f"bench-{sj.label}" / "jobs"
    if root.exists():
        shutil.rmtree(root)
    job_dir = root / sj.job_id
    job_dir.mkdir(parents=True)
    seed_from_s06(files, job_dir)
    job, spec, read, known, glb, relief, n6 = _inputs(job_dir, files)
    runs: dict[str, Any] = {}
    for impl in impls:
        rel = f"s06_printability/bench-{impl}"
        out = job_dir / rel
        out.mkdir(parents=True, exist_ok=True)
        wctx = WorkCtx(job=job, job_dir=job_dir, dir=out, rel_dir=rel,
                       settings=_Paths(REPO), store=_Rows(None))
        ctx = before_change() if impl == "before" else contextlib.nullcontext()
        say(f"{sj.label}: s06 build, {impl} ...")
        with ctx:
            w0, c0 = time.perf_counter(), time.process_time()
            res = GeometryStage()._build(wctx, glb, spec, read, known, relief)
            wall, cpu = time.perf_counter() - w0, time.process_time() - c0
        runs[impl] = {"wall_s": round(wall, 1), "cpu_s": round(cpu, 1),
                      "source": res.get("source"),
                      "vs_stored": compare_geometry(files / "s06_printability" / f"a{n6}", out)}
        say(f"{sj.label}: {impl} {wall:.1f} s wall, {cpu:.1f} s CPU, output "
            f"{'same as stored' if runs[impl]['vs_stored'].get('ok') else 'DIFFERS from stored'}")
    if len(impls) == 2:
        a, b = (job_dir / f"s06_printability/bench-{i}" for i in impls)
        runs["same_output"] = compare_geometry(a, b)
    return {"job": sj.label, "job_id": sj.job_id, "runs": runs}


def write_bench(results: list[dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "bench-s06.json").write_text(json.dumps(results, indent=1) + "\n")
    lines = ["# s06 geometry build, before and after the silhouette change", "",
             ("Same machine, same inputs, one run each (SOURCE: this benchmark). Wall seconds "
              "depend on what else the machine runs; CPU seconds less so."), "",
             ("| Job | Before wall | Before CPU | After wall | After CPU | Speedup (CPU) | "
              "Outputs |"), "|---|---|---|---|---|---|---|"]
    for r in results:
        b, a = r["runs"].get("before", {}), r["runs"].get("after", {})
        same = r["runs"].get("same_output", {})
        ident = same.get("ok") and all(m.get("identical") for m in same.get("meshes", {}).values())
        speed = f"{b['cpu_s'] / a['cpu_s']:.0f}x" if b and a and a.get("cpu_s") else "-"
        verdict = "identical files" if ident else (
            "within tolerance" if same.get("ok") else "differ")
        lines.append(f"| {r['job']} | {b.get('wall_s', '-')} s | {b.get('cpu_s', '-')} s | "
                     f"{a.get('wall_s', '-')} s | {a.get('cpu_s', '-')} s | {speed} | "
                     f"{verdict} |")
    (out_dir / "bench-s06.md").write_text("\n".join(lines) + "\n")
