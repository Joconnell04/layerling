// Pure views over jobs rows and stage_runs manifests for the trace pages and API routes.
//
// Calls for an attempt = its manifest.data.trace entries, plus model_calls and meshy_tasks
// whose ref no entry covers (jobs before tracing, Meshy tasks still pending), marked derived.
// Meshy $ = credits x USD_PER_MESHY_CREDIT ($0.02, ASSUMED).

import { STAGES, STAGE_BY_ID, USD_PER_MESHY_CREDIT } from "../contracts";
import type { StageId } from "../contracts.gen";
import type {
  AttemptView,
  CallRow,
  JobLike,
  JobTrace,
  ManifestLike,
  ModelSpend,
  RunLike,
  SlicerSummary,
  SpendSummary,
  TraceEntry,
  TraceJobRow,
  TraceKind,
} from "./types";

export const STAGE_LABEL: Record<StageId, string> = {
  s01_intake: "Intake",
  s02_color_spec: "Color spec",
  s03_concept: "Concept",
  s04_multiview: "Multi-view",
  s05_mesh: "3D model",
  s06_printability: "Geometry",
  s07_baseline: "Baseline",
  s08_palette: "Palette",
  s09_paint: "Paint",
  s10_write: "Write",
};

export const MODEL_KINDS: readonly TraceKind[] = ["claude", "meshy", "runpod"];
const TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELED", "EXPIRED"]);
const MESHY_TEXT = ["prompt", "texture_prompt", "text_style_prompt"];
const MESHY_IMAGES = [
  "reference_image_urls",
  "image_urls",
  "image_url",
  "texture_image_urls",
  "texture_image_url",
  "image_style_url",
  "input_image_url",
];

const stageNo = (s: string) => STAGE_BY_ID[s as StageId]?.number ?? 99;
const ms = (iso: string | null | undefined) => (iso ? Date.parse(iso) : NaN);
const round6 = (n: number) => Math.round(n * 1e6) / 1e6;

/** Job name: the title, else the add-on name text, else the prompt. */
export function jobName(job: Pick<JobLike, "title" | "addons" | "prompt">): string {
  const title = job.title?.trim();
  if (title) return title;
  const text = (job.addons ?? []).map((a) => a.text?.text?.trim()).find(Boolean);
  return text || job.prompt;
}

