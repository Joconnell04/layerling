# worker (p2p_worker)

The job runner. It runs the thin slice (s01 intake, s03 concept, s05 3D model, s06 geometry,
s07 baseline 3MF) and the full profile (all ten stages, with the paint agent or its stub at s09)
end to end, resumable after a crash, inside the budget caps, against the fake Meshy server
(MESHY_MODE=test), the stored paid outputs (MESHY_MODE=replay, 0 credits, evals/README.md) or
the live API (MESHY_MODE=live).

## Interface

**Package layout** (`worker/src/p2p_worker/`)

| Module | What |
|---|---|
| `orchestrator.py` | `Orchestrator.create_job`, `advance(job_id)`, `resume_job(job_id)`: the state machine |
| `stages/s01_intake.py` ... `s10_write.py` | Stage runners; `stages/base.py` is the resumable `StageContext`, `stages/common.py` the shared result, review and note helpers |
| `stages/s09_stub.py` | s09 switch: `PAINT_AGENT=stub` (default) keeps s08's first pass; `PAINT_AGENT=agent` runs the paint workstream's `stages/s09_paint.py` |
| `paint_edit.py`, `stages/s09_paint_edit.py` | `paint_edit` messages (the studio paint editor, or Claude through the CLI): a new s09 attempt with the ops, the p2p_slicer preview, then s10 through the gate (see Paint edit below) |
| `printing.py` | Paint transfer, CIEDE2000 with the achromatic guard, spool suggestions, despeckle, X2D write and gate, render sheet and preview GLB (s07, s08, s10) |
| `stages/question_bank.yaml` | Intake questions per product, with their effects on size, add-ons and form |
| `stages/prompts.py` | Concept, add-on edit and view prompts; add-on resolution; asks from answers |
| `stages/product_plan.py` | What the concept shows per form (visible add-ons, name, asks) and the concept checklist |
| `intent.py` | The form a request states ("3d", "flat") and custom asks in casual phrasings |
| `imaging.py` | Paste-back alignment, code composites (loop, base, plate and name), view split and normalize, palettes |
| `sweep.py` | Meshy backup status read: kicks jobs waiting on Meshy for 10 minutes |
| `clients/meshy.py` | Async Meshy client, the one price table (`PRICES`), DELETE guard |
| `clients/claude.py` | `ClaudeCodeClaude` (default), `AnthropicClaude` (turnkey), `MockClaude` |
| `clients/supabase.py` | `SupabaseStore` (PostgREST, Storage), `RealtimeListener` |
| `store.py` | `JobStore` interface, `LocalStore` (folders plus a JSON inbox) |
| `budget.py` | `BudgetGuard`: check before every paid call, record after; job spend read back from its spend rows, build spend from every source |
| `credit_ledger.py` | Paid-call ledger per job (`paid_calls.jsonl`), input hashes, the cross-job cache, the credit report (`credits.json`) |
| `fakes/meshy.py` | Fake Meshy server (httpx MockTransport) that replays recorded fixtures |
| `fakes/replay.py` | MESHY_MODE=replay: every Meshy call answered from evals/fixtures (the paid outputs of stored jobs) by input hash, else job, stage and step key; 0 credits; a miss fails the stage |
| `app.py` | FastAPI app and the event-driven job loop (`serve`) |
| `local.py` | `run-local` and the auto-responder that plays the web app |
| `logs.py` | JSON line logs with job/stage/attempt context and redaction |
| `trace.py` | Traces for the admin viewer: one redacted body per Claude, Meshy or slicer call plus `data.trace`; `trace-backfill` |
| `smoke.py` | `smoke-supabase`: one TEST job through `serve` on the real project, then cleanup |
| `stages/adapters.py` | Loads the Meshy GLB and the filament library for the pinned sibling calls |
| `../deploy/` | launchd plist template, `install.sh`, `uninstall.sh` |

**Stage behavior**

| Stage | Calls | Gate | Outputs (role) |
|---|---|---|---|
| s01 intake | Haiku (Claude Code login); form values from `input/request.json` are never asked again; keychains and magnets get the form question (flat charm or 3D figure) unless the request or the order form says | `questions` -> `answers` | `intake.json` (intake_result), `color_regions.json`, `answers.json`, `subjects.json`, `addons.json`, `product.json` (product_spec) |
| s03 concept | Per candidate: figure render (gpt-image-2 + nano-banana-pro, remove_background) -> add-ons drawn in code around it (free) -> Haiku check; only a failed product check buys an edit chained from the figure (nano-banana-pro for a name, nano-banana-2 for a loop or base; figure pixels pasted back), and a failed edit switches model once (at most 2 paid edits per stage). Flat form: charm art -> Creative Lab prototype -> loop drawn in code | `concepts` -> `concept_pick` or `tweak` (Haiku routes, `tweak_decision`) | `figure_r<N>c<M>.png`, `edit_*.png`, `concept_r<N>c<M>[_n].png`, `approved_concept.png` (the product), `figure_concept.png` (the 3D input), `concept.json` |
| s04 multi-view | Image to Image `generate_multi_view` chained from the approved figure (its own model), normalize, Haiku view check, one retry when the back fails | none | `view_concept.png`, `views<k>_<n>.png`, `views.json` (payloads.ViewSet) |
| s05 3D model | With views: Multi-Image to 3D, `image_urls` = `texture_image_urls` = ordered views, no `input_task_id`. Without: Image to 3D from the figure task. Flat: Creative Lab build of the approved prototype. latest, 2k, multi-view thumbnails; Retexture by task id (Image to 3D) or `model_url` | `mesh_review` -> `mesh_decision` | `mesh_m<N>.glb` (approved_mesh), thumbnails, `mesh.json` |
| s06 geometry | Analyze Printability (free; with Meshy off, or a Creative Lab build Meshy will not take by task id, the model goes by `model_url` or the earlier attempt's analysis is reused); the product spec (`product_spec.py`: form from the job, plate shape, add-on colors, palette source); a Sonnet 5 read of the approved concept (`concept_read.py`, cached by sha256); `build_product(vertices, faces, product, addons, target_size_mm, library, settings, out_dir, form=, colors=)` | none | `printability.json`, `product_resolved.json`, `concept_read.json`, `geometry.json` (GeometryResult, paths made job-relative), part meshes, `warnings.json` |
| s02 color spec | Sonnet 5 (Claude Code login): one line per part, LAB from the hex here; SAM 3 (`SAM3_BACKEND`) crops each part from its best photo | none | `color_spec.json` (color_spec), `crop_<subject>_<label>.png` (part_crop) |
| s07 baseline | Multi-Color Print on the s05 task (cartoon, max_colors = AMS slots); `printing.py` chain: paint onto the geometry figure (fit to the uncut figure), guarded color map, despeckle, X2D write with parts and `GeometryResult.supports`; thin slice: `slice_check` | thin slice pass: `result` with render sheet and preview GLB; fail: `review_request`, job REVIEW. Full profile: no gate | `baseline_cartoon.3mf` (Meshy, never shipped), `baseline_x2d.3mf` (baseline_3mf) or `debug_x2d.3mf`, `render_sheet.png`, `preview.glb`, `slice_report.json`, `result.json` |
| s08 palette | none (code): paint transfer, despeckle at the Meshy-color level, same-color patches become regions, guarded map to loaded spools (plus catalog colors with source any), spool suggestions; applies a `palette_edit` when the orchestrator started the attempt for one | none; `spool_suggestion` messages | `region_map.json`, `face_regions.npy`, `paint_state.json` (palette = loaded spools in AMS order; with source any or an edit, the filaments used), `palette.json`, `filaments.json` (library snapshot plus the catalog colors the palette uses), `first_pass.png` |
| s09 paint | stub or the paint agent (see `stages/s09_stub.py`); the agent run ends with the final-vs-concept check (Opus 5.5, effort high, all eight views) | agent's own; a failed concept check goes to review with the side-by-sides | `paint_state.json` (paint_state), `proof/final/*.png` (proof_model, proof_paint, proof_print, proof_sheet), `proof/check/round<N>/compare_print.png` (concept_compare) |
| s10 write | none (code): paint state to face slots, despeckle, X2D write, `slice_check`, previews, the proof set | pass: `result` (baseline_only false, proof_images); fail: `review_request`, job REVIEW | `final.3mf` (final_3mf) or `debug_x2d.3mf`, `render_sheet.png`, `preview.glb`, `proof/*.png` (the 3D model, the paint and the simulated print from the eight proof views), `slice_report.json`, `result.json` |

Profiles: `thin_slice` runs s01, s03, s04, s05, s06, s07; every other stage gets a SKIPPED manifest.
`full` runs all ten (`run-local --profile full`).
The dev cost profile (`P2P_COST_PROFILE=dev` or `--dev`) uses one nano-banana-2 concept (6 cr), add-ons
drawn in code (a nano-banana-2 edit, 6 cr, only when the composite fails its check), no views, and
meshy-6-lite (15 cr).

**Job state rules**

- Every attempt writes `jobs/<job_id>/<stage>/a<N>/manifest.json` after each step. A stage replays
  from the top on every run; finished steps, Meshy tasks, Claude calls and gate replies are cached in
  the manifest, so a replay spends nothing twice.
- STALE: before reusing a SUCCEEDED (or open) attempt, the orchestrator re-hashes its inputs and
  checks each upstream stage is still on the attempt it read. A change starts a new attempt and
  cascades downstream. A DONE job re-checks on every `advance`.
- Retries: a failed stage gets 2 more attempts, then PAUSED with the reason. A failed Meshy concept
  is retried once inside the stage.
- Gates open with an assistant message (`questions`, `concepts`, `mesh_review`), stage status
  WAITING_USER, and consume the user's reply (`messages.consumed_at`). Invalid replies get a `note`.
- Meshy: create once per step key (a pending marker survives a crash between POST and record; on
  resume one list call adopts the task). Wait for the webhook row in `meshy_events`; confirm each
  terminal event with one GET (webhooks are unsigned); a later non-terminal event is ignored.
  Backup status read (FROM JACKSON: approved; 0 credits): a task older than
  `MESHY_MISSED_WEBHOOK_S` (600 s) gets one GET, at most once per that interval
  (`data.meshy_status_reads`), whoever wakes the job; `serve` kicks every job due one each
  `MESHY_SWEEP_S` (600 s, `sweep.py`). A read that finds a terminal task with no webhook row logs
  `meshy_missed_webhook` and records a decision. No other polling.
- Paid-call ledger (`credit_ledger.py`, every Meshy create through `StageContext.meshy_create`):
  a call's identity is its input hash, sha256 of the endpoint and the body by content (a signed
  Storage URL becomes the sha256 of the job file, a data URI the hash of its payload; stage,
  attempt, key and purpose are not part of it). Before the POST: the pre-flight, then an intent
  in `jobs/<id>/paid_calls.jsonl` (stage, attempt, key, purpose, hash, expected credits, why)
  and in the spend table (kind `intent:<endpoint>`, 0 credits, ref
  `<hash>|a<attempt>|<key>|<expected>`); after it, the task id against the intent; at the
  terminal webhook, the final state. A retry, restart, rerun or new attempt anywhere in the job
  with the same inputs takes the task already paid for instead of creating one: reused at 0
  credits when it finished (a copy in the new attempt's `meshy_tasks` with credits 0 and
  `data.meshy_reused`, a trace entry at 0, decision `meshy.reused.<key>`), adopted when still
  running (the booking stays with the payer; the final adjust or refund lands here). Its files
  come from the payer's folder (`asset` events), so a reuse makes no Meshy request; a reused
  task whose files are gone and past retention is marked unusable and the next attempt makes a
  new one. An intent with no task id (a crash between the intent and the record) gets one list
  call, then is adopted or marked abandoned. A FAILED (refunded) task is never reused.
  Generalizes the s05 adoption after a network blip (`orchestrator._adopt_after_transient`
  still runs first and is now redundant).
