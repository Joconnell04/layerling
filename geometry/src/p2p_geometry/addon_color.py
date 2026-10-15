"""Add-on part colors (back, loop, plate, text, base) from the approved product, never "the
darkest loaded PLA" (the old default made a white dog mostly black plastic).

For each add-on part without an explicit filament:
1. Target color, in this order: the job's pick for that role (intake: plate color, name color,
   ...), then the approved concept's read of that add-on (a vision read the worker caches).
2. With no target:
   - loop, base: the figure color the part touches (the texture cluster with the most figure
     area within TOUCH_MM of it): a loop on a black head is black, on a white head white;
   - back of a flat charm, plate: the figure's main color (the pet's palette), so a white dog
     gets a white back and a plate matches the coat;
   - text: the figure palette color with the highest CIEDE2000 against the plate.
3. The target goes to a filament under the achromatic guard (a gray or white target never
   takes a colored spool), owned spools first (an unowned color costs UNOWNED_PENALTY_DE more),
   and a filament the figure itself will use wins when it is within SHARE_DE of the best, so
   add-ons rarely cost an AMS slot. Unowned catalog colors are candidates only when the job
   allows source any.

The figure palette is a k-means of the Meshy texture per face (CIELAB, area weighted), the same
texture the paint agent starts from.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from p2p_contracts.filaments import Filament, FilamentLibrary

from .color import ciede2000, hex_to_lab

TOUCH_MM = 1.5  # figure faces this close to a part are the color it touches
SHARE_DE = 4.0  # a filament the figure uses wins within this of the best match (ASSUMED)
UNOWNED_PENALTY_DE = 3.0  # same rule as the worker's s08 (printing.UNOWNED_PENALTY_DE)
ACHROMATIC_TARGET_C = 10.0  # the worker's and the paint server's guard thresholds
ACHROMATIC_FILAMENT_C = 6.0
GUARD_PENALTY = 100.0
MIN_CLUSTER_SHARE = 0.01
TEXT_MIN_DE = 25.0  # a pet-palette name color must stand out at least this much (ASSUMED)
AUTO_PICK_LINES = ("PLA Basic", "PLA Matte")  # the worker's printing.AUTO_PICK_LINES
ROLE_KEY = {"keyring": "loop", "hanging_loop": "loop", "plate": "plate", "text": "text",
            "back": "back", "base": "base"}
ROLE_ADDON = {"keyring": "keyring_loop", "hanging_loop": "hanging_loop", "plate": "nameplate",
              "back": "flat_back", "base": ("round_base", "oval_base")}


@dataclass
class AddonColorInputs:
    """What the worker knows about add-on colors before geometry runs."""

    face_rgb: np.ndarray | None = None  # (n GLB faces, 3) uint8, Meshy texture per source face
    targets: dict[str, str] = field(default_factory=dict)  # role key -> "#RRGGBB"
    target_sources: dict[str, str] = field(default_factory=dict)  # role key -> job | concept
    source: str = "owned"  # palette source: owned, or any (unowned catalog colors allowed)


def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """(N, 3) uint8 sRGB -> (N, 3) CIELAB D65 (vectorized color.hex_to_lab)."""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = lin @ m.T / np.array([0.95047, 1.0, 1.08883])
    d = 6 / 29
    f = np.where(xyz > d ** 3, np.cbrt(xyz), xyz / (3 * d * d) + 4 / 29)
    return np.stack([116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])],
                    axis=1)


def lab_to_hex(lab) -> str:
    L, a, b = (float(x) for x in lab)
    fy = (L + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200
    d = 6 / 29

    def finv(t):
        return t ** 3 if t > d else 3 * d * d * (t - 4 / 29)

    x, y, z = 0.95047 * finv(fx), finv(fy), 1.08883 * finv(fz)
    r = 3.2404542 * x - 1.5371385 * y - 0.4985314 * z
    g = -0.9692660 * x + 1.8760108 * y + 0.0415560 * z
    bl = 0.0556434 * x - 0.2040259 * y + 1.0572252 * z

    def enc(c):
        c = max(0.0, min(1.0, c))
        c = 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
        return round(255 * c)

    return f"#{enc(r):02X}{enc(g):02X}{enc(bl):02X}"


def _chroma(lab) -> float:
    return math.hypot(float(lab[1]), float(lab[2]))


# ---------------------------------------------------------------- candidates and matching


def candidates(library: FilamentLibrary, source: str = "owned") -> list[Filament]:
    """PLA filaments an add-on may use: loaded (AMS order), owned, then with source any the
    unowned auto-pick lines. The figure is PLA; PETG and PLA do not bond in one print."""
    pla = [f for f in library.filaments if f.material.upper() == "PLA"] or list(library.filaments)
    loaded = sorted((f for f in pla if f.owned and f.ams_slot is not None),
                    key=lambda f: f.ams_slot or 0)
    owned = [f for f in pla if f.owned and f.ams_slot is None]
    out = loaded + owned
    if source == "any":
        out += [f for f in pla if not f.owned and f.line in AUTO_PICK_LINES]
    return out or list(library.filaments)


def match(lab, cands: list[Filament], prefer: set[str] | None = None) -> tuple[Filament, float]:
    """Nearest candidate under the achromatic guard, owned first; a preferred filament (one the
    figure uses) wins within SHARE_DE. Returns (filament, plain CIEDE2000)."""
    achro = _chroma(lab) <= ACHROMATIC_TARGET_C
    rows = []
    for f in cands:
        de = ciede2000(tuple(lab), tuple(f.lab))
        cost = de + (0.0 if f.owned else UNOWNED_PENALTY_DE)
        if achro and _chroma(f.lab) > ACHROMATIC_FILAMENT_C:
            cost += GUARD_PENALTY
        rows.append((cost, de, f))
    rows.sort(key=lambda r: r[0])
    best_cost, best_de, best = rows[0]
    for cost, de, f in rows:
        if prefer and f.id in prefer and cost <= best_cost + SHARE_DE:
            return f, de
    return best, best_de


# ---------------------------------------------------------------- figure palette


@dataclass
class FigurePalette:
    centers: np.ndarray  # (K, 3) CIELAB
    shares: np.ndarray  # (K,) area share
    face_cluster: np.ndarray  # (n figure faces,) cluster id, -1 for faces without texture
    filaments: list[Filament]  # per cluster

    def main(self) -> int:
        return int(np.argmax(self.shares))


def _kmeans(lab: np.ndarray, w: np.ndarray, k: int, iters: int = 12) -> np.ndarray:
    rng = np.random.default_rng(0)
    n = len(lab)
    idx = [int(np.argmax(w))]
    d2 = np.sum((lab - lab[idx[0]]) ** 2, axis=1)
    for _ in range(1, min(k, n)):
        p = d2 * w
        if p.sum() <= 0:
            break
        idx.append(int(rng.choice(n, p=p / p.sum())))
        d2 = np.minimum(d2, np.sum((lab - lab[idx[-1]]) ** 2, axis=1))
    c = lab[idx].copy()
    for _ in range(iters):
        lbl = np.argmin(((lab[:, None, :] - c[None]) ** 2).sum(axis=2), axis=1)
        for j in range(len(c)):
            m = lbl == j
            if m.any():
                c[j] = np.average(lab[m], axis=0, weights=w[m] + 1e-12)
    return c


def figure_palette(geo, face_rgb: np.ndarray, cands: list[Filament], k: int = 8
                   ) -> FigurePalette | None:
    """Texture clusters of the placed figure (area weighted) and the filament each maps to."""
    fs = np.asarray(geo.figure.face_source)
    rgb = np.asarray(face_rgb)
    ok = (fs >= 0) & (fs < len(rgb))
    if ok.sum() < 10:
        return None
    v, f = geo.figure.vertices, geo.figure.faces
    tri = v[f]
    area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    lab_all = np.zeros((len(f), 3))
    lab_all[ok] = srgb_to_lab(rgb[fs[ok]])
    sel = np.nonzero(ok)[0]
    rng = np.random.default_rng(1)
    sample = sel if len(sel) <= 40000 else rng.choice(sel, 40000, replace=False,
                                                        p=area[sel] / area[sel].sum())
    w = area[sample] if len(sel) <= 40000 else np.ones(len(sample))
    centers = _kmeans(lab_all[sample], w, k)
    cluster = np.full(len(f), -1, np.int64)
    # assign in chunks (1M faces x 8 centers)
    for s in range(0, len(sel), 200000):
        part = sel[s:s + 200000]
        cluster[part] = np.argmin(((lab_all[part][:, None, :] - centers[None]) ** 2).sum(axis=2),
                                  axis=1)
    shares = np.bincount(cluster[ok], weights=area[ok], minlength=len(centers))
    shares = shares / max(shares.sum(), 1e-12)
    keep = shares >= MIN_CLUSTER_SHARE
    remap = -np.ones(len(centers), np.int64)
    remap[keep] = np.arange(int(keep.sum()))
    centers, shares = centers[keep], shares[keep]
    cluster = np.where(cluster >= 0, remap[np.clip(cluster, 0, None)], -1)
    fils = [match(c, cands)[0] for c in centers]
    return FigurePalette(centers, shares, cluster, fils)


def touch_cluster(geo, part, pal: FigurePalette) -> int | None:
    """The figure texture cluster with the most area within TOUCH_MM of the part."""
    import trimesh
    from scipy.spatial import cKDTree

    pm = trimesh.Trimesh(part.vertices, part.faces, process=False)
    pts, _ = trimesh.sample.sample_surface(pm, 20000, seed=0)
    v, f = geo.figure.vertices, geo.figure.faces
    lo, hi = pm.bounds[0] - TOUCH_MM, pm.bounds[1] + TOUCH_MM
    cen = v[f].mean(axis=1)
    near_box = np.all((cen >= lo) & (cen <= hi), axis=1)
    idx = np.nonzero(near_box & (pal.face_cluster >= 0))[0]
    if not len(idx):
        return None
    d, _ = cKDTree(pts).query(cen[idx], distance_upper_bound=TOUCH_MM)
    idx = idx[np.isfinite(d)]
    if not len(idx):
        return None
    tri = v[f[idx]]
    area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    w = np.bincount(pal.face_cluster[idx], weights=area, minlength=len(pal.centers))
    return int(np.argmax(w))


# ---------------------------------------------------------------- assignment


def assign_addon_colors(geo, library: FilamentLibrary, ci: AddonColorInputs,
                        explicit: dict[str, str] | None = None,
                        text_fixed: bool = False) -> list[dict[str, Any]]:
    """Set the filament of every add-on part (see module doc). Writes geo.metrics
    ["addon_colors"] and one note; returns the per-part decisions."""
    explicit = explicit or {}
    cands = candidates(library, ci.source)
    pal = figure_palette(geo, ci.face_rgb, cands) if ci.face_rgb is not None else None
    prefer = {f.id for f in pal.filaments} if pal is not None else set()
    out: list[dict[str, Any]] = []
    plate_fil: Filament | None = None

    def fixed(role: str) -> bool:
        kinds = ROLE_ADDON.get(role)
        kinds = kinds if isinstance(kinds, tuple) else (kinds,)
        return any(k in explicit for k in kinds if k)

    def decide(part, lab, why: str, hexsrc: str | None = None):
        fil, de = match(lab, cands, prefer)
        part.filament_id = fil.id
        row = {"part": part.name, "role": part.role, "target_hex": hexsrc or lab_to_hex(lab),
               "from": why, "filament_id": fil.id, "delta_e": round(de, 1), "owned": fil.owned}
        out.append(row)
        return fil

    order = sorted((p for p in geo.parts if p.role != "cutter"),
                   key=lambda p: 1 if p.role == "text" else 0)  # plate before its text
    by_id = {f.id: f for f in library.filaments}
    for part in order:
        role = part.role
        key = ROLE_KEY.get(role, role)
        if role == "text":
            if text_fixed:
                out.append({"part": part.name, "role": role, "from": "job text filament",
                            "filament_id": part.filament_id})
                continue
        elif fixed(role):
            out.append({"part": part.name, "role": role, "from": "job filament",
                        "filament_id": part.filament_id})
            if role == "plate":
                plate_fil = by_id.get(part.filament_id)
            continue
        hx = ci.targets.get(key)
        if hx:
            fil = decide(part, hex_to_lab(hx), ci.target_sources.get(key, "job"), hx.upper())
        elif role == "text":
            fil = _text_color(part, plate_fil, pal, cands, out)
        elif pal is None:  # no texture and no pick: the geometry default stays, said so
            out.append({"part": part.name, "role": role, "from": "no texture or pick (default)",
                        "filament_id": part.filament_id})
            fil = by_id.get(part.filament_id)
        elif role in ("keyring", "hanging_loop", "base"):
            k = touch_cluster(geo, part, pal)
            k = pal.main() if k is None else k
            fil = decide(part, pal.centers[k], "figure color it touches")
        else:  # back, plate
            fil = decide(part, pal.centers[pal.main()], "figure main color")
        if role == "plate":
            plate_fil = fil
        if role == "text" and fil is not None and isinstance(geo.metrics.get("text"), dict):
            geo.metrics["text"]["filament_id"] = fil.id
            if plate_fil is not None:
                geo.metrics["text"]["delta_e_vs_plate"] = round(
                    ciede2000(tuple(fil.lab), tuple(plate_fil.lab)), 1)
    ti = geo.metrics.get("text")
    text_part, plate_part = geo.part("text"), geo.part("plate")
    if isinstance(ti, dict) and text_part is not None and plate_part is not None:
        tf, pf = by_id.get(text_part.filament_id), by_id.get(plate_part.filament_id)
        if tf is not None and pf is not None:  # the nameplate note named the old colors
            geo.notes = [
                (f"Name {ti.get('text')!r}: cap {float(ti.get('cap_height_mm', 0)):.1f} mm, "
                 f"relief {ti.get('relief_mm')} mm, {tf.name} on {pf.name} (dE00 "
                 f"{ciede2000(tuple(tf.lab), tuple(pf.lab)):.0f})")
                if n.startswith("Name ") and " on " in n else n for n in geo.notes]
    geo.metrics["addon_colors"] = out
    if pal is not None:
        geo.metrics["figure_palette"] = [
            {"hex": lab_to_hex(c), "share": round(float(s), 4), "filament_id": f.id}
            for c, s, f in sorted(zip(pal.centers, pal.shares, pal.filaments, strict=True),
                                  key=lambda t: -t[1])]
    if out:
        geo.notes.append("Add-on colors: " + "; ".join(
            f"{r['part']} {r['filament_id']} ({r['from']})" for r in out))
    return out


def _text_color(part, plate: Filament | None, pal: FigurePalette | None,
                cands: list[Filament], out: list) -> Filament | None:
    if plate is None:
        return None
    seen: dict[str, Filament] = {}
    for f in (pal.filaments if pal is not None else []):
        seen.setdefault(f.id, f)
    pool = [f for f in seen.values() if f.id != plate.id]
    best = max(pool, key=lambda f: ciede2000(tuple(f.lab), tuple(plate.lab)), default=None)
    why = "figure palette color with the most contrast to the plate"
    if best is None or ciede2000(tuple(best.lab), tuple(plate.lab)) < TEXT_MIN_DE:
        owned = [f for f in cands if f.owned and f.id != plate.id] or [
            f for f in cands if f.id != plate.id]
        best = max(owned, key=lambda f: ciede2000(tuple(f.lab), tuple(plate.lab)), default=None)
        why = "most contrast to the plate (no figure color stands out)"
    if best is None:
        return None
    part.filament_id = best.id
    out.append({"part": part.name, "role": "text", "target_hex": best.hex.upper(), "from": why,
                "filament_id": best.id,
                "delta_e": round(ciede2000(tuple(best.lab), tuple(plate.lab)), 1),
                "owned": best.owned})
    return best
