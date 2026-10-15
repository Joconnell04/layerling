"""Purge and time lab: slice fixtures under slicer levers and record grams, purge and minutes.

Each run writes the fixture with PrintSettings.process_overrides, slices it with slice_check
(read-back and paint audit on), and records the SliceReport plus G-code counts (filament
changes, nozzle switches, prime tower grams). Results append to a JSONL file so a killed run
resumes where it stopped. Results and conclusions: docs/research/purge-and-time.md.
"""

from __future__ import annotations

import itertools
import json
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from p2p_contracts import PrintSettings

from .demo import jackson_palette
from .gcode_stats import parse_gcode
from .products import PRODUCTS
from .slicer import slice_check
from .writer import write_3mf

MESHY_REL = Path("data/fixtures/meshy/04_multi_color/meshy_multicolor.3mf")
SUPPORT_ON = {"enable_support": "1"}
SLICE_TIMEOUT_S = 300  # the largest fixture slices in about 60 s
# Bambu's values for the keys the writer changes by default (writer.DEFAULT_PROCESS_OVERRIDES).
BAMBU_VALUES = {"prime_volume_mode": "Default", "flush_into_infill": "0"}


@dataclass
class Variant:
    lever: str
    name: str
    overrides: dict[str, str] = field(default_factory=dict)
    layer_mm: float = 0.2
    orient: str = "upright"  # upright | side | back (Meshy figures only)
    fixtures: tuple[str, ...] | None = None  # None = every fixture
    quality_risk: str = ""
    despeckle_mm: float = 0.0  # merge painted regions narrower than this before writing
    adaptive: bool = False  # coarse layers where a slice holds one filament
    figure_overrides: dict[str, str] = field(default_factory=dict)  # Meshy figures only
    writer_defaults: bool = False  # True: start from the writer's defaults, not Bambu's


def find_meshy() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / MESHY_REL).exists():
            return parent / MESHY_REL
    return None


def _rot(orient: str) -> np.ndarray:
    m = np.eye(4)
    if orient == "side":  # lie on its side: +90 degrees about Y
        m[:3, :3] = [[0, 0, 1], [0, 1, 0], [-1, 0, 0]]
    elif orient == "back":  # lie on its back, face up: -90 degrees about X
        m[:3, :3] = [[1, 0, 0], [0, 0, 1], [0, -1, 0]]
    return m


def build(fixture: str, v: Variant, out: Path, settings: PrintSettings) -> dict:
    """Write one fixture under a variant; returns the writer result with expected_slots."""
    from .audit import despeckle
    from .layers import adaptive_profile
    from .reader import read_3mf_full

    pal = jackson_palette()
    if fixture.startswith("beagle"):
        from .retarget import retarget

        src = find_meshy()
        if src is None:
            raise FileNotFoundError("Meshy fixture not found")
        size = float(fixture.removeprefix("beagle"))
        m = _rot(v.orient)
        m[:3, :3] *= size / 75.0
        ams = [f.model_copy(update={"ams_slot": i + 1}) for i, f in enumerate(pal)]
        res, _ = retarget(src, ams, settings, out, transform=m, object_name=fixture)
        if not (v.despeckle_mm or v.adaptive):
            return dict(res, expected_slots=len(res["slots_used"]))
        r = read_3mf_full(out)
        verts, faces, slots, parts = r.vertices, r.faces, r.face_slots, None
    else:
        p = PRODUCTS[fixture]()
        verts, faces, slots, parts = p.vertices, p.faces, p.face_slots, p.parts
    if v.despeckle_mm:
        slots, _, _ = despeckle(verts, faces, slots, min_width_mm=v.despeckle_mm)
    prof = None
    if v.adaptive:
        allv = [verts] + [np.asarray(q.vertices) for q in parts or []]
        allf, off = [faces], len(verts)
        alls = [slots]
        for q in parts or []:
            allf.append(np.asarray(q.faces) + off)
            off += len(q.vertices)
            alls.append(np.full(len(q.faces), q.slot or 0))
        prof = adaptive_profile(np.concatenate(allv), np.concatenate(allf), np.concatenate(alls),
                                fine=v.layer_mm)
    res = write_3mf(verts, faces, slots, pal, settings, out, object_name=fixture, parts=parts,
                    layer_profile=prof)
    used = set(res["slots_used"])
    return dict(res, expected_slots=len(used))


