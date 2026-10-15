import { filamentSlug, hexToLab, normalizeHex } from "../color";
import type { FilamentRow } from "../db.types";
import type { FilamentInput } from "./types";

/** Build a filaments row from form input; LAB is computed from hex (nominal). */
export function filamentRowFrom(
  input: FilamentInput,
  existing: FilamentRow | undefined,
  now: string,
  ownerId: string | null,
): FilamentRow {
  const hex = normalizeHex(input.hex);
  const [L, a, b] = hexToLab(hex);
  const keepMeasured = existing?.lab_source === "measured" && existing.hex === hex;
  return {
    id: input.id ?? filamentSlug(input.brand, input.material, input.line, input.name),
    owner_id: existing?.owner_id ?? ownerId,
    brand: input.brand.trim(),
    line: input.line.trim(),
    name: input.name.trim(),
    material: input.material.trim() || "PLA",
    hex,
    lab_l: keepMeasured ? existing.lab_l : L,
    lab_a: keepMeasured ? existing.lab_a : a,
    lab_b: keepMeasured ? existing.lab_b : b,
    lab_source: keepMeasured ? "measured" : "nominal",
    td_mm: input.td_mm ?? existing?.td_mm ?? null,
    owned: input.owned,
    spools: input.spools,
    ams_slot: input.ams_slot,
    swatch_path: existing?.swatch_path ?? null,
    bambu_filament_id: existing?.bambu_filament_id ?? null,
    slicer_preset: existing?.slicer_preset ?? null,
    density_g_cm3: existing?.density_g_cm3 ?? 1.24,
    notes: input.notes ?? existing?.notes ?? null,
    updated_at: now,
  };
}
