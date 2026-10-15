"""Nozzle map ("Auto For Flush") and filament order inside each layer, as Bambu Studio 02.08.02.61
runs them from the CLI on the X2D (sections 7.1 to 7.3 of docs/research/native-slicer.md).

Nozzles: 0 left (Direct Drive), 1 right (Bowden). Slots are 1-based project filaments. A map is a
list of 1-based nozzles per slot, as `filament_maps` in Bambu's slice_info.

How it works:
- `layer_orders`: per nozzle, walking layers upward, the nozzle's filaments on a layer are ordered to
  minimise flush from the filament the nozzle holds, through the layer, plus the best order of the
  next layer. The layer prints the nozzle that finished the previous layer first, then the other.
- `filament_map`: every map of the used filaments is scored (flush, AMS load and unload time, fill
  print time, a small penalty when the left nozzle gets less than half). Among maps close to the
  best, the one whose filaments best match the CLI's fake AMS trays wins, then the best score.

Checked against all 20 calibration slices (slicer/tests/test_slicer_grouping.py): same map, same
order on every layer, same T sequence.

Constants are SOURCE (spec 7.2, 7.3 and params JSON `source_constants.flush_and_changes`) unless
labelled ASSUMED.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

from .config import SliceConfig, as_list

FLUSH_TO_SECONDS = 0.4536        # SOURCE: grouping score seconds per mm3 of flush
LEFT_SHORT_PENALTY_S = 10.0      # SOURCE: left nozzle holds fewer than ceil(n/2) filaments
ENUM_LIMIT = 10000               # SOURCE: exhaustive while 2^n < 10000 (n <= 13)
FORECAST_MAX = 5                 # SOURCE: permutations with look-ahead up to 5 filaments
EXACT_MAX = 20                   # SOURCE: exact path search up to 20, greedy above
NEAR_BEST_MIN_S = 60.0           # SOURCE: near-best window floor
NEAR_BEST_CHANGES = 5.0          # SOURCE: window = 5 x one-change budget
NEAR_BEST_FRACTION = 0.01        # SOURCE: or 1 percent of the best score
ONE_CHANGE_FACTOR = 1.2          # SOURCE: budget = median pair flush x 0.4536 x 1.2
TRAYS_PER_NOZZLE = 4             # SOURCE: CLI sets 4 fake AMS slots on each nozzle
COLOUR_MATCH_DE = 20.0           # SOURCE: same type and CIEDE2000 under 20 is a match
COLOUR_MISS_COST = 1000.0        # ASSUMED: cost of a filament with no matching tray on its nozzle
LOAD_S = 29.0                    # SOURCE: machine_load_filament_time (read from config when set)
UNLOAD_S = 29.0                  # SOURCE: machine_unload_filament_time
BEAD_SHAPE = 1.0 - math.pi / 4   # 0.2146: rounded-rectangle cross-section h (w - 0.2146 h)

# Fill roles in the score and their speed keys (spec 7.2, 8.2). Role ids as in moves.Role.
FILL_SPEED_KEYS: dict[int, str] = {
    4: "sparse_infill_speed",           # Sparse infill
    5: "internal_solid_infill_speed",   # Internal solid infill
    6: "top_surface_speed",             # Top surface
    7: "internal_solid_infill_speed",   # Bottom surface above layer 0 (ASSUMED key)
    8: "bridge_speed",                  # Bridge
    20: "internal_bridge_speed",        # Internal bridge (falls back to bridge_speed)
}


# ----------------------------------------------------------------------------- order in a layer


def _round(v: float) -> float:
    return round(v, 6)


def path_flush(m: np.ndarray, current: int | None, order: Sequence[int]) -> tuple[float, int]:
    """Flush mm3 and number of changes printing `order` (1-based slots) after `current`."""
    cost = 0.0
    changes = 0
    prev = current
    for f in order:
        if prev is not None and prev != f:
            cost += float(m[prev - 1, f - 1])
            changes += 1
        prev = f
    return cost, changes


def _held_karp(m: np.ndarray, current: int | None, fils: list[int]) -> tuple[int, ...]:
    """Exact cheapest open path through `fils` starting after `current` (ties: lowest ids)."""
    n = len(fils)
    full = (1 << n) - 1
    inf = float("inf")
    best: dict[tuple[int, int], tuple[float, int]] = {}
    for i, f in enumerate(fils):
        c = float(m[current - 1, f - 1]) if current is not None and current != f else 0.0
        best[(1 << i, i)] = (c, -1)
    for mask in range(1, full + 1):
        for last in range(n):
            if not mask & (1 << last) or (mask, last) not in best:
                continue
            c0 = best[(mask, last)][0]
            for nxt in range(n):
                if mask & (1 << nxt):
                    continue
                key = (mask | (1 << nxt), nxt)
                c = c0 + float(m[fils[last] - 1, fils[nxt] - 1])
                if c < best.get(key, (inf, 0))[0] - 1e-9:
                    best[key] = (c, last)
    end = min(range(n), key=lambda i: (_round(best[(full, i)][0]), fils[i]))
    out = []
    mask, cur = full, end
    while cur != -1:
        out.append(fils[cur])
        prev = best[(mask, cur)][1]
        mask ^= 1 << cur
        cur = prev
    return tuple(reversed(out))


def _greedy(m: np.ndarray, current: int | None, fils: list[int]) -> tuple[int, ...]:
    left = sorted(fils)
    out: list[int] = []
    prev = current
    while left:
        nxt = min(left, key=lambda f: (0.0 if prev is None or prev == f
                                       else float(m[prev - 1, f - 1]), f))
        out.append(nxt)
        left.remove(nxt)
        prev = nxt
    return tuple(out)


class NozzleOrderer:
    """Orders one nozzle's filaments on a layer (spec 7.3), memoised on (current, layer, next).

    Rule: with at most 5 filaments on this layer and the next, try every permutation and minimise
    flush from `current` through the layer plus the cheapest order of the next layer; ties go to
    fewer changes, then the lexicographically smallest order. Otherwise an exact open-path search
    (up to 20 filaments, no look-ahead), else nearest neighbour.
    """

    def __init__(self, m: np.ndarray) -> None:
        self.m = np.asarray(m, dtype=float)
        self._cache: dict[tuple[int | None, tuple[int, ...], tuple[int, ...]], tuple[int, ...]] = {}
        self._next: dict[tuple[int, tuple[int, ...]], tuple[float, int]] = {}

    def _best_next(self, last: int, nxt: tuple[int, ...]) -> tuple[float, int]:
        key = (last, nxt)
        hit = self._next.get(key)
        if hit is None:
            hit = min(((_round(c), ch) for c, ch in
                       (path_flush(self.m, last, q) for q in itertools.permutations(nxt))),
                      default=(0.0, 0))
            self._next[key] = hit
        return hit

    def order(self, current: int | None, fils: Iterable[int],
              next_fils: Iterable[int] = ()) -> tuple[int, ...]:
        f = tuple(sorted(set(fils)))
        g = tuple(sorted(set(next_fils)))
        if not f:
            return ()
        key = (current, f, g)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        if len(f) <= FORECAST_MAX and len(g) <= FORECAST_MAX:
            best: tuple[float, int, tuple[int, ...]] | None = None
            for p in itertools.permutations(f):
                c, ch = path_flush(self.m, current, p)
                if g:
                    c2, ch2 = self._best_next(p[-1], g)
                    c, ch = c + c2, ch + ch2
                k = (_round(c), ch, p)
                if best is None or k < best:
                    best = k
            assert best is not None
            out = best[2]
        elif len(f) <= EXACT_MAX:
            out = _held_karp(self.m, current, list(f))
        else:
            out = _greedy(self.m, current, list(f))
        self._cache[key] = out
        return out


@dataclass
class NozzleWalk:
    """One nozzle's filaments walked over all layers."""

    orders: list[tuple[int, ...]]   # per layer, this nozzle's filaments in print order
    flush_mm3: float                # sum of matrix flush over AMS swaps
    swaps: int                      # AMS swaps (changes inside this nozzle)
    first_loads: int                # 1 if the nozzle is used at all


