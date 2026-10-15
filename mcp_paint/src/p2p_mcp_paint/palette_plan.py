"""Cost-aware palette decision (s09, before the paint agent; docs/runs/fidelity/rerun2).

The first fidelity rerun bought every catalog color that matched a little better, and purge
became 85 % of the Frenchie's grams (49.4 of 58.2 g, SOURCE: docs/runs/fidelity/rerun). Here
every filament must earn its place:

- Targets are what the customer approved and asked for: the approved concept's color areas
  (concept_read.json, by share of the figure) and the intake color spec from the photos (face
  part hexes are guesses and are skipped, as in the checklist). Without a concept read, the 3D
  model's texture per region stands in.
- A color the customer would miss: a target left DEFINING_DE or more from every palette
  filament (another hue: pink inner ears printed white are dE00 20, a tan face printed cream
  28) when some candidate brings it under that. Palettes that miss one are refused, whatever
  the target's size, so small defining colors (inner ears, a nose, spots) keep their spool.
- Shades (NOTICE_DE to DEFINING_DE off: light gray against white, dark chocolate against
  black) are a tradeoff: visible error V = sum of weight x max(0, dE00 - NOTICE_DE) against
  purge grams from the simulator's two-nozzle model (p2p_sim PurgeEstimator; the simulator is
  within 11 % of the slicer on the rerun, SOURCE: sim/README.md) and print time from a fit on
  five slices, both in units of V (G_PER_UNIT, H_PER_UNIT), plus BUY_UNIT per catalog spool to
  buy, so an owned spool wins unless the catalog color is clearly better.
- Objective J = V + purge_g / G_PER_UNIT + hours / H_PER_UNIT + BUY_UNIT x spools to buy, over
  palettes of up to the product's cap (CAPS) drawn from the best candidates per target.
  Near-duplicate shades (under MERGE_DE) never both pay their way, so they merge.
- Regions follow the concept: each region's texture color snaps to the concept color area it
  is closest to (within SNAP_DE) and takes that area's filament, so a speckled coat whose
  texture averages to gray prints in the coat's color, not a gray of its own.
- The decision records the chosen palette, why each filament is in it, what was dropped or
  merged and what it would have cost, and the alternatives it beat.

earns_place() is the same test for a filament the agent (or the concept check) wants to add
later: it must lower J, or bring a color the customer would miss under DEFINING_DE.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass

import numpy as np
from p2p_contracts import SimParams

from . import palette as pal

# ASSUMED thresholds and exchange rates (docs/runs/fidelity/rerun2/README.md, "Palette rule").
NOTICE_DE = 6.0  # dE00 under which a printed color reads as the target on a small piece
DEFINING_DE = 15.0  # a defining color this far off is another hue (the checklist's spec limit)
TEXTURE_DEFINING_SHARE = 0.05  # without a concept read: texture regions this big count
SNAP_DE = 20.0  # a region's texture this close to a concept color area takes that area's filament
SNAP_NAME_DE = 30.0  # the same when the concept names the region's part (textures run darker)
# Region label words that name a part (SAM 3 labels: s1.paw_front_left, s1.ear_right_white);
# sides, colors and "back" (a hind paw, not the back) say nothing about which concept area.
NOT_PARTS = {"s1", "s2", "s3", "left", "right", "front", "back", "marking", "other", "white",
             "black", "brown", "gray", "grey", "cream", "silver", "tan", "pink", "red",
             "orange", "dark", "light", "thin", "and"}
MERGE_DE = 8.0  # two palette filaments this close are near-duplicate shades
G_PER_UNIT = 10.0  # purge grams worth one unit of visible error (10 dE00 over 10 % of the figure)
H_PER_UNIT = 1.0  # print hours worth one unit
BUY_UNIT = 1.0  # a catalog spool to buy must gain this much V over an owned one ("clearly
# better": 10 dE00 less over 10 % of the figure, or 5 over 20 %)
SPEC_WEIGHT = 0.5  # the photo spec's total weight next to the concept's 1.0
CAPS = {"keychain": 4, "magnet": 4, "ornament": 4, "nameplate": 5, "figurine": 5}
DEFAULT_CAP = 5
POOL_PER_TARGET = 3  # best owned and best catalog candidates per target
EVAL_PER_SIZE = 6  # palettes per size scored with the purge model
DEFINING_PENALTY = 100.0
# Print time, derived: least squares on five Bambu Studio 2.08 X2D slices (SOURCE: the flat
# and prototype Frenchie, Bronco s09 a2 and both fidelity reruns' slice reports): seconds =
# 236.8 x model g + 39.3 x tool changes + 68.6 x purge g, within 8 % on all five.
TIME_S_PER_MODEL_G = 236.8
TIME_S_PER_CHANGE = 39.3
TIME_S_PER_PURGE_G = 68.6

FACE_GUESS = {"eye", "eyes", "nose", "mouth", "tongue", "pupil", "iris"}


@dataclass
class PlanTarget:
    name: str
    lab: np.ndarray
    weight: float
    kind: str  # concept, spec or texture
    defining: bool


def cap_for(product_type: str, ams_slots: int) -> int:
    return max(1, min(ams_slots, CAPS.get(str(product_type), DEFAULT_CAP)))


def _part_words(text: str) -> set[str]:
    """Part words of a region label or a concept area name, singular (paws -> paw)."""
    import re

    out = set()
    for w in re.findall(r"[a-z]+", str(text).lower()):
        if w in NOT_PARTS or len(w) < 3:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 3 else w)
    return out


def _words(text: str) -> set[str]:
    return {w for w in str(text).lower().replace("_", " ").replace("/", " ").split() if w}


def plan_targets(s) -> list[PlanTarget]:
    """The approved concept's color areas, the photo spec, else the texture by region."""
    inp = s.inp
    out: list[PlanTarget] = []
    cols = [c for c in (inp.concept_read or {}).get("figure_colors") or [] if c.get("hex")]
    total = sum(float(c.get("share") or 0.0) for c in cols) or 1.0
    for c in cols:
        share = float(c.get("share") or 0.0) / total
        out.append(PlanTarget(f"concept {c.get('area')}", pal.hex_lab(c["hex"]), share,
                              "concept", True))
    spec = [x for x in inp.color_spec if x.get("hex") and x.get("required", True)
            and (x.get("source") in ("measured", "customer")
                 or not (_words(x.get("region", "")) & FACE_GUESS))]
    for x in spec:  # the photos' colors; the approved concept wins where they disagree
        out.append(PlanTarget(f"photos {x.get('region')}", pal.hex_lab(x["hex"]),
                              SPEC_WEIGHT / len(spec), "spec", not cols))
    if not cols:
        regs = [r for r in s.regions.values() if r.color_lab is not None and r.area_mm2 > 0]
        area = sum(r.area_mm2 for r in regs) or 1.0
        for r in regs:
            face = r.part in ("eye", "nose", "mouth", "inner_ear", "tongue")
            out.append(PlanTarget(f"texture {r.label}", np.array(r.color_lab, float),
                                  r.area_mm2 / area, "texture",
                                  (face and r.area_mm2 >= 2.0)
                                  or r.area_mm2 / area >= TEXTURE_DEFINING_SHARE))
    return out


