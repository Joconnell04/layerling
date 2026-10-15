# threemf (`p2p_threemf`)

Bambu Studio project 3MF writer with per-triangle filament paint, an independent reader, and the
slicer gate. Target: a file Jackson opens in Bambu Studio that is ready to slice on the X2D with
his presets, colours and paint. Research notes: `docs/research/bambu-3mf.md`.

## Interface

```python
from p2p_threemf import write_3mf, read_3mf, read_3mf_full, slice_check
from p2p_threemf.writer import Part
from p2p_threemf.retarget import retarget_3mf, retarget_and_check
```

`write_3mf(vertices, faces, face_slots, palette, settings, out_path, object_name="model",
config_overrides=None, extra_sidecar=None, parts=None, transform=None) -> dict`
- vertices (N,3) mm Z up, faces (M,3), face_slots (M,) 1-based palette index,
  palette `list[Filament]` (slot = index + 1), settings `PrintSettings`.
- The figure's most common slot becomes the object's filament; other faces get whole-triangle
  `paint_color` codes. `transform` (4x4) moves the figure only.
- `parts`: `Part(vertices, faces, slot=2, name="keyring_loop")` for an add-on in one filament
  (Bambu part-level extruder), `Part(..., face_slots=...)` for a painted part,
  `Part(..., subtype="negative_part")` for a cutter. All parts share one object; overlaps union.
- Writes the X2D 0.4 printer, the process for `settings.layer_mm` and one filament preset per
  palette entry (`Filament.slicer_preset`, " 0.4 nozzle" added if missing). Deterministic bytes.
- Returns `path, sha256, n_faces, slots_used, default_slot, palette, parts, bytes`.

`read_3mf(path) -> (vertices, faces, face_slots, filament_colours)`. `read_3mf_full(path,
world=False) -> ThreeMF` adds `split` (paint subdivided within the triangle), `face_part`,
raw `codes`, preset names, `object_extruders`, `project_config`, our sidecar. Majority slot by
area per triangle; unpainted area counts toward the part or object filament. Reads Bambu's own
projects (1.5M faces with split paint in about 6 s), Meshy Multi-Color files, PrusaSlicer's
`slic3rpe:mmu_segmentation`. Cutters and modifiers are not returned.

`slice_check(path, settings, expected_slots, filament_ids=None, keep_gcode=False, *,
timeout_s=900, out_dir=None, job_root=None, backend=None, verify_readback=True,
min_paint_width_mm=None) -> SliceReport`. Pass = the slicer succeeds, filaments with grams > 0
equal `expected_slots`, print time and grams present, and the read-back matches. Also:
- read-back: Bambu's re-saved model inside the sliced file must give every triangle the same
  filament, the same presets, colours, filament ids and default filament. Presets Bambu marks
  as modified are listed in warnings.
- paint audit: painted regions narrower than 2 x nozzle are warnings; when a slot only has such
  regions and then prints nothing, the error says so.
- `purge_grams` is exact from Bambu's result.json (total minus model per filament), with
  `tower_grams`, `ams_swaps`, `filament_changes`, `nozzle_changes`, `model_time_s`,
  `prepare_time_s` and per-filament `model_grams`, `flush_grams`, `tower_grams`
  (`bambu_numbers`; G-code estimate only when result.json is missing). The slicer log and
  result.json (`<stem>.slice_result.json`) are copied next to the input.
- Backends: `settings.slicer = "bambu_studio"` (gate; `SLICER_BIN` overrides the binary),
  `"orcaslicer"` (second check on a patched copy, see research doc) or `"p2p_slicer"` (our own
  slicer, an estimate: no read-back; see `slicer/README.md`).

`retarget_3mf(meshy_3mf, geometry, job_dir, library, settings, out_path) -> dict` (pinned in
`contracts/geometry.py`). Maps each Meshy colour to the nearest AMS filament by CIEDE2000 (PLA
unless another material wins by 10 Delta E), merges slots, writes all AMS filaments in AMS order
(project slot N = AMS slot N). With a `GeometryResult`: places Meshy's painted mesh with
`geometry.transform` (glTF to print) composed with a fitted Meshy-3MF-to-glTF step, adds each
part PLY with its filament as part extruder ("cutter" becomes a negative volume), refuses frames
that disagree. Returns `path, sha256, n_faces, slots_used, palette, mapping, frame_fit,
warnings`. `figure_source="geometry"` uses the repaired figure PLY and copies paint from the
nearest Meshy face. `retarget_and_check(...)` also runs the gate and turns supports on when the
slicer reports floating regions.

Purge and time (round 2, `docs/research/purge-and-time.md`):
- `PrintSettings.process_overrides` go into the project config (strings; "a,b" becomes a list,
  one value fills a list). The writer's own defaults are `writer.DEFAULT_PROCESS_OVERRIDES`:
  `prime_volume_mode=Saving`, `flush_into_infill=1` (-15 to -21% grams, -8 to -11% time on every
  fixture). A manual nozzle map (`filament_map_mode=Manual`, `filament_map="1,2,1,1,2"`) is
  written to the plate too.
