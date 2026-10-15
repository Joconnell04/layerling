"""The PLAN checklist, the parts code can decide.

Items (PLAN "Paint agent"): spec_coverage, face_parts, markings, islands, creases, slot_count,
sim_matches_intent, purge_under_40pct. Code decides all but sim_matches_intent, which the
agent judges from the simulate_print render at sign-off.

Purge follows PLAN "Changes from docs" (2026-09-24): purge dominates small multicolor prints,
so it is reported as a cost to reduce and never counts toward passed_all (passed=None). The
slicer's purge is used once the gate has run; before that the simulator's two-nozzle estimate.

Face-part colors (eyes, nose, mouth) come from the 3D model's texture, which carries the
approved concept, not from the intake color spec: its hexes are Haiku's guesses from the photos
(the Frenchie's "#8B4513" eyes printed amber where the concept's eyes are near black). A spec
item only rules a face part when it is marked measured or customer (`source`).
"""

from __future__ import annotations

import re

import numpy as np
from p2p_contracts.reports import Checklist, ChecklistItem

from . import palette, regionops

SPEC_DE = 15.0  # a spec color counts as present when a used filament is within this
MIN_ISLAND_MM2 = 1.0  # same-filament pieces under this are specks
SIM_ISLANDS_OK = 3
CREASE_MOVE_OK_MM2 = 2.0
CREASE_MOVE_OK_FRAC = 0.003
GATED = ("spec_coverage", "face_parts", "markings", "islands", "creases", "slot_count",
         "sim_matches_intent")
WORDS = {"saddle": ["back", "saddle"], "mask": ["face", "head", "mask"], "ears": ["ear"],
         "ear": ["ear"], "muzzle": ["muzzle", "face"], "chest": ["chest"], "back": ["back"],
         "eyes": ["eye"], "eye": ["eye"], "nose": ["nose"], "paws": ["paw"], "tail": ["tail"],
         "belly": ["belly"], "legs": ["legs", "paw"], "face": ["face", "head"],
         "inner": ["inner_ear"], "blaze": ["face"], "body": ["body", "sides", "other"]}


FACE_PARTS = {"eye", "nose", "mouth", "tongue", "pupil", "iris"}
TRUSTED_SPEC = ("measured", "customer")


def _face_item(item: dict) -> bool:
    """A spec item about eyes, nose or mouth."""
    return bool(set(_words(item.get("region", ""))) & FACE_PARTS) or (
        item.get("part") in FACE_PARTS)


def _trusted(item: dict) -> bool:
    return item.get("source") in TRUSTED_SPEC


def _allowed(s) -> list:
    """Filaments the paint may use: the palette and owned spools, plus the catalog when the
    job allows unowned colors."""
    seen, out = set(), []
    pool = [s.filament(x) for x in s.palette] + list(s.inp.library)
    if s.inp.allows_unowned:
        pool += list(s.inp.catalog)
    for f in pool:
        if f.id not in seen:
            seen.add(f.id)
            out.append(f)
    return out


def _words(region_text: str) -> list[str]:
    out: list[str] = []
    for w in re.findall(r"[a-z]+", (region_text or "").lower()):
        out += WORDS.get(w, [])
    return out


def _used_filaments(s) -> dict[str, float]:
    area = s.inp.figure.face_areas
    fil = s.face_filament()
    out = {f: float(area[fil == f].sum()) for f in set(fil.tolist())}
    for p in s.inp.parts:
        fid = s.part_filaments.get(p.name) or p.filament_id
        if fid:
            out[fid] = out.get(fid, 0.0) + float(p.mesh.face_areas.sum())
    return out


def _spec_present(s, hexcode: str, candidates: list[int] | None = None) -> tuple[bool, str]:
    lab = palette.hex_lab(hexcode)
    if candidates:
        fids = sorted({s.assignment.get(r, s.default_filament) for r in candidates})
    else:
        fids = sorted(_used_filaments(s))
    if not fids:
        return False, "nothing painted"
    labs = [s.filament(f).lab for f in fids]
    cost, de = palette.guarded_de([lab], labs)
    j = int(np.argmin(cost[0]))
    ok = cost[0, j] <= SPEC_DE
    return bool(ok), f"{fids[j]} dE00 {de[0, j]:.1f}"


def _regions_for(s, text: str) -> list[int]:
    ws = _words(text)
    if not ws:
        return []
    out = []
    for rid, r in s.regions.items():
        lab = r.label.lower()
        if any(w in lab for w in ws):
            out.append(rid)
    return out


def check_spec(s) -> ChecklistItem:
    # guessed face-part hexes are not a target (the texture is, see check_face_parts)
    spec = [x for x in s.inp.color_spec if x.get("hex") and (_trusted(x) or not _face_item(x))]
    if not spec:
        return ChecklistItem(id="spec_coverage", passed=None, by="code",
                             evidence="no color spec for this job")
    misses, notes = [], []
    for item in spec:
        ok, ev = _spec_present(s, item["hex"])
        tag = f"{item.get('region')} {item.get('color')}"
        notes.append(f"{tag}: {ev}")
        if not ok and item.get("required", True):
            misses.append(tag)
    ev = ("missing: " + "; ".join(misses) + ". " if misses else "") + " | ".join(notes)
    return ChecklistItem(id="spec_coverage", passed=not misses, by="code", evidence=ev[:1500])


