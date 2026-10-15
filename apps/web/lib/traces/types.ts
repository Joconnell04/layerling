// Trace viewer types. The worker writes one body per model, vendor or slicer call
// (jobs/<id>/<stage>/a<N>/trace/<seq>-<kind>.json, Storage `artifacts`) and an index entry in
// manifest.data.trace (worker/src/p2p_worker/trace.py, docs/plans/telemetry.md).

import type { StageId } from "../contracts.gen";

export type TraceKind = "claude" | "meshy" | "slicer" | "runpod";

/** One entry of manifest.data.trace, or a call derived from model_calls / meshy_tasks. */
export interface TraceEntry {
  seq: number;
  kind: TraceKind;
  ref: string | null;
  /** Job-relative body path; null for derived calls. */
  path: string | null;
  model: string;
  purpose: string;
  status: string;
  started_at: string | null;
  duration_s: number | null;
  credits: number;
  usd: number;
  usd_list: number;
  summary: string;
  images: string[];
  files: string[];
  error?: string;
  reconstructed?: boolean;
  /** Built from the manifest because no trace entry covers it (older jobs, pending tasks). */
  derived?: boolean;
  /** Derived calls only: what the manifest holds in place of a body. */
  detail?: { text?: string | null; params?: Record<string, unknown> };
}

export interface TraceBody {
  v: number;
  job_id: string;
  stage: StageId;
  attempt: number;
  seq: number;
  kind: TraceKind;
  vendor: string;
  model: string;
  purpose: string;
  ref: string | null;
  status: string;
  error: string | null;
  started_at: string | null;
  duration_s: number | null;
  credits: number;
  usd: number;
  usd_list: number;
  billing: string | null;
  test_mode?: boolean;
  tokens: { in: number; out: number; cache_read: number; cache_write: number };
  input: {
    system: string | null;
    text: string | null;
    images: string[];
    params: Record<string, unknown>;
    system_note?: string;
    [k: string]: unknown;
  };
  output: { text: string | null; json: unknown; files: string[]; [k: string]: unknown };
  rounds: unknown[];
  reconstructed: boolean;
}

/** The manifest fields the viewer reads. Loose on purpose: old manifests predate some keys. */
export interface ManifestLike {
  job_id?: string;
  stage?: StageId;
  attempt?: number;
  status?: string;
  started_at?: string | null;
  finished_at?: string | null;
  duration_s?: number | null;
  inputs?: FileRefLike[];
  outputs?: FileRefLike[];
  model_calls?: ModelCallLike[];
  meshy_tasks?: MeshyTaskLike[];
  credits?: number;
  usd?: number;
  decisions?: { key: string; value: unknown; reason?: string | null; by: string; model?: string | null; at?: string }[];
  pause_reason?: string | null;
  error?: { code?: string; message: string } | null;
  data?: Record<string, unknown>;
}

export interface FileRefLike {
  path: string;
  role?: string | null;
  kind?: string | null;
  bytes?: number | null;
}

export interface ModelCallLike {
  vendor: string;
  model: string;
  purpose: string;
  started_at: string;
  duration_s: number;
  input_tokens?: number;
  output_tokens?: number;
  credits?: number;
  usd?: number;
  usd_list?: number;
  billing?: string;
  request_id?: string | null;
}

export interface MeshyTaskLike {
  task_id: string;
  endpoint: string;
  purpose: string;
  status: string;
  test_mode?: boolean;
  credits?: number;
  created_at: string;
  finished_at?: string | null;
  request?: Record<string, unknown>;
  result?: Record<string, unknown> | null;
}

/** A stage_runs row as the viewer reads it. */
export interface RunLike {
  job_id: string;
  stage: StageId;
  attempt: number;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  usd?: number;
  credits?: number;
  manifest: ManifestLike;
}

/** The jobs-row fields the viewer reads. */
export interface JobLike {
  id: string;
  created_at: string;
  updated_at?: string;
  title?: string | null;
  prompt: string;
  product_type: string;
  status: string;
  stage?: StageId | null;
  target_size_mm?: number | null;
  addons?: { kind: string; text?: { text?: string | null } | null; params?: Record<string, unknown> | null }[];
  photos?: { path: string }[];
  spent_usd?: number;
  spent_credits?: number;
  result?: { threemf?: string | null; slice_ok?: boolean | null; print_time_s?: number | null } | null;
  review_reason?: string | null;
  pause_reason?: string | null;
}

// ------------------------------------------------------------------ API shapes

export interface TraceJobRow {
  id: string;
  name: string;
  prompt: string;
  product_type: string;
  status: string;
  stage: StageId | null;
  credits: number;
  usd: number;
  wall_s: number | null;
  created_at: string;
}

export interface SlicerSummary {
  ok: boolean;
  print_time_s: number | null;
  total_grams: number | null;
  purge_grams: number | null;
  slots_used: number | null;
  errors: number;
  warnings: number;
  version: string | null;
}

export interface AttemptView {
  key: string;
  stage: StageId;
  attempt: number;
  status: string;
  stale: boolean;
  stale_reason: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_s: number | null;
  credits: number;
  usd: number;
  usd_list: number;
  calls: TraceEntry[];
  outputs: FileRefLike[];
  decisions: { key: string; value: unknown; by: string; reason: string | null }[];
  error: string | null;
  pause_reason: string | null;
  slicer: SlicerSummary | null;
}

export interface JobTrace {
  job: {
    id: string;
    name: string;
    prompt: string;
    product_type: string;
    status: string;
    stage: StageId | null;
    created_at: string;
    threemf: string | null;
  };
  request: {
    prompt: string;
    product_type: string;
    size_mm: number | null;
    name_text: string | null;
    addons: string[];
    photos: string[];
  };
  attempts: AttemptView[];
  skipped: StageId[];
  totals: { credits: number; usd: number; usd_list: number; wall_s: number | null; calls: number };
}

export interface CallRow {
  at: string | null;
  job_id: string;
  job_name: string;
  stage: StageId;
  attempt: number;
  kind: TraceKind;
  model: string;
  purpose: string;
  duration_s: number | null;
  credits: number;
  usd: number;
  usd_list: number;
  status: string;
  derived: boolean;
}

export interface ModelSpend {
  model: string;
  calls: number;
  credits: number;
  usd: number;
  usd_list: number;
}

export interface SpendSummary {
  usd: number;
  usd_list: number;
  credits: number;
  calls: number;
  by_model: ModelSpend[];
}

export interface CallsResponse {
  calls: CallRow[];
  summary: SpendSummary;
}
