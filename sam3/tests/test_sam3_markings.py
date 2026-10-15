"""Texture color markings: synthetic meshes for the rules, the real Meshy beagle end to end."""

from pathlib import Path

import numpy as np
import pytest
from p2p_contracts.mcp_tools import LabelRegionsIn
from p2p_contracts.regions import VIEWS, MeshRef, Region, RegionMap
from p2p_sam3.backproject import face_adjacency, face_geometry
from p2p_sam3.color import srgb_to_lab
from p2p_sam3.markings import split_markings

WHITE, BLACK, TAN = (235, 235, 232), (15, 14, 13), (165, 105, 60)
GRAY = (128, 128, 128)
RNG = np.random.default_rng(7)


def cube(n=8, size=20.0):
    """Cube [0, size]^3 as 6 separately meshed sides (vertices duplicated along the edges, like
    UV seams in a GLB), outward winding. Returns (vertices, faces, side (F,) in -x +x -y +y -z +z).
    """
    verts, faces, side = [], [], []
    t = np.linspace(0.0, size, n + 1)
    for axis in range(3):
        for s, val in ((0, 0.0), (1, size)):
            a, b = [i for i in range(3) if i != axis]
            uu, vv = np.meshgrid(t, t)
            p = np.zeros((uu.size, 3))
            p[:, a], p[:, b], p[:, axis] = uu.ravel(), vv.ravel(), val
            base = sum(len(x) for x in verts)
            verts.append(p)
            for j in range(n):
                for i in range(n):
                    q = base + j * (n + 1) + i
                    for tri in ((q, q + 1, q + n + 2), (q, q + n + 2, q + n + 1)):
                        faces.append(list(tri))
                        side.append(2 * axis + s)
    v = np.vstack(verts)
    f = np.array(faces, np.int64)
    c = np.full(3, size / 2)
    tri = v[f]
    nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    flip = (nrm * (tri.mean(axis=1) - c)).sum(axis=1) < 0
    f[flip] = f[flip][:, ::-1]
    return v, f, np.array(side)


def lab_of(rgb, n, noise=1.5):
    return srgb_to_lab(np.tile(np.asarray(rgb, float), (n, 1))) + RNG.normal(0, noise, (n, 3))


def host_region(rid, part, faces_idx, vertices, faces, subject="s1"):
    return Region(
        id=rid, label=f"{subject}.{part}", subject_id=subject, part=part, source="sam3",
        confidence=0.9, n_faces=len(faces_idx), area_mm2=1.0, centroid_mm=(0, 0, 0),
        bbox_min_mm=(0, 0, 0), bbox_max_mm=(1, 1, 1), views_seen=3,
    )  # fmt: skip


def test_welding_joins_seam_split_sides():
    v, f, _ = cube(4)
    centroids, _, _ = face_geometry(v, f)
    from scipy.sparse.csgraph import connected_components

    assert connected_components(face_adjacency(f, centroids), directed=False)[0] == 6
    assert connected_components(face_adjacency(f, centroids, v), directed=False)[0] == 1


def test_host_keeps_main_color_and_markings_are_named_by_color_and_position():
    v, f, side = cube()
    lab = lab_of(TAN, len(f))
    lab[side == 5] = lab_of(BLACK, (side == 5).sum())  # +Z top: the back
    lab[side == 2] = lab_of(WHITE, (side == 2).sum())  # -Y front: the chest
    fr = np.ones(len(f), np.int32)
    body = host_region(1, "body", np.arange(len(f)), v, f)
    res = split_markings(v, f, fr, [body], lab)
    labels = sorted(r.label for r in res.regions)
    assert labels == ["s1.body", "s1.marking_black_back", "s1.marking_white_chest"]
    assert res.region_colors[1]["name"] == "tan"
    by = {r.label: r for r in res.regions}
    for name, s in (("s1.marking_black_back", 5), ("s1.marking_white_chest", 2)):
        r = by[name]
        assert r.source == "texture_cluster" and r.part == "marking" and r.parent_ids == [1]
        assert set(np.flatnonzero(res.face_regions == r.id)) == set(np.flatnonzero(side == s))
        assert r.confidence > 0.95
    assert by["s1.body"].n_faces == 4 * len(f) // 6
    assert res.retired_ids == [] and len(res.clusters) == 3