def walk_nozzle(orderer: NozzleOrderer, sets: Sequence[Sequence[int]],
                start_orders: Sequence[int] | None = None) -> NozzleWalk:
    """Order one nozzle's filaments layer by layer. `sets[i]` holds only this nozzle's filaments.
    `start_orders`, when given, is layer 0's order on this nozzle (fixed by the caller)."""
    cur: int | None = None
    flush = 0.0
    swaps = first = 0
    orders: list[tuple[int, ...]] = []
    n = len(sets)
    for i in range(n):
        if i == 0 and start_orders is not None:
            p = tuple(start_orders)
        else:
            nxt = sets[i + 1] if i + 1 < n else ()
            p = orderer.order(cur, sets[i], nxt)
        for f in p:
            if cur is None:
                first += 1
            elif cur != f:
                flush += float(orderer.m[cur - 1, f - 1])
                swaps += 1
            cur = f
        orders.append(p)
    return NozzleWalk(orders, flush, swaps, first)


def _nozzle_of(filament_map: Sequence[int], slot: int) -> int:
    return int(filament_map[slot - 1]) - 1 if 0 < slot <= len(filament_map) else 0


def layer_orders(layer_sets: Sequence[Iterable[int]], filament_map: Sequence[int],
                 cfg: SliceConfig, first_layer_order: Sequence[int] | None = None
                 ) -> list[list[int]]:
    """Print order of slots on every layer (spec 7.3).

    `layer_sets[i]`: 1-based slots printing on layer i (spec 7.1). `filament_map`: 1-based nozzle
    per slot. `first_layer_order`: layer 0's order when the caller knows it (spec 7.1: by contour
    area of each filament's smallest island, largest first); used as given, after dropping slots not
    on layer 0 and appending missing ones by id. Without it, layer 0 is ordered like any other layer
    from empty nozzles, left nozzle first (ASSUMED).
    """
    sets = [sorted({int(s) for s in ls}) for ls in layer_sets]
    if not sets:
        return []
    mats = cfg.flush_matrix()
    orderers = [NozzleOrderer(mats[0]), NozzleOrderer(mats[1])]
    per_nozzle = [[[s for s in ls if _nozzle_of(filament_map, s) == e] for ls in sets]
                  for e in (0, 1)]
    out: list[list[int]] = []
    cur: list[int | None] = [None, None]
    last: int | None = None
    for i, ls in enumerate(sets):
        if i == 0 and first_layer_order is not None:
            order = [s for s in first_layer_order if s in ls]
            order += [s for s in ls if s not in order]
        else:
            parts = []
            for e in (0, 1):
                nxt = per_nozzle[e][i + 1] if i + 1 < len(sets) else []
                parts.append(list(orderers[e].order(cur[e], per_nozzle[e][i], nxt)))
            first = _nozzle_of(filament_map, last) if last is not None else 0
            order = parts[first] + parts[1 - first]
        for s in order:
            cur[_nozzle_of(filament_map, s)] = s
        if order:
            last = order[-1]
        out.append(order)
    return out


