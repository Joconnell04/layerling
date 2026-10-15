// Server-side data store for the API routes. `supabase` uses the service role key (never sent to
// the browser). `memory` backs local mock mode and tests.

import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import type { MessagePayloadMap } from "../contracts.gen";
import type {
  AnyMessage,
  FilamentRow,
  JobRow,
  MessageRow,
  OrderRow,
  SpendRow,
  StageRunRow,
  WorkerRow,
} from "../db.types";
import { missingOptionalColumn, normalizeJob, normalizeRun, withoutOptionalColumns } from "../data/normalize";
import { hasServiceStore, serverEnv } from "./env";

export type Bucket = "orders" | "inputs" | "artifacts" | "swatches";
export type NewOrder = Omit<OrderRow, "created_at" | "updated_at" | "notified_at" | "notify_error" | "job_id" | "status"> &
  Partial<Pick<OrderRow, "status">>;
export type NewJob = Omit<JobRow, "created_at" | "updated_at" | "spent_usd" | "spent_credits" | "confidence" | "review_reason" | "pause_reason" | "result" | "stage" | "budget_usd" | "budget_credits" | "settings"> &
  Partial<JobRow>;
export type NewMessage = Omit<AnyMessage, "id" | "created_at" | "consumed_at"> & { id?: string };

export interface UploadTarget {
  key: string;
  url: string;
  method: "PUT";
  headers: Record<string, string>;
}

export interface ServerStore {
  readonly kind: "supabase" | "memory";
  // orders
  insertOrder(o: NewOrder): Promise<OrderRow>;
  getOrder(id: string): Promise<OrderRow | null>;
  listOrders(limit?: number): Promise<OrderRow[]>;
  updateOrder(id: string, patch: Partial<OrderRow>): Promise<OrderRow>;
  // storage
  upload(bucket: Bucket, key: string, data: Blob, contentType: string): Promise<void>;
  download(bucket: Bucket, key: string): Promise<Blob>;
  remove(bucket: Bucket, keys: string[]): Promise<void>;
  signedUrl(bucket: Bucket, key: string, ttlS?: number): Promise<string>;
  /** Signed URL whose response carries Content-Disposition: attachment; filename=<filename>. */
  signedDownloadUrl(bucket: Bucket, key: string, filename: string): Promise<string | null>;
  /** A URL the browser can PUT the file to directly (bypasses the 4.5 MB function body cap). */
  createUploadUrl(bucket: Bucket, key: string, contentType: string): Promise<UploadTarget>;
  /** Objects directly under `<prefix>/`, with sizes. */
  listObjects(bucket: Bucket, prefix: string): Promise<{ key: string; size: number }[]>;
  // studio
  listJobs(limit?: number): Promise<JobRow[]>;
  getJob(id: string): Promise<JobRow | null>;
  insertJob(j: NewJob): Promise<JobRow>;
  updateJob(id: string, patch: Partial<JobRow>): Promise<JobRow>;
  listStageRuns(jobId: string): Promise<StageRunRow[]>;
  listMessages(jobId: string): Promise<AnyMessage[]>;
  listMessagesByKind<K extends keyof MessagePayloadMap>(jobIds: string[], kind: K): Promise<MessageRow<K>[]>;
  insertMessage(m: NewMessage): Promise<AnyMessage>;
  listSpend(jobId?: string): Promise<SpendRow[]>;
  listFilaments(): Promise<FilamentRow[]>;
  getFilament(id: string): Promise<FilamentRow | null>;
  upsertFilament(f: FilamentRow, isNew: boolean): Promise<FilamentRow>;
  listWorkers(): Promise<WorkerRow[]>;
  // first look: spend rows as reservations, Meshy webhook events (written by the edge function)
  insertSpend(s: NewSpend): Promise<SpendRow>;
  deleteSpend(id: number): Promise<void>;
  listSpendByKind(kind: string, opts?: { ref?: string; since?: string }): Promise<SpendRow[]>;
  listMeshyEvents(taskId: string): Promise<MeshyEventRow[]>;
}

export type NewSpend = Omit<SpendRow, "id" | "at" | "owner_id" | "job_id" | "stage"> & Partial<SpendRow>;

/** public.meshy_events (20260923000001_init.sql). */
export interface MeshyEventRow {
  id: number;
  received_at: string;
  task_id: string;
  status: string | null;
  event: { status?: string; image_urls?: string[]; progress?: number; [k: string]: unknown };
  processed_at: string | null;
}

// ------------------------------------------------------------------ supabase (service role)

function ok<T>(res: { data: unknown; error: { message: string } | null }): T {
  if (res.error) throw new Error(res.error.message);
  return res.data as T;
}

export class SupabaseServerStore implements ServerStore {
  readonly kind = "supabase" as const;
  constructor(private sb: SupabaseClient) {}

