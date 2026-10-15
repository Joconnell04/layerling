// A display name and swatch color for a filament id. The library row wins; an id the library does
// not have ("generic-pla-gray") still reads as "Gray" with a nominal color instead of the raw id.

import { titleCase } from "./format";
import type { FilamentRow } from "./db.types";

/** Nominal sRGB for color words in filament ids (ASSUMED; the library's hex wins when present). */
const COLOR_WORDS: Record<string, string> = {
  black: "#161616",
  white: "#FFFFFF",
  brown: "#7C4B00",
  red: "#F72323",
  blue: "#0D6284",
  gray: "#8E8E8C",
  grey: "#8E8E8C",
  green: "#2F7D3B",
  yellow: "#F2C230",
  orange: "#F07F1E",
  pink: "#F2A1C0",
  purple: "#6C4A9B",
  beige: "#E4D3B4",
  cream: "#F1E6CF",
  tan: "#C69C6D",
  gold: "#C8A13A",
  silver: "#B9BCBE",
  navy: "#1F2B4A",
  ivory: "#F4EFDF",
  bone: "#E8DCC4",
  charcoal: "#3A3A3A",
};

const NOT_NAME = new Set([
  "generic", "bambu", "lab", "pla", "petg", "abs", "asa", "tpu", "pc", "pa", "hf", "plus", "basic",
  "matte", "silk", "tough", "pro", "cf", "gf", "support",
]);

export interface FilamentLabel {
  id: string | null;
  name: string;
  hex: string | null;
  /** True when the library has this id. */
  known: boolean;
}

export function filamentLabel(
  id: string | null | undefined,
  library: FilamentRow[] | Map<string, FilamentRow>,
  slot?: number,
): FilamentLabel {
  if (!id) return { id: null, name: slot != null ? `Slot ${slot}` : "Unknown", hex: null, known: false };
  const row = library instanceof Map ? library.get(id) : library.find((f) => f.id === id);
  if (row) return { id, name: row.name, hex: row.hex, known: true };
  const words = id.toLowerCase().split(/[-_\s]+/).filter(Boolean);
  const tail: string[] = [];
  for (let i = words.length - 1; i >= 0 && !NOT_NAME.has(words[i]); i--) tail.unshift(words[i]);
  const color = [...words].reverse().find((w) => COLOR_WORDS[w]);
  return {
    id,
    name: tail.length ? titleCase(tail.join(" ")) : id,
    hex: color ? COLOR_WORDS[color] : null,
    known: false,
  };
}
