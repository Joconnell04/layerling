# Bambu vendor data

Copied from Bambu Studio 2.08 (`/Applications/BambuStudio.app/Contents/Resources/profiles/BBL/filament/`)
so sessions without Bambu Studio installed can build the filament catalog.

- `filaments_color_codes.json`: Bambu's color list (314 entries): `fila_id` (e.g. GFA00), `fila_type`
  (PLA Basic, PLA Matte, ...), `fila_color_name.en`, `fila_color` (hex with alpha), `fila_color_type`
  (单色 = single color; others are gradients or multi-color and do not fit one slot).
- `x2d_filament_presets.json`: every instantiated X2D filament preset name with its `filament_id`,
  type and vendor, inherits resolved. Match `fila_id` to `filament_id` to get `slicer_preset`.

Bambu Studio profiles are AGPL-3.0. Refresh both files when Bambu Studio updates.