- The flush matrix follows Bambu's measured tables (`flush.py`), matching the GUI exactly.
- `write_3mf(..., layer_profile=layers.adaptive_profile(...))`: variable layer height.
- `audit.despeckle(...)`: merge painted regions under 2 x nozzle into their neighbour.
- `SliceReport.readback_ok`, `readback_slots` (filaments Bambu read back) and `thin_paint`
  are filled. `gcode_stats` also counts filament changes, nozzle switches and tower grams.
- `lab.py`: slices the fixtures under each lever; results JSONL plus one SliceReport per run.

Other modules: `encoding` (paint codec, split trees, areas), `audit.audit_paint`,
`verify.readback_check`, `profiles` (preset flattening, project config, snapshot),
`products` (keychain, nameplate, figurine, too-thin keychain fixtures), `ply`, `colour`.

## Commands

```bash
uv run pytest threemf/tests                          # slicer tests skip without the apps
uv run python -m p2p_threemf demo threemf/_out/demo_4color.3mf
uv run python -m p2p_threemf demo threemf/_out/k.3mf --product keychain   # nameplate, figurine, keychain_too_thin
uv run python -m p2p_threemf check threemf/_out/demo_4color.3mf --expected-slots 4 --report r.json
uv run python -m p2p_threemf check file.3mf --expected-slots 4 --slicer orcaslicer
uv run python -m p2p_threemf verify threemf/_out/demo_4color.3mf    # Bambu load and save, compare
uv run python -m p2p_threemf read some_project.3mf
uv run python -m p2p_threemf retarget meshy_multicolor.3mf out_x2d.3mf [--geometry geometry.json]
uv run python -m p2p_threemf refresh-profiles --reference bambu_saved_x2d_project.3mf
uv run python -m p2p_threemf lab --results data/slice_reports/purge_lab.jsonl --reports data/slice_reports
```

How "opens ready to slice" is checked without the GUI, on every gate run:
1. Presets: Bambu's re-save keeps `printer_settings_id`, `print_settings_id`,
   `filament_settings_id`, `filament_colour`, `filament_ids`, and `different_settings_to_system`
   stays empty, so Bambu treats them as unmodified system presets.
2. Paint: every triangle reads back with the same filament from Bambu's re-save (7,680 of 7,680
   on the demo; 31,159 of 31,159 on the Meshy retarget).
3. Slots: `slice_info.config` lists grams for exactly the painted slots.
4. It slices with the file's own settings, nothing passed on the command line.

## Known gaps

- Nobody has opened a file in the Bambu Studio GUI (the rule is CLI only). The CLI uses the same
  3MF loader; the GUI's preset selection step is the one path not exercised.
- No thumbnails are written; Bambu regenerates them.
- About 137 project keys (AMS count, nozzle stats, bed type, ...) come from one Bambu-saved X2D
  project (Jackson's Meshy figure) stored in `data/bbl_profiles.json`. They describe his printer.
  `test_snapshot_matches_installed_app` fails when Bambu updates its profiles; rerun
  `refresh-profiles` with a fresh Bambu-saved X2D project.
- Process presets are mapped for the 0.4 nozzle only.
- Flush volumes follow Bambu's measured tables and formula (the GUI's matrix for Jackson's
  filaments is reproduced exactly); new Bambu versions may change the tables (refresh-profiles).
- Without result.json (Orca, or a kept file) `purge_grams` falls back to an estimate (total minus
  model-feature extrusion).
- Reading a 1.5M-face file peaks at about 2.5 GB of memory. A 532k-face Meshy file: retarget
  4.5 s plus slice_check 16.4 s.
- Retarget drops triangles that repeat a vertex and duplicate triangles (`meshfix.clean_mesh`,
  `clean=False` to keep them); face indices then shift by the dropped count (in the sidecar).
- The gate fails on tool commands outside the project's filaments, except Bambu's own
  T65279/T65535 in the X2D end G-code, which Bambu logs as "Invalid T command" on every X2D slice.
- The paint audit's width is the largest inscribed circle measured on triangle centroids; it
  flags a region, it does not predict exactly what the slicer keeps.
- Retargeting keeps Meshy's multi-color mesh by default. Its faces are reordered and its
  vertices differ from the glTF (median 0.1 mm), so `figure_source="geometry"` copies paint by
  nearest face, not by index.
- OrcaSlicer 2.4.2 slices a patched copy (its own X2D G-code, two values clamped).
- The Saving prime mode is Bambu's own option but not yet printed on the X2D; check the first
  colour starts on the first real print. The Bambu CLI stalled once in 170 lab slices; the lab
  retries after 300 s.

## Contract change requests

Round 1 requests 1 to 3 (read-back and thin paint in SliceReport, process_overrides,
GeometryResult.source_glb) are in. Open:

1. `mcp_tools.Write3mfOut`: add `slots_used` and `default_slot`.
2. Done: `SliceReport` has `filament_changes`, `nozzle_changes`, `tower_grams`, `ams_swaps`,
   model and prepare time, and the per-filament split; `slice_check` fills them.