  static fromEnv(): SupabaseServerStore {
    const env = serverEnv();
    return new SupabaseServerStore(
      createClient(env.supabaseUrl!, env.serviceRoleKey!, {
        auth: { persistSession: false, autoRefreshToken: false },
      }),
    );
  }

  async insertOrder(o: NewOrder) {
    return ok<OrderRow>(await this.sb.from("orders").insert(o).select("*").single());
  }
  async getOrder(id: string) {
    return ok<OrderRow | null>(await this.sb.from("orders").select("*").eq("id", id).maybeSingle());
  }
  async listOrders(limit = 200) {
    return ok<OrderRow[]>(
      await this.sb.from("orders").select("*").order("created_at", { ascending: false }).limit(limit),
    );
  }
  async updateOrder(id: string, patch: Partial<OrderRow>) {
    return ok<OrderRow>(await this.sb.from("orders").update(patch).eq("id", id).select("*").single());
  }

  async upload(bucket: Bucket, key: string, data: Blob, contentType: string) {
    const { error } = await this.sb.storage.from(bucket).upload(key, data, { contentType, upsert: true });
    if (error) throw new Error(`upload ${bucket}: ${error.message}`);
  }
  async download(bucket: Bucket, key: string) {
    const { data, error } = await this.sb.storage.from(bucket).download(key);
    if (error || !data) throw new Error(`download ${bucket}: ${error?.message ?? "empty"}`);
    return data;
  }
  async remove(bucket: Bucket, keys: string[]) {
    if (keys.length) await this.sb.storage.from(bucket).remove(keys);
  }
  async signedUrl(bucket: Bucket, key: string, ttlS = 3600) {
    const { data, error } = await this.sb.storage.from(bucket).createSignedUrl(key, ttlS);
    if (error || !data) throw new Error(`sign ${bucket}: ${error?.message ?? "empty"}`);
    return data.signedUrl;
  }
  async signedDownloadUrl(bucket: Bucket, key: string, filename: string) {
    const { data, error } = await this.sb.storage.from(bucket).createSignedUrl(key, 600, { download: filename });
    if (error || !data) throw new Error(`sign ${bucket}: ${error?.message ?? "empty"}`);
    return data.signedUrl;
  }
  async createUploadUrl(bucket: Bucket, key: string, contentType: string): Promise<UploadTarget> {
    const { data, error } = await this.sb.storage.from(bucket).createSignedUploadUrl(key, { upsert: true });
    if (error || !data) throw new Error(`sign upload ${bucket}: ${error?.message ?? "empty"}`);
    const headers: Record<string, string> = { "content-type": contentType, "x-upsert": "true" };
    // The publishable key is public; the storage gateway may ask for it alongside the token.
    const anon = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? process.env.SUPABASE_ANON_KEY;
    if (anon) headers.apikey = anon;
    return { key, url: data.signedUrl, method: "PUT", headers };
  }
  async listObjects(bucket: Bucket, prefix: string) {
    const { data, error } = await this.sb.storage.from(bucket).list(prefix, { limit: 100 });
    if (error) throw new Error(`list ${bucket}: ${error.message}`);
    return (data ?? [])
      .filter((o) => o.id)
      .map((o) => ({ key: `${prefix}/${o.name}`, size: Number((o.metadata as { size?: number } | null)?.size ?? 0) }));
  }

