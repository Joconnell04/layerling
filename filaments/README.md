# filaments (`p2p_filaments`)

`library.json` holds the five AMS filaments. Their `lab` values start as nominal (from the Bambu
Studio colour hex). This package measures the real colours: print a swatch card, photograph it
next to a 24-patch colour checker, run one command. It then writes measured CIELAB D65 and
`td_mm` into `library.json`.

## 1. Print the card

- File: `filaments/swatch_card.3mf` (preview: `swatch_card.png`). One object on plate 1, Textured
  PEI Plate, `0.20mm Standard @BBL X2D`.
- Filament N goes in AMS slot N: 1 black, 2 brown, 3 red, 4 blue PETG, 5 white. The file
  already maps them; keep the mapping Bambu Studio shows.
- Print as is. Don't scale it and don't change the layer height, because every wedge step is a
  whole number of 0.2 mm layers. The file sets 100 % infill so the tiles and bases are solid;
  Bambu Studio shows the process as modified for that reason.
- About 90 min and 22 g (13 g of that is purge and prime tower). Slicer gate report:
  `data/slice_reports/swatch_card.json`.
- The blue steps are PETG sitting on PLA and bond weakly, so don't flex the card. Hold it by the
  edges.

The card is 75 x 49 mm with a flat 2.0 mm top, plus the slot numbers raised 0.4 mm:

| Band | What |
|---|---|
| top strip | ArUco markers 0 and 1 (how the script finds the card) |
| tiles | 15 x 15 mm, 2.0 mm of each filament, AMS slot number in the corner |
| wedge over black | 0.2, 0.4, 0.6, 0.8, 1.2 mm of the filament over black, left to right |
| wedge over white | the same steps over white |
| bottom strip | ArUco markers 2 and 3 |

If the AMS changes, regenerate the card first: `uv run python -m p2p_filaments swatch-card`.

## 2. Photograph it

- Light: daylight from a window, not direct sun, or a 5000K high-CRI lamp. Use one kind of light
  only: turn the room lights off. No flash.
- Lay the card flat next to the checker so both get the same light. Any rotation works.
- Hold the camera roughly straight above them, with both filling most of the frame (the card
  at least 600 px wide in the photo). Keep the phone's shadow off them.
- Glare: look at the tiles on screen. If you see a bright streak, tilt the phone a little or
  move the card until it's gone.
- Main 1x camera, no zoom, no Portrait mode, no filters. JPEG, PNG or HEIC.
- If the script warns that something is clipped, retake with lower exposure (on iPhone, tap
  the screen and drag the sun icon down).

## 3. Run

```bash
uv run python -m p2p_filaments calibrate path/to/photo.jpg
```

It prints the checker fit, each filament's old and measured Lab, and `td_mm`. Output files:

- `filaments/library.json`: only `lab`, `lab_source` ("measured") and `td_mm` change for each
  filament on the card, plus the file's `updated_at`.
- `filaments/calibration/<photo name>/report.json` holds every number. `overlay.png` shows
  exactly what was sampled; each box should sit inside its patch, tile or step.

The library is left alone when the checker fit's mean dE2000 is over 5 (usually mixed light,
glare, or the wrong chart). Add `--dry-run` to write only the report. For a checker made
before November 2014, add `--checker classic24-2005`.

If it can't find the card or the checker, it writes `grid.png`: the photo with pixel
coordinates. Read the points off it and pass them:

```bash
uv run python -m p2p_filaments calibrate photo.jpg \
  --card-corners "412,300 1630,280 1650,1080 400,1100" \
  --checker-corners "1900,350 2900,360 2890,1000 1910,990"
```

`--card-corners`: the card's outer corners, starting top left with the tile row at the top,
then clockwise. `--checker-corners`: the centres of patch 1 (dark skin, brown), patch 6
(bluish green), patch 24 (black) and patch 19 (white).

## How it measures

- Card position: from the four ArUco markers (OpenCV, `DICT_4X4_50`, 1 mm cells). The
  report gives the marker fit error in pixels.
- Checker: OpenCV contrib `mcc` finds the 24 patches. Both ways up are tried and the better
  fit is kept.
- Correction: the photo is linearised with the sRGB curve, then a power per channel and a
  3 x 3 matrix map it to XYZ D65. The 12 parameters are fitted on all 24 patches. The report
  gives the fit error and the leave-one-out error (each patch predicted by a fit that never
  saw it). Leave-one-out is the honest accuracy figure. Filament colours far outside the
  checker's range (the red tile) are extrapolated.
