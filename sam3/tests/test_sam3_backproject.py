"""Back-projection on synthetic face-ID buffers (no renderer needed)."""

import json
import random

import numpy as np
import pytest
from p2p_contracts.regions import MeshRef, RegionMap
from p2p_sam3.backproject import LabelMask, ViewLabels, backproject, masks_from_result
from p2p_sam3.mock import MockSegmenter

FRONT = (0.0, -1.0, 0.0)  # model front faces -Y; the grid below faces -Y
OBLIQUE = (0.0, -1.0, -(3**0.5))  # cos = 0.5 against the grid normal


def grid_mesh(nx=10, nz=10, x0=0.0):
    """Flat grid in the XZ plane at y = 0, 1 mm cells, normals -Y. Cell (i, j): x in [i, i+1],
    z in [j, j+1]; its triangles are 2 * (j * nx + i) and that + 1."""
    xs, zs = np.meshgrid(np.arange(nx + 1, dtype=float), np.arange(nz + 1, dtype=float))
    verts = np.stack([xs.ravel() + x0, np.zeros(xs.size), zs.ravel()], axis=1)

    def v(i, j):
        return j * (nx + 1) + i

    faces = []
    for j in range(nz):
        for i in range(nx):
            faces.append((v(i, j), v(i + 1, j), v(i + 1, j + 1)))
            faces.append((v(i, j), v(i + 1, j + 1), v(i, j + 1)))
    return verts, np.array(faces, np.int64)


def front_buffer(nx=10, nz=10, hidden_cols=()):
    """2 x 2 pixels per cell; top pixel row shows triangle 1, bottom row triangle 0."""
    fid = np.full((2 * nz, 2 * nx), -1, np.int32)
    for r in range(2 * nz):
        j = nz - 1 - r // 2
        for c in range(2 * nx):
            i = c // 2
            if i in hidden_cols:
                continue
            fid[r, c] = 2 * (j * nx + i) + (1 if r % 2 == 0 else 0)
    return fid


def cells_mask(i0, i1, j0, j1, nx=10, nz=10):
    m = np.zeros((2 * nz, 2 * nx), bool)
    for j in range(j0, j1):
        r = 2 * (nz - 1 - j)
        m[r : r + 2, 2 * i0 : 2 * i1] = True
    return m


def cell_faces(i0, i1, j0, j1, nx=10):
    return sorted(
        2 * (j * nx + i) + t for j in range(j0, j1) for i in range(i0, i1) for t in (0, 1)
    )


def region(res, label):
    return next(r for r in res.regions if r.label == label)


def faces_of(res, label):
    return sorted(np.flatnonzero(res.face_regions == region(res, label).id).tolist())


ALL = cells_mask(0, 10, 0, 10)


def test_two_views_specific_part_wins_and_stats():
    verts, faces = grid_mesh()
    fid = front_buffer()
    front = ViewLabels(
        fid,
        [LabelMask("head", ALL, 0.8), LabelMask("eye", cells_mask(2, 4, 6, 8), 0.9)],
        view_dir=FRONT,
    )
    oblique = ViewLabels(fid, [LabelMask("head", ALL, 0.8)], view_dir=OBLIQUE)
    res = backproject(verts, faces, [front, oblique])

    # Eye share on its faces = 1 / (1 + 0.5) = 0.667 >= 0.5 and conf 0.6 >= 0.35: eye beats head.
    assert faces_of(res, "eye") == cell_faces(2, 4, 6, 8)
    k_eye = res.label_keys.index((None, "eye"))
    assert res.share[cell_faces(2, 4, 6, 8), k_eye] == pytest.approx(2 / 3, abs=1e-6)
    eye = region(res, "eye")
    assert eye.n_faces == 8 and eye.area_mm2 == pytest.approx(4.0)
    assert eye.centroid_mm == pytest.approx((3.0, 0.0, 7.0))
    assert eye.bbox_min_mm == (2.0, 0.0, 6.0) and eye.bbox_max_mm == (4.0, 0.0, 8.0)
    assert eye.confidence == pytest.approx(0.6, abs=1e-3)
    assert eye.views_seen == 1 and eye.source == "sam3" and eye.part == "eye"
    assert eye.min_width_mm is None
    head = region(res, "head")
    assert head.n_faces == 200 - 8 and head.views_seen == 2
    assert sorted(r.id for r in res.regions) == [1, 2] and res.next_region_id == 3
    assert res.face_regions.dtype == np.int32 and (res.face_regions > 0).all()
    assert res.stats["seen_faces"] == 200 and res.stats["filled_faces"] == 0


