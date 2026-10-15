# sim (`p2p_sim`)

Offscreen renderer, face-ID buffer, region geometry and `simulate_print` for photo2print. The
paint agent judges the simulated print, so the rules and their sources are in
`docs/research/sim.md`.

Frame everywhere: contract frame (mm, Z up, front faces -Y, base on z = 0). Cameras:
`p2p_contracts.VIEWS`. Rendering is headless (moderngl standalone context; numpy fallback).

## Interface

Mesh IO (`p2p_sim.mesh`)

| Call | Returns |
|---|---|
| `load_glb(path, target_size_mm=None)` | `Mesh` in the contract frame, face order kept, UVs and base-color texture kept. glTF (Y up, front +Z) maps as (x, y, z) -> (x, -z, y). `target_size_mm` = largest extent; None reads glTF units as meters. Meshy GLBs are about 2 units tall, so pass a size. |
| `load_ply(path)`, `load_mesh(path, target_size_mm=None)` | PLY is read as is (already in the contract frame) |
| `save_ply(mesh, path, face_colors=None)`, `save_glb(mesh, path)` | face order kept |
| `Mesh` | `vertices`, `faces`, `uv`, `texture`, `face_normals`, `face_areas`, `face_centroids`, `face_adjacency` (welded, crosses UV seams), `adjacency_edges`, `vertex_normals`, `is_watertight`, `volume_mm3`, `face_texture_colors()` |

Cameras and rasters (`p2p_sim.camera`, `p2p_sim.raster`)

| Call | Returns |
|---|---|
| `camera_matrices(spec_or_view_name, mesh, size_px)` | `Camera`: `view`, `proj`, `view_proj`, `project(points) -> (xy_px, depth_mm)`, `pixel_rays(xy)`, `to_dict()` / `from_dict()`. Pixel (0, 0) is the top-left corner; pixel centers at +0.5. |
| `rasterize(mesh, camera, backend=None)` | `Raster`: `face_id` (int32, -1 background), `depth` (mm, inf background), `bary`, `points(mesh)` |
| `best_region_camera(mesh, face_regions, region_id)`, `camera_for_points(...)` | tight close-up cameras |

Renders (`p2p_sim.render`)

| Call | Returns |
|---|---|
| `render(mesh, view_or_camera, kind, face_colors, face_regions, size_px=1024, sim=None)` | `RenderResult`: `image` (uint8), `face_id`, `depth`, `camera`, `labels` (overlay: region id -> label pixel), `meta(path)` |
| `render_views(mesh, kinds, views, face_colors, face_regions, size_px, sim=None)` | one raster pass per view shared by all kinds |
| `render_crops(mesh, face_regions, region_ids, face_colors, size_px=512)` | tight shaded close-ups, target outlined in magenta, camera included |
| `render_sheet(mesh, face_colors, out_png, kind="shaded", sim=None)` | 3 x 2 grid of the six views (delivery and golden-set review) |

Kinds: `flat` (unlit, exact filament sRGB inside regions), `shaded`, `texture` (SAM 3 input,
likeness), `overlay` (flat + boundaries + numbered labels at each region's widest visible
spot), `crops`, `sim` (simulated print: bleed tint and faint layer lines). Face colors are
per-face sRGB, e.g. `palette_rgb[face_slots - 1]`.

Regions (`p2p_sim.regions`)

| Call | Returns |
|---|---|
| `region_geometry(mesh, face_regions)` | `{id: RegionGeometry}`; `.fields()` gives `n_faces`, `area_mm2`, `centroid_mm`, `bbox_min_mm`, `bbox_max_mm`, `min_width_mm` for `p2p_contracts.Region` |
| `views_seen(mesh, face_regions)` | region id -> number of contract views showing it |
| `fill_regions(region_map, mesh, face_regions)` | copy of a `RegionMap` with geometry filled |

Simulation (`p2p_sim.simulate`)

```python
sim_face_slots, report = simulate_print(
    mesh, face_slots, palette_labs, params=SimParams(),
    face_regions=None,           # region ids for MergedRegion / BleedFlag; default: same-slot patches
    out_dir=None, job_dir=None,  # writes sim_face_filaments.npy, FileRef relative to job_dir
    face_parts=None, part_slots=None,  # multi-part prints: part per face, filament slot per part
)
```

`face_slots` are 3MF slots (palette index + 1) with every face resolved; it may be None when
`face_parts` and `part_slots` are given. `simulate_print_detailed(...)` returns a `SimResult`
with per-face arrays (`face_slots`, `face_regions`, `face_changed`, `face_light_layers`,
`face_hidden`), `layer_slots`, `layer_swaps` and `report`; pass it to `render(..., "sim", sim=res)`.