- Identical inputs and fresh variations: identical inputs reuse, whatever the stage or attempt.
  A caller that wants a new sample passes `fresh=True` (a concept tweak round, a mesh
  regenerate or retexture); inputs identical to a task the job already has are then refused,
  because Meshy has no seed: the stage changes an input first (a tweak round repeated word for
  word adds "Variation <round>." to its prompt; a regenerate takes the next REGEN_VARIANTS entry
  the job has not bought; a retexture takes the next style input, or its text marked
  "Variation <n>.").
- Across jobs (`MESHY_REUSE_ACROSS_JOBS`; default on for live Meshy, off for the fake, whose
  task ids restart in every process): a succeeded task of another job with the same input hash,
  finished within 60 h (Meshy keeps assets 3 days), is reused at 0 credits
  (`jobs/_worker/meshy_cache.jsonl`); its files are copied from that job's folder.
- Pre-flight before every paid create (`StageContext._preflight`): the job caps with the job's
  spend read back from its spend rows (`BudgetGuard.sync_job`: a JobRecord that shows less is
  raised to match), the build caps for live spend, then the free balance GET
  (`MESHY_BALANCE_CHECK`, default on): a balance below the price pauses the stage with
  "Meshy balance 20 credits: 3D model round 1 (generate) needs 35" before anything is sent,
  and the refusal is a `refused` line in the report. A 402 from Meshy at create pauses the same
  way (budget_exceeded) instead of failing the stage.
- Credit report: `jobs/<id>/credits.json`, rebuilt after every ledger event: every paid call
  (stage, attempt, key, purpose, why, how: new, reused, adopted or refused, credits, expected,
  saved, task id, status, where a reuse came from) and totals (paid, reused calls, saved,
  refunded, pending, refused calls, and the job's spend rows, which equal paid). The stage that
  posts the result copies it as `credits.json` (role credit_report), puts the totals in
  manifest data `credits`, and writes one line in the `result` message body ("Meshy credits:
  75 paid, 2 reused (saved 21)"); JobResult has no field for it yet (contract change request
  9).
- Form (s01): `product.json` and `jobs.form` hold `flat` or `figure`. Keychains and magnets ask
  "Flat charm or 3D figure?" (required, `covers: form`) unless the order form (`request.json`
  `form`, `run-local --form`) or the request's own words (`intent.form_from_text`: "3d",
  "figurine", "flat", "relief") set it; answer > order form > request > `FORM_DEFAULTS` (only if
  unanswered). Every other product is `figure`.
- Custom asks (s01): Haiku lists them (`kind|subject|what`); `intent.asks_from_text` merges its
  finds under them so "wearing a sombrero" survives a model miss; bank options marked `ask`
  (collar, bandana, toy) and text typed into a non-geometry question are asks too. A name with no
  place on the product (only a nameplate or a flat keychain engraving of up to 10 characters has
  one) is kept with `buildable: false` and gets a "Not printable here" note. Asks go into the
  figure prompt ("Must show: ..."), the tweak and view prompts ("Keep showing: ..."), and the
  concept check.
