"""Runpod serverless handler: SAM 3 text-prompt segmentation on rendered views.

Input (job["input"]):
    images                list[str]   base64 PNG (a "data:image/png;base64," prefix is allowed)
    prompts               list[str]   short noun phrases, e.g. "eye", "nose", "ear"
    score_threshold       float       keep instances scoring above this (default 0.5)
    max_masks_per_prompt  int         top-k instances per image and prompt (default 10)
    mask_threshold        float       optional, mask logit binarization (default 0.5)

Output:
    results: [{image_index, width, height,
               prompts: [{prompt, scores, boxes (xyxy px), areas, masks (COCO RLE)}]}]
    timing:  {decode_s, vision_s, prompt_s, encode_s, total_s, model_load_s}
    cold_start, model: {id, source, sha256, dtype, device, torch, transformers, load timings}
Errors come back as {"error": "..."}, which Runpod reports as a FAILED job.

The worker is stateless apart from the model, which loads once per worker process. Validation
needs only numpy and the standard library, so tests import this file without torch installed.
"""

from __future__ import annotations

import base64
import binascii
import io
import os
import struct
import time
from pathlib import Path
from typing import Any

import numpy as np

# facebook/sam3 is gated: the Hub download needs HF_TOKEN from an account Meta approved. Every
# source is checked against the sha256 of Meta's model.safetensors before loading, so a mirror
# (SAM3_MODEL_ID=jetjodh/sam3, byte-identical) or a local copy is safe to use.
MODEL_ID = os.environ.get("SAM3_MODEL_ID", "facebook/sam3")
MODEL_REVISION = os.environ.get("SAM3_MODEL_REVISION") or None
MODEL_DTYPE = os.environ.get("SAM3_DTYPE", "float32")  # float32 | bfloat16 | float16
MODEL_DEVICE = os.environ.get("SAM3_DEVICE", "auto")  # auto | cuda | mps | cpu
CACHE_ROOT = os.environ.get("SAM3_HF_CACHE", "/runpod-volume/huggingface-cache/hub")
SAM3_SHA256 = "6d06f0a5f84e435071fe6603e61d0b4cc7b40e0d39d487cfd4d67d8cc11cc14a"
EXPECTED_SHA256 = os.environ.get("SAM3_SHA256", SAM3_SHA256)  # "skip" disables the check
WEIGHTS_FILE = "model.safetensors"
DOWNLOAD_PATTERNS = ["*.json", "*.txt", WEIGHTS_FILE]  # never sam3.pt (a second 3.4 GB copy)

MAX_IMAGES = 32
MAX_PROMPTS = 32
MAX_PROMPT_CHARS = 80
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MIN_SIDE = 16
MAX_SIDE = 2048
MAX_MASKS = 64
DEFAULT_SCORE_THRESHOLD = 0.5
DEFAULT_MASK_THRESHOLD = 0.5
DEFAULT_MAX_MASKS = 10
# Transparent pixels are composited onto this color (Meshy PNGs are RGBA; plain convert("RGB")
# would put the figure on black).
BACKGROUND_RGB = tuple(int(v) for v in os.environ.get("SAM3_BACKGROUND", "235,235,235").split(","))