def test_label_from_one_grazing_view_is_outvoted():
    verts, faces = grid_mesh()
    fid = front_buffer()
    front = ViewLabels(fid, [LabelMask("head", ALL, 0.8)], view_dir=FRONT)
    oblique = ViewLabels(
        fid,
        [LabelMask("head", ALL, 0.8), LabelMask("nose", cells_mask(4, 6, 2, 4), 0.95)],
        view_dir=OBLIQUE,
    )
    res = backproject(verts, faces, [front, oblique])
    assert [r.label for r in res.regions] == ["head"]  # nose share 0.5 / 1.5 = 0.33 < 0.5
    # Passing the vote is not enough: conf 0.33 x 0.95 = 0.32 is under 0.35, so head keeps it.
    res = backproject(verts, faces, [front, oblique], min_vote=0.3)
    assert [r.label for r in res.regions] == ["head"]
    res = backproject(verts, faces, [front, oblique], min_vote=0.3, min_specific_confidence=0.3)
    assert faces_of(res, "nose") == cell_faces(4, 6, 2, 4)


def test_unconfident_specific_part_loses():
    verts, faces = grid_mesh()
    fid = front_buffer()
    view = ViewLabels(
        fid,
        [LabelMask("body", ALL, 0.9), LabelMask("eye", cells_mask(0, 2, 0, 2), 0.3)],
        view_dir=FRONT,
    )
    res = backproject(verts, faces, [view])  # eye conf 0.3 < 0.35: body keeps it
    assert [r.label for r in res.regions] == ["body"]
    res = backproject(verts, faces, [view], min_specific_confidence=0.2)
    assert "eye" in [r.label for r in res.regions]


def test_paired_parts_named_left_right_and_subjects():
    verts, faces = grid_mesh()
    fid = front_buffer()
    view = ViewLabels(
        fid,
        [
            LabelMask("eye", cells_mask(7, 9, 6, 7), 0.9, subject_id="s1"),
            LabelMask("eye", cells_mask(1, 3, 6, 7), 0.8, subject_id="s1"),
            LabelMask("ear", cells_mask(4, 6, 0, 1), 0.9, subject_id="s1"),
            LabelMask("eye", cells_mask(4, 6, 3, 4), 0.9, subject_id="s2"),
            LabelMask("eye", cells_mask(4, 6, 3, 4), 0.6, subject_id="s1"),
        ],
        view_dir=FRONT,
    )
    res = backproject(verts, faces, [view])
    labels = [r.label for r in res.regions]
    assert labels == ["s1.ear", "s1.eye_left", "s1.eye_right", "s2.eye"]
    # Subject's own sides: its left is +X, the eye at larger x.
    assert faces_of(res, "s1.eye_left") == cell_faces(7, 9, 6, 7)
    assert faces_of(res, "s1.eye_right") == cell_faces(1, 3, 6, 7)
    # Same rank, s2 is more confident on the shared cells.
    assert faces_of(res, "s2.eye") == cell_faces(4, 6, 3, 4)
    assert region(res, "s2.eye").subject_id == "s2"
    # Faces nobody labeled stay 0.
    assert (res.face_regions == 0).sum() == 200 - 16