- Concept check (s03): Haiku (`clients/claude.py CHECK_MODEL`) sees the photos, the figure and
  the product against `ProductPlan.check_items()`: parts in the picked plate shape and loop
  spot, the exact name (a read that differs by a letter or case fails), every custom ask, every
  intake pick an image can show (`answer.<question id>`: pose, look, framing, from
  `prompts.answer_checks`; `answer.form` on a 3D keychain or magnet), markings, no stray base,
  and nothing nobody asked for (`no_extras` on the figure, `no_extras.product` on the product:
  no frame, ring, border, backdrop, block or extra object). live2: the Bronco's answer was
  sitting and the approved concept stood; both catfish charms came inside a decorative ring
  frame; neither was checked. Code adds "figure pixels unchanged" (at least 98% of the
  figure's opaque pixels, `IDENTICAL_MIN`) and, for a flat keychain, the engraving Meshy
  echoes. A figure problem gets one new figure render with the problems in its prompt (its
  product starts from code again); a product problem on the code composite gets one paid edit
  with the problems named, and a failed edit switches to the other edit model once. At most
  `MAX_CONCEPT_FIXES` (4) paid regenerations per job; a candidate that still fails is shown
  with its check and approving it is recorded.
- Cheapest first (s03): the loop, base or plate with the name is drawn in code around the
  unchanged figure first (free; decision `concept.cheapest.<cid>.<n>`), and a paid edit is
  bought only when that composite fails the concept check. At most `MAX_PAID_EDITS` (2,
  ASSUMED) paid product edits per s03 stage across its attempts, counted from the credit
  ledger (reused or refunded edits do not count). A candidate whose paid edits all failed is
  shown on its code composite. live2 Bronco: 60 credits of plate edits (four nano-banana-pro
  and two gpt-image-2), and the plate the customer approved was the code composite (SOURCE:
  its s03 manifest).
- Product edit models (s03, FROM JACKSON): a model whose paid product edit failed its check is
  never used again in the job (`data.edit_failures`, across candidates, rounds and attempts),
  and after `MAX_FAILED_EDITS` (2) failed edits no more edits are bought. The model choice is a
  cached step (`edit_model.<cid>.<n>`), so a replay never swaps the model of an edit already
  paid for.
- Budget: before every paid call, job caps ($10, 400 credits; counted in test mode too), then for
  live spend only the build ceiling (`BUILD_SPEND_CEILING_USD`) and the Meshy build cap
  (`MESHY_BUILD_CREDIT_CAP`). Build spend is the larger of the spend table and this machine's
  `jobs/_worker/spend.jsonl` plus the fixture capture ledger `data/ledger.jsonl`, read page by
  page (1,000 rows); a spend table that cannot be read pauses with "Spend table unreadable"
  instead of falling back to the local file. live2: the table read 491 live credits, the local
  file 463 (the 28 fixture credits reach only the table and `data/ledger.jsonl`), and the
  Meshy balance 651 = 1,142 - 491 (SOURCE: all three read after the run). Over a cap: stage
  PAUSED with `JobRecord.pause_reason`, nothing sent.
- Color-only fixes (s05): a `regenerate` whose note only changes colors or markings
  (`intent.color_only_fix`: a color word and no shape, size, pose or part word) is done as a
  Retexture with the note as `text_style_prompt` (10 credits instead of 35; decision
  `mesh.cheaper.m<N>` and one note), except on a flat charm (regenerate only).
- Resume: a user `resume` message (payload `Resume`) on a PAUSED job applies `cap_usd` and
  `cap_credits` if given (also written to `jobs.budget_usd` and `budget_credits`), clears the
  pause and continues. A budget pause resumes the same attempt; any other pause starts a fresh
  one. The worker answers with a `note`.
- Concept tweaks: after 5 re-render rounds `ConceptsPayload.tweaks_exhausted` is true; a further
  re-render tweak gets a `note` and the gate keeps waiting for a pick.
- Spend lands in `ledger.jsonl`, the `spend` table and `job.budget`. Every row fills
  `usd_list` and `billing`: Meshy `credits` (usd_list = the credits at the plan rate), Claude
  Code `subscription` (usd 0, usd_list = the list price Claude Code reports), API `api`, mocks
  `free`, Runpod `gpu_seconds`. Failed Meshy tasks book a refund row. Subscription calls do not
  count toward the $10 cap.
- Colors (s07, s08): CIEDE2000 to the loaded spools with an achromatic guard: a source color
  with chroma under 10 only matches a filament with chroma 6 or less (the paint server's
  thresholds), so the Frenchie's gray goes to black or white, never brown. A color covering at
  least 0.5% of the model (or a required ColorSpec part) with no loaded spool within dE00 10 gets
  a suggestion in manifest data (`spool_suggestions`) and one `spool_suggestion` message per
  spool (payloads.SpoolSuggestion: "gray PLA", target hex and LAB, dE00 now, dE00 with the
  closest spool in `filaments/library.example.json` when one is within 10, affected region
  labels, share of the model, and `filament_id`, the catalog id to apply in one click).
- Filament catalog: `filaments/catalog.json` (every single-color Bambu filament with an X2D
  preset, owned false; `python -m p2p_filaments catalog`, falls back to
  `filaments/library.example.json`). The worker only picks or suggests colors from
  `printing.AUTO_PICK_LINES` (PLA Basic, PLA Matte) on its own: silk, sparkle, translucent,
  glow, wood and CF lines do not print their nominal hex, and PETG, ABS or TPU do not bond with
  PLA in one print. A customer can still name any catalog id in `palette_edit` slots.
- Source any: when the `palette_edit` (or the job, see contract change request 6) asks for
  `source: any`, s08's candidates are the owned spools (loaded first, then owned but not
  loaded) plus the auto-pick catalog colors. Owned spools come first through a named penalty,
  `printing.UNOWNED_PENALTY_DE = 3.0`: an unowned color is compared at its CIEDE2000 plus 3, so
  it only wins when it is more than 3 dE00 closer than the best owned spool (the gray guard
  applies to catalog colors too). The palette is then the filaments the model uses, owned in
  slot order first, at most `ams_slots` (or `max_colors`); `printing.reduce_palette` drops the
  filament covering the least area and re-matches its colors until it fits, never dropping an
  add-on part's filament. With source owned and no edit, s08 is unchanged (palette = every
  loaded spool). Manifest data and `palette.json` record `source` and the `unowned` ids.
- Palette edit: a user `palette_edit` message (payloads.PaletteEdit) on a job with a result
  (DONE, or REVIEW after a result) is consumed first, then checked: unknown filament ids, a
  filament in two slots, more slots than AMS slots, or a recolor whose slot count differs from
  the shipped palette get a `note` and change nothing. A valid edit runs new attempts of s08
  (the edit is stored in its manifest data as `palette_edit`, so a replay re-applies it), s09
  when the profile has it (always the stub: it carries s08's paint state, so the paint agent
  never undoes an edit), and s10, with Meshy switched off. Old attempt folders are never
  touched. s10 passing posts a new `result` and the job returns to DONE; a failed gate goes to
  REVIEW with a `review_request` exactly as before (the last good `jobs.result` stays). Any
  other failure pauses the job with the reason and a `note`.
  - recolor: the paint state that shipped (s10's `paint_state` input; s08's own first pass on
    a thin-slice job) keeps its regions and region map; slot i takes `slots[i]`.
  - refit: s08 re-matches the model colors: to exactly `slots` (palette in that order) when
    given, else auto-picks from owned spools, plus the catalog with `source: any`;
    `max_colors` caps the count. Add-on parts move to the nearest given slot.
  - Edits on a running or waiting job stay unconsumed until it finishes; on a FAILED or
    CANCELLED job they get "Job is closed". No vendor or Claude call is made on this path, so
    no spend is recorded.
- Paint edit: a user `paint_edit` message (payloads.PaintEdit; `paint_edit.py`) on a job with a
  result is consumed after any palette edit, then checked: `base_attempt`, when given, must
  still be the paint behind the result (`p2p_mcp_paint.edits.current_paint`: s10's
  `paint_state` input, else the accepted s09 or s08 paint), else "Paint changed since
  s09_paint/a2; reload" and nothing changes. A valid edit runs:
  - s09 as a new attempt with `stages/s09_paint_edit.PaintEditStage` (manifest data
    `paint_edit`: the payload, the message id, `base`). It loads the job the paint server's way,
    seeds a PaintSession in the attempt folder from the base paint (on a thin-slice job with no
    paint map, from the s07 baseline's connected same-filament patches, no SAM 3), applies the
    ops through the session (fill by region id; brush a face set into a new region; replace a
    slot; a filament by id or by slot; unowned catalog colors with `source: any`; unused
    filaments leave the palette), then previews: simulate_print, `final.3mf` written the way
    s10 writes it (despeckle, `printing.write_print`), p2p_slicer on that file (time, grams,
    purge, per-layer toolpaths in `preview/toolpaths.json` and `.bin`) and the eight proof
    views, paint and simulated print (`preview/proof/`). `before` is the base's own preview
    when it was an edit, else p2p_slicer on the result's 3MF, so both come from one slicer.
    Outputs: `paint_state.json`, `region_map.json`, `face_regions.npy`, `paint_preview.json`
    (role paint_preview), `final.3mf` (preview_3mf), toolpaths, `edit_proof_paint` and
    `edit_proof_print` images. Manifest data `progress` goes edit, preview, done.
  - s10 as a new attempt: the same paint through the slicer gate (Bambu Studio where
    installed; p2p_slicer and "Bambu gate pending" elsewhere) and a new `result`, DONE. A failed
    gate goes to REVIEW as for any s10; any other s10 failure pauses the job with a note.
  - A refused op (a region id that does not exist, a slot past the palette, no free AMS slot,
    an unowned color without `source: any`, a face outside the figure) fails the s09 attempt;
    the job goes back to the base attempt number and status with "Paint edit not applied: op 1
    (fill_region): no region 9...". Large strokes arrive as `faces_path` (input/paint/<name>.txt
    in bucket inputs; `JobStore.fetch_input` downloads it) and are recorded as inputs.
  - No vendor or Claude call; Meshy is switched off on this path.