def _face_axes(s) -> tuple[int, int]:
    """(up axis, left-right axis) of the figure's face in the print frame."""
    return (1, 0) if s.inp.orientation == "flat_face_up" else (2, 0)


def check_face_parts(s, sim_lost: set[int]) -> ChecklistItem:
    by_part: dict[str, list] = {}
    for r in s.regions.values():
        by_part.setdefault(r.part, []).append(r)
    eyes, noses, mouths = by_part.get("eye", []), by_part.get("nose", []), by_part.get("mouth", [])
    problems, ev = [], []
    if not eyes:
        problems.append("no eye region")
    if not noses:
        problems.append("no nose region")
    up, lr = _face_axes(s)
    if eyes and noses:
        nose = max(noses, key=lambda r: r.area_mm2)
        ez = np.mean([e.centroid_mm[up] for e in eyes])
        if ez <= nose.centroid_mm[up]:
            problems.append("eyes are not above the nose")
        ev.append(f"eyes {len(eyes)} at {'y' if up == 1 else 'z'}={ez:.1f}, nose at "
                  f"{nose.centroid_mm[up]:.1f}")
        if len(eyes) >= 2:
            xs = sorted(e.centroid_mm[lr] for e in eyes)
            if not (xs[0] < nose.centroid_mm[lr] < xs[-1]):
                problems.append("nose is not between the eyes")
    spec_for = {"eye": ["eye"], "nose": ["nose"], "mouth": ["mouth", "tongue"]}
    allowed = _allowed(s)
    for r in eyes + noses + mouths:
        if r.area_mm2 < MIN_ISLAND_MM2:
            continue  # color slivers of a part; despeckle material, not the part itself
        fid = s.assignment.get(r.id, s.default_filament)
        if r.id in sim_lost:
            problems.append(f"{r.label} ({r.id}) disappears in the simulated print")
        wants = []
        if r.color_lab is not None:  # the model's own color there, from the approved concept
            wants.append(("the model's texture", np.asarray(r.color_lab, float)))
        for item in s.inp.color_spec:  # only a measured or customer-stated face color rules
            words = _words(item.get("region", ""))
            if (item.get("hex") and _trusted(item)
                    and any(w in spec_for.get(r.part, []) for w in words)):
                wants = [(f"the {item.get('source')} spec ({item.get('color')})",
                          palette.hex_lab(item["hex"]))]
        for what, want in wants[:1]:
            j, _ = palette.nearest([want], allowed)
            best = allowed[int(j[0])]
            _, de_now = palette.guarded_de([want], [s.filament(fid).lab])
            _, de_best = palette.guarded_de([want], [best.lab])
            if best.id != fid and de_now[0, 0] > de_best[0, 0] + 5:
                if best.id not in s.palette and getattr(s, "plan", None):
                    # the palette decision: a closer spool counts only if it earns its place
                    ok, info = s.planner().earns_place(best.id, [r.id])
                    if not ok:
                        pal_f = [s.filament(x) for x in s.palette]
                        jp, _ = palette.nearest([want], pal_f)
                        _, de_pal = palette.guarded_de([want], [pal_f[int(jp[0])].lab])
                        if de_now[0, 0] <= de_pal[0, 0] + 5:
                            ev.append(f"{r.label} in {fid}: {best.id} is closer to {what} "
                                      f"but does not earn its place ({info['why']})")
                            continue
                problems.append(f"{r.label} ({r.id}) is {fid}; {what} says {best.id} "
                                f"(dE00 {de_best[0, 0]:.0f} vs {de_now[0, 0]:.0f})")
        nb = regionops.dominant_neighbor(s.inp.figure, np.where(s.fr == r.id, 0, -1), s.fr)
        other = nb.get(0)
        if other is not None and s.assignment.get(other, s.default_filament) == fid:
            ev.append(f"{r.label} same filament as its surroundings")
    ev.append(f"mouth regions {len(mouths)} (optional)")
    return ChecklistItem(id="face_parts", passed=not problems, by="code",
                         evidence=("; ".join(problems) + ". " if problems else "") + "; ".join(ev))


def check_markings(s) -> ChecklistItem:
    spec = [x for x in s.inp.color_spec if x.get("hex") and x.get("kind") in ("marking", "base")]
    if not spec:
        return ChecklistItem(id="markings", passed=None, by="code",
                             evidence="no markings in the color spec")
    misses, notes = [], []
    for item in spec:
        cands = _regions_for(s, item.get("region", ""))
        ok, ev = _spec_present(s, item["hex"], cands or None)
        where = f"{len(cands)} named regions" if cands else "anywhere"
        notes.append(f"{item.get('region')} {item.get('color')}: {ev} ({where})")
        if not ok:
            misses.append(f"{item.get('region')} {item.get('color')}")
    ev = ("missing: " + "; ".join(misses) + ". " if misses else "") + " | ".join(notes)
    return ChecklistItem(id="markings", passed=not misses, by="code", evidence=ev[:1500])