def test_left_right_are_the_subjects_own_sides():
    """Region labels use anatomical sides: front faces -Y, so the subject's left is +X.

    From the front the viewer sees +X on the image's right, so the eye on the image's left is
    the subject's RIGHT eye. Seen from behind (image mirrored) the names must not change.
    """
    verts, faces = grid_mesh()
    fid = front_buffer()
    image_left, image_right = cells_mask(1, 3, 6, 7), cells_mask(7, 9, 6, 7)
    front = ViewLabels(
        fid,
        [LabelMask("eye", image_left, 0.9), LabelMask("eye", image_right, 0.9)],
        view_dir=FRONT,
    )
    res = backproject(verts, faces, [front])
    assert [r.label for r in res.regions] == ["eye_left", "eye_right"]
    left, right = region(res, "eye_left"), region(res, "eye_right")
    assert left.centroid_mm[0] > right.centroid_mm[0]
    assert faces_of(res, "eye_right") == cell_faces(1, 3, 6, 7)  # the viewer's left

    # Same faces through a mirrored buffer (camera behind, +X on the image's left).
    back = ViewLabels(
        fid[:, ::-1],
        [LabelMask("eye", image_left[:, ::-1] | image_right[:, ::-1], 0.9)],
        view_dir=(0.0, 1.0, 0.0),
    )
    res_back = backproject(verts, faces, [back])
    assert faces_of(res_back, "eye_left") == faces_of(res, "eye_left")
    assert faces_of(res_back, "eye_right") == faces_of(res, "eye_right")


def test_unseen_faces_filled_from_neighbors():
    verts_a, faces_a = grid_mesh()
    verts_b, faces_b = grid_mesh(x0=100.0)  # separate island no view sees
    verts = np.concatenate([verts_a, verts_b])
    faces = np.concatenate([faces_a, faces_b + len(verts_a)])
    fid = front_buffer(hidden_cols=(8, 9))
    view = ViewLabels(
        fid,
        [LabelMask("head", ALL, 0.9), LabelMask("eye", cells_mask(6, 8, 4, 6), 0.9)],
        view_dir=FRONT,
    )
    res = backproject(verts, faces, [view, ViewLabels(fid, [], view_dir=OBLIQUE)])
    assert res.stats["seen_faces"] == 160 and res.stats["filled_faces"] == 40
    # The hidden cells next to the eye become eye; the rest of the hidden strip becomes head.
    assert faces_of(res, "eye") == cell_faces(6, 10, 4, 6)
    assert len(faces_of(res, "head")) == 200 - 16
    assert (res.face_regions[200:] == 0).all()  # unreachable island stays unlabeled
    assert not res.filled[200:].any()
    res_nofill = backproject(verts, faces, [view], fill_unseen=False)
    assert faces_of(res_nofill, "eye") == cell_faces(6, 8, 4, 6)
    assert (res_nofill.face_regions[cell_faces(8, 10, 0, 10)] == 0).all()


def test_small_islands_absorbed():
    verts, faces = grid_mesh()
    fid = front_buffer()
    view = ViewLabels(
        fid,
        [
            LabelMask("head", ALL, 0.9),
            LabelMask("marking", cells_mask(5, 6, 5, 6), 0.9),
            LabelMask("marking", cells_mask(0, 3, 0, 3), 0.9),
        ],
        view_dir=FRONT,
    )
    res = backproject(verts, faces, [view])
    assert sorted(r.label for r in res.regions) == ["head", "marking_1", "marking_2"]
    assert region(res, "marking_1").area_mm2 == pytest.approx(9.0)  # numbered by area
    res = backproject(verts, faces, [view], min_component_area_mm2=2.0)
    assert sorted(r.label for r in res.regions) == ["head", "marking"]
    assert res.stats["filled_faces"] == 2


def test_camera_position_weighting():
    verts, faces = grid_mesh()
    fid = front_buffer()
    near = ViewLabels(
        fid, [LabelMask("eye", cells_mask(0, 5, 0, 10), 0.9)], camera_pos_mm=(5.0, -1000.0, 5.0)
    )
    graze = ViewLabels(fid, [], camera_pos_mm=(5.0, -10.0, 1000.0))  # nearly edge-on
    res = backproject(verts, faces, [near, graze])
    assert faces_of(res, "eye") == cell_faces(0, 5, 0, 10)


