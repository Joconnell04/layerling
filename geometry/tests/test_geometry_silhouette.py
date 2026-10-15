"""projected_silhouette: the net-edge loops give the same shadow as the union of every triangle."""

import numpy as np
import pytest
from manifold3d import CrossSection, FillRule, Manifold
from p2p_geometry.meshio import from_manifold, projected_silhouette


def union_of_triangles(vertices, faces) -> CrossSection:
    """The reference: every triangle turned counterclockwise, unioned (the old s06 code)."""
    tri = np.asarray(vertices)[np.asarray(faces)][:, :, :2]
    area = 0.5 * ((tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1])
                  - (tri[:, 2, 0] - tri[:, 0, 0]) * (tri[:, 1, 1] - tri[:, 0, 1]))
    keep = np.abs(area) > 1e-9
    tri = tri[keep]
    flip = area[keep] < 0
    tri[flip] = tri[flip][:, ::-1]
    return CrossSection([t for t in tri], FillRule.Positive)


def same_shadow(a: CrossSection, b: CrossSection, tol: float = 1e-9) -> None:
    scale = max(abs(a.area()), 1.0)
    assert abs(a.area() - b.area()) <= tol * scale
    assert (a - b).area() <= tol * scale
    assert (b - a).area() <= tol * scale
    assert np.allclose(a.bounds(), b.bounds(), atol=1e-9)


def blob() -> tuple[np.ndarray, np.ndarray]:
    """A closed, folded figure: three spheres and a torus, unioned, tilted so folds overlap."""
    m = (Manifold.sphere(10, 48) + Manifold.sphere(7, 40).translate([12, 3, 6])
         + Manifold.sphere(5, 32).translate([-9, -6, 11])
         + CrossSection.circle(2, 16).translate([8, 0]).revolve(40)
         .rotate([60, 20, 0]).translate([3, 14, -4]))
    v, f = from_manifold(m.rotate([23, -41, 17]))
    return v, f


def test_closed_mesh_matches_the_union():
    v, f = blob()
    same_shadow(projected_silhouette(v, f), union_of_triangles(v, f))


def test_open_mesh_and_a_subset_of_faces_match_the_union():
    v, f = blob()
    low = v[f][:, :, 2].min(axis=1) < np.percentile(v[:, 2], 40)  # a lower band, open
    same_shadow(projected_silhouette(v, f[low]), union_of_triangles(v, f[low]))


def test_soup_with_random_winding_and_split_vertices_matches_the_union():
    rng = np.random.default_rng(7)
    v, f = blob()
    f = f.copy()
    flip = rng.random(len(f)) < 0.5
    f[flip] = f[flip][:, ::-1]
    # UV seams: every face gets its own copy of its corners (same positions, new indices)
    soup_v = v[f].reshape(-1, 3)
    soup_f = np.arange(len(soup_v)).reshape(-1, 3)
    # plus loose overlapping triangles that belong to nothing
    extra = rng.normal(0, 8, size=(40, 3, 3))
    soup_v = np.concatenate([soup_v, extra.reshape(-1, 3)])
    soup_f = np.concatenate([soup_f, np.arange(len(extra) * 3).reshape(-1, 3) + len(soup_f) * 3])
    same_shadow(projected_silhouette(soup_v, soup_f), union_of_triangles(soup_v, soup_f))


def test_scaled_mesh_scales_the_shadow():
    v, f = blob()
    a = projected_silhouette(v, f).area()
    assert projected_silhouette(v * 2.5, f).area() == pytest.approx(a * 6.25, rel=1e-9)


def test_empty_and_degenerate_inputs_give_an_empty_shadow():
    assert projected_silhouette(np.zeros((0, 3)), np.zeros((0, 3), dtype=int)).area() == 0
    v = np.array([[0, 0, 0], [1, 0, 0], [2, 0, 0], [0, 0, 1]], dtype=float)
    assert projected_silhouette(v, np.array([[0, 1, 2], [0, 1, 3]])).area() == 0  # all flat


def test_two_triangles_sharing_an_edge_make_one_square():
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=float)
    cs = projected_silhouette(v, np.array([[0, 1, 2], [0, 2, 3]]))
    assert cs.area() == pytest.approx(1.0)
    assert len(cs.to_polygons()) == 1


def test_edge_table_keys_match_the_row_unique():
    """repair._edge_table keys each edge as one int64; same groups, order and counts as
    np.unique over the (lo, hi) rows it replaced."""
    from p2p_geometry.repair import _edge_table

    f = np.random.default_rng(3).integers(0, 4000, size=(30000, 3))
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    uniq, inv, counts = np.unique(np.sort(e, axis=1), axis=0, return_inverse=True,
                                  return_counts=True)
    e2, fid, uniq2, inv2, counts2 = _edge_table(f)
    assert np.array_equal(e, e2) and np.array_equal(fid, np.tile(np.arange(len(f)), 3))
    assert np.array_equal(uniq, uniq2) and np.array_equal(inv.reshape(-1), inv2)
    assert np.array_equal(counts, counts2)
