// Where the trace viewer reads from. Read-only: selects, Storage download and sign, nothing else.
//
// - supabase: the service role (same client as SupabaseServerStore.fromEnv). Cross-job queries
//   select JSON subpaths only (manifest->model_calls, manifest->meshy_tasks,
//   manifest->data->trace), never whole manifests; one job's page reads its own stage_runs.
// - fixture: the two real jobs after the backfill (lib/traces/fixtures/), in mock mode, local
//   development without the service role, and tests.
// Every JSON response goes through redact() with the server's own secret values.

import fs from "node:fs";
import path from "node:path";
import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import type { StageId } from "../contracts.gen";
import { redact } from "../traces/redact";
import type { JobLike, ManifestLike, RunLike } from "../traces/types";
import { hasServiceStore, serverEnv } from "./env";
import { StoreUnavailable } from "./store";

export type TraceBucket = "inputs" | "artifacts";
export type FileResult = { redirect: string } | { body: Uint8Array | string; type: string } | null;
type RunSummary = Pick<RunLike, "job_id" | "stage" | "attempt" | "status" | "started_at" | "finished_at" | "usd" | "credits">;

export interface TraceSource {
  readonly kind: "supabase" | "fixture";
  jobs(): Promise<{ jobs: JobLike[]; runs: RunSummary[] }>;
  job(id: string): Promise<{ job: JobLike; runs: RunLike[] } | null>;
  manifest(id: string, stage: StageId, attempt: number): Promise<ManifestLike | null>;
  body(id: string, path: string): Promise<unknown | null>;
  calls(days: number | null): Promise<{ jobs: JobLike[]; runs: RunLike[] }>;
  file(id: string, bucket: TraceBucket, path: string): Promise<FileResult>;
}

export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const BODY_PATH = /^s\d\d_[a-z0-9_]+\/a\d+\/trace\/[\w.-]+\.json$/;
export const SIGNED_URL_TTL_S = 300;

// ------------------------------------------------------------------ supabase

function ok<T>(res: { data: unknown; error: { message: string } | null }): T {
  if (res.error) throw new Error(res.error.message);
  return res.data as T;
}

const JOB_LIST = "id,created_at,updated_at,title,prompt,product_type,status,stage,spent_usd,spent_credits,addons";
const CALL_COLS =
  "job_id,stage,attempt,status,started_at,finished_at," +
  "model_calls:manifest->model_calls,meshy_tasks:manifest->meshy_tasks,trace:manifest->data->trace";

export class SupabaseTraceSource implements TraceSource {
  readonly kind = "supabase" as const;
  constructor(private sb: SupabaseClient) {}

  static fromEnv(): SupabaseTraceSource {
    const env = serverEnv();
    return new SupabaseTraceSource(
      createClient(env.supabaseUrl!, env.serviceRoleKey!, {
        auth: { persistSession: false, autoRefreshToken: false },
      }),
    );
  }

  async jobs() {
    const jobs = ok<JobLike[]>(
      await this.sb.from("jobs").select(JOB_LIST).order("created_at", { ascending: false }).limit(500),
    );
    const ids = jobs.map((j) => j.id);
    const runs = ids.length
      ? ok<RunSummary[]>(
          await this.sb
            .from("stage_runs")
            .select("job_id,stage,attempt,status,started_at,finished_at,usd,credits")
            .in("job_id", ids),
        )
      : [];
    return { jobs, runs };
  }

  async job(id: string) {
    const job = ok<JobLike | null>(await this.sb.from("jobs").select("*").eq("id", id).maybeSingle());
    if (!job) return null;
    const runs = ok<RunLike[]>(
      await this.sb
        .from("stage_runs")
        .select("job_id,stage,attempt,status,started_at,finished_at,usd,credits,manifest")
        .eq("job_id", id),
    );
    return { job, runs };
  }

  async manifest(id: string, stage: StageId, attempt: number) {
    const row = ok<{ manifest: ManifestLike } | null>(
      await this.sb
        .from("stage_runs")
        .select("manifest")
        .eq("job_id", id)
        .eq("stage", stage)
        .eq("attempt", attempt)
        .maybeSingle(),
    );
    return row?.manifest ?? null;
  }

  async body(id: string, p: string) {
    const { data, error } = await this.sb.storage.from("artifacts").download(`${id}/${p}`);
    if (error || !data) return null;
    try {
      return JSON.parse(await data.text()) as unknown;
    } catch {
      return null;
    }
  }

  async calls(days: number | null) {
    let q = this.sb.from("stage_runs").select(CALL_COLS).neq("status", "skipped");
    if (days) q = q.gte("updated_at", new Date(Date.now() - days * 86_400_000).toISOString());
    const rows = ok<
      (RunSummary & { model_calls: unknown; meshy_tasks: unknown; trace: unknown })[]
    >(await q.order("started_at", { ascending: false }).limit(5000));
    const runs: RunLike[] = rows.map((r) => ({
      job_id: r.job_id,
      stage: r.stage,
      attempt: r.attempt,
      status: r.status,
      started_at: r.started_at,
      finished_at: r.finished_at,
      manifest: {
        model_calls: (r.model_calls as ManifestLike["model_calls"]) ?? [],
        meshy_tasks: (r.meshy_tasks as ManifestLike["meshy_tasks"]) ?? [],
        data: { trace: r.trace ?? [] },
      },
    }));
    const ids = [...new Set(runs.map((r) => r.job_id))];
    const jobs = ids.length
      ? ok<JobLike[]>(await this.sb.from("jobs").select("id,created_at,title,prompt,product_type,status,addons").in("id", ids))
      : [];
    return { jobs, runs };
  }

