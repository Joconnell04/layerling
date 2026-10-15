import numpy as np
import trimesh
from p2p_sim.mesh import (
    GLTF_TO_CONTRACT,
    Mesh,
    load_glb,
    load_mesh,
    save_glb,
    save_ply,
    to_contract_frame,
)
from PIL import Image


def test_gltf_axes_map_to_contract_frame():
    # glTF: +Y up, +Z front. Contract: +Z up, -Y front.
    up, front, right = np.eye(3)[1], np.eye(3)[2], np.eye(3)[0]
    assert np.allclose(GLTF_TO_CONTRACT @ up, [0, 0, 1])
    assert np.allclose(GLTF_TO_CONTRACT @ front, [0, -1, 0])
    assert np.allclose(GLTF_TO_CONTRACT @ right, [1, 0, 0])
    assert np.isclose(np.linalg.det(GLTF_TO_CONTRACT), 1.0)  # rotation, no mirror


def test_to_contract_frame_seats_and_scales():
    rng = np.random.default_rng(0)
    v = rng.normal(size=(500, 3)) + [3.0, 5.0, -2.0]
    out = to_contract_frame(v, target_size_mm=40.0)
    lo, hi = out.min(0), out.max(0)
    assert np.isclose(lo[2], 0.0)
    assert np.allclose((lo[:2] + hi[:2]) / 2, 0.0, atol=1e-9)
    assert np.isclose((hi - lo).max(), 40.0)
    # meters -> mm when no target size
    out_m = to_contract_frame(v)
    assert np.isclose((out_m.max(0) - out_m.min(0)).max(), (v.max(0) - v.min(0)).max() * 1000)


def _gltf_marker_mesh():
    """A glTF-frame box body with a small nose box on its front (+Z) and a hat on top (+Y)."""
    body = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
    nose = trimesh.creation.box(extents=(0.2, 0.2, 0.2))
    nose.apply_translation((0, 0, 0.6))
    hat = trimesh.creation.box(extents=(0.3, 0.3, 0.3))
    hat.apply_translation((0, 0.65, 0))
    return trimesh.util.concatenate([body, nose, hat])


def test_load_glb_front_and_up(tmp_path):
    tm = _gltf_marker_mesh()
    p = tmp_path / "m.glb"
    p.write_bytes(tm.export(file_type="glb"))
    m = load_glb(p, target_size_mm=50)
    c = m.face_centroids
    # nose faces are the 12 faces of the second box, hat the last 12
    nose, hat = c[12:24].mean(0), c[24:36].mean(0)
    body = c[:12].mean(0)
    assert nose[1] < body[1] - 5  # front is -Y
    assert hat[2] > body[2] + 5  # up is +Z
    assert np.isclose(m.bounds[0, 2], 0.0)


def test_glb_round_trip_keeps_order_uv_and_texture(tmp_path, fig50k):
    m = fig50k.mesh
    p = save_glb(m, tmp_path / "fig.glb")
    back = load_glb(p)
    assert back.n_faces == m.n_faces
    assert np.allclose(back.vertices, m.vertices, atol=1e-3)
    assert np.array_equal(back.faces, m.faces)
    assert back.texture is not None and back.texture.shape == m.texture.shape
    assert np.allclose(back.uv, m.uv, atol=1e-6)
    # face colors from the texture equal the ground-truth paint on nearly every face
    fc = back.face_texture_colors()
    assert (fc == fig50k.face_colors).all(axis=1).mean() > 0.97


def test_ply_round_trip_keeps_face_order(tmp_path, ico):
    rng = np.random.default_rng(1)
    faces = ico.faces[rng.permutation(ico.n_faces)]
    m = Mesh(ico.vertices, faces)
    colors = rng.integers(0, 255, size=(m.n_faces, 3), dtype=np.uint8)
    p = save_ply(m, tmp_path / "m.ply", face_colors=colors)
    back = load_mesh(p)
    assert np.array_equal(back.faces, m.faces)
    assert np.allclose(back.vertices, m.vertices)


def test_adjacency_crosses_uv_seams(ico):
    split = Mesh(ico.triangles.reshape(-1, 3), np.arange(ico.n_faces * 3).reshape(-1, 3))
    assert split.n_vertices == 3 * ico.n_faces
    assert len(split.face_adjacency) == len(ico.face_adjacency) == ico.n_faces * 3 // 2
    assert split.is_watertight
    assert np.isclose(split.volume_mm3, ico.volume_mm3)


def test_non_manifold_and_open_meshes_are_tolerated():
    # three triangles sharing one edge (non-manifold) plus an open border
    v = np.array([[0, 0, 0], [1, 0, 0], [0.5, 1, 0], [0.5, -1, 0], [0.5, 0, 1]], float)
    f = np.array([[0, 1, 2], [1, 0, 3], [0, 1, 4]])
    m = Mesh(v, f)
    assert not m.is_watertight
    assert len(m.face_adjacency) >= 2
    assert m.vertex_normals.shape == (5, 3)


def test_texture_sampling_orientation():
    # 2x2 texture: top row red/green, bottom row blue/white. OpenGL v-up: v=1 is the top row.
    tex = np.array([[[255, 0, 0], [0, 255, 0]], [[0, 0, 255], [255, 255, 255]]], np.uint8)
    from p2p_sim.mesh import sample_texture

    uv = np.array([[0.25, 0.75], [0.75, 0.75], [0.25, 0.25], [0.75, 0.25]], np.float32)
    got = sample_texture(tex, uv, bilinear=False)
    assert got.tolist() == tex.reshape(-1, 3).tolist()
    Image.fromarray(tex)  # sanity: valid image array
