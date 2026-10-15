"""Time SAM 3 on this machine and write mask overlays.

    uv run --package p2p-sam3 --extra local python sam3/scripts/local_bench.py \
        --images a.png b.jpg --prompts eye nose ear mouth head --out /tmp/sam3_bench

Images are resized so the long side is --size px (default 1024, the render size). Each image is
segmented twice; the second pass is the warm per-image time. Writes timing.json and one overlay
PNG per image (all prompts, one color per prompt) to --out. Prints progress as it goes.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
from p2p_sam3.local import LocalSegmenter
from p2p_sam3.overlay import overlay
from PIL import Image


def load(path: str, size: int) -> np.ndarray:
    im = Image.open(path).convert("RGB")
    scale = size / max(im.size)
    im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    return np.asarray(im)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", nargs="+", required=True)
    ap.add_argument("--prompts", nargs="+", default=["eye", "nose", "ear", "mouth", "head"])
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--device", default=None)
    ap.add_argument("--dtype", default=None)
    ap.add_argument("--score-threshold", type=float, default=0.5)
    ap.add_argument("--out", default="/tmp/sam3_bench")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    seg = LocalSegmenter(device=args.device, dtype=args.dtype)
    t = time.perf_counter()
    info = seg.warm()
    load_s = time.perf_counter() - t
    print(f"loaded in {load_s:.1f} s: {info}", flush=True)

    report = {"model": info, "load_s": round(load_s, 2), "prompts": args.prompts, "images": []}
    for path in args.images:
        rgb = load(path, args.size)
        passes = []
        for i in range(2):
            t = time.perf_counter()
            res = seg.segment([rgb], args.prompts, score_threshold=args.score_threshold)
            wall = time.perf_counter() - t
            passes.append({"wall_s": round(wall, 3), **res.requests[0].worker_timing})
            print(f"{Path(path).name} pass {i + 1}: {wall:.2f} s", flush=True)
        img = res.images[0]
        found = {p: img.get(p).scores for p in args.prompts}
        name = f"{Path(path).stem}_overlay.png"
        overlay(rgb, img, args.prompts).save(out / name)
        report["images"].append(
            {
                "image": path,
                "size": [int(rgb.shape[1]), int(rgb.shape[0])],
                "passes": passes,
                "scores": found,
                "overlay": str(out / name),
            }
        )
        print(f"  scores: {found}", flush=True)
        (out / "timing.json").write_text(json.dumps(report, indent=1))
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