/** ai_model for image endpoints, else the endpoint's short name (print/multi-color). */
export function meshyLabel(endpoint: string, request?: Record<string, unknown>): string {
  const short = endpoint.replace(/^openapi\/v1\//, "").replace(/^openapi\//, "");
  if (short === "image-to-image" || short === "text-to-image") {
    return typeof request?.ai_model === "string" ? request.ai_model : short;
  }
  return short;
}

/** `storage:<bucket>/<job_id>/<path>` of this job -> the job-relative path. */
export function jobPath(ref: string, jobId: string): string {
  const m = /^storage:(?:inputs|artifacts)\/([0-9a-f-]{36})\/(.+)$/i.exec(ref);
  return m && m[1].toLowerCase() === jobId.toLowerCase() ? m[2] : ref;
}

function seconds(from: string | null | undefined, to: string | null | undefined): number | null {
  const a = ms(from);
  const b = ms(to);
  return Number.isFinite(a) && Number.isFinite(b) ? Math.max(0, (b - a) / 1000) : null;
}

function taskError(result: Record<string, unknown> | null | undefined): string | undefined {
  const e = result?.task_error as { message?: string } | null | undefined;
  return e && typeof e === "object" && e.message ? String(e.message) : undefined;
}

function vendorKind(vendor: string): TraceKind | null {
  if (vendor === "anthropic") return "claude";
  if (vendor === "meshy" || vendor === "runpod") return vendor;
  return null;
}

const MESHY_FILE = /^(concept_r\d+c\d+\.png|mesh_m\d+\.glb|thumb_m\d+_[a-z]+\.png|baseline_(?!x2d)[a-z]+\.3mf)$/;
const IMAGE = /\.(png|jpe?g|webp|gif)$/i;

/**
 * Meshy task id -> files downloaded from it, from what the attempt recorded (the same matching
 * as the worker backfill): concept candidates, mesh rounds, else the attempt's single task.
 */
export function taskFiles(m: ManifestLike): Map<string, string[]> {
  const out = new Map<string, string[]>();
  const add = (id: unknown, files: unknown[]) => {
    if (typeof id !== "string") return;
    const fs = files.filter((f): f is string => typeof f === "string");
    if (fs.length) out.set(id, [...new Set([...(out.get(id) ?? []), ...fs])]);
  };
  const steps = (m.data?.steps ?? {}) as Record<string, unknown>;
  const keys = (m.data?.meshy_keys ?? {}) as Record<string, string>;
  for (const [k, v] of Object.entries(steps)) {
    if (/^r\d+\.candidates$/.test(k) && Array.isArray(v)) {
      for (const c of v as { meshy_task_id?: string; image?: string }[]) add(c?.meshy_task_id, [c?.image]);
    }
    if (/^m\d+\.mesh$/.test(k) && v && typeof v === "object") {
      const mesh = v as { key?: string; glb?: string; thumbnails?: string[] };
      if (mesh.key && keys[mesh.key]) add(keys[mesh.key], [mesh.glb, ...(mesh.thumbnails ?? [])]);
    }
  }
  const tasks = m.meshy_tasks ?? [];
  if (tasks.length === 1 && !out.has(tasks[0].task_id)) {
    add(tasks[0].task_id, (m.outputs ?? []).map((o) => o.path).filter((p) => MESHY_FILE.test(p.split("/").pop() ?? "")));
  }
  return out;
}

/**
 * Trace entries of one attempt merged with the calls only its manifest knows about. `jobFiles`
 * (task id -> files, across the job) lets a derived task show the images behind its
 * input_task_id.
 */
export function attemptCalls(jobId: string, m: ManifestLike, jobFiles?: Map<string, string[]>): TraceEntry[] {
  const files = taskFiles(m);
  const photos = (m.inputs ?? []).filter((i) => i.role === "photo").map((i) => i.path);
  const raw = (m.data?.trace as TraceEntry[] | undefined) ?? [];
  const entries: TraceEntry[] = raw
    .map((e) => ({ ...e, summary: e.summary ?? "", images: e.images ?? [], files: e.files ?? [] }))
    .sort((a, b) => a.seq - b.seq);
  const covered = new Set(entries.map((e) => e.ref).filter(Boolean) as string[]);
  const derived: TraceEntry[] = [];
  for (const mc of m.model_calls ?? []) {
    const kind = vendorKind(mc.vendor);
    if (!kind || (mc.request_id && covered.has(mc.request_id))) continue;
    derived.push({
      seq: 0,
      kind,
      ref: mc.request_id ?? null,
      path: null,
      model: mc.model,
      purpose: mc.purpose,
      status: "ok",
      started_at: mc.started_at,
      duration_s: mc.duration_s ?? null,
      credits: mc.credits ?? 0,
      usd: mc.usd ?? 0,
      usd_list: mc.usd_list ?? 0,
      summary: "",
      images: kind === "claude" && mc.purpose.startsWith("intake") ? photos : [],
      files: [],
      derived: true,
    });
  }
  for (const t of m.meshy_tasks ?? []) {
    if (covered.has(t.task_id)) continue;
    const req = { ...(t.request ?? {}) } as Record<string, unknown>;
    let text: string | null = null;
    for (const k of MESHY_TEXT) {
      if (typeof req[k] === "string") {
        text = req[k] as string;
        delete req[k];
        break;
      }
    }
    const images: string[] = [];
    for (const k of MESHY_IMAGES) {
      const v = req[k];
      delete req[k];
      for (const x of Array.isArray(v) ? v : v == null ? [] : [v]) {
        if (typeof x !== "string") continue;
        // a data URI (runs without Supabase) cannot be matched here; the backfill matches it
        images.push(x.startsWith("data:") ? `unresolved:${x.slice(5).split(";")[0]}` : jobPath(x, jobId));
      }
    }
    const input = typeof req.input_task_id === "string" ? req.input_task_id : null;
    if (input) images.push(...(jobFiles?.get(input) ?? []).filter((f) => IMAGE.test(f)));
    const status = t.status === "SUCCEEDED" ? "ok" : TERMINAL.has(t.status) ? "error" : "pending";
    const credits = t.credits ?? 0;
    derived.push({
      seq: 0,
      kind: "meshy",
      ref: t.task_id,
      path: null,
      model: meshyLabel(t.endpoint, t.request),
      purpose: t.purpose,
      status,
      started_at: t.created_at,
      duration_s: t.finished_at ? seconds(t.created_at, t.finished_at) : null,
      credits,
      usd: round6(credits * USD_PER_MESHY_CREDIT),
      usd_list: 0,
      summary: (text ?? "").slice(0, 160),
      images: [...new Set(images)],
      files: files.get(t.task_id) ?? [],
      error: status === "error" ? taskError(t.result) ?? t.status : undefined,
      derived: true,
      detail: { text, params: req },
    });
  }
  derived.sort((a, b) => (ms(a.started_at) || 0) - (ms(b.started_at) || 0));
  return [...entries, ...derived];
}

/** Slicer numbers from s07's manifest: the gate step's report, else the summary keys. */
export function slicerSummary(m: ManifestLike): SlicerSummary | null {
  const data = (m.data ?? {}) as Record<string, unknown>;
  const steps = (data.steps ?? {}) as Record<string, unknown>;
  const rep = ((steps.gate as Record<string, unknown> | undefined)?.report ?? null) as Record<string, unknown> | null;
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  const len = (v: unknown) => (Array.isArray(v) ? v.length : 0);
  if (rep && typeof rep === "object") {
    return {
      ok: rep.ok === true,
      print_time_s: num(rep.print_time_s),
      total_grams: num(rep.total_grams),
      purge_grams: num(rep.purge_grams),
      slots_used: num(rep.slots_used),
      errors: len(rep.errors),
      warnings: len(rep.warnings),
      version: typeof rep.slicer_version === "string" ? rep.slicer_version : null,
    };
  }
  if (typeof data.slice_ok === "boolean") {
    return {
      ok: data.slice_ok,
      print_time_s: num(data.print_time_s),
      total_grams: num(data.total_grams),
      purge_grams: null,
      slots_used: null,
      errors: 0,
      warnings: 0,
      version: null,
    };
  }
  return null;
}

/**
 * Time in stages: the summed durations of the attempts that count (not skipped, not stale), gate
 * waits included. A rerun hours later adds its own minutes, not the gap before it.
 */
export function wallSeconds(runs: Pick<RunLike, "status" | "started_at" | "finished_at">[]): number | null {
  const spans = runs
    .filter((r) => r.status !== "skipped" && r.status !== "stale")
    .map((r) => seconds(r.started_at, r.finished_at))
    .filter((s): s is number => s != null);
  return spans.length ? spans.reduce((a, b) => a + b, 0) : null;
}

function byJob<R extends { job_id: string }>(runs: R[]): Map<string, R[]> {
  const out = new Map<string, R[]>();
  for (const r of runs) out.set(r.job_id, [...(out.get(r.job_id) ?? []), r]);
  return out;
}

export function jobRows(
  jobs: JobLike[],
  runs: Pick<RunLike, "job_id" | "status" | "started_at" | "finished_at" | "credits" | "usd">[],
): TraceJobRow[] {
  const runsOf = byJob(runs);
  return jobs
    .map((j) => {
      const rs = runsOf.get(j.id) ?? [];
      return {
        id: j.id,
        name: jobName(j),
        prompt: j.prompt,
        product_type: j.product_type,
        status: j.status,
        stage: (j.stage ?? null) as StageId | null,
        credits: j.spent_credits ?? rs.reduce((s, r) => s + (r.credits ?? 0), 0),
        usd: j.spent_usd ?? rs.reduce((s, r) => s + (r.usd ?? 0), 0),
        wall_s: wallSeconds(rs),
        created_at: j.created_at,
      };
    })
    .sort((a, b) => ms(b.created_at) - ms(a.created_at));
}

function shortValue(v: unknown): unknown {
  if (v == null || typeof v !== "object") return v;
  const s = JSON.stringify(v);
  return s.length > 400 ? `${s.slice(0, 400)}...` : v;
}

export function attemptView(run: RunLike, jobFiles?: Map<string, string[]>): AttemptView {
  const m = run.manifest ?? {};
  const calls = attemptCalls(run.job_id, m, jobFiles);
  const data = (m.data ?? {}) as Record<string, unknown>;
  return {
    key: `${run.stage}-a${run.attempt}`,
    stage: run.stage,
    attempt: run.attempt,
    status: run.status,
    stale: run.status === "stale",
    stale_reason: typeof data.stale_reason === "string" ? data.stale_reason : null,
    started_at: run.started_at,
    finished_at: run.finished_at,
    duration_s: m.duration_s ?? seconds(run.started_at, run.finished_at),
    credits: run.credits ?? m.credits ?? 0,
    usd: run.usd ?? m.usd ?? 0,
    usd_list: round6(calls.reduce((s, c) => s + (c.usd_list ?? 0), 0)),
    calls,
    outputs: (m.outputs ?? []).map((o) => ({ path: o.path, role: o.role ?? null, kind: o.kind ?? null, bytes: o.bytes ?? null })),
    decisions: (m.decisions ?? [])
      .filter((d) => !d.key.startsWith("meshy.get."))
      .map((d) => ({ key: d.key, value: shortValue(d.value), by: d.by, reason: d.reason ?? null })),
    error: m.error?.message ?? null,
    pause_reason: m.pause_reason ?? null,
    slicer: run.stage === "s07_baseline" || run.stage === "s10_write" ? slicerSummary(m) : null,
  };
}

const ADDON_WORDS: Record<string, string> = {
  round_base: "Round base",
  oval_base: "Oval base",
  keyring_loop: "Keyring loop",
  hanging_loop: "Hanging loop",
  nameplate: "Name plate",
  flat_back: "Flat back",
  magnet_pocket: "Magnet pocket",
};

export function jobTrace(job: JobLike, runs: RunLike[]): JobTrace {
  const sorted = [...runs].sort((a, b) => stageNo(a.stage) - stageNo(b.stage) || a.attempt - b.attempt);
  const live = sorted.filter((r) => r.status !== "skipped");
  const jobFiles = new Map<string, string[]>();
  for (const r of live) for (const [id, fs] of taskFiles(r.manifest ?? {})) jobFiles.set(id, fs);
  const attempts = live.map((r) => attemptView(r, jobFiles));
  const liveStages = new Set(live.map((r) => r.stage));
  const skipped = STAGES.map((s) => s.id).filter(
    (id) => !liveStages.has(id) && sorted.some((r) => r.stage === id && r.status === "skipped"),
  );
  const calls = attempts.flatMap((a) => a.calls).filter((c) => c.kind !== "slicer");
  const name = (job.addons ?? []).map((a) => a.text?.text?.trim()).find(Boolean) ?? null;
  return {
    job: {
      id: job.id,
      name: jobName(job),
      prompt: job.prompt,
      product_type: job.product_type,
      status: job.status,
      stage: (job.stage ?? null) as StageId | null,
      created_at: job.created_at,
      threemf: job.result?.threemf ?? null,
    },
    request: {
      prompt: job.prompt,
      product_type: job.product_type,
      size_mm: job.target_size_mm ?? null,
      name_text: name,
      addons: (job.addons ?? []).map((a) => ADDON_WORDS[a.kind] ?? a.kind),
      photos: (job.photos ?? []).map((p) => p.path),
    },
    attempts,
    skipped,
    totals: {
      credits: attempts.reduce((s, a) => s + a.credits, 0),
      usd: round6(attempts.reduce((s, a) => s + a.usd, 0)),
      usd_list: round6(attempts.reduce((s, a) => s + a.usd_list, 0)),
      wall_s: wallSeconds(runs),
      calls: calls.length,
    },
  };
}

/** Every model call (Claude, Meshy, Runpod; not the slicer) across jobs, newest first. */
export function callRows(jobs: Pick<JobLike, "id" | "title" | "addons" | "prompt">[], runs: RunLike[]): CallRow[] {
  const names = new Map(jobs.map((j) => [j.id, jobName(j)]));
  const out: CallRow[] = [];
  for (const r of runs) {
    if (r.status === "skipped") continue;
    for (const c of attemptCalls(r.job_id, r.manifest ?? {})) {
      if (!MODEL_KINDS.includes(c.kind)) continue;
      out.push({
        at: c.started_at ?? r.started_at,
        job_id: r.job_id,
        job_name: names.get(r.job_id) ?? r.job_id.slice(0, 8),
        stage: r.stage,
        attempt: r.attempt,
        kind: c.kind,
        model: c.model,
        purpose: c.purpose,
        duration_s: c.duration_s,
        credits: c.credits ?? 0,
        usd: c.usd ?? 0,
        usd_list: c.usd_list ?? 0,
        status: c.status,
        derived: !!c.derived,
      });
    }
  }
  return out.sort((a, b) => (ms(b.at) || 0) - (ms(a.at) || 0));
}

export interface CallFilter {
  days?: number | null;
  vendor?: string | null;
  q?: string | null;
  now?: number;
}

export function filterCalls(rows: CallRow[], f: CallFilter): CallRow[] {
  const since = f.days ? (f.now ?? Date.now()) - f.days * 86_400_000 : null;
  const q = f.q?.trim().toLowerCase();
  const vendor = f.vendor && f.vendor !== "all" ? f.vendor : null;
  return rows.filter(
    (r) =>
      (since == null || ms(r.at) >= since) &&
      (!vendor || r.kind === vendor) &&
      (!q || `${r.model} ${r.purpose} ${r.job_name}`.toLowerCase().includes(q)),
  );
}

export function spendSummary(rows: CallRow[]): SpendSummary {
  const by = new Map<string, ModelSpend>();
  for (const r of rows) {
    const m = by.get(r.model) ?? { model: r.model, calls: 0, credits: 0, usd: 0, usd_list: 0 };
    m.calls += 1;
    m.credits += r.credits;
    m.usd = round6(m.usd + r.usd);
    m.usd_list = round6(m.usd_list + r.usd_list);
    by.set(r.model, m);
  }
  return {
    usd: round6(rows.reduce((s, r) => s + r.usd, 0)),
    usd_list: round6(rows.reduce((s, r) => s + r.usd_list, 0)),
    credits: rows.reduce((s, r) => s + r.credits, 0),
    calls: rows.length,
    by_model: [...by.values()].sort((a, b) => b.usd - a.usd || b.usd_list - a.usd_list || b.calls - a.calls),
  };
}
