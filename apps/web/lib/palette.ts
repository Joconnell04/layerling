// Palette edits on a finished job: the draft of filament slots the user builds in the picker, the
// contract palette_edit payload it turns into, and the picker's option list (owned spools first,
// then catalog colors grouped by line).

import { hexToLab } from "./color";
import type { FilamentUsage, PaletteEdit } from "./contracts.gen";
import type { AnyMessage, FilamentRow } from "./db.types";

/** One slot in the draft. `from` is the result slot it started as; null for an added color. */
export interface DraftSlot {
  from: number | null;
  id: string;
}

export function draftFrom(usage: FilamentUsage[] | undefined): DraftSlot[] {
  return [...(usage ?? [])].sort((a, b) => a.slot - b.slot).map((u) => ({ from: u.slot, id: u.filament_id ?? "" }));
}

/** True when the draft differs from the result it started from. */
export function draftChanged(draft: DraftSlot[], usage: FilamentUsage[] | undefined): boolean {
  const orig = draftFrom(usage);
  return draft.length !== orig.length || draft.some((d, i) => d.from !== orig[i].from || d.id !== orig[i].id);
}

/**
 * The contract payload. recolor when the slots are the original ones in the original order (only
 * colors swapped), refit when a color was added or removed. source is any as soon as one chosen
 * filament is not owned.
 */
export function paletteEdit(draft: DraftSlot[], usage: FilamentUsage[] | undefined, filaments: FilamentRow[]): PaletteEdit {
  const orig = draftFrom(usage);
  const sameSlots = draft.length === orig.length && draft.every((d, i) => d.from === orig[i].from);
  const byId = new Map(filaments.map((f) => [f.id, f]));
  const unowned = draft.some((d) => byId.get(d.id)?.owned === false);
  return { slots: draft.map((d) => d.id), mode: sameSlots ? "recolor" : "refit", source: unowned ? "any" : "owned" };
}

export interface PickerGroup {
  key: string;
  label: string;
  items: FilamentRow[];
}

/** "Bambu Lab" + "PLA Basic" -> "Bambu PLA Basic". */
export function lineLabel(f: Pick<FilamentRow, "brand" | "line">): string {
  const brand = f.brand.replace(/\s+Lab$/i, "");
  return f.line.toLowerCase().startsWith(brand.toLowerCase()) ? f.line : `${brand} ${f.line}`;
}

function matches(f: FilamentRow, q: string): boolean {
  if (!q) return true;
  const hay = `${f.name} ${f.brand} ${f.line} ${f.material} ${f.hex}`.toLowerCase();
  return q
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((w) => hay.includes(w));
}

/** Owned spools first (AMS slot order, then name), then catalog colors grouped by line. */
export function pickerGroups(filaments: FilamentRow[], query = ""): PickerGroup[] {
  const q = query.trim();
  const hits = filaments.filter((f) => matches(f, q));
  const owned = hits
    .filter((f) => f.owned)
    .sort((a, b) => (a.ams_slot ?? 99) - (b.ams_slot ?? 99) || a.name.localeCompare(b.name));
  const lines = new Map<string, FilamentRow[]>();
  for (const f of hits.filter((x) => !x.owned)) {
    const k = lineLabel(f);
    lines.set(k, [...(lines.get(k) ?? []), f]);
  }
  const groups: PickerGroup[] = [];
  if (owned.length) groups.push({ key: "owned", label: "Owned", items: owned });
  for (const [label, items] of [...lines.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
    groups.push({ key: `line-${label}`, label, items: items.sort((a, b) => a.name.localeCompare(b.name)) });
  }
  return groups;
}

/** The newest palette_edit that no result has answered yet. */
export function pendingPaletteEdit(messages: AnyMessage[]): AnyMessage | null {
  let edit: AnyMessage | null = null;
  let result: AnyMessage | null = null;
  for (const m of messages) {
    if (m.kind === "palette_edit" && (!edit || m.created_at >= edit.created_at)) edit = m;
    if (m.kind === "result" && (!result || m.created_at >= result.created_at)) result = m;
  }
  if (!edit) return null;
  return !result || result.created_at <= edit.created_at ? edit : null;
}

/** Index of the draft slot whose color is nearest the target (plain Lab distance is enough here). */
export function nearestSlot(draft: DraftSlot[], hexOf: (id: string) => string | null, target: string): number {
  const t = hexToLab(target);
  let best = -1;
  let bestD = Infinity;
  draft.forEach((d, i) => {
    const hex = hexOf(d.id);
    if (!hex) return;
    const [L, a, b] = hexToLab(hex);
    const dist = (L - t[0]) ** 2 + (a - t[1]) ** 2 + (b - t[2]) ** 2;
    if (dist < bestD) {
      bestD = dist;
      best = i;
    }
  });
  return best;
}