# ----------------------------------------------------------------------------- fill print time


def _fil_on_nozzle(cfg: SliceConfig, key: str, slot: int, nozzle: int, default: float) -> float:
    """A per-filament value for `slot` as if it were mapped to `nozzle` (its extruder variant)."""
    vals = as_list(cfg.project.get(key))
    n_fil = cfg.n_filaments
    n_var = len(as_list(cfg.project.get("printer_extruder_variant"))) or 6
    try:
        if n_fil and len(vals) == n_fil * n_var:
            return float(vals[(slot - 1) * n_var + cfg.variants[nozzle]])
        if len(vals) >= slot:
            return float(vals[slot - 1])
    except (TypeError, ValueError):
        pass
    return cfg.fil(key, slot, default)


def fill_seconds_from_paths(paths: Iterable[tuple[int, int, float, float, float, bool]],
                 cfg: SliceConfig) -> dict[int, tuple[float, float]]:
    """Fill print seconds per slot on (left, right) for the grouping score (spec 7.2).

    `paths`: (slot, role id, length mm, width mm, height mm, on layer 0). Only sparse, internal
    solid, top, bottom and bridge roles count; walls are skipped. Speed = min(role speed on that
    nozzle, max volumetric speed / (h (w - 0.2146 h))). Layer 0 fill uses
    initial_layer_infill_speed (ASSUMED for roles other than bottom surface).
    """
    speeds: dict[str, tuple[float, float]] = {}

    def role_speed(key: str) -> tuple[float, float]:
        if key not in speeds:
            k = key
            if key == "internal_bridge_speed" and cfg.resolved.get(key) in (None, "", []):
                k = "bridge_speed"
            speeds[key] = (cfg.nozzle(k, 0, 100.0), cfg.nozzle(k, 1, 100.0))
        return speeds[key]

    mvs: dict[int, tuple[float, float]] = {}
    out: dict[int, list[float]] = {}
    for slot, role, length, width, height, first in paths:
        key = FILL_SPEED_KEYS.get(int(role))
        if key is None or length <= 0:
            continue
        slot = int(slot)
        if slot not in mvs:
            mvs[slot] = (_fil_on_nozzle(cfg, "filament_max_volumetric_speed", slot, 0, 12.0),
                         _fil_on_nozzle(cfg, "filament_max_volumetric_speed", slot, 1, 12.0))
        sp = role_speed("initial_layer_infill_speed" if first else key)
        cs = max(height * (width - BEAD_SHAPE * height), 1e-6)
        acc = out.setdefault(slot, [0.0, 0.0])
        for e in (0, 1):
            acc[e] += length / max(min(sp[e], mvs[slot][e] / cs), 1e-6)
    return {k: (v[0], v[1]) for k, v in out.items()}


