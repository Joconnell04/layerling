// What the Paint panel reads for stage 9: the latest s09 run's iterations, checkpoints and
// confidence, and the checklist path (s09 output role "checklist", else the review request's).
// The s09 manifest layout is proposed in the README contract change requests; the reader takes
// a few spellings so it keeps working while the paint stage settles.

import type { AnyMessage, MessageRow, StageRunRow } from "./db.types";

export interface PaintCheckpoint {
  id: string;
  iteration: number | null;
  note: string | null;
  /** Ideal paint render (flat filament colors). */
  before: string | null;
  /** simulate_print render of the same view. */
  after: string | null;
}

export interface PaintInfo {
  run: StageRunRow;
  iterations: number | null;
  maxIterations: number;
  confidence: number | null;
  checkpoints: PaintCheckpoint[];
  checklistPath: string | null;
  reason: string | null;
}

/** PLAN: max 8 paint iterations. */
export const MAX_PAINT_ITERATIONS = 8;

const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

function checkpointsFrom(data: Record<string, unknown>): PaintCheckpoint[] {
  const raw = Array.isArray(data.checkpoints) ? data.checkpoints : [];
  return raw
    .map((c, i): PaintCheckpoint | null => {
      if (!c || typeof c !== "object") return null;
      const o = c as Record<string, unknown>;
      return {
        id: str(o.id) ?? str(o.checkpoint_id) ?? `c${i + 1}`,
        iteration: num(o.iteration) ?? i + 1,
        note: str(o.note),
        before: str(o.before) ?? str(o.ideal) ?? str(o.paint),
        after: str(o.after) ?? str(o.sim) ?? str(o.simulated),
      };
    })
    .filter((c): c is PaintCheckpoint => !!c);
}

export function paintInfo(runs: StageRunRow[], messages: AnyMessage[]): PaintInfo | null {
  // The paint agent's runs; a paint edit (data.paint_edit) has no iterations or checklist.
  const s09 = runs.filter(
    (r) => r.stage === "s09_paint" && r.status !== "skipped" && r.status !== "pending" && !(r.manifest.data as Record<string, unknown> | undefined)?.paint_edit,
  );
  if (!s09.length) return null;
  const run = s09.reduce((a, b) => (b.attempt > a.attempt ? b : a));
  const data = (run.manifest.data ?? {}) as Record<string, unknown>;
  const checkpoints = checkpointsFrom(data);
  const request = [...messages]
    .reverse()
    .find((m): m is MessageRow<"review_request"> => m.kind === "review_request" && m.stage === "s09_paint");
  const checklistPath =
    (run.manifest.outputs ?? []).find((o) => o.role === "checklist")?.path ?? request?.payload.checklist ?? null;
  return {
    run,
    iterations: num(data.iterations) ?? num(data.iteration) ?? (checkpoints.length || null),
    maxIterations: num(data.max_iterations) ?? MAX_PAINT_ITERATIONS,
    confidence: run.confidence ?? num(run.manifest.confidence) ?? request?.payload.confidence ?? null,
    checkpoints,
    checklistPath,
    reason: request?.payload.reason ?? null,
  };
}

export const CHECKLIST_LABELS: Record<string, string> = {
  spec_coverage: "Color spec covered",
  face_parts: "Eyes, nose, mouth",
  markings: "Markings",
  islands: "No small islands",
  creases: "Boundaries on creases",
  slot_count: "Fits AMS slots",
  sim_matches_intent: "Sim matches intent",
  purge_under_40pct: "Purge under 40%",
};