def plan_de(target_labs, fil_labs) -> tuple[np.ndarray, np.ndarray]:
    """(cost, plain dE00) with the achromatic guard for true neutrals only: a gray (C* under
    NEUTRAL_C) never matches a colored filament, but a warm off-white or cream (C* under 10,
    a clear hue) may match a filament of the same hue family (cream to desert tan), which the
    paint tools' stricter guard (palette.guard_mask) blocks."""
    t = np.asarray(target_labs, float).reshape(-1, 3)
    f = np.asarray(fil_labs, float).reshape(-1, 3)
    de = pal.de_matrix(t, f)
    block = pal.guard_mask(t, f)
    if block.any():
        th = np.degrees(np.arctan2(t[:, 2], t[:, 1]))[:, None]
        fh = np.degrees(np.arctan2(f[:, 2], f[:, 1]))[None, :]
        dh = np.abs((th - fh + 180.0) % 360.0 - 180.0)
        tc = pal.chroma(t)[:, None]
        block &= (tc < NEUTRAL_C) | (dh > SAME_HUE_DEG)
    return de + pal.GUARD_PENALTY * block, de


NEUTRAL_C = 4.0  # C*ab under this is a true neutral (gray shading, black, white)
SAME_HUE_DEG = 35.0


def _cost_matrix(targets: list[PlanTarget], cands) -> tuple[np.ndarray, np.ndarray]:
    labs = np.array([t.lab for t in targets]).reshape(-1, 3)
    return plan_de(labs, pal.filament_labs(cands))


