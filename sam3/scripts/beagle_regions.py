"""Full label pass on a Meshy GLB: render, SAM 3, back-projection, texture markings, overlays.

Renders with p2p_sim.render (kind "texture", face-ID buffer) on this package's vertices and face
order; --renderer numpy uses the stand-in in sam3/tests/sam3_raster.py. Writes renders, SAM 3
results, face_regions.npy, region_map.json, region_colors.json and overlay PNGs to --out. SAM 3
results are cached in --out/segments.json; pass --resegment to rerun.

    SAM3_BACKEND=local uv run --all-packages --all-extras python sam3/scripts/beagle_regions.py \
        --glb data/fixtures/meshy/02_image_to_3d_lite/model.glb --out sam3/_out/beagle_regions
"""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
from p2p_contracts.mcp_tools import LabelRegionsIn
from p2p_contracts.regions import VIEWS, MeshRef
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from p2p_sam3.backproject import (
    ViewLabels,
    backproject,
    masks_from_result,
    save_region_map,
)
from p2p_sam3.client import get_segmenter
from p2p_sam3.color import color_name, lab_to_hex, lab_to_srgb
from p2p_sam3.glb import read_glb, sample_texture, to_model_frame
from p2p_sam3.labeler import clean_masks
from p2p_sam3.markings import face_colors_from_texture, split_markings
from p2p_sam3.parts import part_for_prompt
from p2p_sam3.types import SegmentResult
from sam3_raster import camera_for, rasterize, shade

