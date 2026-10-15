"""Faces the geometry stage adds (the plinth cap, hole fills) take their neighbours' paint."""

from pathlib import Path

import numpy as np
import pytest
from p2p_threemf.retarget import fill_from_neighbours

MESHY_3MF = Path(__file__).resolve().parents[2] / ("data/fixtures/meshy/04_multi_color/"
                 "meshy_multicolor.3mf")


def _grid(n: int):
    """n x n quad grid split into triangles; returns faces and each face's cell (i, j)."""
    idx = np.arange((n + 1) ** 2).reshape(n + 1, n + 1)
    faces, cells = [], []
    for i in range(n):
        for j in range(n):
            a, b, c, d = idx[i, j], idx[i + 1, j], idx[i + 1, j + 1], idx[i, j + 1]
            faces += [[a, b, c], [a, c, d]]
            cells += [(i, j), (i, j)]
    return np.array(faces), np.array(cells)


def test_masked_faces_take_the_neighbour_majority_and_flood_inward():
    faces, cells = _grid(12)
    slots = np.full(len(faces), 2)
    inner = (cells[:, 0] >= 3) & (cells[:, 0] < 9) & (cells[:, 1] >= 3) & (cells[:, 1] < 9)
    slots[inner] = np.where(np.arange(inner.sum()) % 2, 1, 5)  # stripes, like the plinth cap
    out, n = fill_from_neighbours(faces, slots, inner)
    assert n == inner.sum()
    assert (out == 2).all()  # interior faces two rings in got it by flooding
    assert (out[~inner] == slots[~inner]).all()  # nothing else touched


def test_ties_go_to_the_lower_slot():
    faces = np.array([[0, 1, 2], [1, 3, 2], [0, 2, 4]])  # face 0 borders faces 1 and 2
    out, _ = fill_from_neighbours(faces, np.array([9, 5, 3]), np.array([True, False, False]))
    assert out[0] == 3


@pytest.fixture(scope="module")
def keychain_3mf(beagle, library, tmp_path_factory):
    if not MESHY_3MF.exists():
        pytest.skip("Meshy multicolor 3MF not on this machine")
    from p2p_contracts.job import PrintSettings

    from p2p_geometry import build_product_geometry
    from p2p_geometry.output import write_outputs
    from p2p_geometry.production_slice import production_3mf

    tmp = tmp_path_factory.mktemp("capfill")
    geo = build_product_geometry(beagle.vertices, beagle.faces, "keychain", [], None, library,
                                 None, size_tier="m", form="flat")
    job = tmp / "job"
    res = write_outputs(geo, job / "s06_printability" / "a1", job_dir=job)
    out = tmp / "keychain.3mf"
    wr = production_3mf(MESHY_3MF, geo, res, job, beagle.vertices, beagle.faces, library,
                        PrintSettings(), out)
    return out, np.load(job / res.face_index_map.path), wr


def test_plinth_cap_is_not_striped(keychain_3mf):
    from p2p_threemf.reader import read_3mf_full

    path, fim, _ = keychain_3mf
    m = read_3mf_full(path)
    n = len(fim)
    faces, slots = m.faces[:n] - m.faces[:n].min(), m.face_slots[:n]
    cap = fim < 0
    assert cap.sum() > 1000
    # every cap face agrees with the majority of its neighbours (a striped cap would not)
    agree = fill_from_neighbours(faces, slots, cap)[0]
    assert (agree[cap] == slots[cap]).mean() > 0.99
    assert len(np.unique(slots[cap])) <= 2  # the soles of the paws and the tail, not noise
