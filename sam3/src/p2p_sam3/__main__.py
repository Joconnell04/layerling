"""Segment images from the command line with any backend and write overlays.

    uv run python -m p2p_sam3 --backend mock --images a.png --prompts eye nose ear --out /tmp/s
    SAM3_BACKEND=runpod uv run python -m p2p_sam3 --images a.png b.png c.png --out /tmp/s
    uv run --package p2p-sam3 --extra local python -m p2p_sam3 --backend local --images a.png

Prints per-request timing and cost; writes result.json (masks as COCO RLE) and one overlay PNG
per image to --out.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .client import get_segmenter, to_rgb_array
from .overlay import overlay


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m p2p_sam3")
    ap.add_argument("--backend", default=None, help="mock | local | runpod (default SAM3_BACKEND)")
    ap.add_argument("--images", nargs="+", required=True, help="PNG files")
    ap.add_argument("--prompts", nargs="+", default=["eye", "nose", "ear"])
    ap.add_argument("--score-threshold", type=float, default=0.5)
    ap.add_argument("--max-masks", type=int, default=10)
    ap.add_argument("--out", default="sam3_out")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    seg = get_segmenter(args.backend)
    res = seg.segment(
        [Path(p) for p in args.images],
        args.prompts,
        score_threshold=args.score_threshold,
        max_masks_per_prompt=args.max_masks,
    )
    (out / "result.json").write_text(res.model_dump_json(indent=1))
    for r in res.requests:
        print(
            f"{res.backend} {r.mode} ref={r.ref} wall {r.wall_s:.2f} s, cold {r.cold_start}, "
            f"exec {r.execution_ms} ms, delay {r.delay_ms} ms, gpu {r.gpu_seconds:.1f} s, "
            f"${r.usd:.4f}, worker {json.dumps(r.worker_timing)}"
        )
    for path, img in zip(args.images, res.images, strict=True):
        rgb = to_rgb_array(Path(path))
        name = out / f"{Path(path).stem}_overlay.png"
        overlay(rgb, img, res.prompts).save(name)
        found = {p.prompt: p.scores for p in img.prompts}
        print(f"{Path(path).name}: {found} -> {name}")
    print(f"total ${res.usd:.4f}, {res.gpu_seconds:.1f} GPU s")


if __name__ == "__main__":
    main()
