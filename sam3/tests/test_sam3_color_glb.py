"""CIELAB, CIEDE2000, color names, and the GLB reader on a GLB built in memory."""

import io
import json
import struct

import numpy as np
import pytest
from p2p_sam3.color import ciede2000, color_name, lab_to_hex, lab_to_srgb, srgb_to_lab
from p2p_sam3.glb import GlbError, read_glb, sample_texture, to_model_frame
from p2p_sam3.markings import face_colors_from_texture
from PIL import Image

# Sharma, Wu, Dalal (2005) CIEDE2000 test pairs.
SHARMA = [
    ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
    ((50.0, 0.0, 0.0), (50.0, -1.0, 2.0), 2.3669),
    ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((50.0, 2.5, 0.0), (50.0, 0.0, -2.5), 4.3065),
]


def test_ciede2000_matches_reference_pairs():
    a = np.array([p[0] for p in SHARMA])
    b = np.array([p[1] for p in SHARMA])
    np.testing.assert_allclose(ciede2000(a, b), [p[2] for p in SHARMA], atol=1e-4)
    np.testing.assert_allclose(ciede2000(b, a), [p[2] for p in SHARMA], atol=1e-4)


def test_lab_round_trip_and_names():
    np.testing.assert_allclose(srgb_to_lab([255, 255, 255]), [100, 0, 0], atol=1e-3)
    rgb = np.array([[12, 200, 99], [160, 104, 57], [3, 3, 3]], float)
    np.testing.assert_allclose(lab_to_srgb(srgb_to_lab(rgb)), rgb, atol=1e-6)
    assert lab_to_hex(srgb_to_lab([160, 104, 57])) == "#a06839"
    names = {h: color_name(srgb_to_lab([int(h[i : i + 2], 16) for i in (0, 2, 4)])) for h in
             ("a06b3f", "e7e7e6", "0c0b0a", "b9b9b9", "6e4a2e")}  # fmt: skip
    # The beagle's cluster colors (tan, white, black), its base (silver), a darker brown.
    assert names == {"a06b3f": "tan", "e7e7e6": "white", "0c0b0a": "black",
                     "b9b9b9": "silver", "6e4a2e": "brown"}  # fmt: skip


def make_glb(texture: Image.Image, translation=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0)) -> bytes:
    """A unit quad in the glTF XY plane facing +Z (front), two triangles, textured."""
    pos = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], np.float32)
    uv = np.array([[0, 1], [1, 1], [1, 0], [0, 0]], np.float32)  # glTF v runs down the image
    idx = np.array([0, 1, 2, 0, 2, 3], np.uint16)
    png = io.BytesIO()
    texture.save(png, format="PNG")
    blobs = [idx.tobytes(), pos.tobytes(), uv.tobytes(), png.getvalue()]
    views, off, bin_ = [], 0, b""
    for blob in blobs:
        views.append({"buffer": 0, "byteOffset": off, "byteLength": len(blob)})
        pad = (-len(blob)) % 4
        bin_ += blob + b"\0" * pad
        off += len(blob) + pad
    js = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "translation": list(translation), "scale": list(scale)}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 1, "TEXCOORD_0": 2},
                                    "indices": 0, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0},
                                                "baseColorFactor": [1, 1, 1, 1]}}],
        "textures": [{"source": 0}],
        "images": [{"bufferView": 3, "mimeType": "image/png"}],
        "accessors": [
            {"bufferView": 0, "componentType": 5123, "count": 6, "type": "SCALAR"},
            {"bufferView": 1, "componentType": 5126, "count": 4, "type": "VEC3"},
            {"bufferView": 2, "componentType": 5126, "count": 4, "type": "VEC2"},
        ],
        "bufferViews": views,
        "buffers": [{"byteLength": len(bin_)}],
    }  # fmt: skip
    jb = json.dumps(js).encode()
    jb += b" " * ((-len(jb)) % 4)
    body = struct.pack("<I4s", len(jb), b"JSON") + jb + struct.pack("<I4s", len(bin_), b"BIN\0")
    body += bin_
    return struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body


def test_read_glb_transform_uvs_and_texture(tmp_path):
    # Texture split along the anti-diagonal: triangle 0 has UVs (0,1) (1,1) (1,0), the part
    # with u + v > 1 (v runs down the image), painted blue; triangle 1 the rest, red.
    n = 64
    v_, u_ = (np.mgrid[:n, :n] + 0.5) / n
    tex = np.where((u_ + v_ > 1)[..., None], [40, 90, 190], [190, 30, 35]).astype(np.uint8)
    path = tmp_path / "quad.glb"
    path.write_bytes(make_glb(Image.fromarray(tex), translation=(1, 2, 3), scale=(2, 2, 2)))
    g = read_glb(path)
    np.testing.assert_allclose(g.vertices[2], [3, 4, 3])  # (1, 1, 0) * 2 + (1, 2, 3)
    assert g.faces.tolist() == [[0, 1, 2], [0, 2, 3]]
    assert g.uvs.shape == (4, 2) and g.materials[0].texture.size == (n, n)
    lab = face_colors_from_texture(g)
    np.testing.assert_allclose(lab, srgb_to_lab([[40, 90, 190], [190, 30, 35]]), atol=0.5)
    # Bilinear sampling at a texel center returns the texel.
    np.testing.assert_allclose(sample_texture(tex, np.array([[0.5 / n, 0.5 / n]])), [[190, 30, 35]])


def test_to_model_frame_front_is_minus_y(tmp_path):
    path = tmp_path / "quad.glb"
    path.write_bytes(make_glb(Image.new("RGB", (2, 2), (200, 200, 200))))
    g = to_model_frame(read_glb(path), height_mm=10.0)
    # glTF Y (up) becomes Z, scaled to 10 mm tall, base at z = 0, centered in XY.
    np.testing.assert_allclose(g.vertices[:, 2].min(), 0.0)
    np.testing.assert_allclose(g.vertices[:, 2].max(), 10.0)
    np.testing.assert_allclose(g.vertices[:, 0].min(), -5.0)
    # The quad faced glTF +Z (front); in the pipeline frame it faces -Y.
    t = g.vertices[g.faces[0]]
    n = np.cross(t[1] - t[0], t[2] - t[0])
    assert n[1] < 0 and abs(n[0]) < 1e-9 and abs(n[2]) < 1e-9


def test_read_glb_rejects_other_files(tmp_path):
    bad = tmp_path / "x.glb"
    bad.write_bytes(b"not a glb at all")
    with pytest.raises(GlbError):
        read_glb(bad)
