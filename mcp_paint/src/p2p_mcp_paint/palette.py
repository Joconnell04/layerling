"""Filament matching and palette choice: CIEDE2000 with an achromatic guard.

The guard: a low-chroma color (gray, black, white, a cool off-white) may only match a
low-chroma filament. Without it, a mid gray is closer to a dark brown than to black or white
(the Frenchie's gray shading became brown flecks at dE00 26.9). With it, gray goes to whichever
of black and white is nearer in lightness, and a gray spool is suggested when neither is within
SWAP_DE of the color.

suggest_palette picks k filaments that minimize the weighted guarded CIEDE2000 of every target
(region colors, weighted by area, and the color spec items) to its nearest palette filament.
Exhaustive over combinations when that is small, else greedy plus single swaps.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
from p2p_contracts.filaments import Filament
from p2p_sim.color import ciede2000, hex_to_rgb, srgb_to_lab

ACHROMATIC_TARGET_C = 10.0  # a target this gray (C*ab) may only match a gray filament
ACHROMATIC_FILAMENT_C = 6.0  # a filament this gray counts as achromatic
GUARD_PENALTY = 100.0  # added dE for a blocked pairing (still picked if nothing else exists)
SWAP_DE = 10.0  # a required color with no loaded match within this gets a swap suggestion
UNOWNED_PENALTY_DE = 3.0  # an unowned catalog color must beat an owned spool by this (s08's rule)
MAX_COMBOS = 60_000


def chroma(lab) -> np.ndarray:
    lab = np.asarray(lab, dtype=np.float64).reshape(-1, 3)
    return np.hypot(lab[:, 1], lab[:, 2])


def hex_lab(h: str) -> np.ndarray:
    return srgb_to_lab(np.array([hex_to_rgb(h)], dtype=np.uint8))[0]


def filament_labs(filaments: list[Filament]) -> np.ndarray:
    return np.array([f.lab for f in filaments], dtype=np.float64).reshape(-1, 3)


def de_matrix(targets, fil_labs) -> np.ndarray:
    """Plain CIEDE2000, (T, N)."""
    t = np.asarray(targets, dtype=np.float64).reshape(-1, 3)
    f = np.asarray(fil_labs, dtype=np.float64).reshape(-1, 3)
    if len(t) == 0 or len(f) == 0:
        return np.zeros((len(t), len(f)))
    a = np.repeat(t, len(f), axis=0)
    b = np.tile(f, (len(t), 1))
    return ciede2000(a, b).reshape(len(t), len(f))


def guard_mask(targets, fil_labs) -> np.ndarray:
    """True where the pairing is blocked: achromatic target, chromatic filament."""
    tc = chroma(targets)[:, None] <= ACHROMATIC_TARGET_C
    fc = chroma(fil_labs)[None, :] > ACHROMATIC_FILAMENT_C
    return tc & fc


def guarded_de(targets, fil_labs) -> tuple[np.ndarray, np.ndarray]:
    """(cost, plain dE): cost adds GUARD_PENALTY to blocked pairings."""
    de = de_matrix(targets, fil_labs)
    return de + GUARD_PENALTY * guard_mask(targets, fil_labs), de


def nearest(targets, filaments: list[Filament]) -> tuple[np.ndarray, np.ndarray]:
    """Index of the nearest filament per target under the guard, and its plain dE00."""
    cost, de = guarded_de(targets, filament_labs(filaments))
    j = np.argmin(cost, axis=1)
    return j, de[np.arange(len(j)), j]


@dataclass
class Target:
    key: str  # "r<id>" for a region, "spec:<i>" for a color spec item
    lab: np.ndarray
    weight: float
    required: bool
    region_ids: tuple[int, ...] = ()
    label: str = ""


def _search(cost: np.ndarray, w: np.ndarray, k: int, fixed: list[int]) -> list[int]:
    n = cost.shape[1]
    k = min(k, n)
    free = [i for i in range(n) if i not in fixed]
    need = max(0, k - len(fixed))
    if need == 0:
        return list(fixed)
    ncomb = 1
    for i in range(need):
        ncomb = ncomb * (len(free) - i) // (i + 1)
    if ncomb <= MAX_COMBOS:
        best, best_v = None, np.inf
        base = cost[:, fixed].min(axis=1) if fixed else np.full(cost.shape[0], np.inf)
        combos = list(itertools.combinations(free, need))
        for s in range(0, len(combos), 4096):
            chunk = np.array(combos[s:s + 4096])  # (C, need)
            m = cost[:, chunk].min(axis=2)  # (T, C)
            m = np.minimum(m, base[:, None])
            v = (w[:, None] * m).sum(axis=0)
            i = int(np.argmin(v))
            if v[i] < best_v:
                best_v, best = float(v[i]), list(chunk[i])
        return list(fixed) + [int(x) for x in best]
    chosen = list(fixed)
    cur = cost[:, chosen].min(axis=1) if chosen else np.full(cost.shape[0], 1e6)
    while len(chosen) < k:
        gains = [(w * np.minimum(cur, cost[:, j])).sum() if j not in chosen else np.inf
                 for j in range(n)]
        j = int(np.argmin(gains))
        chosen.append(j)
        cur = np.minimum(cur, cost[:, j])
    improved = True
    while improved:
        improved = False
        for pos in range(len(fixed), len(chosen)):
            for j in range(n):
                if j in chosen:
                    continue
                trial = chosen[:pos] + [j] + chosen[pos + 1:]
                v = (w * cost[:, trial].min(axis=1)).sum()
                if v < (w * cost[:, chosen].min(axis=1)).sum() - 1e-9:
                    chosen, improved = trial, True
    return chosen


def choose_palette(targets: list[Target], candidates: list[Filament], k: int,
                   fixed: list[str] = (), owned: set[str] | None = None) -> list[Filament]:
    """owned: ids of owned spools; the others cost UNOWNED_PENALTY_DE more (owned first)."""
    if not targets:
        return [f for f in candidates if f.id in fixed][:k]
    labs = np.array([t.lab for t in targets])
    w = np.array([t.weight for t in targets], dtype=np.float64)
    w = w / max(w.sum(), 1e-12)
    cost, _ = guarded_de(labs, filament_labs(candidates))
    if owned is not None:
        cost = cost + np.array([0.0 if f.id in owned else UNOWNED_PENALTY_DE
                                for f in candidates])[None, :]
    fixed_idx = [i for i, f in enumerate(candidates) if f.id in set(fixed)]
    pick = _search(cost, w, k, fixed_idx)
    # keep only filaments that some target actually uses (plus fixed ones)
    j = np.argmin(cost[:, pick], axis=1)
    used = {pick[i] for i in np.unique(j)} | set(fixed_idx)
    return [candidates[i] for i in pick if i in used]


def swap_suggestions(targets: list[Target], palette: list[Filament], loaded: list[Filament],
                     catalog: list[Filament], ams_slots: int,
                     usage_mm2: dict[str, float] | None = None) -> list[dict]:
    """Required colors with no loaded (or palette) match within SWAP_DE: which spool to load.

    Suggests the catalog filament that matches best under the guard, and the loaded spool to
    take out (unused by the palette first, then the least used)."""
    req = [t for t in targets if t.required]
    if not req or not catalog:
        return []
    have = {f.id: f for f in loaded + palette}.values()
    have = list(have)
    labs = np.array([t.lab for t in req])
    _, de_have = nearest(labs, have) if have else (None, np.full(len(req), np.inf))
    j_cat, de_cat = nearest(labs, catalog)
    by_fil: dict[str, dict] = {}
    for i, t in enumerate(req):
        if de_have[i] <= SWAP_DE or de_cat[i] >= de_have[i] - 3.0:
            continue
        f = catalog[int(j_cat[i])]
        s = by_fil.setdefault(f.id, {"load": f.id, "name": f"{f.brand} {f.line} {f.name}",
                                     "hex": f.hex, "for": [], "region_ids": [],
                                     "delta_e_now": 0.0, "delta_e_with": 0.0, "weight": 0.0})
        s["for"].append(t.label or t.key)
        s["region_ids"].extend(t.region_ids)
        s["delta_e_now"] = round(max(s["delta_e_now"], float(de_have[i])), 1)
        s["delta_e_with"] = round(max(s["delta_e_with"], float(de_cat[i])), 1)
        s["weight"] += t.weight
    out = sorted(by_fil.values(), key=lambda s: -s["weight"])
    usage = usage_mm2 or {}
    pal_ids = {f.id for f in palette}
    spare = sorted(loaded, key=lambda f: (f.id in pal_ids, usage.get(f.id, 0.0)))
    for s in out:
        s.pop("weight")
        if len(pal_ids) < ams_slots and len(loaded) < ams_slots:
            s["replace"] = None
            s["slot"] = None
        elif spare:
            victim = spare.pop(0)
            s["replace"] = victim.id
            s["slot"] = victim.ams_slot
        s["region_ids"] = sorted(set(s["region_ids"]))[:50]
    return out
