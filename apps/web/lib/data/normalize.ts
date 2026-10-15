import type { JobRow, SpendRow, StageRunRow } from "../db.types";

// Postgres numeric columns can arrive as strings; coerce the ones the UI does math on.
const num = (v: unknown): number => (typeof v === "string" ? Number(v) : (v as number));

export function normalizeJob(row: Record<string, unknown>): JobRow {
  const r = row as unknown as JobRow;
  return {
    ...r,
    budget_usd: num(r.budget_usd),
    spent_usd: num(r.spent_usd),
    budget_credits: num(r.budget_credits),
    spent_credits: num(r.spent_credits),
    settings: r.settings ?? {},
    photos: r.photos ?? [],
    addons: r.addons ?? [],
    access: r.access ?? "owner",
  };
}

export function normalizeRun(row: Record<string, unknown>): StageRunRow {
  const r = row as unknown as StageRunRow;
  return { ...r, usd: num(r.usd), credits: num(r.credits), manifest: r.manifest ?? {} };
}

export function normalizeSpend(row: Record<string, unknown>): SpendRow {
  const r = row as unknown as SpendRow;
  return { ...r, usd: num(r.usd), credits: num(r.credits) };
}


/**
 * jobs.form and jobs.custom_asks come from migration 20260924000011, which a project may not have
 * yet. PostgREST answers an insert that names a missing column with PGRST204 ("Could not find the
 * 'form' column of 'jobs' in the schema cache"). Callers retry without the optional columns; the
 * worker's intake then asks the form question, so the choice is asked again rather than lost.
 */
export const OPTIONAL_JOB_COLUMNS = ["form", "custom_asks"] as const;

export function missingOptionalColumn(error: { message?: string; code?: string } | null | undefined): boolean {
  if (!error) return false;
  const msg = error.message ?? "";
  return (
    (error.code === "PGRST204" || /column/i.test(msg)) &&
    OPTIONAL_JOB_COLUMNS.some((c) => msg.includes(`'${c}'`) || msg.includes(`"${c}"`) || msg.includes(` ${c} `))
  );
}

export function withoutOptionalColumns<T extends Record<string, unknown>>(row: T): T {
  const out = { ...row };
  for (const c of OPTIONAL_JOB_COLUMNS) delete out[c];
  return out;
}