- Reference values: X-Rite ColorChecker Classic, charts made from November 2014 on, CIELAB
  D50 (X-Rite 2016, "New Color Specifications for ColorChecker SG and Classic Charts",
  doi:10.5281/zenodo.3245895), Bradford-adapted to D65. `classic24-2005` uses the pre-2014
  file from the same source. The manufacturer's sRGB values (Wikipedia "ColorChecker" table)
  are kept in `checker.py` as a cross-check.
- Colour: trimmed mean over the tile, away from the edges and the raised number.
- Light across the card: the white strips are one material, so four patches of them map any
  falloff (a window on one side). Every tile and step is divided by that map, per camera
  channel. The report gives the four gains and warns when they spread by more than 5 %.
- td_mm: per linear sRGB channel, a step of thickness t over a base is modelled as
  `solid + (base - solid) * exp(-t / lambda)`. The fit uses all ten steps, with the black and
  white tiles as bases. `td_mm` is the smallest thickness where the colour over black and over
  white are both within dE2000 2.0 of the 2.0 mm tile. That matches the stepped-swatch idea
  behind HueForge's TD (the thickness where the base stops showing), measured in reflection.
  The simulator's light-over-dark rule can flag any painted layer thinner than the light
  filament's `td_mm`. The report also gives `lambda_mm_rgb` (for blending thin layers), the
  thinnest measured step that already hides both bases, and `extrapolated: true` when `td_mm`
  is past the 1.2 mm step. `--td-threshold` changes the 2.0.

## Commands

```bash
uv run python -m p2p_filaments swatch-card --out filaments/swatch_card.3mf   # + slicer gate
uv run python -m p2p_filaments swatch-card --no-check                         # 3MF and preview only
uv run python -m p2p_filaments calibrate photo.jpg [--checker classic24] [--dry-run]
uv run python -m p2p_filaments demo-photo /tmp/demo.jpg   # synthetic photo, prints the answers
uv run python -m p2p_filaments catalog                     # filaments/catalog.json, counts per line
uv run python -m p2p_filaments seed-catalog --dry-run      # what the seed would insert
uv run python -m p2p_filaments seed-catalog                # catalog -> Supabase shared rows
uv run pytest filaments/tests                              # 36 tests
```

The tests render the card and a checker, shoot them through a camera model with a known colour
cast and tone curve (plus noise and JPEG), and check that each tile comes back within dE2000 2
of the truth (mostly under 1). They also check `td_mm` against the known answer, with the
checker upside down, with manual corners, with light falling off across the card, and that a
`library.json` round trip changes nothing else.

## Catalog

`catalog.json` is every single-color Bambu Lab filament with an X2D preset, as a
FilamentLibrary of spools Jackson does not own (`owned` false, `spools` 0). The worker uses it
for palette picks when a job or a `palette_edit` asks for `source: any`, for spool suggestions,
and to resolve catalog ids in s10. Built from the vendored Bambu Studio files in
`vendor/bambu/` (see its README):

- Single-color entries only; gradients and multi-color spools are skipped.
- `slicer_preset` is the "@BBL X2D 0.4 nozzle" preset whose `filament_id` equals the color's
  `fila_id`; a line without one is skipped. Support materials (PVA, "Support for ...") are
  skipped.
- `hex` drops the alpha; `lab` is computed from it (CIELAB D65, `colour.hex_to_lab`),
  `lab_source` nominal. `material` is the preset's filament type.
- Ids are `bambu-<line>-<color>` slugs (`+` becomes `plus`), e.g. `bambu-pla-basic-jade-white`,
  `bambu-pla-silk-plus-gold`. A slug equal to an owned id in `library.json` is skipped because
  the owned row covers it (today only `bambu-pla-basic-red`).
- Order: PLA Basic, PLA Matte, PETG Basic, PETG HF, then the other lines by name.
- Rerunnable: an unchanged catalog keeps its `updated_at`, so the file only changes when the
  vendor files or the owned ids do. A test fails if the committed file is out of date.

Counts per line are printed by the build (281 in total from Bambu Studio 2.08's list).

`seed-catalog` inserts the catalog into the Supabase `filaments` table as shared rows
(`owner_id` null, `owned` false, `spools` 0), readable by every logged-in user. It reads
`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` from the environment or the root `.env`
(`P2P_ENV_FILE` to point elsewhere) and never prints them. It only inserts ids the table does
not have (`on_conflict=id`, ignore duplicates), snapshots every owned row and the owned ids in
`library.json` before and after, and fails if any of them changed. Running it twice inserts
nothing the second time.

## Not done

- `calibrate` only updates `library.json`; measured values are not written to Supabase.
- The PETG steps on PLA may lift over time; reprint the card if they do.

## Contract change requests

1. `Filament`: add `td_lambda_mm: tuple[float, float, float] | None` (per-channel decay length
   from the wedges) so the simulator can render thin light-over-dark layers, not just flag
   them. It is in `report.json` meanwhile.
