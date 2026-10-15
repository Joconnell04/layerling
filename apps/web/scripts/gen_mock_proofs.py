"""Mock proof renders for the studio's mock mode: the mock GLBs from the eight contract
PROOF_VIEWS through the pipeline's own renderer (p2p_sim.proof.proof_set), so mock results carry a
proof set shaped like s10's (contracts JobResult.proof_images).

Run from the repo root after `npm run gen:mock-assets`:

    uv run python apps/web/scripts/gen_mock_proofs.py

Writes apps/web/public/mock/proof/<set>/<kind>_<view>.jpg for three sets: kc3d (the 3D keychain,
public/mock/kc3d.glb and kc3d-preview.glb), flat (the flat charm, public/mock/preview-flat.glb) and
figurine (mesh.glb and preview.glb, every other product).
Kinds: model (texture; the flat charm has none, so its paint stands in), paint (filament colors),
print (simulate_print). 0 credits, no network.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
from p2p_contracts import SimParams
from p2p_sim.color import srgb_to_lab
from p2p_sim.mesh import load_glb, sample_texture
from p2p_sim.proof import proof_set
from p2p_sim.simulate import simulate_print_detailed

MOCK = Path(__file__).resolve().parents[1] / "public" / "mock"
# Filament slots of the mock result (scripts/gen-mock-assets.mjs FIL): brown, white, black, red.
FIL = ["#7C4B00", "#FFFFFF", "#161616", "#F72323"]
RGB = np.array([[int(h[i:i + 2], 16) for i in (1, 3, 5)] for h in FIL], np.uint8)


def srgb_to_linear8(c: np.ndarray) -> np.ndarray:
    x = c / 255.0
    return np.round(np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4) * 255)


def face_slots(glb: Path) -> np.ndarray:
    """1-based filament slot per face from the preview GLB's linear COLOR_0 (non-indexed
    triangles, uint8 VEC4, as gen-mock-assets writes them)."""
    raw = glb.read_bytes()
    jlen = struct.unpack_from("<I", raw, 12)[0]
    doc = json.loads(raw[20:20 + jlen])
    bin_ = raw[20 + jlen + 8:]
    acc = doc["accessors"][doc["meshes"][0]["primitives"][0]["attributes"]["COLOR_0"]]
    view = doc["bufferViews"][acc["bufferView"]]
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    vc = np.frombuffer(bin_, np.uint8, acc["count"] * 4, start).reshape(-1, 4)[:, :3].astype(float)
    fc = vc[0::3]
    lin = srgb_to_linear8(RGB.astype(float))
    return np.argmin(((fc[:, None, :] - lin[None]) ** 2).sum(-1), axis=1) + 1


def render(name: str, preview: Path, textured: Path | None, orientation: str, size_mm: float) -> None:
    mesh = load_glb(preview, size_mm)
    slots = face_slots(preview)
    assert len(slots) == mesh.n_faces, (len(slots), mesh.n_faces)
    paint = RGB[slots - 1]
    model = paint
    if textured is not None:
        tex = load_glb(textured, size_mm)
        assert tex.n_faces == mesh.n_faces and tex.texture is not None
        uv = tex.uv[tex.faces].mean(axis=1)
        model = sample_texture(tex.texture, uv)
    labs = np.array([srgb_to_lab(c) for c in RGB], float)
    sim = simulate_print_detailed(mesh, slots, labs, SimParams())
    out = MOCK / "proof" / name
    rows = proof_set(mesh, orientation, out, model_rgb=model, paint_rgb=paint, sim=sim,
                     size_px=480, fmt="jpg")
    for sheet in out.glob("*_sheet.png"):
        sheet.unlink()  # the studio shows the views, not the sheets
    print(name, len([r for r in rows if r["view"] != "sheet"]), "views")


if __name__ == "__main__":
    render("kc3d", MOCK / "kc3d-preview.glb", MOCK / "kc3d.glb", "upright", 45)
    render("flat", MOCK / "preview-flat.glb", None, "flat_face_up", 45)
    render("figurine", MOCK / "preview.glb", MOCK / "mesh.glb", "upright", 90)
