"""label_regions: LabelRegionsIn fields reach the segmenter and back-projection."""

import json

import numpy as np
import pytest
from p2p_contracts.mcp_tools import LabelRegionsIn
from p2p_contracts.regions import MeshRef, RegionMap
from p2p_sam3.labeler import RenderedView, drop_small_components, label_regions
from p2p_sam3.mock import MockSegmenter

N = 20  # grid cells per side, 1 mm each; 2 x 2 pixels per cell


def grid():
    """Flat N x N grid in the XZ plane facing -Y, and its front face-ID buffer (2N x 2N px)."""
    xs, zs = np.meshgrid(np.arange(N + 1, dtype=float), np.arange(N + 1, dtype=float))
    verts = np.stack([xs.ravel(), np.zeros(xs.size), zs.ravel()], axis=1)
    faces = []
    for j in range(N):
        for i in range(N):
            v = j * (N + 1) + i
            faces += [(v, v + 1, v + N + 2), (v, v + N + 2, v + N + 1)]
    fid = np.full((2 * N, 2 * N), -1, np.int32)
    for r in range(2 * N):
        for c in range(2 * N):
            fid[r, c] = 2 * ((N - 1 - r // 2) * N + c // 2) + (1 if r % 2 == 0 else 0)
    return verts, np.array(faces, np.int64), fid


def cells(i0, i1, j0, j1):
    m = np.zeros((2 * N, 2 * N), bool)
    for j in range(j0, j1):
        r = 2 * (N - 1 - j)
        m[r : r + 2, 2 * i0 : 2 * i1] = True
    return m


BLACK = np.zeros((2 * N, 2 * N, 3), np.uint8)


def run(req, fixtures, views=None):
    verts, faces, fid = grid()
    views = views or [RenderedView("front", BLACK, fid, view_dir=(0, -1, 0))]
    seg = MockSegmenter(fixtures=fixtures, use_heuristics=False)
    return label_regions(req, verts, faces, views, segmenter=seg, subject_id="s1")


def test_score_threshold_reaches_the_segmenter():
    eyes = {(0, "eye"): [(cells(13, 16, 12, 15), 0.9), (cells(4, 7, 12, 15), 0.4)]}
    strict = run(LabelRegionsIn(prompts=["eye"], score_threshold=0.5), eyes)
    assert [r.label for r in strict.regions] == ["s1.eye"]
    assert strict.segments.images[0].get("eye").scores == [0.9]
    loose = run(LabelRegionsIn(prompts=["eye"], score_threshold=0.3), eyes)
    assert [r.label for r in loose.regions] == ["s1.eye_left", "s1.eye_right"]
    # Subject's left is +X: the 0.9 mask at larger x.
    assert loose.regions[0].centroid_mm[0] > loose.regions[1].centroid_mm[0]
    assert loose.stats["score_threshold"] == 0.3


def test_min_component_frac_drops_mask_speckle():
    blob = cells(5, 15, 5, 15)  # 100 cells, 400 px
    speck = np.zeros_like(blob)
    speck[0, 0] = True  # 1 px island, 0.25 % of the mask
    fx = {(0, "tail"): [(blob | speck, 0.9)]}
    kept = run(LabelRegionsIn(prompts=["tail"], min_component_frac=0.0), fx)
    assert sorted(r.label for r in kept.regions) == ["s1.tail_1", "s1.tail_2"]
    cleaned = run(LabelRegionsIn(prompts=["tail"]), fx)  # default 0.02
    assert [r.label for r in cleaned.regions] == ["s1.tail"]
    assert cleaned.regions[0].n_faces == 200


def test_drop_small_components():
    m = cells(0, 10, 0, 10)
    m[0, -1] = True
    assert drop_small_components(m, 0.02).sum() == m.sum() - 1
    assert drop_small_components(m, 0.0).sum() == m.sum()
    assert not drop_small_components(np.zeros((4, 4), bool), 0.5).any()


def test_views_filter_and_default_prompts(tmp_path):
    verts, faces, fid = grid()
    views = [
        RenderedView("front", BLACK, fid, view_dir=(0, -1, 0)),
        RenderedView("back", BLACK, fid[:, ::-1], view_dir=(0, 1, 0)),
    ]
    fx = {(0, "head"): [(cells(0, N, 10, N), 0.9)], (0, "body"): [(cells(0, N, 0, N), 0.8)]}
    seg = MockSegmenter(fixtures=fx, use_heuristics=False)
    res = label_regions(
        LabelRegionsIn(views=["front"]), verts, faces, views, segmenter=seg, subject_id="s1"
    )
    assert res.view_names == ["front"]
    assert len(res.segments.images) == 1
    assert "eye" in res.stats["prompts"] and "body" in res.stats["prompts"]
    assert sorted(r.label for r in res.regions) == ["s1.body", "s1.head"]
    mesh = MeshRef(path="m.ply", sha256="0" * 64, n_faces=len(faces), n_vertices=len(verts))
    rmap = res.save(tmp_path, "s07/a1", mesh)
    back = RegionMap.model_validate(json.loads((tmp_path / "region_map.json").read_text()))
    assert back == rmap
    assert np.array_equal(np.load(tmp_path / "face_regions.npy"), res.face_regions)
    with pytest.raises(ValueError, match="no rendered views"):
        label_regions(LabelRegionsIn(views=["top"]), verts, faces, views, segmenter=seg)