Rules, in slicer order: layers (walls snap to the layer's slice contour; painted tops and
bottoms penetrate 5 and 3 layers), erosion (surface opening at 2 x nozzle plus in-layer
thickness), bleed (light over darker by L* 30 with under 5 layers), swaps and purge (Bambu
Studio flush volumes, two nozzles). Details and sources: `docs/research/sim.md`.

Proof renders (`p2p_sim.proof`): `proof_set(mesh, orientation, out_dir, model_rgb=,
paint_rgb=, sim=)` draws the product from the eight contract `PROOF_VIEWS` (front, both front
3/4 views, both sides, back, back 3/4, top) in the display pose (a flat charm turned back up),
one PNG per kind (model texture, paint, simulated print) and view plus a 4 x 2 sheet per kind.
`side_by_side(concept, sheet, out)` puts the approved concept next to a sheet. Named views
anywhere in p2p_sim accept the proof view names (`camera.ALL_VIEWS`).

## Purge calibration

`SimParams.nozzles` 2 (the default, the X2D): filaments are split between the two nozzles the
way Bambu's "Auto For Flush" does (every split tried, least flush wins); a change to a
filament already on the other nozzle flushes nothing; a same-nozzle change flushes Bambu's
volume x `FLUSH_CALIBRATION` (0.96); the prime tower is `TOWER_G_PER_LAYER` (0.0386 g) per
layer up to the last change plus `TOWER_G_PER_CHANGE` (0.0236 g) per change. Fitted on three
of our files re-sliced by the Bambu Studio 2.08 CLI with the G-code kept (SOURCE:
`sim/tests/fixtures/purge_calibration.json`; the sim's nozzle split matched the slicer's on all
three):

| Print | Slicer purge (tower) | Two-nozzle model | Old one-nozzle model |
|---|---|---|---|
| Bronco nameplate, s09 a2 | 96.65 g (21.00) | 96.5 g, -0.2 % | 149 g, +54 % |
| 3D Frenchie keychain prototype | 14.82 g (10.97) | 15.8 g, +6 % | 53.6 g, +262 % |
| flat Frenchie keychain, s09 a3 | 3.64 g (2.35) | 6.1 g, +68 % | 15.1 g, +316 % |

Checked afterwards on two prints the fit never saw (the fidelity rerun, docs/runs/fidelity/rerun;
SOURCE: their s10 slice reports): the 3D Frenchie in 4 colors, slicer 49.37 g, model 54.9 g
(+11 %); the Bronco nameplate in 5 colors, slicer 116.23 g, model 116.2 g (0 %).

`PurgeEstimator(product_mesh, params, face_parts)` scores many palettes on one product: the
layer spans are computed once (`layer_spans`, the same rules as `layer_presence`), then each
palette costs one presence table and the nozzle split (`purge_from_presence`), about 50 ms on
the 1.1M-face Frenchie. It skips erosion and bleed, which move few faces: 54.7 g and 119.7 g
on the rerun's paint against `simulate_print`'s 54.9 and 116.2 g. The paint stage's palette
decision uses it (mcp_paint/README.md, Palette decision).

The flat Frenchie is still over: the sim counts 42 tool changes where the slicer made 20. Once
the slicer gate has run, the paint checklist reports the slicer's purge instead.
`SimParams(nozzles=1)` keeps the old model. `SimParams.dark_to_light_weight` is now the plain
multiplier the contract describes (default 1.0 leaves Bambu's volumes as they are; the sim used
to halve dark to light swaps).

Fixtures for tests and demos: `fixtures.make_figurine(target_faces)` (textured two-animal
figurine with ground-truth regions), `products.nameplate(mode)`, `products.keychain()`,
`products.multipart_figure()`.

## Commands

```bash
uv run pytest sim/tests                        # 105 tests, about 80 s
uv run python -m p2p_sim render model.glb --size-mm 60 --out sim/_out/x \
    --kinds flat,shaded,overlay,texture,faceid,sim --sheet --crops 3,5
uv run python -m p2p_sim simulate model.glb --size-mm 60 --out sim/_out/x/sim --render \
    [--slots face_slots.npy --palette palette.json --regions face_regions.npy] \
    [--parts face_parts.npy --part-slots 1,2,4]
uv run python -m p2p_sim regions model.ply --regions face_regions.npy
uv run python -m p2p_sim fixture --faces 200000 --out sim/_out/fixture
uv run python -m p2p_sim demo --out sim/_out   # every sample below plus timings.json (~2 min)
```