def broken_bands(s) -> list[str]:
    """Band-like regions (label names a band, trim, stripe, collar...) printing as scattered
    pieces (regionops.fragmented). live2 sombrero: the hat trim was 15 pieces, none under 1 mm2,
    so the speck count passed while the print showed red spots."""
    out = []
    for rid in sorted(s.regions):
        r = s.regions[rid]
        if not regionops.band_like(r.label):
            continue
        info = regionops.pieces(s.inp.figure, s.fr, rid)
        if regionops.fragmented(info):
            out.append(f"region {rid} {r.label}: {info['pieces']} pieces, largest "
                       f"{100 * info['largest_share']:.0f}%")
    return out


def check_islands(s, sim) -> ChecklistItem:
    fig = s.inp.figure
    n_ideal, a_ideal = regionops.islands(fig, s.face_filament(), MIN_ISLAND_MM2)
    n_sim, a_sim = regionops.islands(fig, sim.face_slots[: fig.n_faces], MIN_ISLAND_MM2)
    bands = broken_bands(s)
    ok = n_ideal == 0 and n_sim <= SIM_ISLANDS_OK and not bands
    ev = (f"pieces under {MIN_ISLAND_MM2} mm2: paint {n_ideal} ({a_ideal:.2f} mm2), simulated "
          f"print {n_sim} ({a_sim:.2f} mm2)")
    if bands:
        ev += ("; bands printing as spots (bridge them into one strip or paint them the color "
               "around them): " + "; ".join(bands))
    return ChecklistItem(id="islands", passed=ok, by="code", evidence=ev[:1500])


def check_creases(s) -> ChecklistItem:
    fig = s.inp.figure
    share = regionops.boundary_on_crease_share(fig, s.fr)
    dry = regionops.snap_to_creases(fig, s.fr, None, 1.0, 30.0)
    moved = float(fig.face_areas[dry != s.fr].sum())
    ok = moved <= max(CREASE_MOVE_OK_MM2, CREASE_MOVE_OK_FRAC * float(fig.face_areas.sum()))
    return ChecklistItem(id="creases", passed=ok, by="code",
                         evidence=f"{100 * share:.1f}% of boundary length on creases; "
                                  f"snap_to_creases would still move {moved:.2f} mm2")


def check_slots(s) -> ChecklistItem:
    used = _used_filaments(s)
    n = len([f for f, a in used.items() if a > 0])
    cap = s.inp.settings.ams_slots
    ok = n <= cap and len(s.palette) <= cap
    return ChecklistItem(id="slot_count", passed=ok, by="code",
                         evidence=f"{n} filaments used, palette {len(s.palette)}, AMS {cap}")


def check_sim(s, summ: dict) -> ChecklistItem:
    return ChecklistItem(id="sim_matches_intent", passed=None, by="model",
                         evidence=f"code facts: slicer changes {summ['changed_pct']}% of the "
                                  f"figure area; regions lost {summ['regions_lost'][:20]}; "
                                  f"bleed flags {summ['bleed_flags']}. The agent judges the render.")


def check_purge(s, summ: dict) -> ChecklistItem:
    base = s.baseline_sim or {}
    ref = f" (baseline estimate {base['purge_g']} g)" if "purge_g" in base else ""
    sl = s.last_slice or {}
    if sl.get("ok") and sl.get("purge_grams") is not None and sl.get("total_grams"):
        tot, pg = float(sl["total_grams"]), float(sl["purge_grams"])
        ev = (f"slicer: purge {pg:.2f} g of {tot:.2f} g ({100 * pg / max(tot - pg, 1e-9):.0f}% "
              f"of model mass); simulator estimate {summ['purge_g']} g{ref}")
    else:
        ev = (f"simulator estimate (two nozzles, calibrated on slicer runs): purge "
              f"{summ['purge_g']} g vs {summ['model_g']} g model ({summ['purge_pct']}%){ref}")
    return ChecklistItem(id="purge_under_40pct", passed=None, by="code",
                         evidence=ev + "; a cost to reduce, not a gate (PLAN changes 2026-09-24)")


def summarize(items: list[ChecklistItem]) -> tuple[bool, float]:
    gate = [i for i in items if i.id in GATED and i.passed is not None]
    passed_all = all(i.passed for i in gate) and not any(
        i.id in GATED and i.by == "model" and i.passed is None for i in items)
    fails = sum(1 for i in gate if not i.passed)
    conf = max(0.0, 1.0 - 0.2 * fails)
    return passed_all, round(conf, 2)


def run_checklist(s) -> Checklist:
    sim = s.run_sim()
    summ = s.sim_summary()
    lost = set(summ["regions_lost"])
    items = [check_spec(s), check_face_parts(s, lost), check_markings(s), check_islands(s, sim),
             check_creases(s), check_slots(s), check_sim(s, summ), check_purge(s, summ)]
    passed_all, conf = summarize(items)
    return Checklist(items=items, passed_all=passed_all, confidence=conf)
