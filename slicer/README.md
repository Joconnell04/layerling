# slicer (`p2p_slicer`)

Our own slicer for the X2D: a painted Bambu project 3MF in, G-code and a SliceReport out, with
time, grams, purge and change counts close to Bambu Studio 2.08. It runs on Mac and Linux. Bambu
Studio's CLI stays the ship gate; this is for previews, estimates, paint checks and the paint
agent. Spec: `docs/research/native-slicer.md`. Ground truth: `slicer/calibration/`.

## Interface

```python
from p2p_slicer.api import slice_project                # project 3MF -> SliceResult (M2)
from p2p_slicer.gcode_read import read_gcode            # G-code bytes -> MoveTable
from p2p_slicer.estimate import estimate                # MoveTable -> TimeResult
from p2p_slicer.usage import usage                      # MoveTable -> Usage (grams, counts)
from p2p_slicer.report import analyze, analyze_gcode, slice_report, cli_result_json
from p2p_slicer.compare import bambu_reference, compare, find_truth_dir
from p2p_slicer.emit import to_gcode                    # MoveTable -> G-code bytes
from p2p_slicer.preview import write_toolpaths, read_toolpaths, print_numbers  # studio preview
```

`write_toolpaths(moves, out_dir, slots=[{slot, filament_id, hex}], per_layer_s=...)` writes the
toolpath preview the studio paint editor draws (contracts `PaintPreview`, toolpaths version 1):
`toolpaths.json` (per layer: z, height, its chains, seconds, filament slots, changes) and
`toolpaths.bin` (uint16 points at 0.01 mm, uint32 chains: first point, count, slot << 8 | role).
A chain is one continuous extrusion; travel is left out, arcs are cut into 12 degree steps, the
prime tower is kept. `print_numbers(report, palette_ids, layers=)` is contracts `PrintNumbers`
with our filament ids in place of Bambu's preset ids. Tests: `slicer/tests/test_preview.py`
(nameplate_5c_020: every extrusion drawn within the 0.01 mm grid, per-layer seconds and changes
add up to the estimate). The Bronco nameplate (267 layers) writes 1.2 MB.

`slice_project(path, *, out_dir=None, gcode=False, overrides=None, expected_slots=None)`
slices a project 3MF with the project's own settings plus the overrides Bambu's CLI applies
(prime volume 45, AMS counts). It returns a `SliceResult`:

| Field | What |
|---|---|
| `report` | `p2p_contracts.SliceReport`, slicer "p2p_slicer": time, model and prepare time, grams, per-filament model, flush and tower grams, the three change counts, warnings |
| `analysis` | `report.Analysis`: `time` (TimeResult), `usage` (Usage), filament maps, per-layer filaments |
| `moves` | the MoveTable; `to_gcode(res.moves, res.analysis)` writes it |
| `plan` | layers: `print_z`, `height`, `slice_z` |
| `filament_maps`, `layer_sets`, `orders` | nozzle per slot, filaments per layer, print order per layer |
| `changes` | one `purge.Change` per filament change: flush, infill it went into, chute, prime, ramming |
| `tower` | `tower.TowerPlan`: position, size, per-layer blocks |
| `timings` | seconds per stage |

With `out_dir` it writes `slice_report.json` and a Bambu-shaped `result.json`; with
`gcode=True` also `plate_1.gcode`. Our G-code read back through `read_gcode` gives the same time
and grams (test_slicer_m2).

M1 interfaces, unchanged:

`MoveTable` (`moves.py`): `rows` is a numpy structured array, one row per motion or timing event
(`MOVE_DTYPE`: end position x y z, relative e, feed f in mm/min, `kind`, raw `role` from
`; FEATURE:`, `slot` 1-based, `nozzle` 0 left / 1 right, `layer` (count of `; CHANGE_LAYER`,
0 = start G-code), `width`, `height`, `accel`, `dwell_s`, `flags`, arc `i` `j` `p`, source
`line`, `args`). `meta` holds the G-code `config` block, `header`, `nozzle_of`, `layer_z`.
Kinds: extrude, travel, retract, unretract, wipe, virtual (`;VG1`), dwell, sync, tool_change,
g29, m204, limits, empty, text (template text we emit). Tool change rows carry the previous slot
in `i` and the slot the target nozzle held in `p`. Rows inside the tower block carry
`Flag.TOWER`.

