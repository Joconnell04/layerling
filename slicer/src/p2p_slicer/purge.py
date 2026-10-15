"""Change plan: which filament changes happen, what each one flushes and where it goes
(sections 7.4 and 7.5 of the spec). The `Change` record is shared with tower.py and api.py.

Per change old -> new (new on nozzle n, which held `held_old`):
- nozzle switch: n differs from the old filament's nozzle. The old filament rams
  `filament_change_length` mm on the tower first.
- AMS swap: n held another filament. Flush V = flush_volumes_matrix[n][held_old][new] x
  flush_multiplier. A pure nozzle switch (n already holds `new`) and a first load flush nothing.
- flush into infill and support: whole sparse-infill islands of this layer whose filament printed
  earlier on the layer, then islands of support printed in the "don't care" support filament, are
  reassigned to `new` until V is used (the last island may overshoot; islands are never split).
- the rest goes to the purge chute: max(rest, 100 mm3); flush_length = chute / filament
  cross-section; the template waits SYNC T{ceil(flush_length / 125) x 5} seconds.
- billing: the first `nozzle_volume` (92) mm3 of the chute purge counts against the filament that
  left the nozzle, the rest against the new one.
- every change primes the new filament on the tower: 15 mm3 in Saving mode, else
  filament_prime_volume (the CLI forces 45).

The first filament is loaded by the start G-code: it is not a Change. `plan_changes` returns only
the T lines after it, so len(result) is Bambu's "filament changes". Checked on all 20 calibration
slices (slicer/tests/test_slicer_grouping.py): counts, T sequence, nozzle switches, AMS swaps,
SYNC seconds, and per change flush length given the sparse and support islands seen in Bambu's
G-code. The billing rule reproduces result.json's per-filament purge grams.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .config import SliceConfig

MIN_CHUTE_MM3 = 100.0        # SOURCE: g_min_purge_volume
SAVING_PRIME_MM3 = 15.0      # SOURCE: Saving mode prime volume
SYNC_STEP_MM = 125.0         # SOURCE: X2D change template, SYNC T{ceil(flush_length / 125) * 5}
SYNC_STEP_S = 5.0            # SOURCE
_NS_ABSORB = __import__("os").environ.get("P2P_NS_ABSORB", "1") == "1"
SUPPORT_KEY = 0              # sparse_islands key for support printed in the "don't care" filament


@dataclass
class Change:
    """One tool change (a T to a different filament), in print order.

    Slots are 1-based, nozzles 0 left / 1 right, layers 0-based (layer 0 = first layer).
    """

    layer: int
    order: int                   # index of this change within its layer (0 = first)
    old: int                     # slot printing before the change (0 = none: the first load)
    new: int                     # slot printing after
    old_nozzle: int
    new_nozzle: int
    nozzle_switch: bool          # T to the other nozzle
    ams_swap: bool               # the target nozzle held another filament (unload, load, flush)
    first_load: bool             # first filament ever loaded on the target nozzle
    held_old: int = 0            # slot the target nozzle held before (0 = none)
    flush_mm3: float = 0.0       # matrix volume x multiplier (0 unless ams_swap)
    absorbed_mm3: float = 0.0    # flushed into this layer's sparse infill (flush_into_infill)
    chute_mm3: float = 0.0       # purged to the chute (at least 100 mm3 when an AMS swap flushes)
    flush_length_mm: float = 0.0  # chute_mm3 / filament cross-section: the template's flush_length
    ramming_mm: float = 0.0      # old filament's filament_change_length on a nozzle switch, else 0
    prime_mm3: float = 0.0       # new filament's prime on the tower
    billed_mm3: dict[int, float] = field(default_factory=dict)  # chute mm3 per slot billed
    sync_s: float = 0.0          # SYNC T seconds after the flush (0 without a flush)
    # Islands reassigned to `new` (printed in `new` right after the change): (owner key, index)
    # into sparse_islands[layer]; the owner key is the slot whose region the sparse island belongs
    # to, or 0 for support.
    absorbed_islands: list[tuple[int, int]] = field(default_factory=list)


def _flag(cfg: SliceConfig, key: str, default: int) -> bool:
    return cfg.s(key, str(default)).strip().lower() in ("1", "true")


def prime_volume(cfg: SliceConfig, slot: int) -> float:
    """Tower prime for the incoming filament (spec 7.6)."""
    if cfg.s("prime_volume_mode", "Saving").strip().lower() == "saving":
        return SAVING_PRIME_MM3
    return cfg.fil("filament_prime_volume", slot, 45.0)


def sync_seconds(flush_length_mm: float) -> float:
    """SYNC T of the X2D change template; the length is rounded to the G-code's 3 decimals."""
    return math.ceil(round(flush_length_mm, 3) / SYNC_STEP_MM) * SYNC_STEP_S