Without `--slots`/`--regions`, textured meshes get k-means texture-cluster regions (a demo
labeler, not the pipeline's). `P2P_SIM_BACKEND=numpy` forces the CPU rasterizer.
`P2P_BAMBU_FLUSH_DIR` points at another Bambu Studio `flush/` folder.

Sample outputs (after `demo`): `sim/_out/<name>/` for `figurine_50k`, `figurine_200k`,
`nameplate_tops`, `nameplate_parts`, `keychain_45mm`, `multipart_figure`, `meshy_beagle`, each
with `flat_*`, `shaded_*`, `overlay_*`, `texture_*`, `faceid_*` (png + npy + depth npy), `sim_*`,
`crops_r*`, `sheet.png`, `sheet_sim.png`, `sim_report.json`, `regions.json`, `renders.json`;
`sim/_out/timings.json` has the timings.

## Known gaps

- Purge: three calibration prints (above); more slices would pin the tower terms. Flush into
  infill is not modeled separately (it is inside the fitted numbers); greedy swap order.
- Layer rule: the default follows Bambu Studio's source, which contradicts PLAN.md's rule 2
  (see Contract change requests). `simulate.QUANTIZE_MODEL = "plan"` restores PLAN.md's rule.
- Bleed has no per-filament translucency (`Filament.td_mm` is not used); the L* 30 threshold
  and the 3D nearest-surface fill are estimates to calibrate.
- A thin part with its own filament (multi-part raised text) keeps its color at any width; it
  prints as a thin wall, which is a geometry check upstream.
- Multi-part inside tests use vertical-line parity: parts with holes can misclassify faces near
  the hole. Which part owns an overlap volume is not modeled (only affects layer presence).
- Model grams is a shell plus 15% infill estimate, not the slicer's number.
- Speed at 194k faces, 1024 px, GL: flat 0.06 s, overlay 0.11, shaded 0.34, texture 0.45, sim
  0.58 per view (first call adds about 0.2 s for normals and upload). The numpy
  fallback is about 15 times slower. `simulate_print` takes 0.8 s at 51k faces, 2.6 s at 194k,
  and 3 to 7 s on the dense 180k to 520k-face product fixtures. `region_geometry` takes about 2 s
  at 200k faces (the min-width ladder).
- `flat` and `overlay` are not antialiased (exact colors); `shaded`, `texture`, `sim` are 2x
  supersampled. Faces smaller than a pixel may be missing from a view's face-ID buffer.

## Contract change requests

Request 3 is partly done: `SimParams.nozzles` (already in the contract) now drives the
two-nozzle purge model; `flush_multiplier` and the paint layers stay constants in simulate.py.
Request 2's semantics are done (dark_to_light_weight is a plain multiplier); the rename is not.

1. `SimParams.flat_angle_deg` says "Surfaces under this tilt get layer-quantized". Bambu
   Studio does the opposite (MultiMaterialSegmentation.cpp): painted surfaces under the tilt are
   projected and keep their XY shape; steeper walls snap to the slice contour. Reword it to
   "Tilt separating projected top/bottom paint (below) from wall contour colors (above)" and add
   `quantize_model: Literal["bambu", "plan"] = "bambu"`. PLAN.md rule 2 needs the same note
   (dated, under "Changes from docs").
2. `SimParams.dark_to_light_weight` (default 2.0) is applied as weight / 2 on top of Bambu's
   flush volumes, which already charge dark to light 3 to 10 times more. Rename to
   `dark_to_light_multiplier` with default 1.0.
3. Add to `SimParams`: `nozzles` (X2D: 2), `top_paint_layers` (5) and `bottom_paint_layers` (3)
   from the process profile, and `flush_multiplier` (1.0), so calibration does not need code.
4. `MergedRegion.area_mm2` is the area lost, which can be part of a region. Add
   `region_area_mm2` (or `whole: bool`) so a full merge is distinguishable from trimmed slivers.
5. Multi-part prints need a contract: `face_parts.npy` (int32 part index per face, same face
   order as the mesh) next to `face_regions.npy`, and a part-to-filament map (for example
   `PaintState.part_filaments: dict[str, str]`), so `simulate_print`, the 3MF writer and the MCP
   agree on parts. `SimulatePrintIn` would then need nothing new.
6. `SimReport.purge_pct` is a percentage (0 to 100+). The checklist id `purge_under_40pct`
   matches that; state it in the field description.