`read_gcode(data, *, first_slot=None, nozzle_of=None) -> MoveTable` reads Bambu's G-code or ours
(`T<n>` is slot n + 1; T65279/T65535 ignored; G28 is a move to 0; G130 an empty block).

`estimate(mt, cfg=None) -> TimeResult`: `total_s`, `prepare_s`, `main_s` (Bambu
`main_predication`), `model_s` (header "model printing time"), `per_role_s`, `travel_s`,
`per_layer_s`, `load_s` and `unload_s` per slot, and `row_s` (seconds per row) so any grouping is
a sum. `feature_type_times()` gives result.json's shape.

`usage(mt, cfg=None) -> Usage`: `per_slot[slot]` with mm3 in `model`, `support`, `tower`,
`flush` buckets (`grams()`, `metres()`), `filament_changes`, `ams_swaps`, `nozzle_switches`,
`slots_used`.

`analyze(mt) / analyze_gcode(path) -> Analysis` bundles both plus filament maps, Bambu filament
ids, layer z and per-layer filaments. `slice_report(analysis, expected_slots=..., filament_ids=...)`
fills `p2p_contracts.SliceReport`; `cli_result_json(analysis)` writes Bambu's result.json shape.

## Pipeline (M2)

| Module | Step |
|---|---|
| `model.py`, `config.py` | read the 3MF (mesh, paint, parts, project settings); per-nozzle and per-filament values for the nozzle map |
| `layers.py`, `section.py` | layer heights (fixed or the variable profile); sections with exact edge keys |
| `segment.py` | paint regions: contour runs, Voronoi split, top and bottom paint carried through the shells |
| `walls.py` | classic walls, single thin wall on narrow islands, infill area |
| `surfaces.py` | top, bottom, bridge, internal bridge, solid (shells and sloped walls), sparse |
| `infill.py` | zig-zag and monotonic solid, concentric narrow solid, grid sparse |
| `grouping.py` | nozzle map ("Auto For Flush" as the CLI runs it) and print order per layer |
| `purge.py` | changes, flush volume, flush into infill, chute purge, billing |
| `tower.py` | prime tower size, place, wall, blocks, ramming, prime, grids |
| `paths.py`, `speeds.py`, `support.py`, `cooling.py` | order, travel, retract and wipe, feature speeds, overhang speeds, layer-time slowdown |
| `templates.py` | Bambu's start, change, layer, timelapse and end G-code, rendered |
| `emit.py`, `api.py` | G-code text; the pipeline |

## Commands

```bash
uv run pytest slicer/tests                                   # parity tests skip without _truth/
uv run python -m p2p_slicer slice in.3mf --out outdir        # report, result.json, plate_1.gcode
uv run python -m p2p_slicer slice in.3mf --out outdir --compare slicer/calibration/_truth/<case>
uv run python -m p2p_slicer analyze plate_1.gcode --result result.json
uv run python -m p2p_slicer compare ours.gcode slicer/calibration/_truth/keychain_4c_020
uv run python -m p2p_slicer inspect final.3mf --out slicer/_out/inspect/x   # see Inspector
uv run python -m p2p_slicer compare outdir slicer/calibration/_truth/keychain_4c_020
uv run python slicer/calibration/parity.py                   # all cases -> parity.md, parity.json
```

The parity run takes about 30 s with 4 workers; from an agent start it with `nohup ... &`.
Bambu's G-code for the calibration cases is gitignored. `find_truth_dir()` looks in
`$P2P_TRUTH_DIR`, this checkout's `slicer/calibration/_truth/`, the main checkout's, and the
`slicer-truth` worktree's.

## Accuracy

### M3: our slice against Bambu's (all 20 calibration cases)

