# apps/web (layerling)

Next.js 16 (App Router), TypeScript, Tailwind 4, three.js. The public brand is **layerling**;
"photo2print" stays in the repo, package names and env. Design direction B, "Paper" (Fraunces
over Instrument Sans, ink on paper, brick accent, layer-line texture, pill buttons). Every color,
font and radius is a CSS variable in `app/globals.css`.

## Interface

### Routes

| Route | Who | What |
|---|---|---|
| `/` | public | Landing: hero with the real beagle photo, concept and filament render; what we make with `from $X` prices; how it works; order form and studio code box |
| `/order`, `/order/thanks` | public | Order form on its own page; confirmation with a short reference |
| `/access` | public | Studio code. Sets the signed session cookie, then goes to `next` (studio paths only) |
| `/login` | public | Jackson's Supabase email and password |
| `/jobs`, `/jobs/new`, `/jobs/[id]` | studio | Jobs list (with the worker state), new job, job page (3D viewer, image strip, decision or result column) |
| `/review`, `/filaments`, `/admin/orders` | studio | Review queue, filaments, orders (status, Start job) |
| `/admin/traces` | studio | Trace jobs: every job with status, stage, credits, $, stage time; search, status and product filters |
| `/admin/traces/[id]` | studio | One job's trace: request, then one card per stage attempt with its calls (prompt, system, refs, reply or result, files, raw body), outputs, slicer numbers, decisions, error, raw manifest |
| `/admin/traces/calls` | studio | Spend strip (spent $, list $, credits, calls, by model; 7 d, 30 d, all) and every model call |

