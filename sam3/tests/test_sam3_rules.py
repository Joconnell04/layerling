"""Post-vote rules from the proof run: nose and mouth caps, head fallback, feet, collar.

Views here use a "one pixel per face" buffer: a (1, N) face-ID row listing the faces a camera
sees, so masks pick faces directly without a renderer.
"""

import numpy as np
from p2p_contracts.regions import Region
from p2p_sam3.backproject import LabelMask, ViewLabels, backproject, face_geometry
from p2p_sam3.color import srgb_to_lab
from p2p_sam3.markings import split_markings
from p2p_sam3.rules import foot_groups, foot_names, subject_frame

# ---------------------------------------------------------------- a subdivided box


def box(n=10, size=(20.0, 20.0, 20.0)):
    """Box [0, sx] x [0, sy] x [0, sz], each side an n x n grid, outward winding.
    Returns (vertices, faces, side) with side in -x +x -y +y -z +z order (0..5)."""
    verts, faces, side = [], [], []
    for axis in range(3):
        for s in (0, 1):
            a, b = [i for i in range(3) if i != axis]
            uu, vv = np.meshgrid(np.linspace(0, size[a], n + 1), np.linspace(0, size[b], n + 1))
            p = np.zeros((uu.size, 3))
            p[:, a], p[:, b], p[:, axis] = uu.ravel(), vv.ravel(), s * size[axis]
            base = sum(len(x) for x in verts)
            verts.append(p)
            for j in range(n):
                for i in range(n):
                    q = base + j * (n + 1) + i
                    faces += [(q, q + 1, q + n + 2), (q, q + n + 2, q + n + 1)]
                    side += [2 * axis + s] * 2
    v, f = np.vstack(verts), np.array(faces, np.int64)
    c = np.asarray(size) / 2
    tri = v[f]
    flip = (np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]) * (tri.mean(1) - c)).sum(1) < 0
    f[flip] = f[flip][:, ::-1]
    return v, f, np.array(side)


def view(visible, masks, direction):
    """One-pixel-per-face view of `visible` faces; masks = [(part, face set, score, subject)]."""
    fid = np.asarray(visible, np.int32)[None, :]
    lm = [LabelMask(p, np.isin(fid, list(fs)), s, subj) for p, fs, s, subj in masks]
    return ViewLabels(fid, lm, view_dir=direction)


def faces_where(cent, side, s, **ranges):
    m = side == s
    for axis, (lo, hi) in ranges.items():
        k = "xyz".index(axis)
        m &= (cent[:, k] >= lo) & (cent[:, k] <= hi)
    return set(np.flatnonzero(m).tolist())


# ---------------------------------------------------------------- caps and head


def test_rear_nose_loses_to_the_one_by_the_eyes_and_one_mouth_is_kept():
    """The back view calls a big patch on the rear "nose" (the Frenchie in proof run 1)."""
    v, f, side = box()
    c, _, _ = face_geometry(v, f)
    front, back = np.flatnonzero(side == 2), np.flatnonzero(side == 3)
    # Ranges on the 2 mm cell grid, so every part is one connected piece.
    eyes = faces_where(c, side, 2, x=(2, 6), z=(12, 16)) | faces_where(c, side, 2, x=(14, 18),
                                                                           z=(12, 16))  # fmt: skip
    nose = faces_where(c, side, 2, x=(8, 12), z=(8, 10))
    mouth = faces_where(c, side, 2, x=(8, 12), z=(4, 6))
    mouth_far = faces_where(c, side, 3, x=(8, 12), z=(16, 18))  # back of the neck
    rear_nose = faces_where(c, side, 3, x=(4, 16), z=(2, 12))  # 7x bigger than the real nose
    everything = set(range(len(f)))
    views = [
        view(front, [("eye", eyes, 0.9, "s1"), ("nose", nose, 0.9, "s1"),
                     ("mouth", mouth, 0.6, "s1"), ("body", everything, 0.9, "s1")], (0, -1, 0)),
        view(back, [("nose", rear_nose, 0.85, "s1"), ("mouth", mouth_far, 0.6, "s1"),
                    ("body", everything, 0.9, "s1")], (0, 1, 0)),
    ]  # fmt: skip
    res = backproject(v, f, views, fill_unseen=False, head_fallback=False)
    by = {r.label: r for r in res.regions}
    assert [lbl for lbl in by if "nose" in lbl or "mouth" in lbl] == ["s1.mouth", "s1.nose"]
    assert by["s1.nose"].centroid_mm[1] < 1 and by["s1.mouth"].centroid_mm[1] < 1  # front side
    assert res.stats["dropped_extra_components"] == 2