SOURCE: `slicer/calibration/parity.md` (every number in `parity.json`), against Bambu Studio
02.08.02.61 CLI slices of the same 3MFs. Targets (FROM JACKSON): time within 5 percent, total
grams within 3 percent, slots used identical.

| Quantity | Median error | Worst | Within target |
|---|---|---|---|
| Total time | 1.25 % | +10.8 % (frenchie_baseline) | 19 of 20 within 5 % |
| Total grams | 0.76 % | +14.1 % (frenchie_baseline) | 18 of 20 within 3 % (nameplate +3.7 %) |
| Slots used | | | 20 of 20 identical |
| Layer count, filament maps | | | 20 of 20 identical |
| Filament changes | | frenchie_baseline 31 vs 27 | 15 of 20 exact; the others off by 2 to 4 |
| Flush (chute) grams | | +1.5 g (bronco_paint, of 76 g) | |
| Tower grams | | +0.33 g (nameplate, of 5.0 g) | |

Where the remaining error comes from, largest first:

- Supports are not generated (spec section 18 leaves tree supports out). Five cases have them
  (meshy_retarget, geo_nameplate, geo_figurine, both bronco); Bambu spends 183 to 539 s and
  1 to 3 g on them. Other errors partly offset them in the totals. The report warns whenever the
  project has supports on.
- Filament sets on a few layers where paint is a sliver (under about 0.3 mm): 17 layers over the
  20 cases differ, 6 of them in frenchie_baseline, where a painted part pokes 0.09 mm out of
  another part. Each wrong layer adds or drops a change, a flush and a prime (about 60 to 120 s
  and 0.1 to 0.3 g).
- Colour-band transitions on the synthetic figurines: Bambu splits those layers into many small
  islands, each with its own wall (up to 980 mm of outer wall on one layer); we print clean
  regions. About 150 s of figurine_4c's 59 minutes.
- Gap infill is not generated (2 to 130 s per case).
- Prime tower on nameplate_5c_020: 0.33 g over, from per-layer tower content shifts not yet
  modelled.

### M1: our estimator and grams on Bambu's own G-code (all 20 calibration cases)

SOURCE: `slicer/tests/test_slicer_m1_parity.py` against result.json from Bambu Studio 02.08.02.61.

| Quantity | Worst case | Median |
|---|---|---|
| Total time | 0.026 % (figurine_4c_020, -0.93 s of 3533 s) | 0.007 % |
| Prepare time | 0.01 s | 0.01 s |
| Per feature time | 4.9 s of 4310 s Undefined, 3.4 s of 2051 s Outer wall (geo_figurine) | under 0.05 s |
| Grams per filament (total and model) | 0.008 mg | 0.000 mg |
| Filament changes, AMS swaps, nozzle switches | exact on all 20 | |
| Slots used, filament maps, layer count, per-layer filament lists | exact | |

How the time model was pinned down: section 10 of the spec, then probe slices. `probe` G-code
(test moves in the start G-code, one feature role per pattern, sliced by the Bambu CLI) showed
five rules the spec leaves out: the corner logic ignores E; the centripetal cap lowers a block's
cruise but not the speed the corner logic compares; arcs take XY tangents and ignore Z; G28 is a
move to 0; a dwell flushes the queue when at least one block is queued and otherwise waits for
the next plan; G130 queues an empty block.

## Gate integration

