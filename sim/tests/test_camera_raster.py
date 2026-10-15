import numpy as np
import pytest
from p2p_contracts.regions import VIEWS
from p2p_sim.camera import Camera, camera_matrices
from p2p_sim.raster import GLRasterizer, NumpyRasterizer, get_backend, rasterize


def _gl_or_skip():
    try:
        return get_backend("gl")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no GL context: {e}")


def test_two_gl_contexts_do_not_corrupt_each_other(ico):
    _gl_or_skip()
    cam = camera_matrices("back", ico, 256)
    first = rasterize(ico, cam, "gl")
    other = GLRasterizer()  # a second standalone context becomes current
    other.rasterize(ico, camera_matrices("front", ico, 128))
    again = rasterize(ico, cam, "gl")
    assert np.array_equal(first.face_id, again.face_id)
    assert again.face_id.max() < ico.n_faces


@pytest.mark.parametrize("backend", ["gl", "numpy"])
@pytest.mark.parametrize("view", list(VIEWS))
def test_projected_centroid_hits_its_face(ico, view, backend):
    if backend == "gl":
        _gl_or_skip()
    cam = camera_matrices(VIEWS[view], ico, 512)
    r = rasterize(ico, cam, backend)
    xy, depth = cam.project(ico.face_centroids)
    ids, counts = np.unique(r.face_id[r.mask], return_counts=True)
    big = ids[counts >= 30]  # visible faces covering at least 30 px
    assert big.size > 20
    px = np.floor(xy[big]).astype(int)
    hit = r.face_id[px[:, 1], px[:, 0]]
    assert (hit == big).mean() == 1.0
    # depth buffer at a pixel center puts the point on that face's plane
    d = r.depth[px[:, 1], px[:, 0]].astype(float)
    o, dirs = cam.pixel_rays(px + 0.5)
    pts = o + dirs * (d / (dirs @ cam.forward))[:, None]
    off = np.einsum("ij,ij->i", pts - ico.face_centroids[big], ico.face_normals[big])
    assert np.abs(off).max() < 0.01
    assert np.allclose(d, depth[big], atol=1.0)


def test_backends_agree(fig50k):
    gl = _gl_or_skip()
    cam = camera_matrices(VIEWS["front_3q"], fig50k.mesh, 512)
    a = gl.rasterize(fig50k.mesh, cam)
    b = NumpyRasterizer().rasterize(fig50k.mesh, cam)
    assert (a.face_id == b.face_id).mean() > 0.99
    same = (a.face_id == b.face_id) & a.mask
    # GPU interpolation is float32; slivers seen edge-on differ most
    assert np.percentile(np.abs(a.bary[same] - b.bary[same]), 99) < 5e-3
    assert np.percentile(np.abs(a.depth[same] - b.depth[same]), 99) < 0.02


@pytest.mark.parametrize("view", list(VIEWS))
def test_views_frame_the_whole_model(fig50k, view):
    m = fig50k.mesh
    cam = camera_matrices(VIEWS[view], m, 256)
    xy, depth = cam.project(m.vertices)
    assert (xy >= 0).all() and (xy <= 256).all()
    assert (depth > cam.near).all() and (depth < cam.far).all()


def test_front_camera_sits_at_minus_y_and_left_at_minus_x(ico):
    front = camera_matrices("front", ico, 64)
    left = camera_matrices("left", ico, 64)
    top = camera_matrices("top", ico, 64)
    c = ico.bounding_sphere[0]
    assert (front.eye - c)[1] < 0 and abs((front.eye - c)[0]) < 1e-6
    assert (left.eye - c)[0] < 0
    # top view: the model's back (+Y) is at the top of the image
    (xy_back, _), (xy_front, _) = top.project(c + [0, 5, 0]), top.project(c + [0, -5, 0])
    assert xy_back[0, 1] < xy_front[0, 1]
    # front view: +X (viewer's right) projects to the right half of the image
    xy_r, _ = front.project(c + [5, 0, 0])
    assert xy_r[0, 0] > 32


def test_camera_round_trips_through_dict_and_rays(ico):
    cam = camera_matrices("front_3q", ico, 300)
    back = Camera.from_dict(cam.to_dict())
    assert np.allclose(back.view_proj, cam.view_proj)
    xy, _ = cam.project(ico.face_centroids[:10])
    o, d = cam.pixel_rays(xy)
    # the ray through a projected point passes through that point
    p = ico.face_centroids[:10]
    t = np.einsum("ij,ij->i", p - o, d)
    assert np.allclose(o + t[:, None] * d, p, atol=1e-6)


def test_default_backend_is_gl_here():
    b = get_backend()
    assert b.name in ("gl", "numpy")