`proxy.ts` (Next 16's middleware) gates the studio pages: a valid access cookie or a Supabase
session, else redirect to `/access?next=...`. `NEXT_PUBLIC_DATA=mock` skips the gate outside
production. API routes check the same things themselves.

### Data adapters (`lib/data/`)

| Adapter | When | How |
|---|---|---|
| `mock` | `NEXT_PUBLIC_DATA=mock` | In-memory tables, simulated worker, seeded jobs (including a BISCUIT keychain and a PEPPER nameplate) |
| `supabase` | A Supabase session exists | supabase-js with cookie sessions (`@supabase/ssr`), RLS, Realtime |
| `server` | An access-code session | Calls `/api/studio/*`, which use the service role server-side; polls every 2 s |

The browser asks `GET /api/access` which session it has; the code never reaches the browser.
Jobs created in a code session have `owner_id = null` and `access = 'code'`.

### API routes (all log JSON lines with a request id; secrets and bytes are redacted)

| Route | Does |
|---|---|
| `POST /api/orders/start` | Validate fields and photo metadata, rate-limit per IP (5 per 10 min, ASSUMED), return an order id, signed upload URLs for `orders/<order_id>/photo_<n>.<ext>` and a signed ticket |
| `POST /api/orders/finish` | Check the ticket and the uploaded photos (count, 15 MB), insert `public.orders` with the service role, then `notifyOrder` after the response |
| `GET/POST/DELETE /api/access` | Session status; exchange the code for the cookie (constant-time compare, 5 wrong tries per 15 min, ASSUMED); sign out |
| `GET /api/admin/orders`, `PATCH /api/admin/orders/[id]`, `POST /api/admin/orders/[id]/job` | List with signed photo URLs; change status; start a job (copy photos to `inputs/<job_id>/input/photo_<n>`, product, size and add-ons from `PRODUCT_DEFAULTS` plus the name text) |
| `/api/studio/jobs`, `jobs/start`, `jobs/finish`, `jobs/[id]`, `jobs/[id]/messages`, `messages`, `spend`, `filaments`, `filaments/[id]/swatch`, `workers`, `file` | The server adapter. Photos go to Storage through signed URLs; `file` redirects to a 1 h signed URL |
| `GET /api/admin/traces` | Trace job list rows |
| `GET /api/admin/traces/[id]` | One job's trace: request, attempts with calls, outputs, decisions (manifests reduced) |
| `GET /api/admin/traces/[id]/raw?stage=&attempt=` | One full stage manifest |
| `GET /api/admin/traces/[id]/body?path=` | One trace body (`s03_concept/a1/trace/01-meshy.json`) |
| `GET /api/admin/traces/[id]/file?bucket=inputs\|artifacts&path=` | 302 to a 5 minute signed URL (`storage:` refs accepted); the fixture file in mock mode |
| `GET /api/admin/traces/calls?days=&vendor=&q=` | Model calls across jobs and the spend summary (the summary covers the day range) |
| `GET /api/studio/jobs/[id]/download?name=` | The 3MF as `<name>-<product>.3mf` (name from `?name`, else the name text, else the title). Supabase: 302 to a 10 min signed URL made with Storage's `download` option, so Storage sends `Content-Disposition: attachment; filename=...` (checked on the real project). Memory store: streams the bytes with that header. Falls back to the latest `result` message when `jobs.result` is not written yet |
| `POST /api/studio/jobs/[id]/uploads` | A signed upload URL into bucket `inputs` for a large paint stroke (`input/paint/<name>.txt` only, 20 MB cap); the paint editor names it in `paint_edit` `faces_path` |

The download redirects instead of streaming because Vercel caps function responses at 4.5 MB
and the M1 3MF was 10.8 MB; a plain cross-origin signed URL loses the `download` attribute, the
Storage header does not.

### Job page

- **Layout**: a large 3D viewer on the left with a Print and Model toggle when both exist, the size
  as one line ("W 21.4 · H 45 · T 8 mm", from s06's bbox, else the target), and a small strip of
  images (render sheet, approved concept, photos) that replace the viewer until a 3D view is picked
  again. The right column holds the one decision waiting (questions, concept, 3D model, review) or,
  once a result exists, the result. No stage list, keycaps or keyboard shortcuts.
- **Result column**: "Ready to print" only when `result.slice_ok` is true. Otherwise "Needs
  review" with `jobs.review_reason`, else "Failed the slicer gate" or "Not checked by the slicer
  gate". Print time, total grams and the slicer gate, then one row per slot with swatch and grams,
  the cost meter, and the actions at the bottom (Apply and Reset while colors are edited,
  Download 3MF).
- **Filament picker**: each row opens a picker with owned spools first (AMS slot order), then
  catalog colors (`owned = false`) grouped by line ("Bambu PLA Basic"), with search, a swatch and a
  "Not owned" tag. Colors can be added (up to `settings.ams_slots`, default 5) and removed. Apply
  sends `palette_edit`: `slots` are the filament ids in slot order; `mode` is `recolor` when the
  slots are the original ones in order (only swaps), `refit` when a color was added or removed;
  `source` is `any` when a chosen filament is not owned. The column shows "Updating colors" until a
  `result` newer than the edit arrives. The mock worker answers with a new s08 and s10 attempt and a
  result in the chosen filaments.
- **Spool suggestions**: contract `spool_suggestion` messages (and older notes that name a spool)
  show as "Add Gray PLA". One with a `filament_id` has an Apply button: it adds the color when a
  slot is free (refit), else swaps it into the slot nearest the target color (recolor).
- **Filament colors**: a preview counts as painted when it has per-vertex COLOR_0 or untextured
  materials in two or more colors (one primitive per filament). If it has neither, PRINT is
  dropped, the header shows "Texture only" and the sheet opens instead.
- **Filament rows**: names and colors come from the filaments table. An id the table does not
  have still reads as a color name with a nominal swatch ("generic-pla-gray" is "Gray"), never the
  raw id. The Supabase session also reads the shared spools and catalog rows (`owner_id` null,
  hidden by RLS) through `/api/studio/filaments`.
- **New job**: keychains and magnets get a Form choice (Ask, Flat charm or Flat relief, 3D
  figure). It follows the request's own words ("3d", "flat") until picked; Ask leaves it to the
  intake question. The pick goes to `jobs.form` (migration 0011; without the column the insert
  is retried without it and intake asks). Asks (props, outfits, poses, names) found in the request
  show as chips under it: `lib/intent.ts` ports the worker's `intent.py` parser and is tested on
  the golden prompts.
- **Form and asks**: the header says Flat charm or 3D figure once anything decided it (the row,
  the concept round, the form answer, the request) and lists the custom asks.
- **Proof views**: at the mesh gate and on the result the main view opens on Views: the approved
  concept (2 x 2 tiles) next to the eight contract `PROOF_VIEWS` (front, front 3/4, left 3/4,
  left, right, back, back 3/4, top), one kind at a time (Print, Paint, Model). Sources, first
  found wins (`lib/fidelity.ts proofSets`): `result.proof_images` (s10), stage outputs with role
  `proof_print`, `proof_paint`, `proof_model` (s10 `proof/`, s09 `proof/final/`), else the page
  renders the GLB itself from the same eight cameras in the browser (`components/job/glbViews.ts`,
  three.js): the Meshy model waiting at the mesh gate, the thin slice preview as Paint. A view
  nothing rendered keeps its tile ("No render"). 3D switches to the viewer.
- **Concept pick**: the candidates take the viewer's place: the original photo next to both
  candidates (at least 420 px each on desktop). Each candidate is the product concept (`image`:
  the figure with its loop, plate and name, or the flat charm) with the figure alone
  (`figure_image`, the 3D input) as a small inset, and the concept check above it in a few words
  per item ("Name checked", "Loop missing", "Markings off"; failures first, the item and the
  model's note on hover). The band sizes itself by its own width (container queries): photo, A and B in one
  row from 1,070 px, else the photo on a short row above A and B, so nothing is cut off at 1,391
  px. Skeletons hold the space until each image loads or while a round renders.
- **Warnings (n)**: s06 `warnings.json` (role `geometry_warnings`), GeometryResult warnings,
  Meshy's counts from `printability.json` (role `printability`), e.g. "Printability warning:
  164,801 degenerate faces", which replaces the bare "Printability: warning", then manifest
  `data.warnings`, skipped `geometry.build_product` decisions and stage errors.
- **Paint editor (Edit)**: once a result exists the stage's display switch has Views, 3D and
  Edit. Edit loads the paint behind the result (`lib/paintEdit.ts currentPaint`: s10's
  `paint_state` input, else the newest succeeded s09, then s08, paint state): paint_state.json,
  the region map it names, face_regions.npy (`lib/meshFiles.ts readNpyInt32`), the figure PLY
  the map indexes (`readPly`, face order kept) and s06's add-on parts. three.js draws every
  face in its filament (`components/job/paintScene.ts`, unindexed triangles, GPU face picking
  into a 1 x 1 target). Tools: Brush (faces within Size mm of the point under the cursor that
  face the camera, `lib/brush.ts` FaceGrid; one stroke is one new region), Fill (the region
  under the cursor), Pick (the face's filament becomes the active slot, then the tool goes
  back). Dragging over the model paints; dragging the background orbits. Slots: the paint's
  palette as the worker will leave it (a filament the edits no longer use drops out), each
  with Change (the catalog picker: replace_slot) and Add color (up to the AMS slots). Undo
  pops the last edit, Reset drops them all; edits survive switching displays. Send posts the
  contract `paint_edit` with `base_attempt` (the paint the edits were drawn on) and `source:
  any` when a color is not owned; a stroke whose face ranges run past 200,000 characters is
  uploaded first (`DataClient.uploadInput`, faces_path). While the worker runs, the editor, the
  preview panel and the result column show its phase (Queued, Painting, Slicing, Gate check;
  `editStatus` reads the s09 edit run's `progress` and the s10 run); tools and Download wait.
  When the new paint lands the editor reloads it. A refused edit shows the worker's note and
  keeps the edits. A job with no paint map (thin slice) shows Map paint, which sends an empty
  `paint_edit`.
- **Paint preview**: under the stage once a paint edit ran: time, filament and purge with the
  change and "Was" (both p2p_slicer), the simulated print (or the paint) from the eight views,
  and the toolpaths top down one layer at a time with a slider (`lib/toolpaths.ts`: the
  version 1 files, framed on the print without the prime tower, the layer below faint; the
  layer's filaments, changes and seconds under it).
- **Paint panel (s09)**: iteration count out of 8, confidence, each checkpoint as the ideal paint
  next to the simulated print with its note, and the contracts Checklist with pass, fail or n/a
  per item. It shows on the job page once s09 has run, and under each job in the review queue.
  The queue lists jobs in review plus any other live job with confidence under 70%, lowest first.

Photos never pass through a function body (Vercel caps request bodies at 4.5 MB): the browser
PUTs them straight to Storage with the signed URL, and a stateless HMAC ticket carries the
validated form between the two calls.

**Traces**: the pages read `lib/server/traces.ts`, which uses the service role when it is set
(selects, Storage download and sign only; cross-job queries select `manifest->model_calls`,
`manifest->meshy_tasks` and `manifest->data->trace`, never whole manifests) and otherwise the
two real jobs in `lib/traces/fixtures/` (mock mode, development, tests; exported by
`scripts/trace_fixtures.py`, never under `public/`). Calls for an attempt are its
`data.trace` entries plus manifest calls no entry covers, marked derived (jobs before tracing,
pending Meshy tasks). Every JSON response goes through `lib/traces/redact.ts` (the worker's
rules: no keys, JWTs, bearer tokens, signed URLs, data URIs or configured secrets). Meshy $ is
credits x $0.02 (ASSUMED). Screenshots: `docs/plans/telemetry_shots/`; studio pages in `docs/design/shots/studio/`.

**Access cookie**: `p2p_access=v1.<exp>.<nonce>.<hmac>`, HMAC-SHA256 with `ACCESS_COOKIE_SECRET`,
30 days, `HttpOnly; Secure; SameSite=Lax` (Secure is dropped only on plain-http localhost).
Verify decodes the signature, requires exactly 32 bytes in canonical base64url, and compares the
raw bytes in constant time. Upload tickets use the same check.

**Resume**: a paused job (budget cap, retry limit) shows Resume in the decision rail with optional
new caps; it sends the contract `resume` message.

**Order email**: AgentMail `POST /v0/inboxes/{AGENTMAIL_INBOX_ID}/messages/send` with Bearer
auth, subject `layerling order: <product> "<name>" for <customer>`, reply-to the customer.
Without a key the order is stored with `notify_error = 'no provider'`. Addresses at reserved test
domains (example.com, .test) never send; they record `notify_error = 'test order'`. Email never
blocks an order.

**Prices**: `predev`, `prebuild` and `pretest` copy `../../costs/pricing.json` to
`lib/pricing.generated.json` (gitignored). The landing cards and the order form show `from $X`,
or `[PRICE]` while the file is missing. `lib/pricing.ts` imports the `@pricing` alias:
`next.config.ts` points it at the generated file when present, else at the checked-in
`lib/pricing.placeholder.json`; vitest always uses the placeholder. So `next build`, `vitest`
and `tsc` work however they are started. In the cost model `from: true` is a display flag and
the number is `price` (an older numeric `from` still works). `postbuild` runs
`scripts/check-landing-price.mjs`, which fails the build when the prerendered landing HTML lacks
a price from the pricing file or shows `[PRICE]`.

**Contracts**: `npm run gen:contracts` regenerates `lib/contracts.gen.ts`. Hand-written mirrors
(`STAGES`, `PROFILES`, `PRODUCT_DEFAULTS`, `WORKER_HEARTBEAT_S`, `WORKER_ONLINE_S`, row types,
order statuses) are checked against `stages.py`, `payloads.py`, `job.py` and every migration by
`lib/contracts.test.ts`.

## Commands

```bash
npm install
npm run sync-env    # copy server keys from the repo root .env into .env.local (prints names only)
npm run dev:mock    # mock data, no gate, http://localhost:3100
npm run dev         # real Supabase; studio needs the access code or Jackson's login
npm test            # vitest
npm run build
npm run typecheck
npm run gen:contracts
npm run gen:mock-assets
uv run python apps/web/scripts/gen_mock_proofs.py   # from the repo root: mock proof renders
uv run python apps/web/scripts/gen_mock_paint_edit.py   # from the repo root: paint editor fixture
PLAYWRIGHT_CORE=<playwright-core dir> BASE_URL=http://localhost:3100 node scripts/studio-fidelity-shots.mjs
PLAYWRIGHT_CORE=<playwright-core dir> BASE_URL=http://localhost:3100 node scripts/paint-editor-shots.mjs
```

Paint editor in mock mode: Olive (3D keychain, full profile) opens Edit on the shipped paint in
`public/mock/paint/base`; the mock worker answers an edit with a new s09 attempt, then s10 and a
result, whose files are `public/mock/paint/edit` (the collar in Bambu PLA Basic Orange and a
white blaze, made by the real pipeline: session ops, simulate_print, the 3MF, p2p_slicer numbers
and toolpaths, proof views) whatever was painted. Biscuit (thin slice) shows Map paint.
Screenshots: `docs/design/shots/studio/paint-editor/`.

Mock seeds for the fidelity screens: Pip (keychain at intake, form question), Juniper (flat charm
at the concept pick), Mochi (3D keychain at the mesh gate), Olive (3D keychain, full profile,
done with a proof set), Biscuit (flat charm, thin slice, done). Screenshots:
`docs/design/shots/studio/fidelity/`.

Env (see `.env.example`): `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` (browser);
`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `ACCESS_CODE`, `ACCESS_COOKIE_SECRET`,
`ORDER_NOTIFY_EMAIL`, `AGENTMAIL_API_KEY`, `AGENTMAIL_INBOX_ID` (server only). Without the
service role key in production the API fails closed (503).

## Verified against the real project

One `TEST ORDER` (test@example.com) went through start, signed upload and finish on the real
Supabase project from the local dev server: row stored with one photo, `notify_error = 'test
order'`, no email sent. The row and the photo were then deleted (0 test orders remain). The proxy
redirects without a cookie, the access cookie opens `/jobs` and `/api/studio/*`. No job was
started from the test order, so the worker never saw it.

## Known gaps

- Rate limits are per server instance and in memory; a determined abuser across Vercel instances
  gets more tries. The numbers (5 orders per 10 min, 5 wrong codes per 15 min) are ASSUMED.
- The server adapter polls every 2 s instead of streaming; each open job page costs one request
  every 2 s.
- A code session sees every job and order (service role). A Supabase session sees only its own
  jobs through RLS, so code-created jobs (`owner_id` null) do not show there.
- Mock mode keeps state in memory; a reload reseeds. In mock mode "Start job" on an order
  creates the job in the server's memory store, which the browser's mock adapter cannot see.
- Landing copy keeps the design's placeholders `[TURNAROUND]` and `[DELIVERY]`; prices show
  `[PRICE]` until `costs/pricing.json` exists.
- The three.js viewer is checked by hand in a browser; jsdom tests stub it.
- "Texture only" is decided after the preview GLB loads in filament view, so an unpainted
  preview shows for one load before the page switches to the sheet.
- The worker's new preview GLB and render sheet were not on main when this was built. The viewer
  takes COLOR_0 or per-filament materials and frames a piece as lying flat when its glTF Y
  extent is under 45% of its longest side; the mock uses a generated flat keychain
  (`public/mock/preview-flat.glb`).
- The Paint panel reads the proposed s09 layout (contract request 4); until the paint stage
  writes it, a real job shows only what it finds (confidence and checklist from the review
  request).
- The job page fetches `geometry.json` and `warnings.json` once per path; a rerun of s06 writes a
  new attempt folder, which the page picks up as a new path.
- Old dark-theme components were removed (ConceptPanel, MeshPanel, ResultPanel), then the proof
  sheet and stage list; their logic lives in the model stage, the decision rail and the result
  column.
- The mock preview GLB keeps its colors after a palette edit; the rows and swatches follow the new
  result.
- No worker stage renders the mesh before the mesh gate (s09 and s10 write the proof set after
  it), so the page renders Meshy's GLB from the eight views with WebGL. Without WebGL the tiles
  say No render and Meshy's four thumbnails are not used in their place.
- Mock proof sets are the mock GLBs through `p2p_sim.proof` (`scripts/gen_mock_proofs.py`), on
  the simulator's dark background; client renders are on the page's light background.
- Paint editor: face picking needs WebGL2 (gl_VertexID); without it the editor says No WebGL.
  The brush takes faces by straight-line distance from the point under the cursor (turned
  toward the camera), not geodesic distance, so a large brush can reach across a thin gap.
  Each pick renders the whole figure once; a fast drag samples at most 8 dabs per frame. The
  figure loads unindexed (about 40 MB of positions for the 1.1M-face Frenchie). Driven with the
  real Bronco paint (532,360 faces, swapped into the mock files for one run) in headless
  Chromium on software GL: loaded in 2.7 s, a hover pick in 80 ms, a stroke committed in 1.8 s;
  a GPU is faster. The mock answers every edit with the same fixture, so after Send the mock
  editor shows the fixture's paint, not what was painted.

## Contract change requests

Answered on main: input photo keys, tweak messages, heartbeat, preview colors, review status,
spend in Realtime. Still open:

1. **Filament id rule.** The web derives ids as brand first word, material, line without the
   material word, color name (reproduces every id in `library.example.json` and Jackson's
   presets). Request: write the rule into `filaments.py`.
2. **Root `.gitignore` `data/`** ignores every directory named `data`, including
   `apps/web/lib/data`. `apps/web/.gitignore` re-includes it; a root rule of `/data/` would be
   safer.
3. **Structured spool suggestion on `Note`.** `Note` is `{text}` and Strict, so the web reads the
   suggestion from the text ("Add <spool>: ... dE00 <now> -> <after>"). Request: an optional
   `suggest: {spool, filament_id?, hex?, de00, de00_after?, region_ids?}`; the web already
   prefers it when present.
4. **s09 manifest layout for the Paint panel.** Proposed, and what the web and mock read:
   `data.iterations`, `data.max_iterations` (8), `data.checkpoints: [{id, iteration, note,
   before, after}]` where `before` is the ideal paint render and `after` the simulate_print
   render of the same view (job-relative paths), `stage_runs.confidence`, and the Checklist JSON
   as an output with role `checklist`. The `review_request` payload's `checklist` and
   `confidence` are the fallback.
5. **Filaments RLS for shared spools.** Jackson's 5 AMS spools are seeded with `owner_id` null,
   which `filaments_all` (owner_id = auth.uid()) hides from a signed-in session. Request: a
   select policy for authenticated users on rows with `owner_id is null`. Until then the
   Supabase adapter adds them from `/api/studio/filaments` (service role).
6. **Order price at order time.** `orders` has no price column. The server quotes from
   `pricing.json` and rejects a stale price, and the signed receipt carries the quote, but the
   admin list cannot show what the customer saw. Request: `orders.quoted_usd numeric(10,2)` and
   `orders.quote_kind text` (size, from, range, unpriced).
7. **First look task id.** The first look reserves a `spend` row (kind `first_look`, ref = order
   id) but has nowhere durable for the Meshy task id, so a reload before the image lands keeps
   polling only from the browser's session storage. Request: `spend.meta jsonb` or
   `orders.first_look_task text`.
