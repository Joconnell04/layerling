import base64
import os
import sys

import numpy as np
import pytest
from p2p_sam3 import rle


def test_import_does_not_load_torch(handler_mod):
    assert "torch" not in sys.modules or os.environ.get("SAM3_TEST_ALLOW_TORCH")
    assert handler_mod._STATE == {}


def test_valid_input_normalized(handler_mod, small_png_b64):
    req = handler_mod.validate(
        {
            "images": [small_png_b64, "data:image/png;base64," + small_png_b64],
            "prompts": ["  eye ", "cat   nose"],
            "score_threshold": 0.4,
            "max_masks_per_prompt": 3,
        }
    )
    assert req["prompts"] == ["eye", "cat nose"]
    assert req["sizes"] == [(48, 32), (48, 32)]
    assert req["score_threshold"] == 0.4
    assert req["mask_threshold"] == 0.5
    assert req["max_masks_per_prompt"] == 3
    assert all(b.startswith(b"\x89PNG") for b in req["images"])


def test_defaults(handler_mod, small_png_b64):
    req = handler_mod.validate({"images": [small_png_b64], "prompts": ["eye"]})
    assert req["score_threshold"] == 0.5
    assert req["max_masks_per_prompt"] == 10


JPEG_B64 = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 40).decode()


@pytest.mark.parametrize(
    "bad, msg",
    [
        (None, "input must be an object"),
        ({}, "images must be a non-empty list"),
        ({"images": [], "prompts": ["eye"]}, "images must be a non-empty list"),
        ({"images": "abc", "prompts": ["eye"]}, "images must be a non-empty list"),
        ({"images": [123], "prompts": ["eye"]}, "non-empty base64 string"),
        ({"images": ["not base64!"], "prompts": ["eye"]}, "not valid base64"),
        ({"images": [JPEG_B64], "prompts": ["eye"]}, "not a PNG"),
        ({"images": ["data:image/png,xyz"], "prompts": ["eye"]}, "data URI must be base64"),
        ({"images": ["OK"], "prompts": [], "extra": 1}, "unknown input keys: extra"),
    ],
)
def test_rejects_bad_input(handler_mod, bad, msg):
    with pytest.raises(handler_mod.ValidationError, match=msg):
        handler_mod.validate(bad)


@pytest.mark.parametrize(
    "patch, msg",
    [
        ({"prompts": []}, "prompts must be a non-empty list"),
        ({"prompts": ["eye", ""]}, r"prompts\[1\] must be a non-empty string"),
        ({"prompts": ["eye", "eye "]}, "repeats"),
        ({"prompts": ["x" * 81]}, "longer than"),
        ({"prompts": [f"p{i}" for i in range(33)]}, "at most 32 prompts"),
        ({"score_threshold": 1.5}, "score_threshold must be a number"),
        ({"score_threshold": "0.5"}, "score_threshold must be a number"),
        ({"score_threshold": True}, "score_threshold must be a number"),
        ({"mask_threshold": -0.1}, "mask_threshold must be a number"),
        ({"max_masks_per_prompt": 0}, "max_masks_per_prompt"),
        ({"max_masks_per_prompt": 2.5}, "max_masks_per_prompt"),
        ({"max_masks_per_prompt": 65}, "max_masks_per_prompt"),
    ],
)
def test_rejects_bad_params(handler_mod, small_png_b64, patch, msg):
    job = {"images": [small_png_b64], "prompts": ["eye"], **patch}
    with pytest.raises(handler_mod.ValidationError, match=msg):
        handler_mod.validate(job)


def test_image_size_limits(handler_mod, png_b64):
    tiny = png_b64(np.zeros((8, 8, 3), np.uint8))
    with pytest.raises(handler_mod.ValidationError, match="sides must be"):
        handler_mod.validate({"images": [tiny], "prompts": ["eye"]})
    many = [png_b64(np.zeros((16, 16, 3), np.uint8))] * 33
    with pytest.raises(handler_mod.ValidationError, match="at most 32 images"):
        handler_mod.validate({"images": many, "prompts": ["eye"]})