def test_subject_sides_and_patchwork_host_retires():
    """Patchwork cube, no color on half of it: the body retires into markings. The tan on
    +X is the subject's LEFT side (front faces -Y, so its left is +X)."""
    v, f, side = cube()
    paint = {5: BLACK, 3: BLACK, 2: WHITE, 4: GRAY, 1: TAN, 0: TAN}  # +Z +Y | -Y | -Z | +X -X
    lab = np.zeros((len(f), 3))
    for s, rgb in paint.items():
        lab[side == s] = lab_of(rgb, (side == s).sum())
    fr = np.full(len(f), 4, np.int32)
    res = split_markings(v, f, fr, [host_region(4, "body", [], v, f)], lab, next_region_id=10)
    assert res.retired_ids == [4]
    assert (res.face_regions != 4).all() and min(r.id for r in res.regions) == 10
    by = {r.label: r for r in res.regions}
    assert sorted(by) == [
        "s1.marking_black_back", "s1.marking_gray_belly", "s1.marking_tan_left",
        "s1.marking_tan_right", "s1.marking_white_chest",
    ]  # fmt: skip
    assert by["s1.marking_tan_left"].centroid_mm[0] == pytest.approx(20.0)
    assert by["s1.marking_tan_right"].centroid_mm[0] == pytest.approx(0.0)
    assert all(r.parent_ids == [4] and r.source == "texture_cluster" for r in res.regions)


def band_grid(nx=60, nz=20):
    """Flat grid (1 mm cells, normals -Y); returns (vertices, faces, face centroid x)."""
    xs, zs = np.meshgrid(np.arange(nx + 1, dtype=float), np.arange(nz + 1, dtype=float))
    v = np.stack([xs.ravel(), np.zeros(xs.size), zs.ravel()], axis=1)
    f = []
    for j in range(nz):
        for i in range(nx):
            q = j * (nx + 1) + i
            f += [(q, q + nx + 2, q + 1), (q, q + nx + 1, q + nx + 2)]
    f = np.array(f, np.int64)
    cx = v[f].mean(axis=1)[:, 0]
    return v, f, cx


def test_gradient_band_dissolves_but_flat_patch_stays():
    v, f, cx = band_grid()
    black, tan = srgb_to_lab(BLACK), srgb_to_lab(TAN)
    # Black for x < 24, a linear ramp to tan over 12 mm, tan after: baked shading.
    t = np.clip((cx - 24) / 12, 0, 1)[:, None]
    lab = black * (1 - t) + tan * t + RNG.normal(0, 1.0, (len(f), 3))
    fr = np.ones(len(f), np.int32)
    head = host_region(1, "head", [], v, f)
    res = split_markings(v, f, fr, [head], lab, k=6)
    assert sorted(c.name for c in res.clusters) == ["black", "tan"]
    assert any(b["dissolved"] for b in res.stats["blends"])
    # The ramp (x 24 to 36) splits near its middle.
    black_c = next(c.index for c in res.clusters if c.name == "black")
    assert 27 < cx[res.face_cluster == black_c].max() < 33

    # A flat gray stripe between white and black is a real marking, not a blend.
    white, gray = srgb_to_lab(WHITE), srgb_to_lab(GRAY)
    lab2 = np.where((cx < 24)[:, None], white, np.where((cx < 36)[:, None], gray, black))
    lab2 = lab2 + RNG.normal(0, 1.0, (len(f), 3))
    res2 = split_markings(v, f, fr, [head], lab2, k=6)
    assert sorted(c.name for c in res2.clusters) == ["black", "gray", "white"]
    assert not any(b["dissolved"] for b in res2.stats["blends"])
    assert any(r.label.startswith("s1.marking_gray") for r in res2.regions)


def test_small_speckle_folds_into_surroundings_and_non_hosts_untouched():
    v, f, side = cube()
    lab = lab_of(TAN, len(f))
    speck = np.flatnonzero(side == 5)[:2]  # 2 triangles of black: 1 % of a side
    lab[speck] = srgb_to_lab(BLACK)
    fr = np.ones(len(f), np.int32)
    fr[side == 4] = 2  # -Z: an eye region (not a host) with its own color
    lab[side == 4] = srgb_to_lab(BLACK)
    regions = [host_region(1, "head", [], v, f), host_region(2, "eye", [], v, f)]
    res = split_markings(v, f, fr, regions, lab)
    assert [r.label for r in res.regions] == ["s1.head", "s1.eye"]
    assert (res.face_regions[side == 4] == 2).all() and (res.face_regions[side != 4] == 1).all()
    assert res.face_cluster[side == 4].max() == -1


# ---------------------------------------------------------------- real Meshy beagle

BEAGLE = Path(__file__).resolve().parents[2] / "data/fixtures/meshy/02_image_to_3d_lite/model.glb"
needs_beagle = pytest.mark.skipif(not BEAGLE.is_file(), reason="Meshy beagle fixture not present")


@pytest.fixture(scope="module")
def beagle():
    from p2p_sam3.glb import read_glb, to_model_frame

    return to_model_frame(read_glb(BEAGLE), height_mm=80.0)


