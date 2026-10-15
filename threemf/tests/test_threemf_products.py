"""Realistic products: keychain with a loop, nameplate with raised text, plain figurine, and a
keychain whose painted eyes are too small to print."""

import json
import re
import zipfile

import numpy as np
import pytest
from p2p_threemf.audit import audit_paint
from p2p_threemf.demo import demo_settings, jackson_palette
from p2p_threemf.products import PRODUCTS, keychain, nameplate
from p2p_threemf.reader import read_3mf_full
from p2p_threemf.slicer import slice_check
from p2p_threemf.writer import Part, write_3mf
from threemf_testutil import needs_bambu


def _closed(v, f) -> bool:
    e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    _, c = np.unique(e, axis=0, return_counts=True)
    tri = np.asarray(v)[f]
    vol = np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6
    return bool((c == 2).all() and vol > 0)


@pytest.mark.parametrize("name", sorted(PRODUCTS))
def test_product_meshes_are_closed(name):
    p = PRODUCTS[name]()
    assert _closed(p.vertices, p.faces)
    for part in p.parts:
        assert _closed(np.asarray(part.vertices), np.asarray(part.faces)), part.name


def test_keychain_dimensions():
    p = keychain()
    allv = np.concatenate([p.vertices] + [np.asarray(q.vertices) for q in p.parts])
    size = allv.max(axis=0) - allv.min(axis=0)
    assert 43 <= size.max() <= 46  # about 45 mm long
    loop = p.parts[0]
    assert np.ptp(np.asarray(loop.vertices)[:, 2]) == pytest.approx(2.5, abs=0.01)


def _write(tmp_path, p, name=None):
    path = tmp_path / f"{name or p.name}.3mf"
    res = write_3mf(p.vertices, p.faces, p.face_slots, jackson_palette(), demo_settings(5), path,
                    object_name=p.name, parts=p.parts)
    return path, res


def test_multipart_round_trip(tmp_path):
    p = nameplate()
    path, res = _write(tmp_path, p)
    r = read_3mf_full(path)
    sizes = [len(p.faces)] + [len(np.asarray(q.faces)) for q in p.parts]
    assert len(r.faces) == sum(sizes)
    assert list(np.unique(r.face_part)) == [1, 2, 3]
    # Figure keeps its paint; plate and text read as their part filament.
    n0 = sizes[0]
    assert np.array_equal(r.face_slots[:n0], p.face_slots)
    assert (r.face_slots[n0:n0 + sizes[1]] == 2).all()
    assert (r.face_slots[n0 + sizes[1]:] == 5).all()
    assert res["slots_used"] == [1, 2, 3, 4, 5]
    assert [q["name"] for q in res["parts"]] == ["nameplate", "plate", "text_BISCUIT"]
    with zipfile.ZipFile(path) as z:
        ms = z.read("Metadata/model_settings.config").decode()
        main = z.read("3D/3dmodel.model").decode()
        obj = z.read("3D/Objects/object_1.model").decode()
    assert ms.count("<part id=") == 3
    assert '<object id="4">' in ms
    assert re.findall(r'<metadata key="extruder" value="(\d+)"/>', ms) == ["5", "2", "5"]
    assert main.count("<component ") == 3
    assert obj.count("<object id=") == 3
    # Add-on parts are not painted.
    tail = obj[obj.index('<object id="2"'):]
    assert "paint_color" not in tail


def test_part_needs_one_of_slot_or_paint(tmp_path):
    p = keychain()
    bad = Part(p.parts[0].vertices, p.parts[0].faces, name="loop")
    with pytest.raises(ValueError):
        write_3mf(p.vertices, p.faces, p.face_slots, jackson_palette(), demo_settings(5),
                  tmp_path / "x.3mf", parts=[bad])