def visible_error(cost_rows: np.ndarray, w: np.ndarray) -> np.ndarray:
    """V per palette column set: cost_rows (T, C) guarded dE of each target to its nearest
    filament in each candidate palette."""
    return (w[:, None] * np.maximum(0.0, cost_rows - NOTICE_DE)).sum(axis=0)


class Planner:
    """Scores palettes for one paint session (figure, regions, parts, targets)."""

    def __init__(self, s, cap: int | None = None):
        from p2p_sim import Mesh
        from p2p_sim.simulate import PurgeEstimator

        self.s = s
        inp = s.inp
        self.cap = cap or cap_for(inp.product_type, inp.settings.ams_slots)
        self.targets = plan_targets(s)
        self.fixed = list(dict.fromkeys(s.part_filaments.values()))
        allowed = list(inp.library) + (list(inp.catalog) if inp.allows_unowned else [])
        seen: set[str] = set()
        self.allowed = [f for f in allowed if not (f.id in seen or seen.add(f.id))]
        for fid in self.fixed:
            if fid not in seen:
                self.allowed.append(s.filament(fid))
                seen.add(fid)
        self.owned = {f.id for f in inp.library}
        meshes = [inp.figure] + [p.mesh for p in inp.parts]
        vs, fs, fp, off = [], [], [], 0
        for i, m in enumerate(meshes):
            vs.append(m.vertices)
            fs.append(m.faces + off)
            fp.append(np.full(m.n_faces, i, np.int64))
            off += m.n_vertices
        self.parts = inp.parts
        product = Mesh(np.concatenate(vs), np.concatenate(fs))
        self.estimator = PurgeEstimator(
            product, SimParams(nozzle_mm=inp.settings.nozzle_mm, layer_mm=inp.settings.layer_mm),
            face_parts=np.concatenate(fp) if len(meshes) > 1 else None)
        self.part_faces = [p.mesh.n_faces for p in inp.parts]
        self._est_cache: dict[tuple, dict] = {}

    # ------------------------------------------------------------ pieces
    def region_labs(self) -> tuple[list[int], np.ndarray]:
        ids = [rid for rid, r in self.s.regions.items() if r.color_lab is not None]
        return ids, np.array([self.s.regions[r].color_lab for r in ids], float).reshape(-1, 3)

    def assignment_for(self, ids: list[str], keep: dict[int, str] | None = None
                       ) -> dict[int, str]:
        """Each region to a palette filament through the concept color area it belongs to:
        the area the concept names by the region's part (paw -> "chest/legs/paws", within
        SNAP_NAME_DE of its texture: Meshy's texture is often darker than the concept), else
        the area nearest its texture (within SNAP_DE) that is big enough to hold it (at least
        a third of the region's share of the figure: a region a fifth of the figure is not
        the 2 % "spots"), else the filament nearest its texture. `keep` overrides."""
        s = self.s
        fils = [s.filament(i) for i in ids]
        rids, labs = self.region_labs()
        out = {rid: s.assignment.get(rid, s.default_filament) for rid in s.regions}
        if rids and fils:
            flabs = pal.filament_labs(fils)
            c_tex, _ = plan_de(labs, flabs)
            pick = np.argmin(c_tex, axis=1)
            snap = [t for t in self.targets if t.kind == "concept"]
            if snap:
                tl = np.array([t.lab for t in snap])
                c_rt, _ = plan_de(labs, tl)
                via = np.argmin(plan_de(tl, flabs)[0], axis=1)
                tot = sum(r.area_mm2 for r in s.regions.values()) or 1.0
                words = [_part_words(t.name.removeprefix("concept ")) for t in snap]
                share = np.array([t.weight for t in snap])
                for i, rid in enumerate(rids):
                    r = s.regions[rid]
                    named = [j for j, w in enumerate(words) if w & _part_words(r.label)]
                    j = None
                    if named:
                        jj = min(named, key=lambda k: c_rt[i, k])
                        if c_rt[i, jj] <= SNAP_NAME_DE:
                            j = jj
                    if j is None:
                        fits = share >= (r.area_mm2 / tot) / 3.0
                        cand = np.where(fits, c_rt[i], np.inf)
                        jj = int(np.argmin(cand))
                        if np.isfinite(cand[jj]) and cand[jj] <= SNAP_DE:
                            j = jj
                    if j is not None:
                        pick[i] = via[j]
            out.update({rid: fils[int(k)].id for rid, k in zip(rids, pick)})
        out.update(keep or {})
        return out

    def estimate(self, ids: list[str], assignment: dict[int, str]) -> dict:
        """Purge and time for a palette and region assignment (the two-nozzle model)."""
        s = self.s
        import hashlib

        key = (tuple(ids), tuple(sorted(assignment.items())),
               hashlib.blake2b(np.ascontiguousarray(s.fr).tobytes(), digest_size=8).hexdigest())
        if key in self._est_cache:
            return self._est_cache[key]
        idx = {f: i + 1 for i, f in enumerate(ids)}
        by_area: dict[str, float] = {}
        areas = {rid: r.area_mm2 for rid, r in s.regions.items()}
        for rid, fid in assignment.items():
            by_area[fid] = by_area.get(fid, 0.0) + areas.get(rid, 0.0)
        default = max(by_area, key=by_area.get) if by_area else ids[0]
        n = int(s.fr.max()) + 1
        lut = np.full(n, idx.get(default, 1), np.int64)
        for rid, fid in assignment.items():
            if 0 < rid < n and fid in idx:
                lut[rid] = idx[fid]
        fslots = lut[s.fr]
        part_slots = [idx.get(s.part_filaments.get(p.name) or p.filament_id or default,
                              idx.get(default, 1)) for p in self.parts]
        slots = np.concatenate([fslots] + [np.full(nf, ps, np.int64)
                                           for nf, ps in zip(self.part_faces, part_slots)])
        fils = [s.filament(i) for i in ids]
        rgb = np.array([pal.hex_to_rgb(f.hex) for f in fils], np.uint8)
        labs = pal.filament_labs(fils)
        body = int(np.bincount(fslots).argmax()) if len(fslots) else 1
        out = self.estimator.estimate(slots, rgb, labs,
                                      part_slots=[body] + part_slots if self.parts else None)
        out["time_s"] = (TIME_S_PER_MODEL_G * out["model_g"] + TIME_S_PER_CHANGE
                         * out["changes"] + TIME_S_PER_PURGE_G * out["purge_g"])
        self._est_cache[key] = out
        return out

    def score(self, ids: list[str], v: float, defining_missed: int,
              assignment: dict[int, str] | None = None) -> dict:
        assignment = assignment if assignment is not None else self.assignment_for(ids)
        est = self.estimate(ids, assignment)
        buy = sum(1 for i in ids if i not in self.owned)
        j = (v + est["purge_g"] / G_PER_UNIT + est["time_s"] / 3600.0 / H_PER_UNIT
             + BUY_UNIT * buy + DEFINING_PENALTY * defining_missed)
        return {"palette": list(ids), "visible_error": round(v, 3), "purge_g": round(
            est["purge_g"], 2), "time_h": round(est["time_s"] / 3600.0, 2),
            "changes": est["changes"], "buy": buy, "defining_missed": defining_missed,
            "J": round(j, 3)}

    # ------------------------------------------------------------ search
    def pool(self) -> list:
        """Fixed add-on filaments plus, per target, the best owned and catalog candidates."""
        if not self.targets:
            return list(self.allowed)
        cost, _ = _cost_matrix(self.targets, self.allowed)
        keep: set[int] = {i for i, f in enumerate(self.allowed) if f.id in self.fixed}
        own = np.array([f.id in self.owned for f in self.allowed])
        for row in cost:
            for mask in (own, ~own):
                idx = np.nonzero(mask)[0]
                if idx.size:
                    keep.update(int(i) for i in idx[np.argsort(row[idx])[:POOL_PER_TARGET]])
        return [self.allowed[i] for i in sorted(keep)]

    def plan(self) -> dict:
        t0 = time.time()
        s = self.s
        pool = self.pool()
        ids_all = [f.id for f in pool]
        fixed_idx = [ids_all.index(f) for f in self.fixed if f in ids_all]
        free = [i for i in range(len(pool)) if i not in fixed_idx]
        w = np.array([t.weight for t in self.targets], float)
        cost, de = _cost_matrix(self.targets, pool) if self.targets else (
            np.zeros((0, len(pool))), np.zeros((0, len(pool))))
        defining = np.array([t.defining for t in self.targets], bool)
        reachable = (cost[defining] < DEFINING_DE).any(axis=1) if defining.any() else \
            np.zeros(0, bool)
        buy = np.array([0.0 if f.id in self.owned else BUY_UNIT for f in pool])
        shortlist: list[tuple[float, tuple[int, ...], float, int]] = []
        lo = max(1, len(fixed_idx))
        for k in range(lo, self.cap + 1):
            need = k - len(fixed_idx)
            if need < 0 or need > len(free):
                continue
            rows = []
            for combo in itertools.combinations(free, need):
                cols = tuple(fixed_idx) + combo
                m = cost[:, list(cols)].min(axis=1) if len(cols) else np.full(len(w), 1e3)
                v = float((w * np.maximum(0.0, m - NOTICE_DE)).sum())
                miss = int(((m[defining] >= DEFINING_DE) & reachable).sum()) if \
                    defining.any() else 0
                rows.append((v + buy[list(cols)].sum() + DEFINING_PENALTY * miss, cols, v,
                             miss))
            rows.sort(key=lambda r: r[0])
            shortlist += rows[:EVAL_PER_SIZE]
        scored, seen = [], set()
        pid = [f.id for f in pool]
        for _, cols, v, miss in shortlist:
            ids = [pool[i].id for i in cols]
            # a filament no region takes prints nothing: score the palette without it
            asg = self.assignment_for(ids)
            used = [i for i in ids if i in set(asg.values()) or i in self.fixed]
            if used != ids:
                ids = used
                asg = self.assignment_for(ids)
                c = [pid.index(i) for i in ids]
                m = cost[:, c].min(axis=1) if c else np.full(len(w), 1e3)
                v = float((w * np.maximum(0.0, m - NOTICE_DE)).sum())
                miss = int(((m[defining] >= DEFINING_DE) & reachable).sum()) if \
                    defining.any() else 0
            key = frozenset(ids)
            if not ids or key in seen:
                continue
            seen.add(key)
            scored.append(self.score(ids, v, miss, asg))
        if not scored:
            ids = [pool[i].id for i in fixed_idx] or [pool[0].id]
            scored.append(self.score(ids, 0.0, 0))
        scored.sort(key=lambda r: r["J"])
        best = scored[0]
        return self._decision(best, scored, pool, cost, de, round(time.time() - t0, 2))

    # ------------------------------------------------------------ the record
    def _serves(self, ids: list[str], pool, de) -> dict[str, list[dict]]:
        pids = [f.id for f in pool]
        cols = [pids.index(i) for i in ids if i in pids]
        out: dict[str, list[dict]] = {i: [] for i in ids}
        if not cols or not self.targets:
            return out
        cost, _ = _cost_matrix(self.targets, [pool[c] for c in cols])
        j = np.argmin(cost, axis=1)
        for t, jj, row in zip(self.targets, j, cost):
            out[ids[int(jj)]].append({"target": t.name, "weight": round(t.weight, 3),
                                      "delta_e": round(float(row[jj]), 1),
                                      "defining": t.defining})
        return out

    def _decision(self, best: dict, scored: list[dict], pool, cost, de, secs: float) -> dict:
        s = self.s
        ids = best["palette"]
        serves = self._serves(ids, pool, de)
        why = {}
        for fid in ids:
            f = s.filament(fid)
            parts = [p.name for p in s.inp.parts if (s.part_filaments.get(p.name)
                                                   or p.filament_id) == fid]
            without = [r for r in scored if set(r["palette"]) == set(ids) - {fid}]
            reason = []
            if parts:
                reason.append(f"add-on {', '.join(parts)}")
            if without:
                alt = without[0]
                reason.append(f"without it: visible error {alt['visible_error']} (now "
                              f"{best['visible_error']}), purge {alt['purge_g']} g, "
                              f"{alt['time_h']} h, defining colors missed "
                              f"{alt['defining_missed']}")
            why[fid] = {"name": f"{f.brand} {f.line} {f.name}", "hex": f.hex,
                        "owned": fid in self.owned, "serves": serves.get(fid, []),
                        "why": "; ".join(reason) or "lowest cost palette"}
        dropped, seen_f = [], set(ids)
        for r in scored[1:]:  # the best palette each rejected filament was in, and its price
            for x in r["palette"]:
                if x in seen_f:
                    continue
                seen_f.add(x)
                d_v = round(r["visible_error"] - best["visible_error"], 2)
                d_g = round(r["purge_g"] - best["purge_g"], 1)
                d_h = round(r["time_h"] - best["time_h"], 2)
                d_b = r["buy"] - best["buy"]
                note = (f"best palette with it {r['palette']}: visible error {d_v:+}, purge "
                       f"{d_g:+} g, {d_h:+} h, spools to buy {d_b:+}"
                       + (f", colors missed {r['defining_missed']}" if r["defining_missed"]
                          else "") + f"; J {r['J']} vs {best['J']}")
                dropped.append({"filament": x, "owned": x in self.owned, "with": r["palette"],
                                "visible_error_with": r["visible_error"],
                                "purge_g_with": r["purge_g"], "time_h_with": r["time_h"],
                                "J_with": r["J"], "why": note})
        merged = []
        labs = {i: np.array(s.filament(i).lab) for i in ids}
        for f in pool:
            if f.id in ids:
                continue
            _, d = plan_de([f.lab], [labs[i] for i in ids])
            j = int(np.argmin(d[0]))
            if float(d[0, j]) < MERGE_DE and any(
                    f.id == x["filament"] for x in dropped):
                merged.append({"filament": f.id, "into": ids[j],
                               "delta_e": round(float(d[0, j]), 1)})
        return {
            "palette": ids, "cap": self.cap, "product_type": s.inp.product_type,
            "palette_source": s.inp.palette_source,
            "buy": [i for i in ids if i not in self.owned],
            "score": best, "filaments": why, "dropped": dropped[:8], "merged": merged,
            "alternatives": scored[:10],
            "targets": [{"name": t.name, "weight": round(t.weight, 3), "kind": t.kind,
                         "defining": t.defining} for t in self.targets],
            "rule": {"notice_de": NOTICE_DE, "defining_de": DEFINING_DE,
                     "snap_de": SNAP_DE, "merge_de": MERGE_DE,
                     "g_per_unit": G_PER_UNIT, "h_per_unit": H_PER_UNIT,
                     "buy_unit": BUY_UNIT, "spec_weight": SPEC_WEIGHT,
                     "time_model_s": [TIME_S_PER_MODEL_G, TIME_S_PER_CHANGE,
                                      TIME_S_PER_PURGE_G]},
            "seconds": secs,
        }

    # ------------------------------------------------------------ one more filament
    def earns_place(self, fid: str, region_ids: list[int]) -> tuple[bool, dict]:
        """Adding fid to the current palette for these regions: allowed when it lowers J or
        brings a defining color under DEFINING_DE, within the cap."""
        s = self.s
        cur = list(s.palette)
        if fid in cur:
            return True, {"why": "already in the palette"}
        if len(cur) + 1 > self.cap:
            return False, {"why": f"the {s.inp.product_type} cap is {self.cap} filaments"}
        new = cur + [fid]
        now_asg = dict(s.assignment)
        new_asg = {**now_asg, **{int(r): fid for r in region_ids}}
        # the default filament prints only faces no region holds
        fixed = set(self.fixed) | ({s.default_filament} if (s.fr == 0).any() else set())
        used_now = [i for i in cur if i in set(now_asg.values()) | fixed]
        used_new = [i for i in new if i in set(new_asg.values()) | fixed]
        rescued: list[str] = []
        v_now = v_new = 0.0
        if self.targets:  # what prints after the change: filaments left unused do not count
            w = np.array([t.weight for t in self.targets], float)
            m_now = _cost_matrix(self.targets, [s.filament(i) for i in used_now])[0].min(axis=1)
            m_new = _cost_matrix(self.targets, [s.filament(i) for i in used_new])[0].min(axis=1)
            v_now = float((w * np.maximum(0.0, m_now - NOTICE_DE)).sum())
            v_new = float((w * np.maximum(0.0, m_new - NOTICE_DE)).sum())
            rescued = [t.name for t, a_, b_ in zip(self.targets, m_now, m_new)
                       if t.defining and a_ >= DEFINING_DE and b_ < DEFINING_DE]
        tot = sum(r.area_mm2 for r in s.regions.values()) or 1.0
        area = sum(s.regions[int(r)].area_mm2 for r in region_ids if int(r) in s.regions)
        gain = v_now - v_new
        a = self.score(used_now, 0.0, 0, now_asg)
        b = self.score(used_new, 0.0, 0, new_asg)
        buy = 0.0 if fid in s.inp.library else BUY_UNIT
        cost = (b["purge_g"] - a["purge_g"]) / G_PER_UNIT + (b["time_h"] - a["time_h"]) / \
            H_PER_UNIT + buy
        info = {"filament": fid, "regions": [int(r) for r in region_ids],
                "area_share": round(area / tot, 4), "gain": round(gain, 3),
                "cost": round(cost, 3), "purge_g": [a["purge_g"], b["purge_g"]],
                "time_h": [a["time_h"], b["time_h"]], "rescues_defining": rescued}
        ok = bool(rescued) or gain > cost
        info["why"] = (f"rescues a defining color ({', '.join(rescued)})" if rescued else
                       f"gain {gain:.2f} {'>' if ok else '<='} cost {cost:.2f} (purge "
                       f"{a['purge_g']} -> {b['purge_g']} g, {a['time_h']} -> {b['time_h']} h"
                       f"{', a spool to buy' if buy else ''})")
        return ok, info


def summary_line(decision: dict) -> str:
    """One line for prompts and manifests."""
    sc = decision["score"]
    drop = "; ".join(f"{d['filament']} ({d['why']})" for d in decision.get("dropped", [])[:4])
    return (f"palette {', '.join(decision['palette'])} (cap {decision['cap']}; buy "
            f"{', '.join(decision['buy']) or 'nothing'}); estimate purge {sc['purge_g']} g, "
            f"{sc['time_h']} h; visible error {sc['visible_error']}"
            + (f"; dropped: {drop}" if drop else ""))