ALLOWED_KEYS = {"images", "prompts", "score_threshold", "max_masks_per_prompt", "mask_threshold"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ValidationError(ValueError):
    pass


# ---------------------------------------------------------------- validation


def _png_size(data: bytes) -> tuple[int, int]:
    """(width, height) from the IHDR chunk, without decoding the image."""
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise ValidationError("not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _decode_b64(s: Any, i: int) -> bytes:
    if not isinstance(s, str) or not s:
        raise ValidationError(f"images[{i}] must be a non-empty base64 string")
    if s.startswith("data:"):
        head, sep, s = s.partition(",")
        if not sep or ";base64" not in head:
            raise ValidationError(f"images[{i}] data URI must be base64")
    if len(s) > (MAX_IMAGE_BYTES * 4) // 3 + 8:
        raise ValidationError(f"images[{i}] is larger than {MAX_IMAGE_BYTES} bytes")
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValidationError(f"images[{i}] is not valid base64: {e}") from None


def _number(inp: dict, key: str, default: float, lo: float, hi: float) -> float:
    v = inp.get(key, default)
    if v is None:
        v = default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= float(v) <= hi):
        raise ValidationError(f"{key} must be a number in [{lo}, {hi}]")
    return float(v)


def validate(job_input: Any) -> dict:
    """Check and normalize a job input. Raises ValidationError with a plain message."""
    if not isinstance(job_input, dict):
        raise ValidationError("input must be an object")
    unknown = sorted(set(job_input) - ALLOWED_KEYS)
    if unknown:
        raise ValidationError(f"unknown input keys: {', '.join(unknown)}")

    images = job_input.get("images")
    if not isinstance(images, list) or not images:
        raise ValidationError("images must be a non-empty list")
    if len(images) > MAX_IMAGES:
        raise ValidationError(f"at most {MAX_IMAGES} images per request")
    decoded: list[bytes] = []
    sizes: list[tuple[int, int]] = []
    total = 0
    for i, s in enumerate(images):
        data = _decode_b64(s, i)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValidationError(f"images[{i}] is larger than {MAX_IMAGE_BYTES} bytes")
        try:
            w, h = _png_size(data)
        except ValidationError:
            raise ValidationError(f"images[{i}] is not a PNG") from None
        if not (MIN_SIDE <= w <= MAX_SIDE and MIN_SIDE <= h <= MAX_SIDE):
            raise ValidationError(f"images[{i}] is {w}x{h}; sides must be {MIN_SIDE}-{MAX_SIDE} px")
        total += len(data)
        decoded.append(data)
        sizes.append((w, h))
    if total > MAX_TOTAL_BYTES:
        raise ValidationError(f"images total {total} bytes; limit {MAX_TOTAL_BYTES}")

    prompts = job_input.get("prompts")
    if not isinstance(prompts, list) or not prompts:
        raise ValidationError("prompts must be a non-empty list")
    if len(prompts) > MAX_PROMPTS:
        raise ValidationError(f"at most {MAX_PROMPTS} prompts per request")
    clean: list[str] = []
    for i, p in enumerate(prompts):
        if not isinstance(p, str) or not p.strip():
            raise ValidationError(f"prompts[{i}] must be a non-empty string")
        p = " ".join(p.split())
        if len(p) > MAX_PROMPT_CHARS:
            raise ValidationError(f"prompts[{i}] is longer than {MAX_PROMPT_CHARS} characters")
        if p in clean:
            raise ValidationError(f"prompts[{i}] repeats {p!r}")
        clean.append(p)

    max_masks = job_input.get("max_masks_per_prompt", DEFAULT_MAX_MASKS)
    if max_masks is None:
        max_masks = DEFAULT_MAX_MASKS
    if (
        isinstance(max_masks, bool)
        or not isinstance(max_masks, int)
        or not (1 <= max_masks <= MAX_MASKS)
    ):
        raise ValidationError(f"max_masks_per_prompt must be an integer in [1, {MAX_MASKS}]")

    return {
        "images": decoded,
        "sizes": sizes,
        "prompts": clean,
        "score_threshold": _number(job_input, "score_threshold", DEFAULT_SCORE_THRESHOLD, 0.0, 1.0),
        "mask_threshold": _number(job_input, "mask_threshold", DEFAULT_MASK_THRESHOLD, 0.0, 1.0),
        "max_masks_per_prompt": max_masks,
    }


# ---------------------------------------------------------------- COCO RLE
# Same algorithm as p2p_sam3.rle.encode; sam3/tests/test_sam3_handler.py keeps them identical.


def encode_rle(mask: np.ndarray) -> dict:
    h, w = mask.shape
    flat = np.asarray(mask, dtype=bool).ravel(order="F")
    n = flat.size
    if n == 0:
        cnts: list[int] = []
    else:
        change = np.flatnonzero(flat[1:] != flat[:-1]) + 1
        lengths = np.diff(np.concatenate(([0], change, [n])))
        if flat[0]:
            lengths = np.concatenate(([0], lengths))
        cnts = [int(c) for c in lengths]
    out: list[str] = []
    for i, c in enumerate(cnts):
        x = c - cnts[i - 2] if i > 2 else c
        more = True
        while more:
            ch = x & 0x1F
            x >>= 5
            more = (x != -1) if (ch & 0x10) else (x != 0)
            if more:
                ch |= 0x20
            out.append(chr(ch + 48))
    return {"size": [int(h), int(w)], "counts": "".join(out)}


def decode_rle(rle: dict) -> np.ndarray:
    """Inverse of encode_rle (used by smoke.py for overlays)."""
    h, w = rle["size"]
    s, cnts, p = rle["counts"], [], 0
    while p < len(s):
        x = k = 0
        more = True
        while more:
            c = ord(s[p]) - 48
            x |= (c & 0x1F) << (5 * k)
            more = bool(c & 0x20)
            p += 1
            k += 1
            if not more and (c & 0x10):
                x |= -1 << (5 * k)
        if len(cnts) > 2:
            x += cnts[-2]
        cnts.append(x)
    vals = np.zeros(len(cnts), bool)
    vals[1::2] = True
    return np.repeat(vals, cnts).reshape((h, w), order="F")


# ---------------------------------------------------------------- model

_STATE: dict[str, Any] = {}


def resolve_model_source() -> tuple[str, str]:
    """(local dir or repo id, source kind). Kind "hub" means the weights still need a download.

    Order: SAM3_MODEL_PATH (a local dir or network volume, e.g. /runpod-volume/models/sam3);
    Runpod's cached-model mount (endpoint "Model" field = MODEL_ID), which lands at
    <cache>/models--<org>--<name>/snapshots/<hash>/; else the Hugging Face Hub.
    """
    explicit = os.environ.get("SAM3_MODEL_PATH")
    if explicit:
        return str(Path(explicit).expanduser()), "path"
    repo_dir = Path(CACHE_ROOT) / ("models--" + MODEL_ID.replace("/", "--"))
    snaps_dir = repo_dir / "snapshots"
    if snaps_dir.is_dir():
        snaps = sorted(p for p in snaps_dir.iterdir() if (p / "config.json").is_file())
        want = MODEL_REVISION
        ref = repo_dir / "refs" / (MODEL_REVISION or "main")
        if ref.is_file():
            want = ref.read_text().strip()
        chosen = next((p for p in snaps if p.name == want), None)
        if chosen is None and snaps and not MODEL_REVISION:
            chosen = max(snaps, key=lambda p: (p.stat().st_mtime, p.name))
        if chosen is not None:
            return str(chosen), "runpod_cache"
    return MODEL_ID, "hub"


def fetch_model(source: str, kind: str) -> Path:
    """Local directory with the weights, downloading from the Hub if needed (no sam3.pt)."""
    if kind != "hub":
        return Path(source)
    from huggingface_hub import snapshot_download

    path = snapshot_download(
        source,
        revision=MODEL_REVISION,
        allow_patterns=DOWNLOAD_PATTERNS,
        token=os.environ.get("HF_TOKEN") or None,
    )
    return Path(path)


def verify_weights(model_dir: Path, expected: str | None = None) -> str:
    """sha256 of model.safetensors; raises if it does not match. Returns the digest or 'skip'."""
    import hashlib

    expected = EXPECTED_SHA256 if expected is None else expected
    if expected.lower() in ("", "skip", "0"):
        return "skip"
    path = Path(model_dir) / WEIGHTS_FILE
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found")
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(16 * 1024 * 1024):
            h.update(chunk)
    digest = h.hexdigest()
    if digest != expected.lower():
        raise RuntimeError(f"{path} sha256 {digest} does not match expected {expected}")
    return digest


def pick_device(torch, want: str = "auto") -> str:
    if want != "auto":
        return want
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


class TorchSam3:
    """SAM 3 through Hugging Face transformers (Sam3Model, Sam3Processor). Loaded once.

    Runs on CUDA (Runpod), Apple MPS (local dev; set PYTORCH_ENABLE_MPS_FALLBACK=1 before torch
    is imported so missing ops fall back to CPU) or CPU.
    """

    def __init__(
        self,
        model_dir: str | Path | None = None,
        device: str | None = None,
        dtype: str | None = None,
    ) -> None:
        import torch
        import transformers
        from transformers import Sam3Model, Sam3Processor

        self.torch = torch
        self.device = pick_device(torch, device or MODEL_DEVICE)
        dtype_name = dtype or MODEL_DTYPE
        self.dtype = {
            "float32": torch.float32,
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
        }[dtype_name]
        if self.device == "cuda":
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
        t = time.perf_counter()
        if model_dir is not None:
            source, kind = str(Path(model_dir).expanduser()), "path"
        else:
            source, kind = resolve_model_source()
        path = fetch_model(source, kind)
        t_fetch = time.perf_counter() - t
        t = time.perf_counter()
        digest = verify_weights(path)
        t_verify = time.perf_counter() - t
        t = time.perf_counter()
        self.model = Sam3Model.from_pretrained(path, dtype=self.dtype, local_files_only=True)
        self.model = self.model.to(self.device).eval()
        self.processor = Sam3Processor.from_pretrained(path, local_files_only=True)
        t_load = time.perf_counter() - t
        self._text: dict[str, Any] = {}
        device_name = torch.cuda.get_device_name(0) if self.device == "cuda" else self.device
        self.info = {
            "id": MODEL_ID if kind in ("hub", "runpod_cache") else str(path),
            "source": kind,
            "sha256": digest,
            "dtype": dtype_name,
            "device": device_name,
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "fetch_s": round(t_fetch, 2),
            "verify_s": round(t_verify, 2),
            "load_s": round(t_load, 2),
        }

    def _sync(self) -> None:
        if self.device == "cuda":
            self.torch.cuda.synchronize()
        elif self.device == "mps":
            self.torch.mps.synchronize()

    def vision(self, image, prompts: list[str] | None = None) -> tuple[Any, list]:
        """Image features, computed once per image and reused for every prompt."""
        del prompts
        inputs = self.processor(images=image, return_tensors="pt").to(self.device)
        with self.torch.inference_mode():
            feats = self.model.get_vision_features(
                pixel_values=inputs["pixel_values"].to(self.dtype)
            )
        self._sync()
        return feats, inputs["original_sizes"].tolist()

    def prompt(self, state, text: str, score_threshold: float, mask_threshold: float):
        """(scores (N,), boxes (N, 4) xyxy px, masks (N, H, W) bool) for one prompt."""
        feats, target_sizes = state
        if text not in self._text:
            self._text[text] = self.processor(text=text, return_tensors="pt").to(self.device)
        with self.torch.inference_mode():
            out = self.model(vision_embeds=feats, **self._text[text])
            res = self.processor.post_process_instance_segmentation(
                out,
                threshold=score_threshold,
                mask_threshold=mask_threshold,
                target_sizes=target_sizes,
            )[0]
        self._sync()
        return (
            res["scores"].float().cpu().numpy(),
            res["boxes"].float().cpu().numpy(),
            res["masks"].bool().cpu().numpy(),
        )


def to_rgb(image, background: tuple[int, ...] = BACKGROUND_RGB):
    """PIL image -> RGB, compositing any transparency onto `background`."""
    from PIL import Image

    if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (*background[:3], 255))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    return image.convert("RGB")


