"""GPU smoke test: run the handler in-process on PNG files and write overlays for a human check.

On a GPU pod or worker shell with the image's environment:
    python /app/smoke.py --images a.png b.png c.png --prompts eye nose ear --out /tmp/smoke
Writes <out>/result.json (handler output, masks as RLE) and <out>/<image>_<prompt>.png overlays.
M2 verification 2 is: eyes, noses and ears labeled cleanly on 3 real Meshy renders.
"""

from __future__ import annotations

import argparse
import base64
import json
import time
from pathlib import Path

import handler
import numpy as np
from PIL import Image

COLORS = np.array(
    [[230, 25, 75], [60, 180, 75], [0, 130, 200], [245, 130, 48], [145, 30, 180], [70, 240, 240]],
    np.uint8,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", nargs="+", required=True)
    ap.add_argument("--prompts", nargs="+", default=["eye", "nose", "ear"])
    ap.add_argument("--score-threshold", type=float, default=0.5)
    ap.add_argument("--max-masks", type=int, default=10)
    ap.add_argument("--out", default="/tmp/sam3_smoke")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    t = time.perf_counter()
    handler.load_model()
    print(f"model loaded in {time.perf_counter() - t:.1f} s")
    job = {
        "id": "smoke",
        "input": {
            "images": [base64.b64encode(Path(p).read_bytes()).decode() for p in args.images],
            "prompts": args.prompts,
            "score_threshold": args.score_threshold,
            "max_masks_per_prompt": args.max_masks,
        },
    }
    for run in ("first", "second"):
        t = time.perf_counter()
        res = handler.handler(job)
        print(f"{run} request: {time.perf_counter() - t:.2f} s wall, timing {res.get('timing')}")
    if "error" in res:
        raise SystemExit(res["error"])
    (out / "result.json").write_text(json.dumps(res, indent=1))

    for img_path, img_res in zip(args.images, res["results"], strict=True):
        rgb = np.asarray(Image.open(img_path).convert("RGB"))
        for pr in img_res["prompts"]:
            over = rgb.astype(np.float32).copy()
            for i, rle in enumerate(pr["masks"]):
                m = handler.decode_rle(rle)
                over[m] = 0.45 * over[m] + 0.55 * COLORS[i % len(COLORS)]
            name = f"{Path(img_path).stem}_{pr['prompt'].replace(' ', '_')}.png"
            Image.fromarray(over.astype(np.uint8)).save(out / name)
            print(
                f"{Path(img_path).name} {pr['prompt']!r}: {len(pr['masks'])} masks, "
                f"scores {pr['scores']}, areas {pr['areas']}"
            )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