def test_deterministic_and_order_independent():
    verts, faces = grid_mesh()
    fid = front_buffer(hidden_cols=(0,))
    masks = [
        LabelMask("head", ALL, 0.8),
        LabelMask("eye", cells_mask(2, 4, 6, 8), 0.9),
        LabelMask("eye", cells_mask(6, 8, 6, 8), 0.9),
        LabelMask("nose", cells_mask(4, 6, 4, 6), 0.7),
        LabelMask("marking", cells_mask(3, 9, 0, 3), 0.75),
    ]
    views = [
        ViewLabels(fid, masks, view_dir=FRONT),
        ViewLabels(fid, masks[:3], view_dir=OBLIQUE),
    ]
    a = backproject(verts, faces, views)
    b = backproject(verts, faces, views)
    shuffled = masks[:]
    random.Random(4).shuffle(shuffled)
    c = backproject(verts, faces, [ViewLabels(fid, shuffled, view_dir=FRONT), views[1]])
    for other in (b, c):
        assert np.array_equal(a.face_regions, other.face_regions)
        assert [r.model_dump() for r in a.regions] == [r.model_dump() for r in other.regions]


def test_save_writes_region_map(tmp_path):
    verts, faces = grid_mesh()
    fid = front_buffer()
    res = backproject(
        verts, faces, [ViewLabels(fid, [LabelMask("eye", cells_mask(2, 4, 6, 8), 0.9)])]
    )
    mesh = MeshRef(
        path="s06_printability/a1/mesh.ply", sha256="0" * 64, n_faces=200, n_vertices=len(verts)
    )
    rmap = res.save(tmp_path / "labels", "s07_baseline/a1/labels", mesh)
    back = RegionMap.model_validate(json.loads((tmp_path / "labels/region_map.json").read_text()))
    assert back == rmap
    arr = np.load(tmp_path / "labels/face_regions.npy")
    assert arr.dtype == np.int32 and arr.shape == (200,)
    assert np.array_equal(arr, res.face_regions)
    assert back.face_regions.path == "s07_baseline/a1/labels/face_regions.npy"
    assert back.next_region_id == 2 and back.regions[0].label == "eye"


def test_masks_from_segment_result():
    img = np.full((20, 20, 3), 230, np.uint8)
    fixture = np.zeros((20, 20), bool)
    fixture[2:6, 2:6] = True
    seg = MockSegmenter(fixtures={(0, "tabby stripes"): [(fixture, 0.9)]}, use_heuristics=False)
    result = seg.segment([img], ["tabby stripes", "left eye"])
    masks = masks_from_result(result, 0)
    assert [(m.part, m.subject_id, m.score) for m in masks] == [("marking", None, 0.9)]
    assert np.array_equal(masks[0].mask, fixture)
    masks = masks_from_result(result, 0, {"tabby stripes": ("s2", "marking")})
    assert masks[0].subject_id == "s2"


def test_bad_inputs():
    verts, faces = grid_mesh()
    fid = front_buffer()
    with pytest.raises(ValueError, match="mask"):
        backproject(verts, faces, [ViewLabels(fid, [LabelMask("eye", np.ones((3, 3), bool))])])
    bad = fid.copy()
    bad[0, 0] = 500
    with pytest.raises(ValueError, match="out of range"):
        backproject(verts, faces, [ViewLabels(bad, [])])


