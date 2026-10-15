# mcp_paint (`p2p_mcp_paint`)

The paint MCP server `paint` and the s09 paint agent. Opus 5.5 edits the figure's paint by
region id and judges the simulated print, not the ideal paint.

## Interface

```bash
uv run python -m p2p_mcp_paint --job <job_id> [--attempt N] [--jobs-dir DIR] [--sam3 local|mock]
uv run python -m p2p_mcp_paint --job <job_id> --base current           # studio edits on the result's paint
uv run python -m p2p_mcp_paint.proof --job <job_id> [--iterations 8]   # agent run + evidence
uv run pytest mcp_paint/tests                                           # no Claude, no slicer
RUN_LIVE_CLAUDE=1 uv run pytest mcp_paint/tests -k live -s              # 1 Opus iteration, Frenchie
```

Server: stdio, every tool in `p2p_contracts.mcp_tools.TOOLS` with exactly its contract input
fields (signatures are generated from the input models) and its contract output model as JSON
text. `render_views` and `simulate_print` also return MCP ImageContent (one sheet per kind, cells
512 px). mcp 2.x renamed FastMCP to `MCPServer`; this uses that.

State lives in `jobs/<id>/s09_paint/a<N>/` and is rewritten after every mutation:
`face_regions.npy`, `region_map.json`, `paint_state.json` (contracts), `session.json`
(checkpoints, tool log), `checkpoints/c<NNN>/`, `renders/`, `labels/` (SAM 3 cache, reused by
later attempts on the same figure), `cache/` (mesh npz; `load_ply` takes 10 s on 1M faces).

| Module | What |
|---|---|
| `job.py` | Loads s06 geometry (figure, parts), s06 `product_spec.json` (palette source) and `concept_read.json`, s07 X2D baseline paint (figure part, face for face), s05 GLB texture per face (through `face_index_map` if any), color spec (s02, else s01 `color_regions.json`), `filaments/library.json` (owned) and the catalog: every PLA Basic and PLA Matte color of `filaments/catalog.json` (54; `library.example.json` only when it is missing) |
| `labeling.py` | SAM 3 (`p2p_sam3`, local backend by default) on lit texture-color renders; flat products use the top and 3/4 views. Each labeled region is split by baseline filament: the dominant keeps the id, the rest become `<label>_<filament>` regions. That is where Meshy's color mistakes become addressable |
| `regionops.py` | merge, split (components, plane, geodesic seeds), grow, shrink, smooth_boundary (closing then opening), snap_to_creases, despeckle (region pieces, then same-filament islands), broken bands (`pieces`, `fragmented`, `bridge_pieces`, `consolidate`), all geodesic mm on the welded face graph |
| `palette.py` | CIEDE2000 matching with the achromatic guard, palette search (exhaustive under 60k combos, else greedy plus swaps), spool-swap suggestions |
| `palette_plan.py` | The cost-aware palette decision before the agent starts, and `earns_place` for any filament added later (see "Palette decision") |
| `session.py` | `PaintSession`: every tool as a method; product mesh = figure + add-on parts for renders and `simulate_print` |
| `checklist.py` | The PLAN checklist items code can decide |
| `concept_check.py` | Final print vs approved concept from all eight proof views (Opus 5.5, effort high), fix routing, review side-by-sides |
| `agent.py` | `run_paint`: Claude driver (Agent SDK), stub driver, iteration loop, final write and gate |
| `edits.py` | The studio paint editor's ops (contracts PaintOp: fill_region, brush_faces, replace_slot) through the session, face ranges, `current_paint`, seeding a session from a finished paint (or the s07 baseline's patches), and the preview: simulate_print, the 3MF, p2p_slicer numbers and toolpaths, the proof views (contracts PaintPreview) |
| `server.py`, `proof.py`, `prompts.py` | stdio server, evidence runner, prompts |

Worker: `worker/src/p2p_worker/stages/s09_paint.py` (`PaintStage`, registered in `RUNNERS`).
It records s06 and s07 as inputs, runs `run_paint` in the stage attempt folder, adds the
outputs (`paint_state`, `region_map`, `face_regions`, `checklist`, `slice_report`, `preview`,
`paint_3mf` or `debug_3mf`), books each Claude turn in the ledger (usd 0, list price in the ref)
and raises `NeedsReview` with a `review_request` when the gate fails or confidence is under 0.7.