def load_model(factory=TorchSam3) -> dict:
    """Load the model once per worker process."""
    if "model" not in _STATE:
        t0 = time.perf_counter()
        model = factory()
        _STATE.update(model=model, load_s=time.perf_counter() - t0, served=0)
    return _STATE


def run(req: dict, st: dict | None = None) -> dict:
    """Segment a validated request. st: {model, load_s, served}; default the worker's model."""
    from PIL import Image

    t_start = time.perf_counter()
    st = st if st is not None else load_model()
    model = st["model"]
    cold = st["served"] == 0
    st["served"] += 1

    timing = {"decode_s": 0.0, "vision_s": 0.0, "prompt_s": 0.0, "encode_s": 0.0}
    k = req["max_masks_per_prompt"]
    results = []
    for idx, data in enumerate(req["images"]):
        t = time.perf_counter()
        image = to_rgb(Image.open(io.BytesIO(data)))
        timing["decode_s"] += time.perf_counter() - t

        t = time.perf_counter()
        state = model.vision(image, req["prompts"])
        timing["vision_s"] += time.perf_counter() - t

        per_prompt = []
        for p in req["prompts"]:
            t = time.perf_counter()
            scores, boxes, masks = model.prompt(
                state, p, req["score_threshold"], req["mask_threshold"]
            )
            timing["prompt_s"] += time.perf_counter() - t

            t = time.perf_counter()
            order = np.argsort(-np.asarray(scores, dtype=np.float64), kind="stable")[:k]
            per_prompt.append(
                {
                    "prompt": p,
                    "scores": [round(float(scores[j]), 4) for j in order],
                    "boxes": [[round(float(v), 2) for v in boxes[j]] for j in order],
                    "areas": [int(masks[j].sum()) for j in order],
                    "masks": [encode_rle(masks[j]) for j in order],
                }
            )
            timing["encode_s"] += time.perf_counter() - t
        w, h = req["sizes"][idx]
        results.append({"image_index": idx, "width": w, "height": h, "prompts": per_prompt})

    timing = {key: round(v, 4) for key, v in timing.items()}
    timing["total_s"] = round(time.perf_counter() - t_start, 4)
    timing["model_load_s"] = round(st["load_s"], 3) if cold else 0.0
    return {"results": results, "timing": timing, "cold_start": cold, "model": model.info}


def handler(job: dict) -> dict:
    try:
        req = validate((job or {}).get("input"))
    except ValidationError as e:
        return {"error": f"validation: {e}"}
    try:
        return run(req)
    except Exception as e:  # noqa: BLE001  reported to the caller as a FAILED job
        return {"error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    import runpod

    load_model()  # warm before taking jobs so the first request does not pay the load
    runpod.serverless.start({"handler": handler})
