import sys
from pathlib import Path
import numpy as np
from PIL import Image
from p2p_threemf.reader import read_3mf_full
from p2p_sim.mesh import Mesh
from p2p_sim.render import render
from p2p_sim.camera import ALL_VIEWS
src, out, view = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
t = read_3mf_full(src, world=True)
v = t.vertices - t.vertices.mean(0) * np.array([1, 1, 0])
v[:, 2] -= v[:, 2].min()
mesh = Mesh(vertices=v, faces=t.faces)
pal = [tuple(int(c.lstrip("#")[i:i+2], 16) for i in (0, 2, 4)) for c in t.filament_colours]
slots = np.clip(t.face_slots - 1, 0, len(pal) - 1)
fc = np.array(pal, dtype=np.uint8)[slots]
print(src.name, len(t.faces), "faces", t.filament_colours, "views", list(ALL_VIEWS)[:20], file=sys.stderr)
r = render(mesh, view, kind="shaded", face_colors=fc, size_px=1200, bg=(236, 233, 226))
Image.fromarray(r.image).save(out)
im = np.asarray(Image.open(out)); mask = np.abs(im.astype(int) - [236, 233, 226]).sum(2) > 12
ys, xs = np.where(mask); pad = 40
im = im[max(ys.min()-pad,0):ys.max()+pad, max(xs.min()-pad,0):xs.max()+pad]
Image.fromarray(im).save(out)