def test_head_fallback_fills_the_face_between_side_heads_and_stops_at_the_collar():
    """SAM 3 scored "head" only from the sides; the face (front view) was body."""
    v, f, side = box()
    c, _, _ = face_geometry(v, f)
    eyes = faces_where(c, side, 2, x=(2, 6), z=(14, 18)) | faces_where(c, side, 2, x=(14, 18),
                                                                           z=(14, 18))  # fmt: skip
    nose = faces_where(c, side, 2, x=(8, 12), z=(10, 12))
    heads = faces_where(c, side, 0, y=(0, 12), z=(10, 20)) | faces_where(c, side, 1, y=(0, 12),
                                                                             z=(10, 20))  # fmt: skip
    collar = set().union(*(faces_where(c, side, s, z=(6, 8)) for s in (0, 1, 2)))
    everything = set(range(len(f)))
    masks = [("eye", eyes, 0.9, "s1"), ("nose", nose, 0.9, "s1"), ("head", heads, 0.8, "s1"),
             ("collar", collar, 0.9, "s1"), ("body", everything, 0.9, "s1")]  # fmt: skip
    vis = np.flatnonzero(np.isin(side, [0, 1, 2]))
    views = [view(vis, masks, (0, -1, 0))]
    off = backproject(v, f, views, fill_unseen=False, head_fallback=False)
    on = backproject(v, f, views, fill_unseen=False)
    face_between = faces_where(c, side, 2, x=(8, 12), z=(14, 18))  # forehead between the eyes
    below_collar = faces_where(c, side, 2, x=(8, 12), z=(0, 4))

    def label_of(res, faces):
        ids = {int(res.face_regions[i]) for i in faces}
        return {r.label for r in res.regions if r.id in ids}

    assert label_of(off, face_between) <= {"s1.body", "s1.body_1", "s1.body_2"}
    assert label_of(on, face_between) == {"s1.head"}
    assert all(lbl.startswith("s1.body") for lbl in label_of(on, below_collar))
    assert on.stats["head_fallback_faces"] > 0
    assert [r.label for r in on.regions if r.part == "head"] == ["s1.head"]  # one region


# ---------------------------------------------------------------- feet


def test_floating_pieces_join_their_foot_but_close_feet_on_the_ground_stay_apart():
    # Four feet on the ground (two only 4 mm apart, like a sitting dog), an ankle piece 4 mm
    # above one of them, and a speck 4 mm from another.
    def blob(x, y, z, r=3.0, n=30):
        t = np.linspace(0, 2 * np.pi, n, endpoint=False)
        return np.stack([x + r * np.cos(t), y + r * np.sin(t), np.full(n, z)], 1)

    pts = [blob(10, -10, 0), blob(-10, -10, 0), blob(10, 10, 0), blob(-0.0, 10, 0),
           blob(10, -10, 4, r=2), blob(-10, 10, 0, r=0.2, n=4)]  # fmt: skip
    cent = np.vstack(pts)
    groups, start = [], 0
    for p in pts:
        groups.append(np.arange(start, start + len(p)))
        start += len(p)
    areas = np.ones(len(cent))
    areas[groups[5]] = 0.05
    feet = foot_groups(groups, cent, areas, gap_mm=5.0, ground_tol_mm=2.0)
    assert sorted(sorted(m) for m in feet) == [[0, 4], [1], [2], [3, 5]]


def test_foot_names_use_front_back_split_by_area():
    # Five feet in the subject frame (x' = left, y' = back): the tiny one must not set the split.
    cents = np.array([[-5, -14, 0], [10, -13, 0], [7, 3, 0], [-15, 0, 0], [-10, 17, 0]], float)
    names = foot_names(cents, center_x=0.0, weights=np.array([300, 270, 300, 130, 7]))
    assert names[:2] == ["_front_right", "_front_left"]
    assert all(nm.startswith("_back") for nm in names[2:])
    four = foot_names(cents[:4], 0.0)
    assert sorted(four) == ["_back_left", "_back_right", "_front_left", "_front_right"]


def test_subject_frame_follows_a_turned_subject():
    keys = [("s1", "nose"), ("s1", "tail"), ("s1", "body")]
    cent = np.array([[-10.0, -10.0, 5.0], [10.0, 10.0, 5.0], [0.0, 0.0, 5.0]])
    label = np.array([0, 1, 2])
    fwd, left = subject_frame(label, keys, "s1", np.ones(3), cent)
    np.testing.assert_allclose(fwd, [-(0.5**0.5), -(0.5**0.5)])
    np.testing.assert_allclose(left, [0.5**0.5, -(0.5**0.5)])
    # Facing -Y: the contract's convention (left is +X).
    cent2 = np.array([[0.0, -10.0, 5.0], [0.0, 10.0, 5.0], [0.0, 0.0, 5.0]])
    fwd, left = subject_frame(label, keys, "s1", np.ones(3), cent2)
    np.testing.assert_allclose(fwd, [0, -1])
    np.testing.assert_allclose(left, [1, 0])


# ---------------------------------------------------------------- collar


def region(rid, part, subject="s1"):
    return Region(id=rid, label=f"{subject}.{part}", subject_id=subject, part=part,
                  source="sam3", confidence=0.9, n_faces=1, area_mm2=1.0, centroid_mm=(0, 0, 0),
                  bbox_min_mm=(0, 0, 0), bbox_max_mm=(1, 1, 1))  # fmt: skip


def test_dark_band_touching_the_collar_joins_it_instead_of_becoming_a_marking():
    v, f, side = box(12, (20.0, 20.0, 36.0))
    c, _, _ = face_geometry(v, f)
    white, black = srgb_to_lab((235, 235, 232)), srgb_to_lab((15, 15, 15))
    lab = np.tile(white, (len(f), 1))
    band = (c[:, 2] > 18) & (c[:, 2] < 24) & np.isin(side, [0, 1, 2, 3])
    lab[band] = black
    fr = np.full(len(f), 1, np.int32)
    collar = (c[:, 2] >= 24) & (c[:, 2] < 27) & np.isin(side, [0, 1, 2, 3])
    fr[collar] = 2
    lab[collar] = black
    with_collar = split_markings(v, f, fr, [region(1, "body"), region(2, "collar")], lab)
    assert [r.label for r in with_collar.regions] == ["s1.body", "s1.collar"]
    assert (with_collar.face_regions[band] == 2).all()
    assert with_collar.stats["absorbed_faces"] == {2: int(band.sum())}
    no_collar = split_markings(
        v, f, np.where(collar, 1, fr), [region(1, "body")], lab, absorb_parts=()
    )
    assert any(r.label.startswith("s1.marking_black") for r in no_collar.regions)
