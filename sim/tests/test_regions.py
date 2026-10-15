import numpy as np
import pytest
from p2p_contracts.manifest import FileRef
from p2p_contracts.regions import MeshRef, Region, RegionMap
from p2p_sim.fixtures import grid_plane
from p2p_sim.geodesic import distance_to_boundary
from p2p_sim.regions import fill_regions, min_widths, region_geometry, views_seen


def _strip(m, width, angle_deg=0.0):
    c = m.face_centroids
    a = np.radians(angle_deg)
    x = c[:, 0] * np.cos(a) + c[:, 1] * np.sin(a)
    return np.where(np.abs(x) < width / 2, 2, 1)


@pytest.mark.parametrize("width", [0.5, 1.2, 3.0])
@pytest.mark.parametrize("angle", [0.0, 30.0])
def test_min_width_on_known_strip(width, angle):
    m = grid_plane(20, 20, 0.1)
    w = min_widths(m, _strip(m, width, angle))[2]
    assert w == pytest.approx(width, rel=0.12)


def test_distance_field_is_straight_line_not_zigzag():
    m = grid_plane(20, 20, 0.1)
    lab = _strip(m, 1.2, 30.0)
    d, _, _ = distance_to_boundary(m, lab)
    assert d[lab == 2].max() == pytest.approx(0.6, abs=0.06)


def test_region_geometry_fields():
    m = grid_plane(10, 10, 0.1)
    lab = _strip(m, 2.0)
    g = region_geometry(m, lab)
    assert set(g) == {1, 2}
    s = g[2]
    assert s.area_mm2 == pytest.approx(2.0 * 10, rel=0.03)
    assert s.centroid_mm[0] == pytest.approx(0.0, abs=0.05)
    assert s.bbox_min_mm[1] == pytest.approx(-5.0) and s.bbox_max_mm[1] == pytest.approx(5.0)
    assert s.n_faces == int((lab == 2).sum())
    assert s.min_width_mm == pytest.approx(2.0, rel=0.1)


def test_region_without_boundary_has_no_width(ico):
    g = region_geometry(ico, np.full(ico.n_faces, 3))
    assert g[3].min_width_mm is None
    assert g[3].area_mm2 == pytest.approx(ico.surface_area_mm2)


def test_fixture_widths_are_meaningful(fig200k):
    """Jagged face-level boundaries must not collapse every width to the face size."""
    g = region_geometry(fig200k.mesh, fig200k.face_regions)
    lab = fig200k.labels
    by = {lab[k]: v.min_width_mm for k, v in g.items()}
    assert by["s1.stripe_1"] > 1.2  # drawn 1.6 mm wide
    assert by["s1.stripe_thin_1"] < 0.8  # drawn 0.5 mm wide
    assert by["s2.body"] > 0.5


def test_fill_regions_updates_region_map(fig50k):
    fr = fig50k.face_regions
    regs = [Region(id=rid, label=name, part="body", source="manual", n_faces=0, area_mm2=0,
                   centroid_mm=(0, 0, 0), bbox_min_mm=(0, 0, 0), bbox_max_mm=(0, 0, 0))
            for rid, name in fig50k.labels.items()]
    rm = RegionMap(
        mesh=MeshRef(path="m.ply", sha256="x", n_faces=fig50k.mesh.n_faces,
                     n_vertices=fig50k.mesh.n_vertices),
        face_regions=FileRef(path="face_regions.npy", sha256="y"),
        regions=regs, next_region_id=max(fig50k.labels) + 1,
    )
    out = fill_regions(rm, fig50k.mesh, fr)
    r16 = next(r for r in out.regions if r.id == 16)
    assert r16.n_faces == int((fr == 16).sum()) and r16.area_mm2 > 100
    assert r16.views_seen >= 4
    RegionMap.model_validate(out.model_dump())


def test_views_seen_counts(fig50k):
    vs = views_seen(fig50k.mesh, fig50k.face_regions)
    assert vs[1] == 6  # the base shows in every view
    assert vs[24] <= 4  # the tag hangs on the front
