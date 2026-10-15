"""The pinned entry point writes geometry.json (GeometryResult) plus PLYs into the stage folder."""

import numpy as np
import trimesh
from geo_fixtures import PRODUCT_CASES
from p2p_contracts.geometry import GeometryResult
from p2p_contracts.job import PrintSettings
from p2p_geometry import build_product


def test_build_product_writes_contract_files(tmp_path, figure, library):
    out = tmp_path / "job1" / "s06_printability" / "a1"
    product, addons, target = PRODUCT_CASES["nameplate"]
    res = build_product(figure.vertices, figure.faces, product, addons, target, library,
                        PrintSettings(), out)
    assert isinstance(res, GeometryResult)
    again = GeometryResult.model_validate_json((out / "geometry.json").read_text())
    assert again == res
    assert (out / "geometry_report.json").exists()
    assert res.figure.mesh.path == "s06_printability/a1/figure.ply"
    assert res.figure.filament_id is None and res.figure.watertight
    assert {p.role for p in res.parts} == {"plate", "text"}
    for ref in [res.figure, *res.parts]:
        m = trimesh.load(tmp_path / "job1" / ref.mesh.path, process=False)
        assert m.is_watertight == ref.watertight
    # repair dropped the fin and filled a hole, so the map is written
    fim = np.load(tmp_path / "job1" / res.face_index_map.path)
    fig = trimesh.load(out / "figure.ply", process=False)
    assert len(fim) == len(fig.faces) and fim.dtype == np.int32
    assert np.array_equal(fim[: figure.fin_face], np.arange(figure.fin_face))
    t = np.array(res.transform)
    assert t.shape == (4, 4) and np.allclose(t[3], [0, 0, 0, 1])


def test_identity_face_map_is_not_written(tmp_path, library):
    box = trimesh.creation.box((1.0, 1.2, 0.8))
    res = build_product(box.vertices, box.faces, "figurine", [], 60.0, library, None, tmp_path)
    assert res.face_index_map is None
    assert res.parts[0].role == "base"
