// Spool suggestions: assistant `note` messages that say which spool to load and how far off the
// nearest loaded one is, e.g. "Add gray PLA: mid gray maps to brown at dE00 26.9 -> 3.1".
// contracts Note is {text} only, so the suggestion is read from the text; a structured
// `suggest` object (see the README contract change request) wins when present. The contract
// spool_suggestion message carries the catalog filament id to apply in one click.

import type { AnyMessage } from "./db.types";

export interface SpoolSuggestion {
  messageId: string;
  stage: string | null;
  text: string;
  /** "gray PLA" */
  spool: string;
  /** CIEDE2000 from the wanted color to the nearest loaded spool. */
  de00: number | null;
  /** CIEDE2000 once the suggested spool is loaded, when the note says. */
  de00After: number | null;
  /** Catalog filament id to apply in one click, when the worker found one. */
  filamentId: string | null;
  /** The color the loaded spools cannot match. */
  targetHex: string | null;
}

interface StructuredSuggest {
  spool?: string;
  name?: string;
  filament_id?: string;
  de00?: number;
  de00_after?: number;
}

const VERB = /\b(?:add|load|buy|swap in|suggest(?:ed)?(?: adding)?)\s+(?:an?\s+|one\s+|the\s+)?/i;
const STOP = /\s*(?:[:;,.()—]|\s(?:to|for|would|fixes|so|because|spool|filament)\b|$)/i;
const DE = /(?:dE ?00|dE ?2000|ΔE ?00|ΔE ?2000|delta ?E(?: ?00)?|CIEDE2000)\s*(?:of|=|:|at)?\s*(\d+(?:\.\d+)?)/i;
const AFTER = /(\d+(?:\.\d+)?)\s*(?:->|→|to)\s*(\d+(?:\.\d+)?)/;

export function parseSpoolSuggestion(
  text: string,
): Omit<SpoolSuggestion, "messageId" | "stage" | "text" | "filamentId" | "targetHex"> | null {
  const verb = VERB.exec(text);
  const de = DE.exec(text);
  if (!verb || !de) return null;
  const rest = text.slice(verb.index + verb[0].length);
  const stop = STOP.exec(rest);
  const spool = rest.slice(0, stop ? stop.index : rest.length).trim();
  if (!spool || spool.length > 40) return null;
  const after = AFTER.exec(text.slice(de.index));
  return {
    spool,
    de00: Number(de[1]),
    de00After: after && Number(after[1]) === Number(de[1]) ? Number(after[2]) : null,
  };
}

/** Suggestions from assistant notes, newest first, one per spool. */
export function spoolSuggestions(messages: AnyMessage[]): SpoolSuggestion[] {
  const out: SpoolSuggestion[] = [];
  const seen = new Set<string>();
  for (const m of [...messages].reverse()) {
    if (m.kind === "spool_suggestion") {
      const p = m.payload;
      const key = p.suggestion.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({
        messageId: m.id,
        stage: m.stage ?? null,
        text: m.body ?? `Add ${p.suggestion}`,
        spool: p.suggestion,
        de00: p.delta_e_now,
        de00After: p.delta_e_with ?? null,
        filamentId: p.filament_id ?? null,
        targetHex: p.target_hex,
      });
      continue;
    }
    if (m.kind !== "note" || m.role !== "assistant") continue;
    const payload = (m.payload ?? {}) as { text?: string; suggest?: StructuredSuggest };
    const text = payload.text ?? m.body ?? "";
    const s = payload.suggest;
    const parsed = s?.spool || s?.name
      ? { spool: (s.spool ?? s.name)!, de00: s.de00 ?? null, de00After: s.de00_after ?? null }
      : parseSpoolSuggestion(text);
    const extra = { filamentId: s?.filament_id ?? null, targetHex: null };
    if (!parsed) continue;
    const key = parsed.spool.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ messageId: m.id, stage: m.stage ?? null, text, ...parsed, ...extra });
  }
  return out;
}