def sliced_stats(sliced: Path) -> dict:
    with zipfile.ZipFile(sliced) as z:
        g = z.read("Metadata/plate_1.gcode").decode("utf-8", "replace")
        root = ET.fromstring(z.read("Metadata/slice_info.config"))
        seq = json.loads(z.read("Metadata/filament_sequence.json")) if (
            "Metadata/filament_sequence.json" in z.namelist()) else {}
    fmap_s = next((m.get("value") for m in root.iter("metadata")
                   if m.get("key") == "filament_maps"), "")
    fmap = [int(x) for x in fmap_s.split()] if fmap_s else None
    st = parse_gcode(g, filament_map=fmap)
    order = [int(x) for x in (seq.get("plate_1", {}).get("sequence") or [])]
    trans = Counter(tuple(sorted(p)) for p in itertools.pairwise(order) if p[0] != p[1])
    return {"layers": st.layers, "filament_changes": st.filament_changes,
            "nozzle_changes": st.nozzle_changes, "max_changes_per_layer": st.max_changes_per_layer,
            "tower_g": st.tower_g, "model_g": st.model_total, "filament_maps": fmap_s,
            "transitions": {f"{a}-{b}": c for (a, b), c in trans.most_common()}}


def max_cut_map(transitions: dict[str, int], n_filaments: int) -> str:
    """Nozzle per filament that puts the most frequent transitions across the two nozzles."""
    pairs = [(tuple(int(x) for x in k.split("-")), c) for k, c in transitions.items()]
    used = sorted({s for (a, b), _ in pairs for s in (a, b)})
    best, best_score = None, -1
    for bits in itertools.product((1, 2), repeat=len(used)):
        side = dict(zip(used, bits, strict=True))
        score = sum(c for (a, b), c in pairs if side[a] != side[b])
        if score > best_score:
            best, best_score = side, score
    side = best or {}
    return ",".join(str(side.get(i, 1)) for i in range(1, n_filaments + 1))