PARTS = ["eye", "nose", "mouth", "ear", "paw", "tail", "head", "collar", "tag", "base"]
VIEW_NAMES = ["front", "back", "left", "right"]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glb", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--height-mm", type=float, default=80.0)
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--resegment", action="store_true")
    ap.add_argument("--no-markings", action="store_true")
    ap.add_argument("--renderer", choices=["sim", "numpy"], default="sim")
    ap.add_argument("--subject", default="dog", help="SAM 3 prompt for the whole subject")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    req = LabelRegionsIn(prompts=[*PARTS, args.subject], views=VIEW_NAMES)
    prompt_map = {args.subject: ("s1", "body"), "base": (None, "base")}

    mesh = to_model_frame(read_glb(args.glb), args.height_mm)
    v, f = mesh.vertices, mesh.faces
    tex = np.asarray(mesh.materials[0].texture)
    log(f"mesh {len(v)} vertices, {len(f)} faces, bbox {v.min(0).round(1)} {v.max(0).round(1)}")

    renders = []
    t = time.time()
    if args.renderer == "sim":
        # p2p_sim texture render on the same vertices and face order (sim UVs are v-up).
        from p2p_sim.mesh import Mesh as SimMesh
        from p2p_sim.render import render as sim_render

        sm = SimMesh(v, f, uv=np.c_[mesh.uvs[:, 0], 1.0 - mesh.uvs[:, 1]], texture=tex)
        for name in req.views:
            r = sim_render(sm, name, kind="texture", size_px=args.size)
            Image.fromarray(r.image).save(out / f"render_{name}.png")
            renders.append((name, r.image, r.face_id.astype(np.int32), r.camera.eye))
    else:

        def albedo(fi, b):
            return sample_texture(tex, (mesh.uvs[f[fi]] * b[:, :, None]).sum(axis=1))

        for name in req.views:
            cam = camera_for(VIEWS[name], v, args.size)
            fid, bary = rasterize(v, f, cam)
            rgb = shade(fid, bary, f, mesh.normals, cam, albedo)
            Image.fromarray(rgb).save(out / f"render_{name}.png")
            renders.append((name, rgb, fid, cam.pos))
    log(f"rendered {', '.join(r[0] for r in renders)} ({args.renderer}) in {time.time() - t:.1f} s")

    seg_path = out / "segments.json"
    if seg_path.exists() and not args.resegment:
        seg = SegmentResult.model_validate_json(seg_path.read_text())
        log(f"loaded cached SAM 3 results ({seg.backend})")
    else:
        segmenter = get_segmenter()
        log(f"segmenting with {segmenter.backend}")
        seg = segmenter.segment(
            [r[1] for r in renders], req.prompts, score_threshold=req.score_threshold
        )
        seg_path.write_text(seg.model_dump_json())
        log(f"segmented in {sum(r.wall_s for r in seg.requests):.1f} s")
    for im in seg.images:
        found = {p.prompt: [round(s, 2) for s in p.scores] for p in im.prompts if p.scores}
        log(f"  {req.views[im.image_index]}: {found}")

    pmap = {p: ("s1", part_for_prompt(p)) for p in req.prompts} | prompt_map
    views = [
        ViewLabels(
            fid,
            clean_masks(masks_from_result(seg, i, pmap), req.min_component_frac),
            camera_pos_mm=cam_pos,
            name=name,
        )
        for i, (name, _, fid, cam_pos) in enumerate(renders)
    ]
    t = time.time()
    bp = backproject(v, f, views, min_vote=req.min_vote, min_component_frac=req.min_component_frac)
    log(f"backprojected in {time.time() - t:.1f} s: {bp.stats}")
    face_regions, regions, next_id = bp.face_regions, bp.regions, bp.next_region_id
    colors = {}
    face_lab = face_colors_from_texture(mesh)
    if not args.no_markings:
        t = time.time()
        mk = split_markings(v, f, face_regions, regions, face_lab, next_region_id=next_id)
        face_regions, regions, next_id = mk.face_regions, mk.regions, mk.next_region_id
        colors = mk.region_colors
        log(f"markings in {time.time() - t:.1f} s; clusters " + ", ".join(
            f"{c.name} {c.hex} {c.area_mm2:.0f} mm2" for c in mk.clusters
        ))  # fmt: skip
        log(f"  blends {mk.stats.get('blends')}; retired hosts {mk.retired_ids}")
    # Median texture color of every other region (flat preview and region_colors.json).
    for r in regions:
        if r.id not in colors:
            m = face_regions == r.id
            lab = np.median(face_lab[m], axis=0)
            colors[r.id] = {
                "lab": [round(float(x), 2) for x in lab],
                "hex": lab_to_hex(lab),
                "name": color_name(lab),
            }

    mesh_ref = MeshRef(
        path=Path(args.glb).name,
        sha256=hashlib.sha256(Path(args.glb).read_bytes()).hexdigest(),
        n_faces=len(f),
        n_vertices=len(v),
    )
    save_region_map(face_regions, regions, next_id, out, "s07_labels/a1", mesh_ref)
    (out / "region_colors.json").write_text(
        json.dumps({str(k): c for k, c in colors.items()}, indent=2)
    )
    log("regions:")
    for r in regions:
        log(
            f"  {r.id:3d} {r.label:28s} {r.source:15s} {colors[r.id]['name']:8s} "
            f"{colors[r.id]['hex']} faces {r.n_faces:6d} area {r.area_mm2:8.1f} conf {r.confidence}"
        )

    write_overlays(out, renders, regions, face_regions, colors)
    log(f"wrote {out}")


def region_palette(n: int) -> list[tuple[int, int, int]]:
    """n distinct colors: golden-angle hues, alternating two brightness levels."""
    out = []
    for i in range(n):
        h, s, v = (i * 0.618034) % 1.0, 0.7, 0.95 if i % 2 == 0 else 0.7
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        out.append((int(r * 255), int(g * 255), int(b * 255)))
    return out


