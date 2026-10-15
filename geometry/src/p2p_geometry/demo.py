"""Build the five products from a Meshy GLB, render previews, write 3MF/STL, slice.

    uv run python -m p2p_geometry.demo --glb model.glb --out geometry/_out [--slice] [--only keychain]

Writes <out>/<product>/ (geometry.json, PLYs, report), <out>/<product>.png, <out>/<product>.3mf,
<out>/stl/, and <out>/summary.json after every product (partial results survive a kill).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from p2p_contracts.filaments import FilamentLibrary
from p2p_contracts.job import AddOn, AddOnKind, ProductType, TextSpec

from .export import write_plain_3mf, write_stl_set
from .meshio import load_meshy_glb
from .output import write_outputs
from .product import build_product_geometry
from .render import render_product

CASES: dict[str, tuple[ProductType, list[AddOn], float | None]] = {
    # the flat charm (README tables); the 3D keychain is form "figure", the default
    "keychain": (ProductType.KEYCHAIN,
                 [AddOn(kind=AddOnKind.KEYRING_LOOP, params={"form": "flat"})], 45.0),
    "nameplate": (
        ProductType.NAMEPLATE,
        [AddOn(kind=AddOnKind.NAMEPLATE, text=TextSpec(text="BISCUIT"))],
        120.0,
    ),
    "ornament": (ProductType.ORNAMENT, [AddOn(kind=AddOnKind.HANGING_LOOP)], 70.0),
    "magnet": (
        ProductType.MAGNET,
        [AddOn(kind=AddOnKind.FLAT_BACK),
         AddOn(kind=AddOnKind.MAGNET_POCKET, params={"pocket_d_mm": 10, "pocket_depth_mm": 2})],
        60.0,
    ),
    "figurine": (ProductType.FIGURINE, [AddOn(kind=AddOnKind.ROUND_BASE)], 90.0),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glb", required=True)
    ap.add_argument("--library", default="filaments/library.json")
    ap.add_argument("--out", default="geometry/_out")
    ap.add_argument("--slice", action="store_true")
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    lib = FilamentLibrary.model_validate_json(Path(a.library).read_text())
    mesh = load_meshy_glb(a.glb)
    summary_path = out / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    for name, (product, addons, target) in CASES.items():
        if a.only and name not in a.only:
            continue
        t = time.time()
        geo = build_product_geometry(mesh.vertices, mesh.faces, product, addons, target, lib, None)
        res = write_outputs(geo, out / name)
        png = render_product(geo, out / f"{name}.png", lib, mesh.face_colors, title=name)
        tmf = write_plain_3mf(geo, out / f"{name}.3mf")
        write_stl_set(geo, out / "stl", name)
        entry = {
            "build_s": round(time.time() - t, 1),
            "size_mm": [round(x, 1) for x in geo.size_mm],
            "figure_watertight": geo.figure.report.watertight,
            "parts": {p.name: {"role": p.role, "filament": p.filament_id, "watertight": w.watertight}
                      for p, w in zip(geo.parts, res.parts)},
            "warnings": geo.warnings,
            "notes": geo.notes,
            "preview": str(png),
            "threemf": str(tmf),
        }
        if a.slice:
            from .slicecheck import slice_file

            # Flat products are height fields and need no supports; the magnet pocket roof is
            # a 10 mm bridge that tree(auto) would otherwise fill with support.
            supports = geo.orientation != "flat"
            sr = slice_file(tmf, out / name / "slice", supports=supports)
            entry["slice"] = {
                "ok": sr.ok, "supports": "tree(auto)" if supports else "off",
                "print_time": sr.print_time, "print_time_s": sr.print_time_s,
                "grams": sr.grams, "layers": sr.layers, "support_features": sr.support_lines,
                "errors": sr.errors, "slicer_s": round(sr.seconds, 1),
            }
        summary[name] = entry
        summary_path.write_text(json.dumps(summary, indent=2))
        print(name, json.dumps({k: entry[k] for k in ("size_mm", "warnings")}),
              json.dumps(entry.get("slice", {})), flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