def test_edge_speckle_cleaned_by_default():
    verts, faces = grid_mesh(20, 20)
    fid = front_buffer(20, 20)
    eyes = cells_mask(2, 8, 11, 17, 20, 20) | cells_mask(12, 18, 11, 17, 20, 20)  # 36 mm2 each
    speck = np.zeros_like(eyes)
    speck[0, 0:2] = True  # top pixel row of cell (0, 19): exactly one triangle, 0.5 mm2
    hole = np.zeros_like(eyes)
    hole[4, 10:12] = True  # one unlabeled triangle inside the head
    head = cells_mask(0, 20, 8, 20, 20, 20) & ~hole
    view = ViewLabels(
        fid, [LabelMask("head", head, 0.9), LabelMask("eye", eyes | speck, 0.9)], view_dir=FRONT
    )
    res = backproject(verts, faces, [view])
    assert sorted(r.label for r in res.regions) == ["eye_left", "eye_right", "head"]
    head_id = region(res, "head").id
    assert res.face_regions[2 * (19 * 20 + 0) + 1] == head_id  # speck joins its surroundings
    assert res.face_regions[2 * (17 * 20 + 5) + 1] == head_id  # hole filled
    raw = backproject(verts, faces, [view], min_component_frac=0.0)
    assert sorted(r.label for r in raw.regions) == ["eye_1", "eye_2", "eye_3", "head"]
    assert raw.face_regions[2 * (17 * 20 + 5) + 1] == 0


def test_extra_eyes_beyond_two_per_subject_are_dropped():
    """A false "eye" (weaker, smaller) on top of two real ones is refilled; with no cap it
    stays."""
    verts, faces = grid_mesh(20, 20)
    fid = front_buffer(20, 20)
    masks = [
        LabelMask("eye", cells_mask(4, 7, 14, 17, 20, 20), 0.92, "s1"),
        LabelMask("eye", cells_mask(13, 16, 14, 17, 20, 20), 0.92, "s1"),
        LabelMask("eye", cells_mask(9, 11, 9, 11, 20, 20), 0.58, "s1"),  # false
        LabelMask("head", cells_mask(0, 20, 8, 20, 20, 20), 0.9, "s1"),
    ]
    view = ViewLabels(fid, masks, view_dir=FRONT)
    res = backproject(verts, faces, [view])
    assert sorted(r.label for r in res.regions) == ["s1.eye_left", "s1.eye_right", "s1.head"]
    assert res.stats["dropped_extra_components"] == 1
    assert res.face_regions[2 * (9 * 20 + 9)] == region(res, "s1.head").id
    uncapped = backproject(verts, faces, [view], max_per_subject={})
    assert sorted(r.label for r in uncapped.regions) == [
        "s1.eye_1", "s1.eye_2", "s1.eye_3", "s1.head",
    ]  # fmt: skip


def test_four_paws_named_front_back_left_right():
    """Horizontal grid (x right, y toward the back) seen from above, four paw blobs."""
    n = 10
    xs, ys = np.meshgrid(np.arange(n + 1, dtype=float), np.arange(n + 1, dtype=float))
    verts = np.stack([xs.ravel(), ys.ravel(), np.zeros(xs.size)], axis=1)
    faces = []
    for j in range(n):
        for i in range(n):
            q = j * (n + 1) + i
            faces += [(q, q + 1, q + n + 2), (q, q + n + 2, q + n + 1)]
    faces = np.array(faces, np.int64)
    rows, cols = np.mgrid[: 2 * n, : 2 * n]
    fid = (2 * ((rows // 2) * n + cols // 2) + rows % 2).astype(np.int32)  # row = y, col = x

    def blob(i0, j0):
        m = np.zeros((2 * n, 2 * n), bool)
        m[2 * j0 : 2 * j0 + 4, 2 * i0 : 2 * i0 + 4] = True
        return m

    paws = [LabelMask("paw", blob(i, j), 0.9, "s1") for i, j in ((1, 1), (7, 1), (1, 7), (7, 7))]
    res = backproject(verts, faces, [ViewLabels(fid, paws, view_dir=(0, 0, 1))])
    cents = {r.label: tuple(round(x, 3) for x in r.centroid_mm[:2]) for r in res.regions}
    assert cents == {
        "s1.paw_front_left": (8.0, 2.0),  # small y is the front; +X is the subject's left
        "s1.paw_front_right": (2.0, 2.0),
        "s1.paw_back_left": (8.0, 8.0),
        "s1.paw_back_right": (2.0, 8.0),
    }
