"""Sample assets for the fake Meshy server: PNG images, a figure GLB, and a Multi-Color 3MF.

The figure is a sitting dog built from solids (manifold3d), watertight, about 5,700 triangles,
in Meshy's glTF frame (Y up, front +Z, about 1.5 units tall). Its colors follow a beagle: tan
head, white body, black saddle and nose. The 3MF is the same mesh the way Meshy's Multi-Color
writes it (glTF turned Z up, x39.495, centred, base on z = 0; checked against a real Meshy file
in threemf/src/p2p_threemf/retarget.py), so p2p_geometry.build_product and
p2p_threemf.retarget_3mf run on fake jobs exactly as on real ones.
"""

from __future__ import annotations

import hashlib
import io
import re
import tempfile
from functools import cache
from pathlib import Path

import numpy as np

MESHY_Z_UP = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])
MESHY_SCALE = 39.495  # glTF units -> Meshy 3MF mm (75 mm tall figure)
# Meshy's own slot colors for the three regions (hex, approximate CIELAB)
MESHY_COLORS = (("Tan", "#BE8C50", (62.0, 12.0, 35.0)),
                ("White", "#F5F5F0", (96.0, 0.0, 2.0)),
                ("Black", "#191919", (9.0, 0.0, 0.0)))


def png(seed: str, size: int = 64, alpha: bool = True) -> bytes:
    """A solid-color PNG whose color depends on seed (so different tasks differ)."""
    from PIL import Image

    h = hashlib.sha256(seed.encode()).digest()
    color = (h[0], h[1], h[2], 255)
    mode = "RGBA" if alpha else "RGB"
    im = Image.new(mode, (size, size), color if alpha else color[:3])
    if alpha:  # transparent corners, like remove_background output
        for x in range(size // 8):
            for y in range(size // 8):
                im.putpixel((x, y), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


@cache
def figure() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(vertices, faces, face_slots) of the dog in the glTF frame; slots index MESHY_COLORS."""
    import manifold3d as m3d

    def ell(center, radii):
        return m3d.Manifold.sphere(1.0, 48).scale(radii).translate(center)

    solid = (ell((0, 0.55, 0), (0.45, 0.42, 0.62))          # body
             + ell((0, 1.15, 0.35), (0.36, 0.34, 0.36))      # head
             + ell((0, 1.05, 0.68), (0.16, 0.12, 0.16))      # muzzle
             + ell((-0.33, 1.2, 0.3), (0.08, 0.28, 0.16))    # ears
             + ell((0.33, 1.2, 0.3), (0.08, 0.28, 0.16)))
    for x in (-0.25, 0.25):
        for z in (-0.35, 0.35):
            solid = solid + (m3d.Manifold.cylinder(0.45, 0.12, 0.12, 24).rotate((90, 0, 0))
                             .translate((x, 0.45, z)))
    mesh = solid.to_mesh()
    v = np.asarray(mesh.vert_properties)[:, :3].astype(np.float64)
    f = np.asarray(mesh.tri_verts).astype(np.int64)
    v[:, 1] -= v[:, 1].min()
    c = v[f].mean(axis=1)
    slots = np.full(len(f), 2)  # white body
    slots[c[:, 1] > 0.85] = 1  # tan head
    slots[(c[:, 1] > 0.7) & (c[:, 1] < 0.95) & (c[:, 2] < 0.1)] = 3  # black saddle
    slots[c[:, 2] > 0.8] = 3  # black nose
    return v, f, slots


@cache
def glb() -> bytes:
    """The figure as a GLB with per-face colors."""
    import trimesh

    v, f, slots = figure()
    rgb = {i + 1: tuple(int(h[k:k + 2], 16) for k in (1, 3, 5)) + (255,)
           for i, (_, h, _) in enumerate(MESHY_COLORS)}
    colors = np.array([rgb[s] for s in slots], dtype=np.uint8)
    mesh = trimesh.Trimesh(v, f, face_colors=colors, process=False)
    return trimesh.exchange.gltf.export_glb(trimesh.Scene(mesh))


@cache
def threemf(n_colors: int = 4) -> bytes:
    """The figure as a Meshy-style Multi-Color 3MF (Bambu project, per-triangle paint)."""
    from p2p_contracts import Filament, PrintSettings
    from p2p_threemf import write_3mf

    v, f, slots = figure()
    mv = (v @ MESHY_Z_UP.T) * MESHY_SCALE
    mv[:, 0] -= (mv[:, 0].max() + mv[:, 0].min()) / 2
    mv[:, 1] -= (mv[:, 1].max() + mv[:, 1].min()) / 2
    mv[:, 2] -= mv[:, 2].min()
    k = max(1, min(n_colors, len(MESHY_COLORS)))
    palette = [Filament(id=f"meshy-c{i + 1}", brand="Bambu", line="PLA Basic", name=name,
                        hex=hx, lab=lab) for i, (name, hx, lab) in enumerate(MESHY_COLORS[:k])]
    face_slots = np.minimum(slots, k)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "model.3mf"
        write_3mf(mv, f, face_slots, palette, PrintSettings(), out, object_name="meshy")
        return out.read_bytes()


# ---------------------------------------------------------------- synthetic concept images
# The fake server draws what each image call would return, so the concept and multi-view code
# (paste-back alignment, view split, palettes) runs on real pixels in tests: a round-1 render is
# a small figure on a transparent background; an add-on edit is the same figure, scaled and
# moved, with a plate, loop or base drawn in; a multi-view task returns the figure from three
# sides on white; a Creative Lab prototype is a round badge around the figure.
FIG = 256


def _seed_colors(seed: str) -> list[tuple[int, int, int]]:
    h = hashlib.sha256(seed.encode()).digest()
    return [(60 + h[i] % 160, 60 + h[i + 1] % 160, 60 + h[i + 2] % 160) for i in (0, 3, 6)]


def _png(im) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def figure_image(seed: str, size: int = FIG):
    """A sitting-animal silhouette: body, head, ears and a marking, in three seeded colors."""
    from PIL import Image, ImageDraw

    body, head, mark = _seed_colors(seed)
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    s = size / 256
    d.ellipse((70 * s, 110 * s, 186 * s, 230 * s), fill=body + (255,))
    d.ellipse((88 * s, 40 * s, 168 * s, 120 * s), fill=head + (255,))
    d.polygon([(92 * s, 60 * s), (100 * s, 22 * s), (118 * s, 50 * s)], fill=head + (255,))
    d.polygon([(164 * s, 60 * s), (156 * s, 22 * s), (138 * s, 50 * s)], fill=head + (255,))
    d.ellipse((120 * s, 140 * s, 170 * s, 190 * s), fill=mark + (255,))
    d.ellipse((110 * s, 70 * s, 120 * s, 80 * s), fill=(20, 20, 20, 255))
    d.ellipse((136 * s, 70 * s, 146 * s, 80 * s), fill=(20, 20, 20, 255))
    return im


def open_image(data: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(data)).convert("RGBA")


def has_alpha(data: bytes) -> bool:
    import numpy as np

    return bool((np.asarray(open_image(data))[..., 3] < 128).any())


def _placed(src, scale: float, x: int, y: int, size: int = FIG):
    from PIL import Image

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    w, h = src.size
    f = src.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    return canvas, f, (x, y)


def edit_image(src_bytes: bytes, prompt: str) -> bytes:
    """What an add-on edit returns: the figure unchanged but smaller and moved, plus the add-on
    the prompt asks for (plate and name, loop, or base)."""
    from PIL import ImageDraw

    src = open_image(src_bytes)
    p = prompt.lower()
    if "name plate" in p:
        canvas, f, at = _placed(src, 0.55, 10, 70)
        d = ImageDraw.Draw(canvas)
        d.rounded_rectangle((6, 190, 250, 236), radius=10, fill=(90, 70, 50, 255))
        m = re.search(r'the name "([^"]+)"', prompt)
        d.text((140, 205), m.group(1) if m else "Name", fill=(250, 250, 250, 255))
        canvas.alpha_composite(f, at)
    elif "loop" in p:
        canvas, f, at = _placed(src, 0.8, 26, 50)
        canvas.alpha_composite(f, at)
        d = ImageDraw.Draw(canvas)
        d.ellipse((112, 20, 144, 52), outline=(200, 200, 200, 255), width=6)
    elif "base" in p:
        canvas, f, at = _placed(src, 0.75, 32, 20)
        d = ImageDraw.Draw(canvas)
        d.ellipse((30, 196, 226, 240), fill=(120, 120, 120, 255))
        canvas.alpha_composite(f, at)
    else:  # a tweak: the figure itself changes a little
        canvas, f, at = _placed(src, 1.0, 0, 0)
        canvas.alpha_composite(f, at)
        d = ImageDraw.Draw(canvas)
        d.ellipse((96, 150, 126, 180), fill=(230, 230, 230, 255))
    return _png(canvas)


def view_images(src_bytes: bytes, n: int = 3) -> list[bytes]:
    """generate_multi_view as the live probe returned it: n separate RGBA images, transparent
    background, each cropped tight to the subject; three-quarter, front, back."""
    from PIL import ImageEnhance, ImageOps

    src = open_image(src_bytes)
    out = []
    for i in range(n):
        # the fake's 3/4 view is the figure turned a little; its front is narrower and mirrored
        if i == 0:
            v = src.rotate(6, resample=3, expand=True)
        else:
            v = ImageOps.mirror(src).resize((int(src.width * 0.8), src.height), 3)
        if i >= 2:  # the back: no face to see, a little darker
            v = ImageEnhance.Brightness(v).enhance(0.85)
        box = v.getchannel("A").getbbox() or (0, 0, v.width, v.height)
        out.append(_png(v.crop(box)))
    return out


def charm_image(src_bytes: bytes) -> bytes:
    """A Creative Lab prototype: a round badge with the figure in low relief inside."""
    from PIL import Image, ImageDraw

    src = open_image(src_bytes)
    canvas = Image.new("RGBA", (FIG, FIG), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    d.ellipse((16, 16, 240, 240), fill=(220, 210, 190, 255), outline=(150, 130, 100, 255), width=8)
    f = src.resize((170, 170), Image.LANCZOS)
    canvas.alpha_composite(f, (43, 43))
    return _png(canvas)


def image_from_ref(ref: str | None) -> bytes | None:
    """Bytes of a data URI reference, else None."""
    if not ref or not ref.startswith("data:"):
        return None
    head, _, payload = ref.partition(",")
    if ";base64" not in head:
        return None
    import base64

    return base64.b64decode(payload)