- Despeckle: before every 3MF write, painted islands narrower than 2 x nozzle merge into the
  neighbour they share the longest boundary with (`p2p_sim.simulate.erode`). Manifest data
  `despeckle` has faces changed and islands before and after.
- Previews: `render_sheet.png` is `p2p_sim.render_sheet` of the written 3MF read back as placed
  on the plate (filament colors, flat products lying flat, negative parts left out);
  `preview.glb` has COLOR_0 exact per face (each color keeps its own copy of the vertices it
  touches, linear RGB, glTF frame in metres).
- Filament library: the job owner's Supabase `filaments` rows plus the shared ones (owner_id
  null, the studio AMS), else `filaments/library.json`. s08 snapshots it as `filaments.json`.
- Prime tower: `write_print` applies `p2p_geometry.production_slice.tower_overrides` to the
  footprint, so a wide nameplate does not cover Bambu's default tower spot.
- Size tier: a jobs row with `size_tier` and no `target_size_mm` gets the tier's size
  (contracts `SIZE_TIERS_MM`); `build_product` maps the size back to the tier.
- Slicer gate (s07 thin slice, s10): only an X2D project that passes `slice_check` ships.
  `JobResult` carries `slice_ok`, `print_time_s` and grams per slot with filament ids. A failed
  gate (or no retarget, library or slicer) keeps the file as `debug_x2d.3mf`, posts a
  `review_request` ("slicer gate failed: " plus the errors), sets `jobs.review_reason`, and moves
  the job to REVIEW, never DONE. A `review_decision` of `rerun_paint` (or a `resume`) starts a
  fresh s07 attempt that reuses the Meshy 3MF already downloaded (0 credits); `reject` fails the
  job; `approve` is refused with a note. `rerun --job <id> --stage s07_baseline` does the same
  from the CLI.
- Review kinds (`errors.NeedsReview.kind`, `errors.review_kind`): gate (the slicer gate failed;
  StageError code slicer_failed), estimate (only p2p_slicer checked the file; "Bambu gate
  pending") and fidelity (the paint or final-vs-concept check failed or confidence is low while
  the gate passed; code validation, `detail.review` fidelity). The review reason says which:
  s09 writes "Paint check failed; slicer gate passed: ..." or "Slicer gate failed at paint:
  ...", and the jobs row keeps the stage's confidence on a fidelity review (0 otherwise).
  `approve` on a fidelity review accepts the paused attempt as it is (decision
  `review.approved`, by user) and the job goes on through s10's gate; on gate and estimate
  reviews it is refused as before. live2 sombrero: the hat band failed the final check, the
  Bambu gate passed, and the review said the slicer gate failed, so approve was refused.
  Manifests written before `detail.review`: an s09 review with `slice_ok` true reads as
  fidelity.
- Intake: the new-job form's size and add-ons (nameplate text, keyring position) go into the
  intake request and the bank questions they cover (`covers:` in question_bank.yaml) are never
  asked. Size = answer > form > intake > product default; add-ons = defaults < intake < form <
  answers. Haiku answers compactly: bank question ids (questions are rebuilt from the bank), a
  short description per subject, and one line per color area (`s1|saddle on the back|black|m|#1A1A1A`),
  written to `color_regions.json` and appended to each subject's description. Output is capped
  (1,200 tokens), thinking off, effort low. Frenchie photos: 6.7 s and 428 output tokens (proof
  run 1: 130.8 s and 13,703).
- Concept prompts: each picked option adds its `prompt` meaning, never the question text. Every
  color region is listed as "keep every one". Meshy Image to Image has no negative-prompt field
  (the old `negative_prompt` on 3D tasks is deprecated and inert), so the no-base constraint is
  written per model: GPT Image gets a closing "Hard constraints, do not draw any of these" list;
  Nano Banana gets a positive description of empty space first and an "Avoid:" list last.
- Signed URLs: Meshy gets signed Storage URLs made at call time (key `<job_id>/<job path>`, so
  `input/photo_1.jpg` is `inputs/<job_id>/input/photo_1.jpg`); a data URI only without
  Supabase. Manifests, stage_runs and events the worker writes never hold signed URLs, tokens or
  data URIs (`util.manifest_safe`: Storage URLs become `storage:<bucket>/<key>`, other URLs lose
  their query). Meshy asset URLs live only in memory; a download after a restart gets fresh
  ones with one GET.
- Heartbeat: `workers` row on every `advance` and every `WORKER_HEARTBEAT_S` (15 s) in `serve`.
- Heavy work off the event loop (`offload.py`): the s06, s07, s08 and s10 builds
  (`run_heavy`, a picklable `WorkCtx` whose decisions replay on the stage) and the whole s09
  paint run (`s09_paint.paint_in_child`; its log lines go to `s09_paint/a<N>/paint.log` and on
  into the worker log as `paint` events) run in a spawn-started child process pool
  (`P2P_OFFLOAD_WORKERS`, 2 by default, ASSUMED; a fresh child every 4 tasks). A thread was not
  enough: manifold3d's CrossSection union holds the GIL (live2 sombrero s06: 8.9 min, the log
  silent for 5 min, Realtime timeouts, then a dropped Supabase keep-alive failed the Bronco's
  s05; a scratch probe on the sombrero mesh saw the loop stall up to 138 s beside a thread).
  `P2P_OFFLOAD=thread` keeps the old threads; the Settings dataclass defaults to thread, so
  tests keep their in-process slicer stub, and `Settings.load` defaults to process.
- run-stages: fresh attempts of the named stages in order, whatever the job's profile, with
  the Meshy client replaced by one that refuses every call (a stage that needs Meshy fails and
  says so). s07 reuses the Meshy 3MF an earlier attempt downloaded.