def run_one(fixture: str, v: Variant, reports_dir: Path, base_settings: PrintSettings) -> dict:
    # Levers are measured against Bambu's own values, not the writer's defaults.
    overrides = {} if v.writer_defaults else dict(BAMBU_VALUES)
    overrides.update(v.overrides)
    if fixture.startswith("beagle"):
        overrides.update(v.figure_overrides)
        if "enable_support" not in overrides:
            overrides.update(SUPPORT_ON)  # the real beagle needs supports; baseline had them on
    settings = base_settings.model_copy(update={"layer_mm": v.layer_mm,
                                                "process_overrides": overrides})
    tmp = Path(tempfile.mkdtemp(prefix="p2p_lab_"))
    try:
        path = tmp / f"{fixture}.3mf"
        t0 = time.time()
        res = build(fixture, v, path, settings)
        for _attempt in range(2):  # the Bambu CLI occasionally stalls after loading; retry once
            rep = slice_check(path, settings, res["expected_slots"], filament_ids=res["palette"],
                              keep_gcode=True, out_dir=tmp, timeout_s=SLICE_TIMEOUT_S)
            if rep.output is not None or not any("timed out" in e for e in rep.errors):
                break
        stats = sliced_stats(Path(rep.output.path)) if rep.output else {}
        name = f"{fixture}__{v.lever}__{v.name}"
        rep_out = rep.model_copy(update={"output": None, "log": None})
        (reports_dir / f"{name}.json").write_text(rep_out.model_dump_json(indent=1) + "\n")
        purge = rep.purge_grams
        return {
            "fixture": fixture, "lever": v.lever, "variant": v.name, "overrides": overrides,
            "layer_mm": v.layer_mm, "orient": v.orient, "quality_risk": v.quality_risk,
            "ok": rep.ok, "slots_used": rep.slots_used, "expected_slots": rep.expected_slots,
            "readback_ok": rep.readback_ok, "thin_paint": len(rep.thin_paint),
            "minutes": round(rep.print_time_s / 60, 1) if rep.print_time_s else None,
            "total_g": rep.total_grams, "purge_g": purge,
            "tower_g": stats.get("tower_g"),
            "flush_g": round(purge - stats["tower_g"], 2) if purge is not None and stats else None,
            "errors": rep.errors[:3],
            "warnings": [w for w in rep.warnings if not w.startswith("thin paint")][:4],
            "wall_s": round(time.time() - t0, 1), "report": f"{name}.json", **stats,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def levers() -> list[Variant]:
    beagles = ("beagle45", "beagle75", "beagle90")
    return [
        Variant("baseline", "base"),
        # (a) nozzles
        Variant("a_nozzle", "single_nozzle",
                {"filament_map_mode": "Manual", "filament_map": "1,1,1,1,1"}),
        Variant("a_nozzle", "max_cut", {"filament_map_mode": "Manual"}),  # map filled per fixture
        # (b) flush targets and prime tower
        Variant("b_flush_into", "infill", {"flush_into_infill": "1"}),
        Variant("b_flush_into", "objects", {"flush_into_objects": "1"},
                quality_risk="purge colours mixed into the visible object"),
        Variant("b_tower", "width_20", {"prime_tower_width": "20"}),
        Variant("b_tower", "no_sparse_layers", {"wipe_tower_no_sparse_layers": "1"},
                quality_risk="experimental; nozzle travels down to the tower"),
        Variant("b_tower", "prime_saving", {"prime_volume_mode": "Saving"}),
        Variant("b_tower", "prime_fast", {"prime_volume_mode": "Fast"}),
        Variant("b_tower", "infill_gap_300", {"prime_tower_infill_gap": "300%"}),
        Variant("b_tower", "no_tower", {"enable_prime_tower": "0"},
                quality_risk="no priming after a swap; under-extrusion at colour starts"),
        # (c) flush volumes
        Variant("c_flush", "multiplier_0.8", {"flush_multiplier": "0.8"},
                quality_risk="less flush than Bambu's colour model; possible colour bleed"),
        Variant("c_flush", "multiplier_0.6", {"flush_multiplier": "0.6"},
                quality_risk="less flush than Bambu's colour model; possible colour bleed"),
        # (d) layer height
        Variant("d_layer", "0.16", layer_mm=0.16),
        Variant("d_layer", "0.24", layer_mm=0.24, quality_risk="coarser layers"),
        Variant("d_layer", "0.28", layer_mm=0.28, quality_risk="coarser layers"),
        # (e) supports, Meshy figures only
        Variant("e_support", "none", {"enable_support": "0"}, fixtures=beagles,
                quality_risk="floating regions print in mid-air"),
        Variant("e_support", "normal", {"enable_support": "1", "support_type": "normal(auto)"},
                fixtures=beagles),
        Variant("e_support", "tree_plate_only",
                {"enable_support": "1", "support_on_build_plate_only": "1"}, fixtures=beagles),
        Variant("b_tower", "rib_wall_off", {"prime_tower_rib_wall": "0"}),
        Variant("b_tower", "rib_wall_off_width_20",
                {"prime_tower_rib_wall": "0", "prime_tower_width": "20"}),
        Variant("d_layer", "adaptive_0.2_0.28", adaptive=True),
        # (g) paint: merge details the slicer cannot print (under 2 x nozzle)
        Variant("g_paint", "despeckle_0.8", despeckle_mm=0.8),
        # (b) more infill to absorb flush when flushing into infill
        Variant("b_flush_into", "infill_density_30",
                {"flush_into_infill": "1", "sparse_infill_density": "30%"}),
        # combinations of the levers that pass every guard
        Variant("z_combined", "saving_infill",
                {"prime_volume_mode": "Saving", "flush_into_infill": "1"}),
        Variant("z_combined", "writer_defaults", writer_defaults=True),
        Variant("z_combined", "saving_infill_ribs_off",
                {"prime_volume_mode": "Saving", "flush_into_infill": "1",
                 "prime_tower_rib_wall": "0"}),
        Variant("z_combined", "saving_infill_0.24",
                {"prime_volume_mode": "Saving", "flush_into_infill": "1"}, layer_mm=0.24,
                quality_risk="coarser layers"),
        Variant("z_combined", "saving_infill_mult_0.8",
                {"prime_volume_mode": "Saving", "flush_into_infill": "1",
                 "flush_multiplier": "0.8"},
                quality_risk="less flush than Bambu's colour model; possible colour bleed"),
        Variant("z_combined", "saving_infill_back",
                {"prime_volume_mode": "Saving", "flush_into_infill": "1"}, orient="back",
                fixtures=("beagle45",)),
        # (f) orientation, the Meshy figure at keychain size
        Variant("f_orient", "side", orient="side", fixtures=("beagle45",)),
        Variant("f_orient", "back", orient="back", fixtures=("beagle45",)),
    ]


FIXTURES = ("keychain", "nameplate", "figurine", "beagle45", "beagle75", "beagle90")


def run_lab(results: Path, reports_dir: Path, variants: list[Variant] | None = None,
            fixtures: tuple[str, ...] = FIXTURES, log=print) -> list[dict]:
    """Run every (fixture, variant) not already in `results` (JSONL); returns all rows."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    done: dict[tuple[str, str, str], dict] = {}
    if results.exists():
        for line in results.read_text().splitlines():
            r = json.loads(line)
            done[(r["fixture"], r["lever"], r["variant"])] = r
    base = PrintSettings()
    for v in variants or levers():
        for fx in fixtures:
            if v.fixtures and fx not in v.fixtures:
                continue
            if fx.startswith("beagle") and find_meshy() is None:
                continue
            key = (fx, v.lever, v.name)
            if key in done:
                continue
            vv = v
            if v.lever == "a_nozzle" and v.name == "max_cut":
                b = done.get((fx, "baseline", "base"))
                if not b or not b.get("transitions"):
                    continue
                n = len(jackson_palette())
                vv = Variant(v.lever, v.name, dict(v.overrides, filament_map=max_cut_map(
                    b["transitions"], n)), v.layer_mm, v.orient, v.fixtures, v.quality_risk)
            try:
                row = run_one(fx, vv, reports_dir, base)
            except Exception as e:  # noqa: BLE001  record and keep going
                row = {"fixture": fx, "lever": v.lever, "variant": v.name, "ok": False,
                       "errors": [f"{type(e).__name__}: {e}"]}
            done[key] = row
            with results.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            log(f"{fx:10s} {v.lever:13s} {v.name:16s} ok={row.get('ok')} "
                f"min={row.get('minutes')} g={row.get('total_g')} purge={row.get('purge_g')} "
                f"tower={row.get('tower_g')} changes={row.get('filament_changes')}")
    return list(done.values())


def summarize(rows: list[dict], fixtures: tuple[str, ...] = FIXTURES) -> str:
    """Markdown table per fixture: grams, purge, tower, minutes, changes, change vs baseline."""
    by = {(r["fixture"], r["lever"], r["variant"]): r for r in rows}
    out = []
    for fx in fixtures:
        base = by.get((fx, "baseline", "base"))
        if not base or not base.get("total_g"):
            continue
        out.append(f"\n### {fx}\n")
        out.append("| Lever | Variant | Total g | Purge g | Tower g | Min | Changes | "
                   "Total g vs base | Min vs base | Guard |")
        out.append("|---|---|---|---|---|---|---|---|---|---|")
        for (f2, lever, variant), r in by.items():
            if f2 != fx:
                continue
            if not r.get("total_g"):
                err = "; ".join(r.get("errors", []))[:80]
                out.append(f"| {lever} | {variant} | failed: {err} | | | | | | | gate failed |")
                continue
            dg = (r["total_g"] - base["total_g"]) / base["total_g"] * 100
            dm = (r["minutes"] - base["minutes"]) / base["minutes"] * 100
            guard = []
            if not r.get("ok"):
                guard.append("gate failed")
            if r.get("slots_used") != base.get("slots_used"):
                guard.append(f"slots {r.get('slots_used')}")
            if (r.get("thin_paint") or 0) > (base.get("thin_paint") or 0):
                guard.append(f"thin {base.get('thin_paint')}->{r.get('thin_paint')}")
            if any("floating" in w for w in r.get("warnings", [])):
                guard.append("floating")
            if r.get("quality_risk"):
                guard.append("risk")
            out.append(f"| {lever} | {variant} | {r['total_g']:.2f} | {r['purge_g']:.2f} | "
                       f"{(r.get('tower_g') or 0):.2f} | {r['minutes']:.1f} | "
                       f"{r.get('filament_changes')} | {dg:+.1f}% | {dm:+.1f}% | "
                       f"{', '.join(guard) or 'ok'} |")
    return "\n".join(out) + "\n"
