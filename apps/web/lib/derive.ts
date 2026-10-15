// Pure view derivations for the job page: stage timeline, open gates, cost.

import {
  JOB_CAP_CREDITS,
  JOB_CAP_USD,
  PROFILES,
  STAGES,
  WORKER_ONLINE_S,
  type StageInfo,
  type UserPayloadKind,
} from "./contracts";
import type { StageStatus } from "./contracts.gen";
import type { AnyMessage, JobRow, MessageRow, SpendRow, SpendVendor, StageRunRow } from "./db.types";

// ------------------------------------------------------------------ stages

export interface StageView {
  info: StageInfo;
  status: StageStatus;
  inProfile: boolean;
  attempts: number;
  run: StageRunRow | null;
  usd: number;
  credits: number;
  durationS: number | null;
  current: boolean;
}

export function stageViews(job: JobRow, runs: StageRunRow[], now: number = Date.now()): StageView[] {
  const profile = PROFILES[job.profile] ?? PROFILES.full;
  return STAGES.map((info) => {
    const mine = runs.filter((r) => r.stage === info.id).sort((a, b) => a.attempt - b.attempt);
    const run = mine.at(-1) ?? null;
    const inProfile = profile.includes(info.id);
    const status: StageStatus = run?.status ?? (inProfile ? "pending" : "skipped");
    let durationS: number | null = null;
    if (run?.started_at) {
      const end = run.finished_at ? Date.parse(run.finished_at) : now;
      durationS = Math.max(0, (end - Date.parse(run.started_at)) / 1000);
    }
    return {
      info,
      status,
      inProfile,
      attempts: mine.length,
      run,
      usd: mine.reduce((s, r) => s + (r.usd || 0), 0),
      credits: mine.reduce((s, r) => s + (r.credits || 0), 0),
      durationS,
      current: job.stage === info.id,
    };
  });
}

/** 1-based index of the job's current stage within its profile, e.g. [2, 4] for thin slice concept. */
export function stageProgress(job: JobRow): [number, number] {
  const profile = PROFILES[job.profile] ?? PROFILES.full;
  const idx = job.stage ? profile.indexOf(job.stage) : -1;
  const done = job.status === "done";
  return [done ? profile.length : Math.max(0, idx + 1), profile.length];
}

// ------------------------------------------------------------------ gates

const RESPONSE: Record<"questions" | "concepts" | "mesh_review" | "review_request", UserPayloadKind[]> = {
  questions: ["answers"],
  concepts: ["concept_pick", "tweak"],
  mesh_review: ["mesh_decision"],
  review_request: ["review_decision"],
};

type GateKind = keyof typeof RESPONSE;

function responsesTo(messages: AnyMessage[], gate: AnyMessage, kinds: UserPayloadKind[]): AnyMessage[] {
  const i = messages.indexOf(gate);
  return messages
    .slice(i + 1)
    .filter((m) => m.role === "user" && (kinds as string[]).includes(m.kind))
    .concat(
      messages
        .slice(0, i)
        .filter((m) => m.reply_to === gate.id && (kinds as string[]).includes(m.kind)),
    );
}

function latest<K extends AnyMessage["kind"]>(messages: AnyMessage[], kind: K): MessageRow<K> | undefined {
  return messages.filter((m) => m.kind === kind).at(-1) as MessageRow<K> | undefined;
}

function all<K extends AnyMessage["kind"]>(messages: AnyMessage[], kind: K): MessageRow<K>[] {
  return messages.filter((m) => m.kind === kind) as MessageRow<K>[];
}

export interface Gates {
  questions: { message: MessageRow<"questions">; answer: MessageRow<"answers"> | null; open: boolean } | null;
  concepts: {
    rounds: MessageRow<"concepts">[];
    latest: MessageRow<"concepts">;
    approved: MessageRow<"concept_pick"> | null;
    /** A tweak was sent after the latest round; a new round is rendering. */
    pending: boolean;
    open: boolean;
    thread: AnyMessage[];
  } | null;
  mesh: {
    reviews: MessageRow<"mesh_review">[];
    latest: MessageRow<"mesh_review">;
    decision: MessageRow<"mesh_decision"> | null;
    open: boolean;
  } | null;
  review: {
    request: MessageRow<"review_request">;
    decision: MessageRow<"review_decision"> | null;
    open: boolean;
  } | null;
  result: MessageRow<"result"> | null;
  /** The gate waiting on the user right now, newest first. */
  active: GateKind | null;
}