**Claude**: `CLAUDE_BACKEND=claude_code` (default) runs `claude -p` with `--system-prompt`,
`--setting-sources ""`, `--strict-mcp-config`, `--tools ""`, `--json-schema`, and
`ANTHROPIC_API_KEY`, `ANTHROPIC_API_KEY_TURNKEY`, `ANTHROPIC_AUTH_TOKEN` removed from the child
env. Photos go in as base64 image blocks over stream-json (`CLAUDE_VISION=read` uses file paths and
the Read tool instead). Output is validated against the contract model with one repair round.
`CLAUDE_BACKEND=api` uses the turnkey key and only when set; nothing ever falls back to it.
`MOCK_CLAUDE=1` forces MockClaude; `run-local` and `run-stages` use MockClaude unless
`--claude`. Every `claude -p` call runs with `MAX_THINKING_TOKENS=0`,
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` (intake 1,200, color spec 1,600, routing 600) and `--effort low`.

**Handoff to s06 onward (fidelity/back)**: s03 writes `concept.json` `form`, `addons_shown`,
`name_text`, and `addon_hex` / `text_hex` (the add-on and name colors the approved concept drew,
measured from its pixels). s01's `product.json` (role product_spec) and `job.form` carry the form
for `build_product`. `mesh.json` has `form`, `inputs` and `chainable`: a Creative Lab build
(`endpoint` keychain-build or magnet-build) is not an accepted `input_task_id` for Analyze or
Multi-Color, so s06 and s07 must send `model_url` for it (they send `input_task_id` today and a
flat job pauses at s06; the flat tests stop at s05).

**Webhooks**: production events arrive through the `meshy-webhook` edge function into
`meshy_events`; `serve` hears the insert over Realtime, finds the job by task id, confirms with
one GET and sets `processed_at`. For a local tunnel, `serve` also accepts
`POST /webhooks/meshy?token=$MESHY_WEBHOOK_TOKEN` (401 without the token).

**Serve**: Realtime subscriptions on `messages`, `jobs` and `meshy_events` kick the job they name;
on startup every active job is kicked. `P2P_ONLY_JOBS=<id,...>` limits a worker to those jobs
(the smoke run uses it so no real job is touched). In MESHY_MODE=test with Supabase, the fake
Meshy server only inserts `meshy_events` rows (like the edge function), so the Realtime path is
the one exercised; `FAKE_MESHY_WEBHOOKS=0` turns even that off.

**Logs**: JSON lines in `logs/worker.jsonl` (`P2P_LOG_DIR` to move it), rotated at 10 MB, 5
backups. Each line has `ts`, `level`, `event`, `msg`, and where known `job_id`, `stage`,
`attempt`, `duration_ms`, `meshy_task_id`. Events: `stage_start`, `stage_end`, `job_status`,
`gate_open`, `gate_reply`, `meshy_create`, `meshy_get`, `meshy_done`, `meshy_waiting`,
`claude_call`, `artifact_fetch`, `realtime_change`, `webhook_received`, `job_resumed`,
`advance_done`. Redacted before writing: data URIs, URL query strings (signed URLs, tokens),
bearer tokens, JWTs, key shapes (`msy_`, `sk-ant`, `sb_secret`), and the configured secret
values; httpx request logging is off. The worker writes `logs/.gitignore` (`*`) on first run.

**Traces**: every stage run binds its attempt (`trace.attempt(ctx)` in the orchestrator), and
each call writes one body to `<stage>/a<N>/trace/<seq>-<kind>.json` (uploaded to `artifacts`
like any output) and appends a small entry (model, purpose, time, credits, $, list $, summary,
image and file paths) to the manifest's `data.trace`. Claude bodies keep the system prompt,
user text, photo paths (never base64), raw reply, parsed JSON, tokens and the repair round;
Meshy bodies keep the prompt, reference images as job paths, the other params and the full task
object, and the downloaded files are linked to the task that made them; the slicer body keeps
the report numbers and the log (the `.slice.log` is also an output now, so it reaches Storage).
Bodies go through `util.manifest_safe`, the log redactor with the configured secrets, and a cut
at 200,000 characters. One entry per (kind, ref); a replay records nothing twice; a tracing
error is logged as `trace_failed` and never fails a stage. Data URIs from runs without Supabase
are matched back to job files by hash. `trace-backfill` builds reconstructed bodies for jobs run
before tracing (system prompt and raw reply marked not captured). With `--upload` it adds the
traces to the `stage_runs` rows Supabase already has (a PATCH of `manifest` only; status and
money columns untouched), uploads the trace files and slicer logs, and reports attempts that
exist only on disk instead of inserting them.

**Service** (not loaded): `worker/deploy/install.sh` fills the plist template with this checkout
and uv, writes `~/Library/LaunchAgents/com.photo2print.worker.plist`, and loads it
(`--no-load` to only write it). It runs `uv run python -m p2p_worker serve` on 127.0.0.1:8787,
restarts on crash, and sends stdout and stderr to `logs/worker.out.log` and `worker.err.log`.
`worker/deploy/uninstall.sh` stops and removes it.

## Resilience (robust/resilience)

What broke in live run 2 and what handles it now (docs/runs/live2):

- **Stage limits and the watchdog** (`lifecycle.py`, orchestrator `_supervise`): every stage run
  has an idle limit (no progress) and a total limit (active time), ASSUMED values in
  `STAGE_LIMITS` (s06 60/90 min, s09 60 min/6 h, the rest 15 to 45 min idle).
  Progress is a checkpoint, a heavy build starting or ending, or a paint log line. Waiting on
  a webhook in-process does not count, and the monotonic clock stops while the Mac sleeps. A
  run over a limit is cancelled (its child process or `claude -p` is killed) and fails as
  vendor_timeout. One `stage_running` log line per job per minute while a stage runs.
- **Retries with backoff** (`lifecycle.plan_retry`): a network blip, a timeout, an interrupted
  worker, a failed Claude call or a dead child process replays the same attempt: cached steps,
  Meshy tasks and gate replies stand. A failed Meshy task or a bug gets a fresh attempt that
  adopts the failed attempt's paid tasks (`Services.adopt_tasks`, default
  `lifecycle.default_adopt`). The wait between tries is `P2P_RETRY_BACKOFF_S` (10,60,300,900
  s, ASSUMED); serve kicks the job when it is due. Then the old retry limit, then PAUSED.
  Normal failures need no hand-run mark_stale.
- **Leases** (`data.run` on the manifest): a RUNNING attempt whose worker process is gone
  replays in place. The paint agent starts a fresh attempt instead: its conversation died
  with the worker. Dying at the same checkpoint 4 times in a row is a crash loop: a fresh
  attempt, then the retry limit. An attempt a live worker holds is left alone.
- **Child processes** (`offload.py`): one spawned process per heavy call, not a pool, so a
  timeout or a restart can kill it (with its process group). A child exits within 1 s if its
  worker dies.
- **Sleep** (`watchdog.py`): serve holds `caffeinate -i -w <pid>` while a job is queued,
  running, or asked the customer less than 30 min ago (`KEEP_AWAKE=0` turns it off). A wall
  clock jump over 30 s is a wake: new Realtime socket, and every active job and every job with
  an unconsumed reply or Meshy event is kicked. A job waiting on Meshy reads meshy_events and
  makes its backup status read if due (the 10-minute rule stands). A blocked loop logs
  `loop_stall`.
- **Supabase**: reads, PATCHes and upserts retry on a dropped connection or 429/5xx. Messages
  get worker-chosen ids and insert with ignore-duplicates. spend and meshy_events rows are
  looked up after a failed insert before inserting again. consumed_at and processed_at are
  only set where null. After every Realtime join, and at startup, serve reconciles from the
  tables (`store.pending_work`).
- **Replays post and pay nothing twice**: messages posted in a step or gate get ids from
  where they were posted; a gate saves the reply before consuming it; a review decision is
  applied before it is consumed, and one older than the current review request is ignored.
  A Claude result is saved with its call record before the spend row. A Meshy spend row is
  booked once per task, and the job's spend is its ledger. A create whose reply was lost
  keeps its pending marker, so the replay looks the task up before paying.
- **/health**: stages with active and idle time against their limits, child processes,
  retries waiting, Realtime, keep-awake, the last clock jump, event-loop lag; `ok` false with
  the problems.

**Chaos** (`tests/test_chaos.py`): one full job (all ten stages, the paint agent's scripted
driver, a fidelity review approved) on the fake Meshy and MockClaude, killed at 143 points:
stage boundaries, before each paid call, after each create and before its webhook, after each
webhook, every step, Claude call, gate and store write, mid geometry and the other heavy
builds, mid slice and mid paint. Meshy finishes in-flight tasks while the worker is down or
after the restart. The job must finish with the reference's outputs, the same paid POSTs and
each task booked once. `CHAOS_EACH=1` runs one job per kill point (hours on a busy machine).

**For robust/credit-guard**: its ledger reuses same-input tasks across attempts, which
covers most of `adopt_tasks`; set `Services.adopt_tasks` to its own function, or leave the
default. Its `meshy_create` (commit 04a2a65) still marks the intent abandoned and drops the
pending marker on every create error. A dropped connection or read timeout after Meshy made
the task then buys it again (the chaos lost-reply test). Keep the marker unless Meshy
answered with a 4xx, as `stages/base.py` does here. Both branches change `stages/base.py`,
`budget.py` and `clients/supabase.py`.

## Commands

```bash
uv run pytest worker/tests                       # 3 opt-in tests skipped
RUN_SLICER=1 uv run pytest worker/tests -m real_slicer    # the installed Bambu CLI gates a keychain
RUN_LIVE_CLAUDE=1 uv run pytest worker/tests -k live -s   # one real Haiku intake via Claude Code
RUN_SUPABASE=1 uv run pytest worker/tests -m supabase     # TEST job on the real project, cleaned up
uv run python -m p2p_worker smoke-supabase               # same, as one command with a report