## Studio paint edits

The same operations the studio paint editor sends (message kind `paint_edit`), for an agent
working without the browser:

- `open_job(job_id, base="current")` (or an attempt folder such as `s09_paint/a2`): a new s09
  attempt seeded from that paint, no SAM 3. A job with no paint map (thin slice) starts from the
  s07 baseline's connected same-filament patches, largest first.
- `apply_paint_ops(ops, source)`: fill_region (region ids), brush_faces (figure face ids as
  ranges, "0-9,15,20-31", or a file with them), replace_slot (every face, region, default and
  part of one slot). A filament is a `filament_id` or a palette `slot`. The palette rules are
  the session's (`paint_region` for fills, a free AMS slot for a new color, unowned catalog
  colors only with `source: any`; any catalog line, not only the auto-pick lines). Filaments the
  ops leave unused drop out at the end. A refused op names itself: "op 2 (brush_faces): face
  4000 is outside the figure".
- `preview_print(toolpaths, proof)`: simulate_print, `final.3mf` (the session's write), p2p_slicer
  time, grams, purge and per-layer toolpaths, and the paint and print views from the eight sides
  in `preview/`, returned as PaintPreview with the print sheet image. `before` is the previous
  preview, so preview once before editing.

These edits stay in the MCP attempt. To make one the job's result, post it through the worker
(`uv run python -m p2p_worker paint-edit --job <id> --ops '[...]'`): the worker repeats the ops
in its own s09 attempt, writes the 3MF the way s10 does, and runs s10's gate.

## Rules the tools enforce

- Achromatic guard: a target with C*ab <= 10 may only match a filament with C*ab <= 6; a blocked
  pairing costs +100 dE. Mid gray (L* 50) is dE00 about 27 from the loaded brown and about 30 from
  black or white, so without the guard gray becomes brown (the Frenchie's flecks).
- `paint_region` accepts palette filaments only, or `add_to_palette=true` with a free AMS slot.
  An unowned catalog color only when the job's palette source is any (s06 product spec);
  `suggest_palette(apply=true)` on an owned-only job applies owned spools and only suggests the
  swap. The palette search charges an unowned color 3 dE00 (s08's rule).
- `suggest_palette` returns a swap when a required color (a face part, a region over 1% of the
  figure, or a spec item) has no loaded match within dE00 10 and the catalog does at least 3 dE00
  better. It names the spool to load and the loaded spool to take out (unused first).
- `slice_check` expects every slot the written paint uses: a filament the slicer drops fails the
  gate.
- Checklist (code): spec_coverage (spec color within dE00 15 of a used filament; eye, nose and
  mouth hexes from intake are guesses and are skipped unless marked measured or customer),
  face_parts (eyes and nose present, eyes above the nose, nose between the eyes, parts over 1 mm2
  survive the sim, parts in the color of the model's texture there, which carries the concept,
  when an allowed filament is more than 5 dE00 closer), markings (spec markings present on
  regions named like them), islands (no same-filament piece under 1 mm2 in the paint, at most 3
  in the sim), creases (snap_to_creases would move at most 0.3% of the area), slot_count.
  sim_matches_intent is the agent's sign-off. Purge is reported with `passed=None` and never
  counts toward `passed_all` (PLAN changes, 2026-09-24); after the gate it is the slicer's
  number, before it the simulator's two-nozzle estimate (sim/README.md, Purge calibration).
- Confidence = min(agent sign-off, 1 - 0.2 x failed code items); 0 when the gate fails.
- A band prints as one strip or not at all (live2 sombrero: the hat's striped band printed as
  15 red pieces, largest 12%, 1.7 to 3.8 mm wide with gaps of 3.7 mm median and 8.3 mm at most,
  derived from the job files; no piece was under 1 mm2, so despeckle and the islands item never
  saw it). The cause was upstream of every despeckle: Meshy's 5-color paint had no green or
  yellow and split the band into red and black dashes on straw; the agent painted the red
  dashes the hat color and the final check recolored the black ones red. Now:
  - A band-like region (its label, or the final check's note about it, says band, trim,
    collar, strap, ribbon, brim, belt, bandana, scarf, hem, rim, border, harness, leash or
    bow; coat stripes, spots and patches are markings and never change) is fragmented when it
    covers at least 10 mm2 in 4 or more pieces with none holding 60% (thresholds ASSUMED).
  - `consolidate` bridges neighboring pieces across gaps up to 10 mm with strips at the band's
    own width (shortest face paths, never through eyes, nose or mouth) when that makes one
    strip and adds at most 1.5x its area; otherwise each piece takes the color around it.
  - Where: `merge_regions` on a band-like label (bridge only), the final check's
    `paint_region` fixes on the regions it names, and the runner before the final check and
    before the write (checkpointed as `bands (...)`).
  - The islands item fails on a fragmented band-like region ("bands printing as spots"), the
    region table has a pieces column, and the system prompt says a band is one strip in one
    color or none, never one stripe color kept as dashes.

## Palette decision

Before the first agent turn, `run_paint` decides the palette in code and paints every region
with it (`PaintSession.apply_palette_plan`, checkpoint c001, `palette_plan.json` in the
attempt, `palette.plan` decision in the s09 manifest). The first fidelity rerun let the agent
buy every closer catalog color and purge became 85 % of the Frenchie's grams (SOURCE:
docs/runs/fidelity/rerun). The rule (constants ASSUMED, in `palette_plan.py`):

- Targets: the approved concept's color areas by share (`concept_read.json`), plus the photo
  spec at half weight (face-part guesses skipped). The texture by region stands in when there
  is no concept read.
- A color the customer would miss: a target 15 dE00 or more from every palette filament (the
  checklist's spec limit) when a candidate brings it under 15. Such palettes are refused, so
  small defining colors keep their spool (pink inner ears are 20 from white).
- Shades between 6 and 15 dE00 trade against cost: J = visible error (sum of share x dE00 over
  6) + purge g / 10 + print hours + 1 per spool to buy. Purge is the simulator's two-nozzle
  model through `p2p_sim.simulate.PurgeEstimator` (50 ms per palette; 54.7 and 119.7 g on the
  rerun paint, against the simulator's 54.9 and 116.2 and the slicer's 49.4 and 116.2 g).
  Print time is a fit on five slices (236.8 s per model g, 39.3 s per tool change, 68.6 s per
  purge g; within 8 %). An owned spool wins unless a catalog color saves at least one unit of
  visible error, and near shades never both pay their way, so they merge.
- Caps: keychain, magnet and ornament 4 filaments, nameplate and figurine 5 (AMS slots).
- Regions take the filament of the concept color area their texture is closest to (within
  20 dE00), else their texture's nearest, so a speckled coat that averages to gray prints in
  the coat's color. Cream may match a cream filament (the planner's guard blocks only true
  neutrals, C* under 4, or a hue 35 degrees off).
- Later additions: `paint_region(add_to_palette=true)` (the agent's and the concept check's)
  goes through `earns_place`: it must lower J or bring a missed color under 15; refusals and
  additions are recorded in `palette_plan.json`. `suggest_palette` re-runs the decision.
  `P2P_PALETTE_PLAN=0` turns the whole step off.

## Agent loop

System prompt (job independent, cached by Claude Code) carries the PLAN checklist, the purge
rule and the protocol. The first message has the job, the region table (id, label, filament,
area, width, pieces, texture Lab, dE00), the baseline sim facts, the code checklist and the approved
concept image. Up to 8 iterations at effort medium; each must end with `checkpoint` (the runner
checks disk and asks once if it is missing) and a JSON verdict line. Sign-off resumes the session
at effort high. Renders and simulate_print sheets show all eight proof views for a 3D figure.

Final vs concept (`concept_check.py`): a fresh Opus 5.5 session at effort high sees the approved
concept, the photos, the 3D model's texture and the simulated print from all eight sides, the
job facts (form, add-on filaments, geometry notes) and the region table, and answers with
discrepancies, each routed: `addon_color` (applied to `part_filaments`), `paint_region` with
region ids (applied), other paint issues (one more agent turn), `text`, `plate_shape`, `loop`,
`other` (review). At most 3 judge calls; a major discrepancy left sends the job to review with
the concept next to the print sheet (`proof/check/round<N>/compare_print.png`). Confidence is
the lowest of the code checklist, the agent and the check. The runner then reloads the final
state, writes `final.3mf`, runs the gate and renders the proof set (`proof/final/`: model,
paint and print from the eight views, plus sheets; `proof/proof.json`). The agent cannot call
`open_job` and has no other tools.

Claude runs only through the Claude Code login: `ANTHROPIC_API_KEY`, `ANTHROPIC_API_KEY_TURNKEY`,
`ANTHROPIC_AUTH_TOKEN` and `DISABLE_PROMPT_CACHING` are removed from the environment while the
CLI starts.

## Proof (real Meshy jobs on disk, 0 credits)

`uv run python -m p2p_mcp_paint.proof --job <id>`; evidence in `mcp_paint/_out/<name>/`
(`compare_print_*.jpg` = simulated print before and after, filament colors; `compare_paint_*` =
paint; `checklist.json`, `slice_report.json`, `summary.json`). Full renders and state are in
`jobs/<id>/s09_paint/a<N>/`. Gate numbers are SOURCE (Bambu Studio CLI 2.08 slice reports);
list price is SOURCE (the CLI's `total_cost_usd`), billed $0 on the Claude Code login.

| | Frenchie keychain (a3) | Bronco nameplate (a2) |
|---|---|---|
| Iterations, wall time | 2, 12.6 min | 4, 7.2 min |
| Gate before (s07) | 9.54 g, purge 6.67 g, 38 min 53 s | 127.15 g, purge 98.67 g, 9 h 22 min |
| Gate after | 6.51 g, purge 3.64 g, 29 min 10 s | 124.79 g, purge 96.65 g, 9 h 7 min |
| Checklist | all code items pass except spec_coverage (pink inner ears: no pink spool) | all pass; no color spec on this M1 job |
| Confidence, review | 0.75, review (spool swap) | 0.80, no review |
| Claude (Opus 5.5) | 3 turns, $0.50 list, 407k cache-read tokens | 5 turns, $1.32 list, 2.03M cache-read tokens |

Frenchie: the brown flecks were neutral gray texture (L* 40 to 55, a* and b* near 0) that the
retarget sent to brown. The agent painted every `<part>_brown` piece white or black by its
lightness, turned the eyes to the spec's brown and grew them 0.3 mm so they survive the print,
despeckled at 1 mm2 (183 islands to 3) and snapped boundaries to creases. The brown eyes read
amber at #7C4B00; a darker brown spool would be closer. Bronco: cleaned the white specks and black
smudges on the brown head, painted neutral paw specks white, and dropped the mouth line, which
was thinner than the nozzle.

## Known gaps

- `simulate_print` takes about 45 s on the 1.13M-face Frenchie product (figure plus back). The
  result is cached per paint, so repeated renders are free.
- `select_by_pixel` uses the 1024 px view frame; sheets show 512 px cells.
- Region crops are not paired with pet photo crops (the photos are not region-aligned yet).
- `label_regions(replace=false)` only adds face parts that are missing; it never re-splits
  markings.
- The initial region map is only as good as SAM 3: tested on the Frenchie (flat relief, top
  and 3/4 views) and the Bronco (upright, five views).
- The concept check judges the simulated print against one concept image; the other sides are
  judged against the 3D model's texture. Geometry fixes (name, plate shape, loop) cannot happen
  in s09; they go to review.
- A second Opus session resumes for sign-off, so the MCP server restarts once (about 10 s of
  job loading on the Frenchie).

## Contract change requests

1. `SimReport.merged[].kind` is never set by `p2p_sim` (always the default "whole"), so "region
   lost" is computed here from per-face slots. Either fill it in the sim or drop the default.
2. `RenderOut.images` is `list[dict]`; a typed `RenderImage` model (kind, view, path, width,
   height, region_id) would let the web app read it.
3. `ChecklistItem.id` has `purge_under_40pct`; with purge now a cost (PLAN changes 2026-09-24),
   rename to `purge` and allow `passed=None` explicitly in the description.
4. `PaintState` has no place for spool-swap suggestions or the agent's sign-off; they live in
   `paint_run.json` and the manifest `data` for now.
5. Worker (its owner): `thin_slice` skips `s09_paint`, and `full` also lists s02, s04, s08 and s10,
   which have no runners yet. A profile of thin_slice plus s09 would run paint today.
6. `mcp_tools.SelectByPixelIn`: say that x, y are in the 1024 px view frame.
7. PLAN.md "Changes from docs" (orchestrator): mcp 2.x renamed FastMCP to `MCPServer`
   (`mcp.server.mcpserver`); the paint server uses it. The Agent SDK reports the session's
   cumulative `total_cost_usd` on every result, so per-turn list price is the difference.