def test_transform_is_applied_to_figure_only(tmp_path):
    p = keychain()
    m = np.eye(4)
    m[:3, :3] *= 0.5
    m[:3, 3] = [10, 0, 0]
    path = tmp_path / "t.3mf"
    write_3mf(p.vertices, p.faces, p.face_slots, jackson_palette(), demo_settings(5), path,
              parts=p.parts, transform=m)
    r = read_3mf_full(path)
    fig = r.vertices[r.faces[r.face_part == 1]].reshape(-1, 3)
    assert fig[:, 0].max() == pytest.approx(p.vertices[:, 0].max() * 0.5 + 10, abs=1e-4)
    loop = r.vertices[r.faces[r.face_part == 2]].reshape(-1, 3)
    assert loop[:, 2].max() == pytest.approx(2.5, abs=1e-4)


def test_audit_width_estimate_on_strips():
    # A 20 x 20 mm grid with strips 0.5 mm and 1.5 mm wide painted across it.
    n = 200
    xs = np.linspace(0, 20, n + 1)
    gx, gy = np.meshgrid(xs, xs, indexing="ij")
    v = np.stack([gx.ravel(), gy.ravel(), np.zeros(gx.size)], axis=1)
    idx = np.arange((n + 1) ** 2).reshape(n + 1, n + 1)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel(), idx[:-1, 1:].ravel()
    f = np.concatenate([np.stack([a, b, c], 1), np.stack([a, c, d], 1)])
    cy = v[f].mean(axis=1)[:, 1]
    s = np.full(len(f), 5)
    s[(cy > 5) & (cy < 5.5)] = 1
    s[(cy > 12) & (cy < 13.5)] = 3
    au = audit_paint(v, f, s, min_width_mm=0.8)
    assert [r.slot for r in au.thin] == [1]
    assert au.thin[0].width_mm == pytest.approx(0.5, abs=0.15)
    assert au.slots_only_thin == [1]
    assert any("slot 1" in w for w in au.warnings())


def test_audit_flags_small_eyes():
    assert audit_paint(*_mesh(keychain())).thin == []
    au = audit_paint(*_mesh(PRODUCTS["keychain_too_thin"]()))
    assert au.slots_only_thin == [1, 3]
    assert all(r.width_mm < 0.8 for r in au.thin)


def _mesh(p):
    return p.vertices, p.faces, p.face_slots


@needs_bambu
@pytest.mark.parametrize("name", ["keychain", "nameplate", "figurine"])
def test_products_pass_the_gate(tmp_path, name):
    p = PRODUCTS[name]()
    path, res = _write(tmp_path, p)
    r = slice_check(path, demo_settings(5), p.expected_slots,
                    filament_ids=[f.id for f in jackson_palette()])
    assert r.ok, r.errors
    assert r.slots_used == p.expected_slots == len(res["slots_used"])
    assert not [w for w in r.warnings if w.startswith("thin paint")]
    assert r.print_time_s > 0 and r.total_grams > 0


@needs_bambu
def test_too_thin_paint_fails_the_gate(tmp_path):
    p = PRODUCTS["keychain_too_thin"]()
    path, _ = _write(tmp_path, p)
    r = slice_check(path, demo_settings(5), p.expected_slots)
    assert not r.ok
    assert r.slots_used < p.expected_slots
    assert any("only had paint under 0.8 mm" in e for e in r.errors)
    assert any(w.startswith("thin paint") for w in r.warnings)


@needs_bambu
def test_bambu_keeps_part_filaments(tmp_path):
    from p2p_threemf.slicer import BambuStudioBackend
    from p2p_threemf.verify import readback_check

    p = nameplate()
    path, _ = _write(tmp_path, p)
    raw = BambuStudioBackend().load_and_save(path, tmp_path / "rs")
    assert raw.ok, raw.errors
    rb = readback_check(path, raw.sliced_3mf)
    assert rb.ok, rb.errors
    assert rb.faces_same_slot == rb.faces_in
    with zipfile.ZipFile(raw.sliced_3mf) as z:
        ms = z.read("Metadata/model_settings.config").decode()
        cfg = json.loads(z.read("Metadata/project_settings.config"))
    assert ms.count("<part id=") == 3
    assert cfg["different_settings_to_system"] == [""] * 7