  async listJobs(limit = 200) {
    const rows = ok<Record<string, unknown>[]>(
      await this.sb.from("jobs").select("*").order("created_at", { ascending: false }).limit(limit),
    );
    return rows.map(normalizeJob);
  }
  async getJob(id: string) {
    const row = ok<Record<string, unknown> | null>(await this.sb.from("jobs").select("*").eq("id", id).maybeSingle());
    return row ? normalizeJob(row) : null;
  }
  async insertJob(j: NewJob) {
    let res = await this.sb.from("jobs").insert(j).select("*").single();
    // jobs.form and custom_asks (migration 20260924000011) may not exist yet: insert without them.
    if (res.error && missingOptionalColumn(res.error)) {
      res = await this.sb.from("jobs").insert(withoutOptionalColumns(j as Record<string, unknown>)).select("*").single();
    }
    return normalizeJob(ok(res));
  }
  async updateJob(id: string, patch: Partial<JobRow>) {
    return normalizeJob(ok(await this.sb.from("jobs").update(patch).eq("id", id).select("*").single()));
  }
  async listStageRuns(jobId: string) {
    const rows = ok<Record<string, unknown>[]>(
      await this.sb.from("stage_runs").select("*").eq("job_id", jobId).order("stage").order("attempt"),
    );
    return rows.map(normalizeRun);
  }
  async listMessages(jobId: string) {
    return ok<AnyMessage[]>(
      await this.sb.from("messages").select("*").eq("job_id", jobId).order("created_at").order("id"),
    );
  }
  async listMessagesByKind<K extends keyof MessagePayloadMap>(jobIds: string[], kind: K) {
    if (!jobIds.length) return [];
    return ok<MessageRow<K>[]>(
      await this.sb.from("messages").select("*").in("job_id", jobIds).eq("kind", kind).order("created_at"),
    );
  }
  async insertMessage(m: NewMessage) {
    return ok<AnyMessage>(await this.sb.from("messages").insert(m).select("*").single());
  }
  async listSpend(jobId?: string) {
    let q = this.sb.from("spend").select("*").order("at", { ascending: false }).limit(500);
    if (jobId) q = q.eq("job_id", jobId);
    const rows = ok<SpendRow[]>(await q);
    return rows.map((r) => ({ ...r, usd: Number(r.usd), credits: Number(r.credits) }));
  }
  async listFilaments() {
    return ok<FilamentRow[]>(
      await this.sb.from("filaments").select("*").order("owned", { ascending: false }).order("name"),
    );
  }
  async getFilament(id: string) {
    return ok<FilamentRow | null>(await this.sb.from("filaments").select("*").eq("id", id).maybeSingle());
  }
  async upsertFilament(f: FilamentRow, isNew: boolean) {
    const { updated_at: _u, ...values } = f;
    const q = isNew ? this.sb.from("filaments").insert(values) : this.sb.from("filaments").update(values).eq("id", f.id);
    return ok<FilamentRow>(await q.select("*").single());
  }
  async listWorkers() {
    return ok<WorkerRow[]>(await this.sb.from("workers").select("*").order("last_seen", { ascending: false }));
  }
  async insertSpend(s: NewSpend) {
    return ok<SpendRow>(await this.sb.from("spend").insert(s).select("*").single());
  }
  async deleteSpend(id: number) {
    ok(await this.sb.from("spend").delete().eq("id", id));
  }
  async listSpendByKind(kind: string, opts: { ref?: string; since?: string } = {}) {
    let q = this.sb.from("spend").select("*").eq("kind", kind).order("id").limit(1000);
    if (opts.ref) q = q.eq("ref", opts.ref);
    if (opts.since) q = q.gte("at", opts.since);
    return ok<SpendRow[]>(await q);
  }
  async listMeshyEvents(taskId: string) {
    return ok<MeshyEventRow[]>(
      await this.sb.from("meshy_events").select("*").eq("task_id", taskId).order("received_at", { ascending: false }),
    );
  }
}

// ------------------------------------------------------------------ memory (mock mode, tests)

const uuid = () => crypto.randomUUID();
const now = () => new Date().toISOString();

export class MemoryServerStore implements ServerStore {
  readonly kind = "memory" as const;
  orders = new Map<string, OrderRow>();
  files = new Map<string, { data: Blob; type: string }>();
  jobs = new Map<string, JobRow>();
  runs: StageRunRow[] = [];
  messages: AnyMessage[] = [];
  spend: SpendRow[] = [];
  filaments = new Map<string, FilamentRow>();
  workers: WorkerRow[] = [];

  async insertOrder(o: NewOrder) {
    const row: OrderRow = {
      status: "new",
      notified_at: null,
      notify_error: null,
      job_id: null,
      ...o,
      created_at: now(),
      updated_at: now(),
    };
    this.orders.set(row.id, row);
    return structuredClone(row);
  }
  async getOrder(id: string) {
    const o = this.orders.get(id);
    return o ? structuredClone(o) : null;
  }
  async listOrders() {
    return [...this.orders.values()].sort((a, b) => b.created_at.localeCompare(a.created_at)).map((o) => structuredClone(o));
  }
  async updateOrder(id: string, patch: Partial<OrderRow>) {
    const o = this.orders.get(id);
    if (!o) throw new Error("order not found");
    const next = { ...o, ...patch, updated_at: now() };
    this.orders.set(id, next);
    return structuredClone(next);
  }

