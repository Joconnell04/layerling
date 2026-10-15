"""Mock files for the studio paint editor: the 3D keychain's shipped paint and one paint edit,
made by the real pipeline (p2p_mcp_paint.edits: the session's region ops, simulate_print, the
3MF, p2p_slicer numbers and toolpaths, the eight proof views).

Run from the repo root:

    uv run python apps/web/scripts/gen_mock_paint_edit.py

Reads apps/web/public/mock/kc3d-preview.glb (45 mm, filament slots brown, white, black, red, as
scripts/gen_mock_proofs.py reads them) and writes apps/web/public/mock/paint/:
  figure.ply                       the figure, face order kept
  base/  paint_state.json, region_map.json, face_regions.npy   (s09_paint/a1: what shipped)
  edit/  the same three after the edit (s09_paint/a2), paint_preview.json, toolpaths.json,
         toolpaths.bin, proof/{paint,print}_<view>.jpg
The edit (mock only): the collar (the largest red patch) to Bambu PLA Basic Orange and a white
blaze brushed on the forehead. 0 credits, no network.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
from p2p_contracts.payloads import PaintOp
from p2p_contracts.regions import Region
from p2p_mcp_paint.edits import apply_ops, encode_faces, preview, seed
from p2p_mcp_paint.job import load_fixture, load_library
from p2p_mcp_paint.session import PaintSession
from p2p_sim.geodesic import components
from p2p_sim.mesh import Mesh, load_glb, save_ply

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gen_mock_proofs import face_slots

MOCK = Path(__file__).resolve().parents[1] / "public" / "mock"
OUT = MOCK / "paint"
IDS = ["generic-pla-brown", "generic-pla-white", "generic-pla-black", "bambu-pla-basic-red"]
WORD = {"generic-pla-brown": "brown", "generic-pla-white": "white", "generic-pla-black": "black",
        "bambu-pla-basic-red": "red"}
ORANGE = "bambu-pla-basic-orange"


def regions_from(mesh, slots: np.ndarray):
    n, comp = components(mesh, np.ones(mesh.n_faces, bool), slots)
    area = np.bincount(comp, weights=mesh.face_areas, minlength=n)
    rid = np.empty(n, np.int32)
    rid[np.argsort(-area, kind="stable")] = np.arange(1, n + 1, dtype=np.int32)
    fr = rid[comp]
    first = np.full(n + 1, -1, np.int64)
    first[fr[::-1]] = np.arange(len(fr))[::-1]
    counts: dict[str, int] = {}
    regions, assign = [], {}
    for r in range(1, n + 1):
        fid = IDS[int(slots[first[r]]) - 1]
        w = WORD[fid]
        counts[w] = counts.get(w, 0) + 1
        label = "s1.body" if r == 1 else f"s1.{'collar' if w == 'red' and counts[w] == 1 else w}_{counts[w]}"
        regions.append(Region(id=r, label=label, subject_id="s1", part="body" if r == 1 else "other",
                              source="meshy_color", n_faces=0, area_mm2=0.0, centroid_mm=(0, 0, 0),
                              bbox_min_mm=(0, 0, 0), bbox_max_mm=(0, 0, 0)))
        assign[r] = fid
    return fr, regions, assign


def blaze(s: PaintSession, radius: float = 3.2) -> np.ndarray:
    """Front-facing brown faces within `radius` of the highest front point of the head."""
    fig = s.inp.figure
    c, n = fig.face_centroids, fig.face_normals
    fil = s.face_filament()
    front = (n[:, 1] < -0.3) & (fil == "generic-pla-brown")
    top = np.nonzero(front)[0][np.argmax(c[front, 2])]
    p = c[top] - np.array([0, 0, 2.5])
    d = np.linalg.norm(c - p, axis=1)
    return np.nonzero((d < radius) & (n[:, 1] < 0.1))[0]


def rewrite(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text().replace(old, new))


def welded(mesh: Mesh) -> Mesh:
    """The preview GLB is a triangle soup (each face its own corners); weld the corners so the
    slicer sees closed surfaces. Face order is kept."""
    v, inv = np.unique(np.round(mesh.vertices, 4), axis=0, return_inverse=True)
    return Mesh(vertices=v, faces=inv.reshape(-1)[mesh.faces], name="figure")


def main() -> None:
    mesh = welded(load_glb(MOCK / "kc3d-preview.glb", 45))
    slots = face_slots(MOCK / "kc3d-preview.glb")
    fr, regions, assign = regions_from(mesh, slots)
    owned, catalog = load_library()
    with tempfile.TemporaryDirectory() as tmp:
        job = Path(tmp) / "mockjob"
        job.mkdir()
        inp = load_fixture(job, mesh, baseline_filaments=np.array([IDS[k - 1] for k in slots], object),
                           library=owned, catalog=catalog, orientation="upright")
        inp.figure_rel = "s06_printability/a1/figure.ply"
        base = PaintSession.from_inputs(inp, job / "s09_paint" / "a1", face_regions=fr,
                                        regions=regions, assignment=assign)
        base.palette = [f for f in IDS if f in set(assign.values())]
        base.default_filament = "generic-pla-brown"
        base._save()
        before = preview(base, job / "s09_paint" / "a1" / "preview", proof=False, toolpaths=False)

        edit = PaintSession(inp, job / "s09_paint" / "a2", 2)
        seed(edit, job / "s09_paint" / "a1" / "paint_state.json")
        collar = next(r.id for r in edit.regions.values() if r.label.startswith("s1.collar"))
        ops = [PaintOp(op="fill_region", region_ids=[collar], filament_id=ORANGE),
               PaintOp(op="brush_faces", faces=encode_faces(blaze(edit)),
                       filament_id="generic-pla-white")]
        res = apply_ops(edit, ops, source="any")
        after = preview(edit, job / "s09_paint" / "a2" / "preview", edit=res, before=before.after,
                        base_attempt="s09_paint/a1")

        if OUT.exists():
            shutil.rmtree(OUT)
        (OUT / "base").mkdir(parents=True)
        (OUT / "edit" / "proof").mkdir(parents=True)
        save_ply(mesh, OUT / "figure.ply")
        for name in ("paint_state.json", "region_map.json", "face_regions.npy"):
            shutil.copy(job / "s09_paint" / "a1" / name, OUT / "base" / name)
            shutil.copy(job / "s09_paint" / "a2" / name, OUT / "edit" / name)
        for d in ("base", "edit"):
            rewrite(OUT / d / "region_map.json", '"fixture"', '"s06_printability/a1/figure.ply"')
        pv = job / "s09_paint" / "a2" / "preview"
        for name in ("toolpaths.json", "toolpaths.bin"):
            shutil.copy(pv / name, OUT / "edit" / name)
        for p in (pv / "proof").glob("*.jpg"):
            shutil.copy(p, OUT / "edit" / "proof" / p.name)
        (OUT / "edit" / "paint_preview.json").write_text(after.model_dump_json(indent=1))
    print(json.dumps({"faces": mesh.n_faces, "regions": len(regions), "ops": len(ops),
                      "changed_faces": res.changed_faces, "before": before.after.model_dump(
                          include={"print_time_s", "total_grams", "purge_grams"}),
                      "after": after.after.model_dump(
                          include={"print_time_s", "total_grams", "purge_grams"}),
                      "timings": after.timings_s}, indent=1))


if __name__ == "__main__":
    main()