# Thin slice on this machine against the fake Meshy server and MockClaude, gates auto-answered
uv run python -m p2p_worker run-local --photo a.jpg --photo b.jpg \
    --prompt "keychain of my dog and cat hugging" --product keychain --auto
#   --form flat|figure                 keychain or magnet form, as the order form sets it
#   --tweak "make the ears floppier"   scripted concept tweak (repeatable)
#   --mesh-action retexture           scripted 3D review action
#   --dev                              cheap cost profile
#   --claude                           real Claude via the Claude Code login
#   --supabase                         mirror rows and files to Supabase

uv run python -m p2p_worker status --job <id>
uv run python -m p2p_worker reply --job <id> --kind concept_pick \
    --payload '{"candidate_id": "r1c1", "approve": true}'
uv run python -m p2p_worker reply --job <id> --kind palette_edit \
    --payload '{"mode": "refit", "source": "any", "max_colors": 3}'   # then advance
# Repaint a finished job without the browser: posts paint_edit on the current paint, applies
# only that edit (no other stage runs), prints the notes, the p2p_slicer preview and the result.
uv run python -m p2p_worker paint-edit --job <id> \
    --ops '[{"op": "fill_region", "region_ids": [4], "filament_id": "generic-pla-white"},
            {"op": "brush_faces", "faces": "1200-1260,1302", "slot": 3}]' [--source any] [--note ...]
uv run python -m p2p_worker paint-edit --job <id> --ops '[]'   # a thin-slice job: map its paint
uv run python -m p2p_worker paint-preview --job <id>            # the newest paint edit's preview
uv run python -m p2p_worker advance --job <id>     # resume (after a crash, a reply, a webhook)
uv run python -m p2p_worker resume --job <id>      # clear a pause, then advance
uv run python -m p2p_worker rerun --job <id> --stage s07_baseline   # fresh attempt, 0 credits
uv run python -m p2p_worker run-local ... --profile full   # all ten stages
# fresh attempts of chosen stages, Meshy off (0 credits); --claude for Sonnet in s02
SAM3_BACKEND=local uv run --with torch==2.14.0 --with torchvision==0.29.0 \
    --with transformers==5.17.0 python -m p2p_worker run-stages --job <id> \
    --stages s07,s02,s08,s09,s10 --claude
uv run python -m p2p_worker serve --port 8787      # FastAPI + Realtime loop (Supabase if configured)

# Traces for jobs run before tracing: disk only, then (once) to Supabase. No Meshy or Claude call.
uv run python -m p2p_worker trace-backfill --job <id> [--job <id>] [--jobs-dir DIR] [--force]
uv run python -m p2p_worker trace-backfill --job <id> --upload
```

Environment (names only): `MESHY_MODE`, `MESHY_API_KEY`, `MESHY_BUILD_CREDIT_CAP`,
`MESHY_REUSE_ACROSS_JOBS`, `MESHY_BALANCE_CHECK`,
Environment (names only): `MESHY_MODE` (test, replay or live), `MESHY_API_KEY`, `MESHY_BUILD_CREDIT_CAP`,
`MESHY_WEBHOOK_TOKEN`, `MESHY_MISSED_WEBHOOK_S`, `MESHY_SWEEP_S`, `CLAUDE_BACKEND`, `CLAUDE_VISION`, `MOCK_CLAUDE`,
`ANTHROPIC_API_KEY_TURNKEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
`BUILD_SPEND_CEILING_USD`, `JOB_CAP_USD`, `JOB_CAP_CREDITS`, `BASELINE_STYLES`,
`P2P_COST_PROFILE`, `P2P_JOBS_DIR`, `P2P_LOG_DIR`, `P2P_ONLY_JOBS`, `FAKE_MESHY_WEBHOOKS`,
`VENDOR_WAIT_S`, `WORKER_ID`, `WORKER_HEARTBEAT_S`, `PAINT_AGENT` (stub or agent),
`SAM3_BACKEND` (mock, local, runpod, off), `P2P_OFFLOAD` (process or thread),
`P2P_OFFLOAD_WORKERS`, `P2P_REPLAY_STORE` and `P2P_REPLAY_JOBS` (MESHY_MODE=replay).
`P2P_OFFLOAD_WORKERS`, `P2P_RETRY_BACKOFF_S`, `P2P_STAGE_LIMIT_SCALE`, `P2P_PROGRESS_LOG_S`,
`KEEP_AWAKE`, `KEEP_AWAKE_USER_S`.

## M3 proof (0 credits, copies of the two real jobs)

`run-stages --stages s07,s02,s08,s09,s10 --claude` (SAM3_BACKEND=local), then s07 to s10 again
with the final code. Meshy was off; no credits. The real Bambu Studio CLI gated every file.

| Job | Before (shipped) | After (s10, stub paint) |
|---|---|---|
| Frenchie keychain 819cc491 | black, brown, white; gray went to brown (dE00 26.9); 1,576 patches under a 0.8 mm disk (1,217 brown); 38.9 min, 9.5 g | black and white; gray to black (dE00 33.3) with "gray PLA" suggested (Bambu Lab PLA Basic Gray, dE00 9.2); 0 patches; gate pass, 21.9 min, 4.8 g |
| Bronco nameplate 1785a480 | 215 patches under a 0.8 mm disk; 9 h 22 min, 127.2 g | 0 patches; gate pass, 9 h 17 min, 126.3 g, black, brown, white |

Stage times on the Frenchie (1.1M faces): s07 68 s with the gate, s02 36 s (Sonnet 5.3 s,
SAM 3 load most of the rest), s08 26 s, s10 36 s. Intake on the Frenchie photos: 6.7 s.

## Paint edit proof (0 credits, a copy of the Bronco nameplate)

`paint-edit` on a scratch copy of job 1785a480 (thin slice, 532,360 faces, 267 layers), Meshy
off, the real Bambu Studio CLI as s10's gate. SOURCE: the attempts' paint_preview.json and
slice_report.json.

| Edit | s09 (edit + preview) | p2p_slicer before, after | s10 (Bambu gate) |
|---|---|---|---|
| `--ops '[]'`: map the paint (245 patches from the s07 baseline) | 44.7 s | 9 h 13 min, 127.1 g, purge 99.4 g; 9 h 09 min, 126.3 g, purge 98.7 g | 34.0 s, pass, 9 h 17 min, 126.3 g |
| fill a brown patch white, brush 4,001 faces black (65,117 faces change) | 33.0 s (sim 12.3, write 4.4, slice 10.8, proof views 2.0) | 9 h 09 min, 126.3 g, purge 98.7 g; 8 h 16 min, 114.6 g, purge 87.5 g | 31.5 s, pass, 8 h 24 min |

p2p_slicer against the Bambu gate on the same file: time -1.8 % (29,735 s against 30,269 s),
grams within 0.1 g on the mapped paint. The toolpath preview is 1.2 MB for the 267 layers.

## Known gaps

- No live Meshy call has been made from the worker. MESHY_MODE=test is the fake server built from
  the docs and the four recorded fixtures (image to image, image to 3D lite, analyze, multi-color).
  Multi-Image to 3D and Retexture have no recording; the fake reuses the Image to 3D shape.
- `serve` on the real Supabase project has run only through `smoke-supabase` (one keychain job,
  33 s, every check passed, all rows and objects removed). No real web app session has driven it.
- The fake Meshy server now serves a watertight dog figure (manifold3d, about 5,700 triangles)
  and a matching Meshy-style Multi-Color 3MF, so the real `build_product` and `production_3mf`
  run in every pipeline test. Tests stub `slice_check` (FakeSlicer) except the opt-in
  real_slicer test. Meshy Repair (10 cr) is not called.
- Tweak routes other than rerender and follow-up (retexture, regenerate, paint edit, geometry op)
  are recorded as decisions only. After 5 mesh review rounds the stage pauses.
