# geometry (stage 6)

Scales, repairs and orients the Meshy mesh, then adds the exact-geometry parts a product needs:
keyring and hanging loops, round and oval bases, a nameplate with the pet's name, a flat back,
and a magnet pocket. The figure keeps Meshy's triangles in Meshy order (a map records every
change), so paint and labels carry over. Add-ons are separate closed parts; nothing is ever
booleaned into the figure.

## Interface

```python
from p2p_geometry import build_product

res = build_product(figure_vertices, figure_faces, product, addons, target_size_mm,
                    library, settings, out_dir)   # -> p2p_contracts.geometry.GeometryResult
```

- `figure_vertices, figure_faces`: the Meshy GLB mesh in glTF scene coordinates (Y up, front +Z,
  node transforms applied), faces in GLB order. `load_meshy_glb(path)` returns exactly that.
  A GLB path with `figure_faces=None` also works and fills `source_glb`.
- `product`: `ProductType` or its string. `addons` merge with `PRODUCT_DEFAULTS` by kind.
- `form` (keyword): the job's product form, `figure` (full 3D) or `flat` (a charm). None reads an
  add-on's `form` param, then the legacy `orientation` param, then the product default:
  `figure` for everything but the magnet (`product.resolve_form`). A 3D figure is never
  flattened unless the job chose flat.
- `colors` (keyword): `AddonColorInputs` (Meshy texture per GLB face, role targets from the job
  or the approved concept, palette source); every add-on without an explicit filament then takes
  its color from the product (`addon_color.py`), never the darkest loaded spool.
- `target_size_mm`: longest side of the whole product, loop and base included. For a nameplate
  it is the plate width. None uses the product default.
- `library`: `FilamentLibrary`, a path, or None (finds `filaments/library.json` upward).
- Writes into `out_dir`: `figure.ply`, one PLY per part, `face_index_map.npy` (when repair or
  the plinth cut changed faces; -1 marks added faces), `geometry.json` (the `GeometryResult`)
  and `geometry_report.json` (decisions, measurements, the repair report).
- `transform` is row-major, glTF frame to print frame (mm, Z up, front -Y, bottom on z = 0).
- `orientation`: `upright`, `flat_face_up` (keychain and magnet lying on their back, face up),
  or `flat_back_down` (lying on a flank, `display: left|right`).
- `supports`: `none` for flat products (height fields; tree supports would fill the magnet
  pocket), `tree` for upright ones.
- `notes`: one line per decision. Part roles: `base`, `keyring`, `hanging_loop`, `plate`,
  `text`, `back`, `cutter`. Filament ids are always concrete except the figure's.

Helpers, each returning manifold3d solids: `keyring_loop`, `hanging_loop`, `round_base`,
`oval_base`, `plate`, `nameplate(text: TextSpec, ...)`, `flat_back`, `magnet_pocket`,
`text_mesh`, `layout_text`, `repair_figure`, `detect_plinth`, `cut_below`.
`build_product_geometry` returns the richer `ProductGeometry` without writing files.

## Form and print pose

The form is the customer's choice at intake and is drawn in the approved concept
(docs/runs/fidelity/DEEP-DIVE.md). Geometry keeps it.

| Form | Print pose | Why |
|---|---|---|
| figure (default; keychain, figurine, nameplate, ornament, other) | Upright, full depth, as Meshy made it | Supports only under belly and chin; the face points -Y and is never a support surface |
| flat (magnet default; a keychain when the job chose a flat charm) | Face up (+Z), head toward +Y | A true relief: a height field with no supports. A flat ring is loaded along its layers |

A flat charm is a true relief of the front view: after Meshy's plinth is cut, a plane parallel to
the bed cuts the figure so only its front stays (the deepest plane that keeps 97 % of the front
silhouette, never less than 35 % of the depth; `relief_cut` param `off` disables it), and only
that front is squeezed, as far as the thickness aim needs (10 mm keychain, 15 mm magnet, never
below x0.2). It sits on a flat back: the shadow grown by a 1 mm rim, 3 mm thick (4 mm with a
magnet pocket), plus a layer-by-layer filler. A magnet with form figure stands upright on a
round base that holds the pocket.