def plan_changes(orders: Sequence[Sequence[int]], filament_map: Sequence[int], cfg: SliceConfig, *,
                 first_slot: int | None = None,
                 sparse_islands: Sequence[Mapping[int, Sequence[float]]] | None = None
                 ) -> list[Change]:
    """Every filament change of the print, in order (spec 7.4, 7.5).

    `orders[i]`: slots in print order on layer i (grouping.layer_orders). `filament_map`: 1-based
    nozzle per slot. `first_slot`: filament loaded by the start G-code (default: the first slot of
    the first non-empty layer); when it differs from that slot, the first change happens on
    layer 0. `sparse_islands[i]`: owner slot -> island volumes in mm3 (extrusion volume without
    the flow ratio) in print order, for sparse infill of the regions printed in that slot; key 0
    holds support islands printed in the "don't care" support filament. Without it nothing is
    flushed into the object, so every flush goes to the chute (an upper bound).
    """
    mats = cfg.flush_matrix()
    into_infill = _flag(cfg, "flush_into_infill", 1)
    into_support = _flag(cfg, "flush_into_support", 1)

    def nozzle(slot: int) -> int:
        return int(filament_map[slot - 1]) - 1 if 0 < slot <= len(filament_map) else 0

    if first_slot is None:
        first_slot = next((o[0] for o in orders if o), 0)
    if not first_slot:
        return []
    held = [0, 0]
    held[nozzle(first_slot)] = first_slot
    cur = first_slot
    out: list[Change] = []
    for layer, order in enumerate(orders):
        pool: Mapping[int, Sequence[float]] = {}
        if sparse_islands is not None and layer < len(sparse_islands):
            pool = sparse_islands[layer] or {}
        taken: set[tuple[int, int]] = set()
        printed: list[int] = []
        k = 0
        for slot in order:
            if slot == cur:
                printed.append(slot)
                continue
            n_new, n_old = nozzle(slot), nozzle(cur)
            h = held[n_new]
            ch = Change(layer=layer, order=k, old=cur, new=slot, old_nozzle=n_old,
                        new_nozzle=n_new, nozzle_switch=n_new != n_old,
                        ams_swap=h not in (0, slot), first_load=h == 0, held_old=h)
            if ch.nozzle_switch:
                ch.ramming_mm = cfg.fil("filament_change_length", cur, 0.0)
            ch.prime_mm3 = prime_volume(cfg, slot)
            if ch.ams_swap:
                ch.flush_mm3 = float(mats[n_new, h - 1, slot - 1])
            if ch.flush_mm3 <= 0 and not ch.first_load and _NS_ABSORB and slot == order[-1]:
                # a pure nozzle switch to the layer's last filament purges nothing, yet Bambu
                # still hands it the sparse infill of the filaments printed earlier on the layer,
                # as far as the matrix volume old -> new would reach (ASSUMED from the
                # calibration G-code: on two-filament layers the last filament prints all the
                # sparse infill; with three, a switch to the middle one takes none)
                _absorb(ch, pool, taken, printed, into_infill, into_support,
                        budget=float(mats[n_new, cur - 1, slot - 1]))
            if ch.flush_mm3 > 0:
                _absorb(ch, pool, taken, printed, into_infill, into_support)
                ch.chute_mm3 = max(ch.flush_mm3 - ch.absorbed_mm3, MIN_CHUTE_MM3)
                ch.flush_length_mm = ch.chute_mm3 / cfg.filament_area(slot)
                ch.sync_s = sync_seconds(ch.flush_length_mm)
                first_bill = min(cfg.nozzle("nozzle_volume", n_new, 92.0), ch.chute_mm3)
                ch.billed_mm3 = {h: first_bill}
                ch.billed_mm3[slot] = ch.billed_mm3.get(slot, 0.0) + ch.chute_mm3 - first_bill
            out.append(ch)
            held[n_new] = slot
            cur = slot
            printed.append(slot)
            k += 1
    return out


def _absorb(ch: Change, pool: Mapping[int, Sequence[float]], taken: set[tuple[int, int]],
            printed: Sequence[int], into_infill: bool, into_support: bool,
            budget: float | None = None) -> None:
    """Reassign whole islands to the new filament until the flush volume is used. Order: sparse
    islands of the slots printed earlier on the layer (in their print order), then support
    (ASSUMED; the calibration slices never had more islands than flush)."""
    keys = [s for s in printed if s in pool] if into_infill else []
    if into_support and SUPPORT_KEY in pool:
        keys.append(SUPPORT_KEY)
    for key in keys:
        for idx, vol in enumerate(pool[key]):
            if ch.absorbed_mm3 >= (ch.flush_mm3 if budget is None else budget):
                return
            if (key, idx) in taken:
                continue
            taken.add((key, idx))
            ch.absorbed_islands.append((key, idx))
            ch.absorbed_mm3 += float(vol)


def change_counts(changes: Sequence[Change]) -> dict[str, int]:
    """The three counts of spec 7.4: filament changes (every T after the first), AMS swaps
    (result.json filament_change_times) and nozzle switches."""
    return {"filament_changes": len(changes),
            "ams_swaps": sum(1 for c in changes if c.ams_swap),
            "nozzle_switches": sum(1 for c in changes if c.nozzle_switch)}


def tool_sequence(changes: Sequence[Change], first_slot: int) -> list[int]:
    """1-based slot of every T in the print, the start G-code's load first."""
    return [first_slot] + [c.new for c in changes]