- The launchd service has not been loaded or started here (by instruction).
- In run-local with MESHY_MODE=live and no Supabase, webhooks cannot reach the local store; the
  job advances only through the one-GET-per-resume recovery. Use `--supabase` or `serve` with a
  tunnel for live runs.
- A crash between a Meshy create and the manifest write is recovered by listing recent tasks and
  matching on type, time, model and prompt; two indistinguishable matches pause the stage.
- HEIC photos are not converted (Pillow cannot open them); the web app should upload JPEG or PNG.
- SupabaseStore uses a synchronous HTTP client inside the event loop; fine for one worker, not
  for many concurrent jobs. Artifact uploads (a 40 MB GLB) still block the loop for seconds.
- Paint edits (`s09_paint_edit.py`) and the s02 crops, s03 paste-back and s04 view normalize
  still run in threads; their work is lighter, but native code there can hold the GIL too.
- s06 used to spend most of its time in `p2p_geometry.meshio.projected_silhouette` (a
  CrossSection union of every triangle: 83 s per call, 6 calls on the sombrero's 1.29M faces).
  It now unions the triangles' net boundary loops, the same region: under 1 s a call, and s06
  on replays of the stored jobs takes 19 s on the sombrero (8.9 min live) and 4 s on the
  Bronco (17.9 min live) with identical s06 files (SOURCE: evals/reports/replay-from-s06.md).
- Prompt caching on Haiku 4.5 needs a 4,096-token prefix; the intake system prompt is shorter, so
  it does not cache.
- s04 was probed live once, with nano-banana (3 credits): three separate RGBA views,
  three-quarter, front, back, billed once (docs/research/meshy-iteration.md section 3). The
  pipeline's models (nano-banana-pro, gpt-image-2) have not returned views live yet.
- The concept and view checks ran live once each through the Claude Code login (Haiku, 0
  credits, about 7 to 9 s and a list price of about $0.015 per check, SOURCE: that run): on the
  Bronco concept with a code-drawn plate it read "Bronco" exactly, failed a plate reading
  "Bronko", and flagged the missing bow tie (the request asked for one; the old concept has
  none); on the probe's three Frenchie views it passed all three and called the three-quarter
  view "front". One run is not an accuracy measure. Paste-back is tested on the fake server's
  drawn figures and on synthetic edits of the real Frenchie concept, not on a live edit.
- Flat charms: the Creative Lab Keychain and Fridge Magnet paths run to s05 on the fake server
  only; no live prototype or build yet. The 2.0 mm base is ASSUMED. s06 and s07 are the back
  half (see the handoff).
- s05 does not yet run the free Claude check of the four mesh thumbnails before the review
  (meshy-iteration.md section 4, item 4). Retexture with `multiview_image_urls` (meshy-7) is
  untested live.
- s08 regions are same-color patches of Meshy's paint (`source: meshy_color`), labeled
  `s1.body` and `s1.marking_<color>_<n>`; SAM 3 part labels on the mesh are the paint agent's.
- The preview GLB is full resolution (the Frenchie is 1.1M faces, about 25 MB); no decimation
  library is installed.
- `run-stages` on a real job was run on copies of the job folders; running it in place writes
  new attempts into the real folder (and, with `--supabase`, the job row).
- Palette edits: an upstream change later (a rerun of s07, a new photo) starts a normal s08
  attempt, which drops the edit. A worker crash in the middle of an edit resumes s08 with the
  edit, but the s09 that follows runs the configured paint switch, not the stub. On a
  thin-slice job, a `review_decision` rerun after a failed edit gate reruns the thin-slice
  profile, not s10. A refit's catalog colors use the default density (1.24 g/cm3).
- Paint edits: like palette edits, an upstream change later (a rerun of s07 or s08, or a
  review `rerun_paint`) starts a normal s09 attempt, which drops the edits. A worker stopped
  inside an edit's s09 finishes the edit on the job's next advance
  (`paint_edit.resume_open_paint_edit`; finished steps are cached; the s09 switch also runs
  the edit runner for an attempt with manifest data `paint_edit`), then s10. A worker stopped
  inside that s10 leaves it to the profile's own loop on a full job and to a new edit on a
  thin-slice one. The preview renders the proof views once and s10 renders the result's proof
  set again (about 2 s on the Bronco).
  p2p_slicer leaves out Bambu's tree supports, so a supported print's preview reads short on
  time and grams until s10's gate numbers arrive.

- The credit ledger, cross-job reuse and the balance pre-flight have run only against the fake
  Meshy server. Live asset URLs carry the task id in their path
  (`assets.meshy.ai/<account>/tasks/<task_id>/output/model.glb`, SOURCE: the live2 manifests),
  which is what the reuse keys downloaded files on. The balance is account-wide, so web-app use
  lowers it too; the build cap still counts only the spend table.
- A task adopted while still running shows its credits in the adopting attempt's trace, so the
  admin trace view can count it twice (as with the older transient adoption); the spend table
  and `credits.json` count it once.
- `jobs/_worker/meshy_cache.jsonl` and `paid_calls.jsonl` are local files: two workers on two
  machines would not see each other's reuses (the spend table's intent rows would still show
  both).

## Contract changes made

Made on branch fidelity/front for the front fidelity work (s01 to s05). Schemas regenerated
(`uv run python -m p2p_contracts.export`) and mirrored into apps/web (`npm run gen:contracts`).

1. **job.py `ProductForm`** (`flat`, `figure`), `FORM_CHOICE_PRODUCTS` (keychain, magnet),
   `FORM_DEFAULTS` (keychain figure, magnet flat; ASSUMED, used only when unanswered) and
   `product_form()` (every other product is `figure`).
2. **job.py `CustomAsk`**: `kind` (prop, outfit, pose, expression, text, other), `text`,
   `subject_id`, `source` (request, answer, form), `buildable`.
3. **JobRecord**: `form: ProductForm | None` and `custom_asks: list[CustomAsk]`, set by s01.
   Migration `supabase/migrations/20260924000011_product_form.sql` adds `jobs.form` and
   `jobs.custom_asks` (not applied). The worker writes them in a separate PATCH and stops for
   the run if the columns are missing; `ingest_job` reads a `form` the new-job form set.
4. **job.py `MAX_CONCEPT_FIXES = 4`**: paid concept regenerations per job (ASSUMED).
5. **payloads.IntakeResult**: `form` (what the request or order form set, so the question is not
   asked) and `custom_asks`.
6. **payloads.ProductSpec** (new, s01 `product.json`, role product_spec; schema
   `product_spec.schema.json`): product type, form, size, add-ons, custom asks, picks, and who
   decided form and size.
7. **payloads.CheckItem, ConceptCheck** (new): per-item verdicts (`kind` part, text, ask,
   markings, no_extra_base, figure_identical, view; `on` figure, product or view; `by` model or
   code), overall `passed`, `text_read`, `figure_identical`.
8. **payloads.ConceptCandidate**: `image` is now the product concept the customer approves;
   new `figure_image` (the figure alone, the 3D input), `form`, `product_task_id` (the add-on
   edit, or the Creative Lab prototype s05 builds), `product_source` (figure, edit, code,
   prototype) and `check`. `meshy_task_id` stays the figure task (tweaks and s04/s05 chain
   from it). Old candidates validate unchanged.
9. **payloads.ConceptsPayload**: `form`.
10. **payloads.View, ViewSet** (new, s04 `views.json`; schema `view_set.schema.json`): ordered
    views for s05 (approved figure first), `skipped` and `reason`, dropped views, check items.
11. **stages.py `PROFILE_THIN_SLICE`** now includes s04 (a 3D figure is built from real views,
    meshy-iteration.md section 3). apps/web `lib/contracts.ts` PROFILES mirrors it.
12. No new message kinds: checks ride on `concepts`; unbuildable asks are `note`s.

Made on branch fidelity/back (s06 to s10, geometry, paint, sim), kept small and separate:

13. **regions.PROOF_VIEWS** (and `p2p_contracts.PROOF_VIEWS`): the eight proof cameras (the six
    contract views plus `left_3q`, azimuth -125, and `back_3q`, azimuth 125, both 20 degrees up).
14. **payloads.ProofImage** (kind `model` | `paint` | `print`, view, path) and
    **JobResult.proof_images** (default empty): s10 fills it with the shipped product from all
    eight views.