The loop goes where the customer said (FROM JACKSON: the intake answer and the approved
concept decide). The answer names an area, not a seat (`loop_area`): `position: top` ("Top of
the head") is the head, the top 12 % of the figure below its highest seat (fixed in the figure's
own frame on the first scale pass, so a rescale never turns the back into the head); `back`
("On the back") is the top of the figure behind the head; `left` or `right` ("On the side") a
flank; `any` ("Wherever it hangs best") or no answer lets geometry choose. Within the area the
least tilted sturdy seat wins (every seat passed the thickness and stem tests; among seats
within 1 degree of the best tilt the highest wins). Only `any` moves the loop off the head:
then the head wins when it hangs within 8 degrees, else the highest seat that does (over the
center of mass), else the least tilted one. The Frenchie on its head hangs 27 degrees tilted,
and that is reported, not fixed by moving the loop: `metrics.hang` has the tilt, the pivot (top
of the hole) and the center of mass in the print frame, the best tilt in the area and anywhere
on top, and a note says what `any` would give. s10 draws the piece as it hangs from those
(`p2p_sim.proof.hanging_view`, proof view `hanging`). `hang: head` (legacy) is the head area,
`hang: highest` the old highest-seat rule. The ring that sticks out of the figure's rear is
`position: rear`. An upright keyring is 5 mm thick along its hole (4 mm flat; ASSUMED, needs a
test print); where ears leave no room, the standard ring is used.

## Rules enforced (warnings when a rule cannot be met)

- Plinth (flat products): the bottom slab is found from the horizontal section profile of the
  upright figure (flat bottom, covers at least 25 % of the shadow, near-constant section, then a
  sharp drop, under 25 % of the height), and its top refined to the upward faces there. The
  figure is cut 0.4 % of its height above that top: faces below are dropped, faces crossing the
  plane are clipped (the pieces keep their Meshy face in `face_index_map`), and the opening is
  capped (-1 in the map). `remove_plinth: keep` in any add-on's params turns it off. The SAM 3
  "base" label is not available yet at stage 6, so this is geometric.
- Loops: hole at least 4 mm (default 5), ring wall and thickness at least 2.5 mm (keyring
  3 x 4 mm, hanging loop 2.5 x 3 mm), stem at least 2 mm into the figure (rule: 1.5).
  `position` is `top`, `back`, `left`, `right`, `rear` or `any` in the figure's own terms (left
  is the viewer's left; see "Form and print pose" for the areas). Unset, every side the pose allows is scored by local thickness (upright: inscribed
  sphere at the stem; flat: outline width at the stem), capped at "clearly sturdy", with the top
  preferred by 1.25x so the piece hangs upright. A position that faces the bed warns and falls
  back to auto. On each side the spot is the highest flush seat whose footprint sits over solid
  material at least 4 mm thick, so a thin ear never gets the loop. The hanging tilt is reported.
- Text: kerned by HarfBuzz from the font's GPOS/kern tables. Fonts (OFL): Nunito
  (`rounded`, `rounded-bold` default, `rounded-black`) and Fredoka (`friendly`,
  `friendly-bold`). One line or any two-line split at a space, whichever allows the larger cap
  height (two lines must win by 20 %). Never under 5 mm, raised if more than 3 % of the ink would
  be thinner than 0.8 mm (the heavier weight of the family is tried first). If a name still does
  not fit, the plate grows and a warning suggests the wider plate. Relief 0.8 to 1.2 mm in whole
  layers. Color: highest CIEDE2000 against the plate, same material only. `engraved` is a flush
  inlay.
- Plates and bases: at least 3 mm (default 4); every foot sinks at least 1 mm; bases sized from
  the lower 30 % of the figure plus 3 mm. Tip check uses the center of mass of the union of all
  parts (overlaps counted once, cutters subtracted).
- Keychain: at least 4 mm thick along the load path from the ring into the body.
- Magnet pocket (10 x 2 mm magnet by default): 10.3 x 2.2 mm, open on the back, 1.5 mm from the
  outline, cut from the back, plate or base only.
- Repair: welds UV seams, drops collapsed, duplicate and dangling fin faces, fixes winding in
  place, fills holes up to 15 % of the bbox diagonal.

- Plate shape: the nameplate add-on's `shape` (the intake answer): `rectangle` (1 mm corners),
  `rounded` (5 mm, default) or `oval` (an ellipse; the plate deepens so the figure's footprint
  sits inside it and the name uses the middle 70 % of its depth).