def test_handler_returns_error_not_raise(handler_mod):
    out = handler_mod.handler({"id": "x", "input": {"images": [], "prompts": ["eye"]}})
    assert out["error"].startswith("validation: ")
    assert handler_mod._STATE == {}  # validation failures never load the model


def test_handler_rle_matches_package(handler_mod):
    rng = np.random.default_rng(11)
    for _ in range(40):
        h, w = rng.integers(1, 50, size=2)
        m = rng.random((h, w)) < rng.random()
        assert handler_mod.encode_rle(m) == rle.encode(m)
        assert np.array_equal(handler_mod.decode_rle(rle.encode(m)), m)
    m = np.ones((3, 3), bool)
    assert handler_mod.encode_rle(m) == rle.encode(m)


def test_model_source_resolution(handler_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(handler_mod, "CACHE_ROOT", str(tmp_path))
    monkeypatch.delenv("SAM3_MODEL_PATH", raising=False)
    monkeypatch.setattr(handler_mod, "MODEL_REVISION", None)
    assert handler_mod.resolve_model_source() == ("facebook/sam3", "hub")

    repo = tmp_path / "models--facebook--sam3"
    for h in ("aaa111", "bbb222"):
        (repo / "snapshots" / h).mkdir(parents=True)
        (repo / "snapshots" / h / "config.json").write_text("{}")
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("aaa111\n")
    src, kind = handler_mod.resolve_model_source()
    assert kind == "runpod_cache" and src.endswith("aaa111")
    assert handler_mod.fetch_model(src, kind) == repo / "snapshots" / "aaa111"

    monkeypatch.setenv("SAM3_MODEL_PATH", "/weights/sam3")
    assert handler_mod.resolve_model_source() == ("/weights/sam3", "path")


def test_verify_weights(handler_mod, tmp_path):
    import hashlib

    (tmp_path / "model.safetensors").write_bytes(b"weights")
    good = hashlib.sha256(b"weights").hexdigest()
    assert handler_mod.verify_weights(tmp_path, good) == good
    with pytest.raises(RuntimeError, match="does not match"):
        handler_mod.verify_weights(tmp_path, "0" * 64)
    assert handler_mod.verify_weights(tmp_path, "skip") == "skip"
    with pytest.raises(FileNotFoundError):
        handler_mod.verify_weights(tmp_path / "missing", good)
    # Default expectation is the LFS sha256 of Meta's facebook/sam3 model.safetensors.
    assert handler_mod.EXPECTED_SHA256 == (
        "6d06f0a5f84e435071fe6603e61d0b4cc7b40e0d39d487cfd4d67d8cc11cc14a"
    )


def test_pick_device(handler_mod):
    class Flag:
        def __init__(self, v):
            self.v = v

        def is_available(self):
            return self.v

    class FakeTorch:
        def __init__(self, cuda, mps):
            self.cuda = Flag(cuda)
            self.backends = type("B", (), {"mps": Flag(mps)})()

    assert handler_mod.pick_device(FakeTorch(True, True)) == "cuda"
    assert handler_mod.pick_device(FakeTorch(False, True)) == "mps"
    assert handler_mod.pick_device(FakeTorch(False, False)) == "cpu"
    assert handler_mod.pick_device(FakeTorch(True, True), "cpu") == "cpu"


def test_transparent_pixels_composited(handler_mod, tmp_path):
    from p2p_sam3.client import to_rgb_array
    from PIL import Image

    rgba = np.zeros((20, 20, 4), np.uint8)
    rgba[5:10, 5:10] = (200, 10, 10, 255)
    im = Image.fromarray(rgba, "RGBA")
    out = np.asarray(handler_mod.to_rgb(im))
    assert tuple(out[0, 0]) == (235, 235, 235) and tuple(out[6, 6]) == (200, 10, 10)
    im.save(tmp_path / "a.png")
    assert np.array_equal(to_rgb_array(tmp_path / "a.png"), out)