15. No other contract edits: `SimParams.nozzles` already existed and the simulator now uses it.

Made on branch studio/paint-editor (the studio paint editor). Schemas regenerated, mirrored into
apps/web (`npm run gen:contracts`, `lib/contracts.ts` MESSAGE_ROLES).

16. **Message kind `paint_edit`** (user; `payloads.PaintEdit`): `ops` (at most 500, applied in
    order; empty previews the base paint, or maps the paint of a job without one),
    `base_attempt` (the attempt folder the ops were drawn on, `s0[89]_<stage>/a<N>`; refused
    when the paint moved on), `source` (owned | any), `note`. Migration
    `supabase/migrations/20260924000012_paint_edit_kind.sql` adds the kind to
    `messages_kind_check` (not applied).
17. **payloads.PaintOp**: `op` fill_region (`region_ids`), brush_faces (`faces`: figure face ids
    as ranges "0-9,15,20-31", up to `FACES_INLINE_MAX` = 200,000 characters, else `faces_path`:
    a job-relative file with the same ranges, input/paint/<name>.txt in bucket inputs; optional
    `label` for the new region) or replace_slot (`from_slot`); the filament by `filament_id` or
    `slot` (exactly one; a model validator checks the combinations).
18. **payloads.PaintPreview** (s09 paint edit output `paint_preview.json`, role paint_preview;
    schema `paint_preview.schema.json`) with **PrintNumbers** (slicer, time, grams, purge,
    changes, layers, per-filament SliceFilament) before and after, **SimSummary**, the proof
    images, `toolpaths` (the version 1 JSON index plus binary, documented on the model),
    warnings and step timings.
19. **mcp_tools**: `OpenJobIn.base` ("current" or an attempt folder: a new attempt seeded from a
    finished paint), tools `apply_paint_ops` (PaintOpsIn: ops, source; StateOut) and
    `preview_print` (PreviewPrintIn: toolpaths, proof; PaintPreview).

## Product spec in s06 (fidelity back half)

s06 resolves what geometry and paint need about the approved product once and writes it as
`product_resolved.json` (with where each value came from); the paint stage reads the palette
source from it. Form: `JobRecord.form` (set by s01), then s01's `product.json`, then the older
defensive reads (job.json or input/request.json `form`, the answers' `form` question). Plate
shape: the nameplate add-on's `shape` (the `plate_shape` answer), then `product.json` picks.
Add-on colors have no intake field yet: `product_spec.read_spec` reads `addon_colors`
{role: value} or `<role>_color` (`name_color` for the text) from job.json or
input/request.json, or answers with those question ids (hex, filament id or a plain color
name); TODO(front): a plate color and name color question. Palette source as before (worker
contract request 6). With no form, geometry keeps the full 3D figure (flat only for magnets).
s07 and s08 give an add-on color the AMS does not hold (source any) its own palette slot.

## Contract change requests

Round 1 requests (pause_reason and result on JobRecord, pinned sibling signatures, a `resume`
kind, `tweaks_exhausted`) landed and are used. Open:

1. **SpendEvent.usd_list and SpendEvent.billing** in contracts/job.py. The columns exist; the
   worker passes both next to the event (`JobStore.record_spend(event, usd_list=, billing=)`).
2. **apps/web `inputStorageKey`** still strips `input/` (`inputs/<job_id>/photo_1.jpg`). The
   worker now uses the contract key `<job_id>/input/photo_1.jpg` and still reads the old one;
   the web app should upload to the contract key.
3. **GeometryResult mesh paths.** `build_product` only receives `out_dir`, so it cannot write
   job-relative `FileRef.path` values. The worker rewrites absolute or out_dir-relative paths;
   either pass `job_dir` too or state in geometry.py that paths are relative to `out_dir`.
4. **MeshyTask.webhook_events.** A list of event ids per task would make "one GET per event"
   auditable from the manifest; today it lives in `data.meshy_gets` and the logs.
5. **Root .gitignore: `logs/`.** The worker writes `logs/.gitignore` at runtime; a root entry
   is cleaner (outside this workstream's paths).
6. **Job-level palette source.** "The job asks for source any" has no field: add
   `palette_source: Literal["owned", "any"] = "owned"` to JobRecord and a `palette_source`
   column on `jobs` (and the new-job form). Meanwhile s08 reads `palette_source` from
   `input/request.json`, then from the jobs row when the column exists.
7. **CheckKind `answer` and `extra`** (payloads.CheckKind). Intake picks (`answer.<question
   id>`, `answer.form`) ride on kind `ask`, and the nothing-extra items (`no_extras`,
   `no_extras.product`) on kind `no_extra_base`; the ids tell them apart meanwhile. Own kinds
   would let the studio show "Pose: sitting" apart from the customer's typed asks.
8. **StageError code `needs_review` (or `fidelity_failed`)** in contracts/manifest.py. A
   fidelity review uses code `validation` with `detail.review` fidelity meanwhile; gate and
   estimate reviews keep `slicer_failed`.
9. **JobResult.credits** (payloads): the job's Meshy numbers next to the file, e.g.
   `CreditSummary(paid, reused_calls, saved, refunded, refused_calls)`. Meanwhile the result
   message body carries "Meshy credits: 75 paid, 2 reused (saved 21)", the result stage's
   manifest data `credits` the totals, and `credits.json` (role credit_report) the full report.
10. **MeshyTask.reuse** (manifest): `how` (reused | adopted) and the payer (job, stage,
    attempt, key) on a task an attempt did not pay for. Meanwhile the copy carries credits 0
    and `(reused)` in its purpose, and `data.meshy_reused[task_id]` has the rest.
11. **spend kind `intent:<endpoint>`**: 0-credit rows written before every paid create. They
    add nothing to any sum, but a reader that lists Meshy spend rows (the cost meter's event
    count, `costs/src/p2p_costs/ingest.py` notes) should skip kinds starting with `intent:`.

## Credit guard (robust/credit-guard)

What live run 2 would have spent under these rules (ASSUMED, from the job manifests and
reports in docs/runs/live2; SOURCE for what was spent):

| Job | Spent (SOURCE: spend table) | Would save | Why |
|---|---|---|---|
| Bronco eb8f1de4 | 159 | 60 | Six plate edits (4 x nano-banana-pro 9, 2 x gpt-image-2 12) are not bought: the first code composites fail only the figure's own pedestal (as the ones at n=2 did), the figure fix runs (21), and the new figures' composites pass (as the ones at n=4 did, and one of them is what the customer approved). The retry after the network blip adopts the 35-credit model by input hash whatever the failure, instead of relying on the build cap to stop the second purchase. |
| Sombrero 77032daf | 96 | 12 | Two round-base edits (nano-banana-2 6 each) are not bought if the code-drawn base passes the check (r1c1's paid edit came back without a base at all). The s04 retry changed its prompt, so it is not a repeat. |
| Catfish f67c6a54 | 73 | 0 | Flat charm: the Creative Lab prototypes are the product and the loop was already drawn in code; no call repeated its inputs. |
| Total | 328 | 72 (22 %) | |

Tests: `worker/tests/test_credit_guard.py` (fake Meshy, POSTs counted).

## Differences from PLAN.md found while building

- Retexture does not accept a Multi-Image to 3D task id, so s05 retextures by `model_url`
  ([retexture docs](https://docs.meshy.ai/api/retexture)).
- `texture_image_urls` cannot be combined with `texture_prompt`
  ([multi-image-to-3d docs](https://docs.meshy.ai/api/multi-image-to-3d)); a text tweak on the 3D
  model becomes a Retexture `text_style_prompt`.
- The stage 3/4 prompt rule "on a shared base" is replaced: bases, plates, loops and text are exact
  geometry now, so the concept asks for one connected figure with a flat underside and no base.
- Stage 7 runs one Multi-Color style (cartoon) by default, not both (orchestrator instruction).
- Stage 8 builds its first-pass regions from Meshy's Multi-Color paint, not from SAM 3 labels
  (stage 7's labels belong to the paint agent's tools).
- More in `docs/research/meshy.md`, section "Differences from PLAN.md".