def write_overlays(out: Path, renders, regions, face_regions, colors) -> None:
    """regions_<view>.png (region colors, borders, labels), flat_<view>.png (each region in its
    texture color), and sheet.png (both rows cropped to the model, plus a legend)."""
    font = ImageFont.load_default(size=15)
    small = ImageFont.load_default(size=13)
    pal = region_palette(len(regions))
    rid_color = {r.id: pal[i] for i, r in enumerate(regions)}
    lut = np.zeros((max([r.id for r in regions], default=0) + 1, 3), np.float32)
    flat = np.zeros_like(lut)
    for r in regions:
        lut[r.id] = rid_color[r.id]
        flat[r.id] = lab_to_srgb(colors[r.id]["lab"])
    # One crop box for every view: union of the model's pixels plus a margin.
    fg = np.any([fid >= 0 for _, _, fid, _ in renders], axis=0)
    ys, xs = np.nonzero(fg)
    m = 24
    box = (
        max(int(xs.min()) - m, 0),
        max(int(ys.min()) - m, 0),
        min(int(xs.max()) + m, fg.shape[1]),
        min(int(ys.max()) + m, fg.shape[0]),
    )
    tiles, flats = [], []
    for name, rgb, fid, _ in renders:
        vis = fid >= 0
        reg = np.zeros(fid.shape, np.int64)
        reg[vis] = face_regions[fid[vis]]
        o = rgb.astype(np.float32).copy()
        lab = vis & (reg > 0)
        o[lab] = 0.3 * rgb[lab] + 0.7 * lut[reg[lab]]
        edge = np.zeros_like(vis)
        edge[:-1] |= reg[:-1] != reg[1:]
        edge[:, :-1] |= reg[:, :-1] != reg[:, 1:]
        o[edge & vis] = 20
        im = Image.fromarray(o.astype(np.uint8)).crop(box)
        draw = ImageDraw.Draw(im)
        for r in regions:
            ry, rx = np.nonzero(reg == r.id)
            if ry.size < 400:
                continue
            k = np.argmin((ry - ry.mean()) ** 2 + (rx - rx.mean()) ** 2)
            x, y = int(rx[k]) - box[0], int(ry[k]) - box[1]
            text = r.label.split(".", 1)[-1]
            x0, y0, x1, y1 = draw.textbbox((x, y), text, font=small, anchor="mm")
            draw.rounded_rectangle((x0 - 4, y0 - 3, x1 + 4, y1 + 3), 4, fill=(18, 18, 20))
            draw.text((x, y), text, font=small, fill=(240, 240, 240), anchor="mm")
        draw.text((10, 8), name, font=font, fill=(30, 30, 30))
        im.save(out / f"regions_{name}.png")
        tiles.append(im)
        shade_l = rgb.astype(np.float32).mean(axis=2, keepdims=True) / 255.0
        p = np.full(rgb.shape, 235, np.float32)
        p[lab] = flat[reg[lab]] * np.clip(0.55 + 0.6 * shade_l[lab], 0, 1.2)
        p[edge & vis] = 60
        fim = Image.fromarray(np.clip(p, 0, 255).astype(np.uint8)).crop(box)
        fim.save(out / f"flat_{name}.png")
        flats.append(fim)
    tw, th = tiles[0].size
    row_h = 24
    legend_w = 360
    legend_h = 40 + row_h * len(regions)
    sheet_h = max(2 * th, legend_h)
    sheet = Image.new("RGB", (tw * len(tiles) + legend_w, sheet_h), (235, 235, 235))
    for i, (a, b) in enumerate(zip(tiles, flats, strict=True)):
        sheet.paste(a, (i * tw, 0))
        sheet.paste(b, (i * tw, th))
    draw = ImageDraw.Draw(sheet)
    lx = tw * len(tiles)
    draw.rectangle((lx, 0, sheet.width, sheet_h), fill=(22, 22, 24))
    draw.text((lx + 16, 12), "id  region", font=font, fill=(150, 150, 155))
    for i, r in enumerate(regions):
        y = 40 + i * row_h
        draw.rounded_rectangle((lx + 16, y, lx + 30, y + 14), 3, fill=rid_color[r.id])
        draw.rounded_rectangle(
            (lx + 34, y, lx + 48, y + 14), 3, fill=tuple(int(c) for c in flat[r.id])
        )
        draw.text((lx + 58, y), f"{r.id:>2}", font=small, fill=(150, 150, 155))
        draw.text((lx + 82, y), r.label, font=small, fill=(235, 235, 238))
        draw.text((lx + 290, y), colors[r.id]["name"], font=small, fill=(150, 150, 155))
    sheet.save(out / "sheet.png")


if __name__ == "__main__":
    main()