  async file(id: string, bucket: TraceBucket, p: string): Promise<FileResult> {
    // Photos: the contract key <job>/input/photo_1.jpg, else the older <job>/photo_1.jpg.
    const keys = [`${id}/${p}`];
    if (bucket === "inputs") keys.push(`${id}/${p.split("/").pop()}`);
    for (const key of [...new Set(keys)]) {
      const { data, error } = await this.sb.storage.from(bucket).createSignedUrl(key, SIGNED_URL_TTL_S);
      if (!error && data?.signedUrl) return { redirect: data.signedUrl };
    }
    return null;
  }
}

// ------------------------------------------------------------------ fixtures

interface Fixture {
  job: JobLike;
  stage_runs: RunLike[];
  bodies: Record<string, unknown>;
  docs: Record<string, string>;
  images: Record<string, string>;
}

// The fixture reads are marked turbopackIgnore: fixtures serve mock mode, development and tests
// only, so a production build must not trace them (or, through the dynamic paths, the whole
// project) into the server bundle.
export function fixturesDir(): string {
  const cands = [
    path.join(/*turbopackIgnore: true*/ process.cwd(), "lib", "traces", "fixtures"),
    path.join(/*turbopackIgnore: true*/ process.cwd(), "apps", "web", "lib", "traces", "fixtures"),
  ];
  return cands.find((d) => fs.existsSync(/*turbopackIgnore: true*/ d)) ?? cands[0];
}

let fixtureCache: Fixture[] | null = null;

export function loadFixtures(): Fixture[] {
  if (fixtureCache) return fixtureCache;
  const dir = fixturesDir();
  const files = fs.existsSync(/*turbopackIgnore: true*/ dir)
    ? fs.readdirSync(/*turbopackIgnore: true*/ dir).filter((f) => /^[0-9a-f-]{36}\.json$/.test(f))
    : [];
  fixtureCache = files.map(
    (f) => JSON.parse(fs.readFileSync(/*turbopackIgnore: true*/ path.join(dir, f), "utf8")) as Fixture,
  );
  return fixtureCache;
}

export class FixtureTraceSource implements TraceSource {
  readonly kind = "fixture" as const;
  constructor(private fixtures: Fixture[] = loadFixtures()) {}

  private find(id: string) {
    return this.fixtures.find((f) => f.job.id === id) ?? null;
  }

  async jobs() {
    return { jobs: this.fixtures.map((f) => f.job), runs: this.fixtures.flatMap((f) => f.stage_runs) };
  }

  async job(id: string) {
    const f = this.find(id);
    return f ? { job: f.job, runs: f.stage_runs } : null;
  }

  async manifest(id: string, stage: StageId, attempt: number) {
    return this.find(id)?.stage_runs.find((r) => r.stage === stage && r.attempt === attempt)?.manifest ?? null;
  }

  async body(id: string, p: string) {
    return this.find(id)?.bodies[p] ?? null;
  }

  /** All runs; the route filters calls by their own time. */
  async calls() {
    return { jobs: this.fixtures.map((f) => f.job), runs: this.fixtures.flatMap((f) => f.stage_runs) };
  }

  async file(id: string, _bucket: TraceBucket, p: string): Promise<FileResult> {
    const f = this.find(id);
    if (!f) return null;
    const img = f.images[p];
    if (img) {
      const dir = fixturesDir();
      const full = path.join(/*turbopackIgnore: true*/ dir, img);
      if (!full.startsWith(dir) || !fs.existsSync(/*turbopackIgnore: true*/ full)) return null;
      return { body: new Uint8Array(fs.readFileSync(/*turbopackIgnore: true*/ full)), type: "image/jpeg" };
    }
    const doc = f.docs[p];
    if (doc != null) {
      return { body: doc, type: p.endsWith(".json") ? "application/json" : "text/plain; charset=utf-8" };
    }
    return null;
  }
}

// ------------------------------------------------------------------ choice, responses

/** The service role when configured; fixtures in mock mode, development and tests. */
export function getTraceSource(): TraceSource {
  if (hasServiceStore()) return SupabaseTraceSource.fromEnv();
  const env = serverEnv();
  if (env.mock || process.env.NODE_ENV !== "production") return new FixtureTraceSource();
  throw new StoreUnavailable();
}

/** Secret values this server holds; never echoed even if a manifest somehow carried one. */
export function traceSecrets(): string[] {
  const keys = ["SUPABASE_SERVICE_ROLE_KEY", "ACCESS_CODE", "ACCESS_COOKIE_SECRET", "AGENTMAIL_API_KEY", "NEXT_PUBLIC_SUPABASE_ANON_KEY"];
  return keys.map((k) => process.env[k]?.trim() ?? "").filter((v) => v.length >= 8);
}

export function traceJson(body: unknown, status = 200): Response {
  return Response.json(redact(body, traceSecrets()), { status, headers: { "cache-control": "no-store" } });
}
