"""Helpers for demos and the CLI: texture-cluster regions, face-ID images, palettes.

texture_regions() is a stand-in labeler (region source "texture_cluster") so a raw Meshy GLB can
be rendered with overlays and simulated before SAM 3 labels exist. It is not the pipeline's
labeler.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .color import hex_to_rgb, lab_to_srgb, srgb_to_lab
from .geodesic import components
from .mesh import Mesh


def kmeans_lab(lab: np.ndarray, k: int, weights: np.ndarray | None = None, iters: int = 30):
    """Deterministic weighted k-means in Lab (farthest-point init). Returns (centers, labels)."""
    x = np.asarray(lab, dtype=np.float64)
    w = np.ones(len(x)) if weights is None else np.asarray(weights, dtype=np.float64)
    centers = [x[np.argmax(w)]]
    for _ in range(1, k):
        d = np.min(np.stack([np.linalg.norm(x - c, axis=1) for c in centers]), axis=0)
        centers.append(x[np.argmax(d * np.sqrt(w))])
    c = np.array(centers)
    lab_idx = np.zeros(len(x), dtype=np.int64)
    for _ in range(iters):
        d = np.linalg.norm(x[:, None, :] - c[None], axis=2)
        lab_idx = np.argmin(d, axis=1)
        new = np.array([
            np.average(x[lab_idx == j], axis=0, weights=w[lab_idx == j]) if (lab_idx == j).any()
            else c[j] for j in range(k)
        ])
        if np.allclose(new, c, atol=1e-3):
            break
        c = new
    return c, lab_idx


def texture_regions(mesh: Mesh, k: int = 5, min_faces: int = 30):
    """Cluster face texture colors into k colors, split into connected regions, and absorb
    regions smaller than min_faces into their largest neighbor.

    Returns (face_regions int32 (F,) from 1, face_cluster (F,), cluster_labs (k, 3)).
    """
    cols = mesh.face_texture_colors()
    if cols is None:
        raise ValueError("mesh has no texture or vertex colors")
    lab = srgb_to_lab(cols)
    centers, cl = kmeans_lab(lab, k, mesh.face_areas)
    for _ in range(3):
        n, comp = components(mesh, np.ones(mesh.n_faces, dtype=bool), cl)
        sizes = np.bincount(comp, minlength=n)
        small = sizes[comp] < min_faces
        if not small.any():
            break
        adj = mesh.face_adjacency
        a, b = np.concatenate([adj[:, 0], adj[:, 1]]), np.concatenate([adj[:, 1], adj[:, 0]])
        m = small[a] & ~small[b]
        if not m.any():
            break
        # each small face takes the cluster of its largest non-small neighbor component
        score = sizes[comp[b[m]]]
        order = np.lexsort((-score, a[m]))
        fa = a[m][order]
        first = np.ones(len(fa), dtype=bool)
        first[1:] = fa[1:] != fa[:-1]
        cl[fa[first]] = cl[b[m][order][first]]
    n, comp = components(mesh, np.ones(mesh.n_faces, dtype=bool), cl)
    return (comp + 1).astype(np.int32), cl, centers


def face_id_image(face_id: np.ndarray) -> np.ndarray:
    """Distinct color per face id (hash), black background."""
    fid = face_id.astype(np.int64)
    h = (fid * 2654435761) & 0xFFFFFFFF
    rgb = np.stack([(h >> 16) & 255, (h >> 8) & 255, h & 255], axis=-1).astype(np.uint8)
    rgb = np.maximum(rgb, 40)
    rgb[face_id < 0] = 0
    return rgb


def load_palette(path: str | Path) -> np.ndarray:
    """Palette file -> (K, 3) Lab. Accepts a JSON list of hex strings, of {"lab": [L, a, b]} or
    {"hex": "#RRGGBB"} objects, or {"palette": [...]}."""
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        data = data.get("palette", data.get("filaments"))
    labs = []
    for item in data:
        if isinstance(item, str):
            labs.append(srgb_to_lab(np.array(hex_to_rgb(item), dtype=np.uint8)))
        elif "lab" in item:
            labs.append(np.asarray(item["lab"], dtype=np.float64))
        else:
            labs.append(srgb_to_lab(np.array(hex_to_rgb(item["hex"]), dtype=np.uint8)))
    return np.array(labs)


def palette_json(labs: np.ndarray) -> list[dict]:
    rgb = lab_to_srgb(labs)
    return [{"hex": "#{:02X}{:02X}{:02X}".format(*c), "lab": [round(float(v), 3) for v in lab]}
            for c, lab in zip(rgb, labs)]