def fill_seconds_from_moves(rows: np.ndarray, cfg: SliceConfig) -> dict[int, tuple[float, float]]:
    """`fill_seconds_from_paths` from MoveTable rows (ours or Bambu's G-code read back)."""
    from .estimate import row_geometry
    from .moves import Kind
    ext = rows["kind"] == Kind.EXTRUDE
    fill = ext & np.isin(rows["role"], list(FILL_SPEED_KEYS))
    if not fill.any():
        return {}
    length = row_geometry(rows)["length"]
    first_layer = int(rows["layer"][ext].min()) if ext.any() else 0
    idx = np.nonzero(fill)[0]
    return fill_seconds_from_paths(((int(rows["slot"][i]), int(rows["role"][i]), float(length[i]),
                          float(rows["width"][i]), float(rows["height"][i]),
                          int(rows["layer"][i]) == first_layer) for i in idx), cfg)


# ----------------------------------------------------------------------------- nozzle map


def fake_trays(cfg: SliceConfig) -> list[list[int]]:
    """Slots whose colour and type the CLI puts in each nozzle's fake AMS trays: tray j of nozzle
    e holds filament (4e + j) mod N (left = slots 1-4, right = slots 5, 1, 2, 3 with 5 slots)."""
    n = cfg.n_filaments
    if n == 0:
        return [[], []]
    return [[(TRAYS_PER_NOZZLE * e + j) % n + 1 for j in range(TRAYS_PER_NOZZLE)] for e in (0, 1)]


def colour_costs(cfg: SliceConfig) -> np.ndarray:
    """(N, N) cost of matching filament a to a tray holding filament b's colour and type:
    CIEDE2000 when same type and under 20, else COLOUR_MISS_COST (ASSUMED)."""
    from p2p_threemf.colour import ciede2000, hex_to_lab
    cols = as_list(cfg.resolved.get("filament_colour"))
    types = as_list(cfg.resolved.get("filament_type"))
    n = len(cols)
    labs = []
    for c in cols:
        try:
            labs.append(hex_to_lab(c))
        except ValueError:
            labs.append((0.0, 0.0, 0.0))
    out = np.full((n, n), COLOUR_MISS_COST)
    for a in range(n):
        for b in range(n):
            ta = types[a] if a < len(types) else ""
            tb = types[b] if b < len(types) else ""
            if ta != tb:
                continue
            de = ciede2000(labs[a], labs[b])
            if de < COLOUR_MATCH_DE:
                out[a, b] = de
    return out


def map_colour_cost(fmap: Sequence[int], used: Sequence[int], trays: list[list[int]],
                    costs: np.ndarray) -> float:
    """Sum over used filaments of the best tray match on the filament's nozzle. Trays are not
    exclusive (ASSUMED; with the calibration palettes every filament finds its own colour)."""
    total = 0.0
    for s in used:
        e = _nozzle_of(fmap, s)
        total += min((float(costs[s - 1, t - 1]) for t in trays[e]), default=COLOUR_MISS_COST)
    return total


@dataclass
class MapScore:
    """One candidate nozzle map and its score parts (seconds)."""

    mask: int                        # bit i set = used filament i (ascending ids) on the right
    filament_map: list[int]          # 1-based nozzle per slot
    score: float
    flush_mm3: float
    swaps: int
    first_loads: int
    fill_s: float
    penalty_s: float
    colour_cost: float = 0.0


def one_change_budget(used: Sequence[int], mats: np.ndarray) -> float:
    """Median over used pairs of max(flush a to b, b to a), x 0.4536 x 1.2 (seconds). Both
    nozzles' matrices count; the median of an even count averages the middle two (ASSUMED)."""
    vals = [max(max(float(mats[k, a - 1, b - 1]), float(mats[k, b - 1, a - 1]))
                for k in range(mats.shape[0]))
            for a, b in itertools.combinations(used, 2)]
    if not vals:
        return 0.0
    return float(np.median(vals)) * FLUSH_TO_SECONDS * ONE_CHANGE_FACTOR