def geometric_parts(mesh):
    """Stand-in part sets from geometry (no SAM 3): base disk, head above the neck, body."""
    c, _, _ = face_geometry(mesh.vertices, mesh.faces)
    base = c[:, 2] < 7.0
    head = c[:, 2] > 47.0
    body = ~base & ~head
    return {"base": base, "head": head, "body": body}


@needs_beagle
def test_beagle_markings_from_texture(beagle):
    from p2p_sam3.markings import face_colors_from_texture

    parts = geometric_parts(beagle)
    fr = np.zeros(len(beagle.faces), np.int32)
    regions = []
    for rid, name in enumerate(("base", "head", "body"), start=1):
        fr[parts[name]] = rid
        regions.append(host_region(rid, name, [], beagle.vertices, beagle.faces))
    lab = face_colors_from_texture(beagle)
    res = split_markings(beagle.vertices, beagle.faces, fr, regions, lab)
    # Expected coat: tan head and ears, white blaze, muzzle and chest, black saddle.
    assert sorted(c.name for c in res.clusters) == ["black", "tan", "white"]  # no blend band
    assert res.region_colors[2]["name"] == "tan"  # head (with the ears in this coarse split)
    by = {r.label: r for r in res.regions}
    face = by["s1.marking_white_face"]  # blaze and muzzle
    assert face.parent_ids == [2] and face.centroid_mm[1] < -15 and face.area_mm2 > 500
    body = [r for r in res.regions if r.parent_ids == [3]]
    saddle = max((r for r in body if "_black_" in r.label), key=lambda r: r.area_mm2)
    assert saddle.label == "s1.marking_black_back"
    assert saddle.centroid_mm[1] > 5 and saddle.area_mm2 > 1500
    chest = max((r for r in body if "_white_" in r.label), key=lambda r: r.area_mm2)
    assert chest.label == "s1.marking_white_chest" and chest.centroid_mm[1] < 0
    assert all(r.confidence > 0.85 for r in res.regions if r.source == "texture_cluster")


@needs_beagle
def test_beagle_full_label_pass_with_rasterized_views(beagle, tmp_path):
    """render -> segment (mock masks from geometry through the face-ID buffer) -> back-project ->
    texture markings -> RegionMap, through label_regions and LabelRegionsIn."""
    from p2p_sam3.glb import sample_texture
    from p2p_sam3.labeler import RenderedView, label_regions
    from p2p_sam3.markings import face_colors_from_texture
    from p2p_sam3.mock import MockSegmenter
    from sam3_raster import camera_for, rasterize, shade

    v, f = beagle.vertices, beagle.faces
    tex = np.asarray(beagle.materials[0].texture)
    parts = geometric_parts(beagle)
    views, fixtures = [], {}
    for i, name in enumerate(("front", "back", "left", "right")):
        cam = camera_for(VIEWS[name], v, 512)
        fid, bary = rasterize(v, f, cam)
        rgb = shade(fid, bary, f, beagle.normals, cam,
                    lambda fi, b: sample_texture(tex, (beagle.uvs[f[fi]] * b[:, :, None]).sum(1)))  # fmt: skip
        views.append(RenderedView(name, rgb, fid, camera_pos_mm=cam.pos))
        for prompt, part in (("head", "head"), ("dog", "body"), ("base", "base")):
            m = (fid >= 0) & parts[part][np.maximum(fid, 0)]
            fixtures[(i, prompt)] = [(m, 0.9)] if m.any() else []
    seg = MockSegmenter(fixtures=fixtures, use_heuristics=False)
    req = LabelRegionsIn(prompts=["head", "dog", "base"])
    res = label_regions(
        req, v, f, views, subject_id="s1", segmenter=seg,
        prompt_map={"dog": ("s1", "body"), "base": (None, "base")},
        face_colors_lab=face_colors_from_texture(beagle),
    )  # fmt: skip
    by = {r.label: r for r in res.regions}
    assert {"base", "s1.head", "s1.marking_white_face", "s1.marking_black_back"} <= set(by)
    assert res.markings.region_colors[by["s1.head"].id]["name"] == "tan"
    assert res.backprojection.stats["seen_faces"] > 0.6 * len(f)
    assert (res.face_regions > 0).mean() > 0.95
    mesh = MeshRef(path="s05/model.glb", sha256="0" * 64, n_faces=len(f), n_vertices=len(v))
    rmap = res.save(tmp_path, "s07_labels/a1", mesh)
    assert RegionMap.model_validate_json((tmp_path / "region_map.json").read_text()) == rmap
    ids = {r.id for r in rmap.regions}
    assert set(np.unique(np.load(tmp_path / "face_regions.npy"))) - {0} <= ids