export function deriveGates(messages: AnyMessage[]): Gates {
  const ordered = [...messages].sort((a, b) => a.created_at.localeCompare(b.created_at));
  const open: { kind: GateKind; at: string }[] = [];

  let questions: Gates["questions"] = null;
  const q = latest(ordered, "questions");
  if (q) {
    const answer = (responsesTo(ordered, q, RESPONSE.questions)[0] as MessageRow<"answers">) ?? null;
    questions = { message: q, answer, open: !answer && q.payload.questions.length > 0 };
    if (questions.open) open.push({ kind: "questions", at: q.created_at });
  }

  let concepts: Gates["concepts"] = null;
  const c = latest(ordered, "concepts");
  if (c) {
    const after = responsesTo(ordered, c, RESPONSE.concepts);
    const approved =
      (all(ordered, "concept_pick").find((m) => m.payload.approve) as MessageRow<"concept_pick">) ??
      null;
    const pending = after.some((m) => m.kind === "tweak");
    const firstConcept = ordered.find((m) => m.kind === "concepts")!;
    const thread = ordered.filter(
      (m) =>
        m.created_at >= firstConcept.created_at &&
        (m.kind === "tweak" || m.kind === "tweak_decision" || (m.kind === "note" && m.stage === "s03_concept")),
    );
    concepts = {
      rounds: all(ordered, "concepts"),
      latest: c,
      approved,
      pending,
      open: !approved && !pending,
      thread,
    };
    if (concepts.open) open.push({ kind: "concepts", at: c.created_at });
  }

  let mesh: Gates["mesh"] = null;
  const m = latest(ordered, "mesh_review");
  if (m) {
    const decision =
      (responsesTo(ordered, m, RESPONSE.mesh_review)[0] as MessageRow<"mesh_decision">) ?? null;
    mesh = { reviews: all(ordered, "mesh_review"), latest: m, decision, open: !decision };
    if (mesh.open) open.push({ kind: "mesh_review", at: m.created_at });
  }

  let review: Gates["review"] = null;
  const r = latest(ordered, "review_request");
  if (r) {
    const decision =
      (responsesTo(ordered, r, RESPONSE.review_request)[0] as MessageRow<"review_decision">) ?? null;
    review = { request: r, decision, open: !decision };
    if (review.open) open.push({ kind: "review_request", at: r.created_at });
  }

  const active = open.sort((a, b) => b.at.localeCompare(a.at))[0]?.kind ?? null;
  return { questions, concepts, mesh, review, result: latest(ordered, "result") ?? null, active };
}

// ------------------------------------------------------------------ cost

export interface CostView {
  usd: number;
  credits: number;
  capUsd: number;
  capCredits: number;
  usdPct: number;
  creditsPct: number;
  byVendor: { vendor: SpendVendor; usd: number; credits: number; events: number }[];
  level: "ok" | "warn" | "over";
}

export function costView(job: JobRow, spend: SpendRow[] = []): CostView {
  const capUsd = job.budget_usd || JOB_CAP_USD;
  const capCredits = job.budget_credits || JOB_CAP_CREDITS;
  const usdPct = job.spent_usd / capUsd;
  const creditsPct = job.spent_credits / capCredits;
  const vendors = new Map<SpendVendor, { usd: number; credits: number; events: number }>();
  for (const s of spend) {
    if (s.job_id && s.job_id !== job.id) continue;
    const v = vendors.get(s.vendor) ?? { usd: 0, credits: 0, events: 0 };
    v.usd += s.usd;
    v.credits += s.credits;
    v.events += 1;
    vendors.set(s.vendor, v);
  }
  const worst = Math.max(usdPct, creditsPct);
  return {
    usd: job.spent_usd,
    credits: job.spent_credits,
    capUsd,
    capCredits,
    usdPct,
    creditsPct,
    byVendor: [...vendors.entries()].map(([vendor, v]) => ({ vendor, ...v })),
    level: worst >= 1 ? "over" : worst >= 0.75 ? "warn" : "ok",
  };
}

// ------------------------------------------------------------------ worker

export type WorkerState = "online" | "stale" | "offline";

/** Online under WORKER_ONLINE_S (contracts). Stale up to 5 min is ASSUMED; older is offline. */
export function workerState(lastSeen: string | null | undefined, now: number = Date.now()): WorkerState {
  if (!lastSeen) return "offline";
  const age = now - Date.parse(lastSeen);
  if (age < WORKER_ONLINE_S * 1000) return "online";
  if (age <= 5 * 60_000) return "stale";
  return "offline";
}