def score_maps(layer_sets: Sequence[Iterable[int]], cfg: SliceConfig, *,
               fill_seconds: dict[int, tuple[float, float]] | None = None,
               cli_fake_ams: bool = True) -> tuple[list[MapScore], list[int]]:
    """Score every map of the used filaments (spec 7.2 step 2). Returns (scores in mask order,
    used slots). More than 13 used filaments: maps from a greedy single-flip search (ASSUMED)."""
    sets = [sorted({int(s) for s in ls}) for ls in layer_sets]
    used = sorted({s for ls in sets for s in ls})
    n_slots = max([cfg.n_filaments] + used)
    mats = cfg.flush_matrix()
    orderers = [NozzleOrderer(mats[0]), NozzleOrderer(mats[1])]
    walks: dict[tuple[int, frozenset[int]], NozzleWalk] = {}
    swap_s = cfg.f("machine_load_filament_time", LOAD_S) + cfg.f("machine_unload_filament_time",
                                                                  UNLOAD_S)
    load_s = cfg.f("machine_load_filament_time", LOAD_S)
    fills = fill_seconds or {}
    trays = fake_trays(cfg) if cli_fake_ams else [[], []]
    ccost = colour_costs(cfg) if cli_fake_ams else None
    n = len(used)
    need_left = math.ceil(n / 2)

    def walk(e: int, subset: frozenset[int]) -> NozzleWalk:
        key = (e, subset)
        w = walks.get(key)
        if w is None:
            w = walk_nozzle(orderers[e], [[s for s in ls if s in subset] for ls in sets])
            walks[key] = w
        return w

    def score(mask: int) -> MapScore:
        right = frozenset(s for i, s in enumerate(used) if mask >> i & 1)
        left = frozenset(used) - right
        fmap = [2 if s in right else 1 for s in range(1, n_slots + 1)]
        wl, wr = walk(0, left), walk(1, right)
        flush = wl.flush_mm3 + wr.flush_mm3
        swaps = wl.swaps + wr.swaps
        first = wl.first_loads + wr.first_loads
        fill = sum(fills.get(s, (0.0, 0.0))[1 if s in right else 0] for s in used)
        pen = LEFT_SHORT_PENALTY_S if len(left) < need_left else 0.0
        total = FLUSH_TO_SECONDS * flush + swap_s * swaps + load_s * first + fill + pen
        ms = MapScore(mask, fmap, total, flush, swaps, first, fill, pen)
        if ccost is not None:
            ms.colour_cost = map_colour_cost(fmap, used, trays, ccost)
        return ms

    if n == 0:
        return [score(0)], used
    if (1 << n) < ENUM_LIMIT:
        return [score(m) for m in range(1 << n)], used
    # Too many filaments to enumerate: start all left, flip single filaments while it helps.
    seen = {0: score(0)}
    best = seen[0]
    improved = True
    while improved:
        improved = False
        for i in range(n):
            m = best.mask ^ (1 << i)
            if m not in seen:
                seen[m] = score(m)
            if seen[m].score < best.score - 1e-9:
                best, improved = seen[m], True
    return [seen[m] for m in sorted(seen)], used


def pick_map(scores: Sequence[MapScore], used: Sequence[int], cfg: SliceConfig, *,
             cli_fake_ams: bool = True) -> MapScore:
    """Spec 7.2 step 3: among maps within max(60, 5 x one-change budget, 1 percent of best) of the
    best score, the lowest colour match cost wins, then the best score, then the lowest mask."""
    best = min(scores, key=lambda s: (_round(s.score), s.mask))
    if not cli_fake_ams:
        return best
    window = max(NEAR_BEST_MIN_S, NEAR_BEST_CHANGES * one_change_budget(used, cfg.flush_matrix()),
                 NEAR_BEST_FRACTION * best.score)
    near = [s for s in scores if s.score <= best.score + window + 1e-9]
    return min(near, key=lambda s: (_round(s.colour_cost), _round(s.score), s.mask))


def filament_map(layer_sets: Sequence[Iterable[int]], cfg: SliceConfig, *,
                 fill_seconds: dict[int, tuple[float, float]] | None = None,
                 cli_fake_ams: bool = True) -> list[int]:
    """1-based nozzle per project slot, as Bambu's CLI picks it in "Auto For Flush" (spec 7.2).

    `layer_sets[i]`: 1-based slots printing on layer i. `fill_seconds`: slot -> (seconds on the
    left, seconds on the right) for its fill paths (`fill_seconds_from_paths`); None counts none,
    which changes nothing when both nozzles print a filament equally fast (12 mm3/s filaments at
    0.2 mm). `cli_fake_ams`: apply the CLI's fake AMS trays and colour match (True for parity with
    Bambu's CLI). Unused slots map left.
    """
    scores, used = score_maps(layer_sets, cfg, fill_seconds=fill_seconds, cli_fake_ams=cli_fake_ams)
    return list(pick_map(scores, used, cfg, cli_fake_ams=cli_fake_ams).filament_map)