- `p2p_threemf.slice_check` dispatches on `PrintSettings.slicer`: `bambu_studio` (the gate),
  `orcaslicer` (second check), `p2p_slicer` (`NativeSlicerBackend`, calls
  `p2p_slicer.api.slice_project` once the core has it; until then it reports "p2p_slicer is not
  installed"). `get_backend(name)` and `backend_available(name)` are the registry.
- `SLICER_BIN` (process environment) overrides the Bambu binary: an absolute path or a name on
  PATH, for a Linux build on a cloud VM. The version then comes from the slicer log.
- Bambu's `result.json` is no longer thrown away. `slice_check` fills, all exact on the 20 truth
  cases (SOURCE: `threemf/tests/test_threemf_slicer_backends.py::test_truth_replay`, run against
  `slicer/calibration/_truth`):

| SliceReport field | From |
|---|---|
| `purge_grams` | result.json: sum of `total_used_g - main_used_g` (was a G-code estimate) |
| `tower_grams`, per filament `tower_grams` | G-code: extrude moves under `; FEATURE: Prime tower` |
| per filament `model_grams` | result.json `main_used_g` |
| per filament `flush_grams` | total minus model minus tower (Bambu's own billing) |
| `ams_swaps` | result.json `filament_change_times` (else VFLUSH blocks that purge) |
| `filament_changes`, `nozzle_changes` | G-code T changes, `; NOZZLE_CHANGE_START` markers |
| `model_time_s`, `prepare_time_s` | G-code header "model printing time"; prediction minus that |

  Seconds per feature have no field yet: the whole result.json is kept next to the slicer log as
  `<stem>.slice_result.json`.
- Cloud fallback (worker): `p2p_worker.printing.gate_settings` switches a `bambu_studio` gate to
  `p2p_slicer` when Bambu Studio is missing and p2p_slicer can run. The report says slicer
  `p2p_slicer` and its first warning starts "not ship-gated". s07 and s10 then post the result with
  `slice_ok` None, decide `slicer_gate = estimate`, and hold the job in REVIEW with "Bambu gate
  pending"; approve is refused; a rerun on a Mac worker gates it with Bambu Studio. s09's final
  paint gate uses p2p_slicer the same way (`printing.paint_slice_fn`). With neither slicer the
  gate fails "bambu_studio is not installed", as before.

## Inspector

```bash
uv run python -m p2p_slicer inspect <file.3mf | file.gcode.3mf | file.gcode> --out <dir>
    [--project final.3mf] [--result result.json] [--reference bambu.gcode.3mf]
    [--slice auto|p2p|bambu|none]
```

Replaces opening a file in the Bambu Studio GUI. Writes `<dir>/inspection.json`,
`<dir>/renders/` and a static light `<dir>/index.html` (no script, no network).

- Project 3MF: sliced with p2p_slicer when `api.slice_project` exists, else with the Bambu CLI
  (headless, into `<dir>/bambu/`), unless `--slice none` (model panels only).
- Sliced `.gcode.3mf` (Bambu's or ours) or `.gcode`: read as is. Bambu's `result.json` is found
  next to the file (the CLI folder, or slice_check's `<stem>.slice_result.json`).
- Every slice goes through the core's `gcode_read` and `estimate`, then `inspect.measure`, so ours
  and Bambu's are read the same way. For Bambu files Bambu's own numbers (slice_info, result.json,
  G-code header) are shown next to ours with deltas (`parity`). Without the slicer core installed
  the page shows the model, the plate and Bambu's numbers only.
- Renders: plate iso (orthographic, Bambu's thumbnail direction) and top at 2 px/mm with the
  tower and the right-nozzle strip; our model_iso next to Bambu's `plate_1.png`; the painted model
  from front, front 3/4, right, back 3/4, back, left, top (`p2p_contracts.VIEWS` plus back_3q);
  toolpaths by filament and by feature (framed on the model) and the prime tower; up to 12 layer
  views (first, last, most changes, 25/50/75 percent, changes of the extruded filament set);
  per-layer time chart (SVG, stacked model/tower/flush/other, elapsed line).
- Checks: paths off the plate or too high, right-nozzle paths under x 20.5, tower off the plate or
  over an object, 2+ empty layers, PLA with PETG, AMS swaps without flush, painted slots that print
  nothing, package completeness and the G-code checksum, plus slice_info and slicer-log warnings.

Validation (SOURCE: all 20 cases in `slicer/calibration/_truth`, inspector on the merged core
M1 checkpoint b8be057): layers, filament changes, nozzle switches and AMS swaps equal Bambu's on
every case; total grams within 0.01 g and every filament within 0.005 g (flush billed 92 mm3 to the
filament that last left the nozzle, the rest to the new one, which also explains the 0.114 g
"transfer" noted in native-slicer-integration.md finding 3); our time is -0.03 to +0.01 percent
from Bambu's. `keychain_thin_3c_020` is flagged "Painted but not printed: slot 1". Wall clock per
case 1.4 to 6.9 s at low load (Frenchie, 1.1M faces, is the slowest), up to 21 s at load average 30;
`inspect final.3mf` on Frenchie including the Bambu CLI slice: 13 to 16 s.

`inspection.json` (version 1; slots 1-based, nozzles 0 left 1 right, seconds, mm, mm3, grams):
`source` (path, kind, project, sliced, sliced_by, result_json, estimate), `printer` (preset,
bed_mm, max_height_mm, nozzles with x_range_mm), `process`, `plate` (bed_mm, objects_bbox_mm,
first_filament, tower bbox and basis sliced or estimate), `objects[].parts[]` (faces per
filament), `filaments[]` (slot, colour, preset, type, nozzle, used, faces, usage
model/support/flush/tower/total grams and meters, load and unload s, `reference` = Bambu's),
`totals` (+ `reference`), `roles[]` (time, percent, m, g, Bambu time), `travel`, `layers[]` (z,
height, time, elapsed, extruded and planned filaments, changes, AMS swaps, flush, time split),
`switches[]` (layer, z, from, to, nozzle, nozzle_switch, ams_swap, flush mm3 and g, billed),
`grouping`, `warnings[]` (code, level, text, source), `package`, `parity[]`, `renders`, `run`.
### Speed

SOURCE: one case at a time on this Mac (Apple silicon) while other jobs shared it (load average
about 20), wall seconds for `slice_project` without writing G-code, numba caches warm. Expect
some spread run to run.

| Case | Faces | Layers | Changes | Seconds |
|---|---|---|---|---|
| keychain_1c_020 | 73,856 | 25 | 0 | 0.6 |
| keychain_thin_3c_020 | 299,208 | 25 | 18 | 0.9 |
| frenchie_paint_3c_020 | 1,134,704 | 51 | 21 | 5.1 |
| bronco_paint_3c_020 | 537,360 | 267 | 459 | 12.0 |
| geo_figurine_3c_020 | 32,690 | 450 | 729 | 16.7 (moves stage since cut from 8.7 to 4.0 s) |

The target from the brief is 4 to 11 s for 200k faces; faces cost little (a 1.1M-face model
slices in 5 s). Long multicolour prints are the slow case: time goes with layers times
filaments per layer (segmentation, and the moves of 729 changes and their tower blocks).

## Rules found in Bambu's output (not in the spec)

- Top and bottom paint carried into the shell layers stays inside the section of every layer it
  passes, not only the layer it lands on (a raised eye's top paint stops two layers down).
- A layer contour edge takes paint only from a part segment running along it; a painted part
  that only crosses the layer contour colours nothing.
- Narrow internal solid (nothing left after a 3 mm inset) prints as concentric loops.
- A pure nozzle switch to the layer's last filament hands that filament the sparse infill
  printed earlier on the layer, up to the matrix volume old to new (ASSUMED rule; what the
  G-code shows is that on two-filament layers the last filament prints all the sparse infill).

## Contract change requests

1. `SliceReport.feature_time_s: dict[str, float] | None` (Bambu `feature_type_times`, or ours),
   so seconds per feature travel with the report instead of `<stem>.slice_result.json`.
2. `SliceReport.gate: Literal["ship", "estimate"]` (default "ship"). Today "estimate" is inferred
   from `slicer == "p2p_slicer"`; `costs/ingest.py` should skip estimate reports for prices.
3. `StageError.code`: add `slicer_pending` for "Bambu gate pending" (it reuses `slicer_failed`
   with `detail.gate = "estimate"` meanwhile).
4. `Inspection` model for `inspection.json` (section 15 of the spec; the shape the inspector writes
   is documented below) and a stage folder for inspector output.