- Add-on colors (`addon_color.py`, when `colors` is given): the job's pick for the role, then
  the approved concept's read of that add-on, else the figure color the part touches (loops,
  bases: the texture cluster with the most figure area within 1.5 mm), the figure's main color
  (a flat charm's back, a plate), and for the name the figure palette color with the most
  contrast to the plate. Targets map under the achromatic guard, owned first (+3 dE00 for an
  unowned color), and a filament the figure uses wins within 4 dE00. Unowned catalog colors
  only with source any. `metrics.addon_colors` and `metrics.figure_palette` record it.

`AddOn.params` read: `position`, `hang`, `loop_inner_d_mm`, `loop_section_mm`,
`loop_thickness_mm`, `base_t_mm`, `margin_mm`, `plate_w_mm`, `plate_d_mm`, `plate_t_mm`,
`corner_mm`, `shape`, `figure_height_frac`, `back_t_mm`, `rim_mm`, `pocket_d_mm`,
`pocket_depth_mm`, and on any add-on `form`, `orientation` (legacy), `display`, `relief_scale`,
`relief_cut`, `max_thickness_mm`, `remove_plinth`.

## Commands

```bash
uv run pytest geometry/tests       # 92 tests, about 90 s; beagle and slicer tests skip when absent
uv run python -m p2p_geometry.production_slice --glb model.glb --meshy-3mf multicolor.3mf \
    --out geometry/_out --reports data/slice_reports
uv run python -m p2p_geometry.demo --glb model.glb --out geometry/_out --slice   # quick, 1 color
```

`production_slice` builds the five products, writes each job folder under `geometry/_out/`,
previews `geometry/_out/<product>.png`, then the p2p_threemf production writer (Meshy's
multicolor paint moved onto the geometry figure, add-ons as parts, writer defaults) and the
slicer gate. The Meshy 3MF is fitted to the UNCUT figure (see Integration requests).

The tables below are the flat charm (the demo and tier cases pin `form: flat`); the 3D
keychain's numbers are in docs/runs/fidelity/rerun/.

## Results on the Meshy beagle

meshy-6-lite GLB plus Meshy's multicolor 3MF (cartoon, 5 colors), Jackson's 5 AMS filaments,
Bambu Studio 2.08, X2D 0.4 nozzle, 0.20 mm Standard, threemf writer defaults
(prime_volume_mode Saving, flush_into_infill 1), supports from `GeometryResult.supports`.
Every slice passed the gate (3 slots, read-back ok). Numbers are SOURCE: the slicer.
Reports: `data/slice_reports/geometry_<product>__baseline__production.json` (with a `config` key).

| Product | Size (mm) | Pose, supports | Print time | Total g | Purge g |
|---|---|---|---|---|---|
| keychain | 24 x 45 x 10 | flat face up, none | 1h 6m | 14.4 | 11.3 |
| nameplate "BISCUIT" | 120 x 56 x 53 | upright, tree | 8h 2m | 112.4 | 82.2 |
| ornament | 43 x 56 x 70 | upright, tree | 9h 36m | 123.8 | 100.6 |
| magnet (10 x 2 mm) | 39 x 60 x 15 | flat face up, none | 1h 40m | 24.9 | 15.4 |
| figurine | 74 x 82 x 90 | upright, tree | 14h 0m | 193.2 | 134.7 |

Purge dominates because Meshy's paint changes color about twice per layer; that is the paint
stages' problem, not geometry's. Repair drops one dangling fin face; the flat products also drop
Meshy's plinth (4,308 faces, 8 % of the height) and cap the cut with 1,615 faces.

## Size tiers

`build_product(..., size_tier="xs"|"s"|"m"|"l")` takes the size from `SIZE_TIERS_MM` (figurine
height, nameplate plate width, the longest side for the rest); a tier wins over
`target_size_mm`, and `notes` starts with the tier. Loops, plates, backs and pockets keep their
fixed minimum sizes at every tier; flat charms aim at 22 % (keychain) or 25 % (magnet) of their
size in thickness.

`uv run python -m p2p_geometry.tiers --glb ... --meshy-3mf ... --reports data/slice_reports`
builds and slices all 20, writes `geometry_<product>_<tier>__production.json` (config has
`size_tier`, `printability_flags`, overrides), heroes to `geometry/_out/hero/<product>_m.png`
(1600 px, transparent, filament colors), and flags plus the smallest safe size to
`geometry/_out/tiers/summary.json`. For plates wider than about 120 mm it moves the prime tower
behind the object; at Bambu's default spot the 150 mm nameplate failed with a tower collision.
The p2p_threemf writer now does this for every product and size (`place_prime_tower`: keeps
Bambu's spot when clear, else behind, beside or in front of the object, never left of
x = 24 mm where the X2D's right nozzle cannot reach), and `retarget` gives faces the geometry
stage added (-1 in `face_index_map`, e.g. the plinth cap) their neighbours' majority paint
(`fill_from_neighbours`; `retarget_3mf` does it when `figure_source="geometry"`).

Print time, total grams, purge grams (SOURCE: slicer; all 20 passed the gate, 3 slots):

| Product | XS | S | M | L |
|---|---|---|---|---|
| keychain (40/45/52/60 mm) | 0h 54m, 11 g, 9 g | 1h 06m, 14 g, 11 g | 1h 17m, 17 g, 13 g | 1h 29m, 21 g, 14 g |
| nameplate | 5h 15m, 70 g, 56 g | 6h 34m, 90 g, 69 g | 8h 02m, 112 g, 82 g | 10h 22m, 150 g, 102 g |
| ornament | 6h 28m, 80 g, 71 g | 7h 57m, 101 g, 85 g | 9h 36m, 124 g, 101 g | 12h 06m, 160 g, 121 g |
| magnet | 1h 07m, 15 g, 11 g | 1h 20m, 19 g, 13 g | 1h 39m, 25 g, 15 g | 2h 18m, 37 g, 20 g |
| figurine | 7h 06m, 92 g, 77 g | 10h 21m, 138 g, 106 g | 14h 00m, 193 g, 135 g | 19h 47m, 291 g, 172 g |

Flags (rules in `rules.py`; the detail rule: eyes are 6.3 % of the beagle's height, SOURCE,
and must stay 2 mm across, ASSUMED, so the figure must be at least 32 mm tall):

| Product | Flagged tiers | Smallest safe |
|---|---|---|
| keychain | none at 40/45/52/60 mm (the old 30 and 38 mm tiers failed: eyes 1.3 and 1.9 mm) | 40 mm |
| nameplate | none for "BISCUIT" (XS caps 7.6 mm, figure 34 mm) | 76 mm; 90 mm for long names |
| ornament | none | 42 mm |
| magnet | none | 32 mm |
| figurine | none | 36 mm tall |

Long names on the nameplate: "Bartholomew" and "Arthur, Maud & Biscuit" need a 90 mm plate at
XS (5.0 mm caps, the plate widens and warns); S and up fit (5.8, 7.4 and 9.8 mm caps at S, M, L).
Loops never got thin: the seat was the head top at every tier (13 to 18 mm wide on the keychain,
11 to 13 mm thick on the ornament) and the keychain load path stayed at 4 mm.

## Speed

`projected_silhouette` (base footprint, plate depth, plinth ratio, relief outline) is the union
of the triangles, computed from their net boundary: opposite XY edges cancel, the edges left
(folds, open borders) chain into loops with the same winding numbers, and Clipper2 fills them
with FillRule.Positive. It used to union every triangle (83 s a call on the sombrero's
1,291,050 faces, six calls per build); now under 1 s, and the sombrero's build writes
byte-identical files (`uv run python -m p2p_evals bench-s06`; evals/reports/bench-s06.md).
`repair`'s edge table keys each edge as one int64 instead of np.unique over rows (same result).

## Known gaps

- A Creative Lab relief (the flat-charm path of fidelity/front) is built flat without a plinth
  or relief cut (s06 passes `remove_plinth: keep`, `relief_cut: off`) and still gets the
  standard flat back and filler under its own base; not yet checked on a real build.
- The head band is a height rule (the top 12 % below the highest seat), not a head label; on a
  figure whose ears block the head top it can be the neck.
- A hanging figure's tilt uses the pivot at the top of the hole; a key ring's wire can also
  roll the ring, which is not modeled. Needs one test print.

- Loops at `rear` on an upright figure stick out sideways and need supports under the ring.
- The beagle magnet reaches the x0.2 depth floor at 15 mm thick.
- Plinth detection is geometric; a figure standing on a very thick block or rock is left alone.
- Previews come from a small z-buffer rasterizer, not the sim renderer.

## Integration requests (outside geometry/)

1. s07 / `retarget_3mf`: when `face_index_map` shows faces dropped (the plinth on flat
   products), use `figure_source="geometry"`, and fit the Meshy 3MF to the uncut figure
   (`source_glb` placed by `transform`), not to `figure.ply`. `fit_meshy_frame` takes corners of
   the bounding box, so the cut figure shifts the paint by the plinth height (about 8 % of the
   height on the beagle). `production_slice.production_3mf` shows the working call.
2. s07 / slicer gate: take supports from `GeometryResult.supports` (`none` -> enable_support 0).