  async upload(bucket: Bucket, key: string, data: Blob, contentType: string) {
    this.files.set(`${bucket}/${key}`, { data, type: contentType });
  }
  async download(bucket: Bucket, key: string) {
    const f = this.files.get(`${bucket}/${key}`);
    if (!f) throw new Error(`download ${bucket}: not found`);
    return f.data;
  }
  async remove(bucket: Bucket, keys: string[]) {
    for (const k of keys) this.files.delete(`${bucket}/${k}`);
  }
  async signedUrl(bucket: Bucket, key: string) {
    const f = this.files.get(`${bucket}/${key}`);
    if (!f) return `/mock/render-sheet.svg`;
    const b64 = Buffer.from(await f.data.arrayBuffer()).toString("base64");
    return `data:${f.type};base64,${b64}`;
  }
  /** No signed URLs in memory: the download route streams the bytes itself. */
  async signedDownloadUrl(_bucket: Bucket, _key: string, _filename: string): Promise<string | null> {
    return null;
  }
  async createUploadUrl(bucket: Bucket, key: string, contentType: string): Promise<UploadTarget> {
    const q = new URLSearchParams({ bucket, key });
    return { key, url: `/api/uploads/memory?${q}`, method: "PUT", headers: { "content-type": contentType } };
  }
  async listObjects(bucket: Bucket, prefix: string) {
    const head = `${bucket}/${prefix}/`;
    return [...this.files.entries()]
      .filter(([k]) => k.startsWith(head) && !k.slice(head.length).includes("/"))
      .map(([k, f]) => ({ key: k.slice(bucket.length + 1), size: f.data.size }));
  }

  async listJobs() {
    return [...this.jobs.values()].sort((a, b) => b.created_at.localeCompare(a.created_at)).map((j) => structuredClone(j));
  }
  async getJob(id: string) {
    const j = this.jobs.get(id);
    return j ? structuredClone(j) : null;
  }
  async insertJob(j: NewJob) {
    const row: JobRow = {
      created_at: now(),
      updated_at: now(),
      stage: null,
      settings: {},
      budget_usd: 10,
      budget_credits: 400,
      spent_usd: 0,
      spent_credits: 0,
      confidence: null,
      review_reason: null,
      pause_reason: null,
      result: null,
      ...j,
      id: j.id || uuid(),
    } as JobRow;
    this.jobs.set(row.id, row);
    return structuredClone(row);
  }
  async updateJob(id: string, patch: Partial<JobRow>) {
    const j = this.jobs.get(id);
    if (!j) throw new Error("job not found");
    const next = { ...j, ...patch, updated_at: now() };
    this.jobs.set(id, next);
    return structuredClone(next);
  }
  async listStageRuns(jobId: string) {
    return this.runs.filter((r) => r.job_id === jobId);
  }
  async listMessages(jobId: string) {
    return this.messages.filter((m) => m.job_id === jobId);
  }
  async listMessagesByKind<K extends keyof MessagePayloadMap>(jobIds: string[], kind: K) {
    return this.messages.filter((m) => jobIds.includes(m.job_id) && m.kind === kind) as unknown as MessageRow<K>[];
  }
  async insertMessage(m: NewMessage) {
    const row = { id: uuid(), created_at: now(), consumed_at: null, ...m } as AnyMessage;
    this.messages.push(row);
    return structuredClone(row);
  }
  async listSpend(jobId?: string) {
    return this.spend.filter((s) => !jobId || s.job_id === jobId);
  }
  async listFilaments() {
    return [...this.filaments.values()];
  }
  async getFilament(id: string) {
    return this.filaments.get(id) ?? null;
  }
  async upsertFilament(f: FilamentRow) {
    const row = { ...f, updated_at: now() };
    this.filaments.set(f.id, row);
    return row;
  }
  async listWorkers() {
    return this.workers;
  }
  meshyEvents: MeshyEventRow[] = [];
  private spendSeq = 0;
  async insertSpend(s: NewSpend) {
    const row: SpendRow = { id: ++this.spendSeq, at: now(), owner_id: null, job_id: null, stage: null, ...s };
    this.spend.push(row);
    return structuredClone(row);
  }
  async deleteSpend(id: number) {
    this.spend = this.spend.filter((s) => s.id !== id);
  }
  async listSpendByKind(kind: string, opts: { ref?: string; since?: string } = {}) {
    return this.spend
      .filter((s) => s.kind === kind && (!opts.ref || s.ref === opts.ref) && (!opts.since || s.at >= opts.since))
      .sort((a, b) => a.id - b.id)
      .map((s) => structuredClone(s));
  }
  async listMeshyEvents(taskId: string) {
    return this.meshyEvents.filter((e) => e.task_id === taskId).reverse();
  }
}

const g = globalThis as unknown as { __p2pMemoryStore?: MemoryServerStore };

export class StoreUnavailable extends Error {
  status = 503;
  constructor() {
    super("Storage not configured");
  }
}

/**
 * The service-role store when configured. A process-wide memory store only in mock mode, tests
 * and local development; production without the service role key fails closed.
 */
export function getStore(): ServerStore {
  if (hasServiceStore()) return SupabaseServerStore.fromEnv();
  const env = serverEnv();
  if (env.mock || process.env.NODE_ENV !== "production") {
    return (g.__p2pMemoryStore ??= new MemoryServerStore());
  }
  throw new StoreUnavailable();
}

export function resetMemoryStore(): MemoryServerStore {
  g.__p2pMemoryStore = new MemoryServerStore();
  return g.__p2pMemoryStore;
}
