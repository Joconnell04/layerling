// Server adapter for access-code sessions: the browser calls this app's /api/studio routes, which
// use the service role server-side. Live updates come from polling those routes every 2 s.

import type { MessagePayloadMap } from "../contracts.gen";
import { WORKER_HEARTBEAT_S, type UserPayloadKind } from "../contracts";
import type { AnyMessage, FilamentRow, JobRow, MessageRow, SpendRow, StageRunRow, WorkerRow } from "../db.types";
import { addonsFor } from "../products";
import { photoMeta, putFile, sha256Hex, type UploadTarget } from "../upload";
import { assertPayload } from "../validate";
import type {
  DataClient,
  FilamentInput,
  NewJobInput,
  RowEvent,
  SendMessageInput,
  SessionUser,
  Unsubscribe,
} from "./types";

export const POLL_MS = 2000;
const CODE_USER: SessionUser = { id: "access-code", email: null };

interface Bundle {
  job: JobRow | null;
  runs: StageRunRow[];
  messages: AnyMessage[];
  spend: SpendRow[];
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

/** Row signature for change detection while polling. */
function sig(row: object): string {
  const r = row as { updated_at?: string; consumed_at?: string | null; last_seen?: string };
  return r.updated_at ?? r.last_seen ?? `${r.consumed_at ?? ""}`;
}

export class ServerDataClient implements DataClient {
  readonly mode = "server" as const;
  private bundles = new Map<string, { at: number; p: Promise<Bundle> }>();

  constructor(
    private fetchImpl: typeof fetch = (...a) => fetch(...a),
    private pollMs: number = POLL_MS,
  ) {}

  private async api<T>(path: string, init?: RequestInit): Promise<T> {
    const res = await this.fetchImpl(path, { credentials: "same-origin", ...init });
    if (!res.ok) {
      const body = (await res.json().catch(() => ({}))) as { error?: string };
      throw new ApiError(res.status, body.error ?? res.statusText ?? "Request failed");
    }
    return (await res.json()) as T;
  }

  private post<T>(path: string, body: unknown, method = "POST"): Promise<T> {
    return this.api<T>(path, { method, headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  }

  /** One request serves getJob, listStageRuns and listMessages called together. */
  private bundle(jobId: string, fresh = false): Promise<Bundle> {
    const hit = this.bundles.get(jobId);
    if (!fresh && hit && Date.now() - hit.at < 750) return hit.p;
    const p = this.api<Bundle>(`/api/studio/jobs/${encodeURIComponent(jobId)}`);
    this.bundles.set(jobId, { at: Date.now(), p });
    p.catch(() => this.bundles.delete(jobId));
    return p;
  }

  // ------------------------------------------------------------------ auth

  async getUser() {
    const s = await this.api<{ access: boolean }>("/api/access").catch(() => ({ access: false }));
    return s.access ? CODE_USER : null;
  }
  async signIn(): Promise<SessionUser> {
    throw new Error("Use the access code");
  }
  async signOut() {
    await this.api("/api/access", { method: "DELETE" });
  }
  onAuthChange(): Unsubscribe {
    return () => {};
  }

  // ------------------------------------------------------------------ jobs

  async listJobs() {
    return (await this.api<{ jobs: JobRow[] }>("/api/studio/jobs")).jobs;
  }
  async getJob(id: string) {
    return (await this.bundle(id)).job;
  }
  async listStageRuns(jobId: string) {
    return (await this.bundle(jobId)).runs;
  }
  async listMessages(jobId: string) {
    return (await this.bundle(jobId)).messages;
  }
  async listMessagesByKind<K extends keyof MessagePayloadMap>(jobIds: string[], kind: K) {
    if (!jobIds.length) return [];
    const q = new URLSearchParams({ kind, jobs: jobIds.join(",") });
    return (await this.api<{ messages: MessageRow<K>[] }>(`/api/studio/messages?${q}`)).messages;
  }
  async listSpend(jobId?: string) {
    const q = jobId ? `?job=${encodeURIComponent(jobId)}` : "";
    return (await this.api<{ spend: SpendRow[] }>(`/api/studio/spend${q}`)).spend;
  }

  async createJob(input: NewJobInput): Promise<JobRow> {
    if (!input.prompt.trim()) throw new Error("Prompt required");
    if (!input.photos.length) throw new Error("Add at least one photo");
    const start = await this.post<{ job_id: string; ticket: string; uploads: UploadTarget[] }>(
      "/api/studio/jobs/start",
      {
        prompt: input.prompt,
        productType: input.productType,
        targetSizeMm: input.targetSizeMm,
        profile: input.profile,
        title: input.title ?? null,
        addons: input.addons ?? addonsFor(input.productType),
        form: input.form ?? null,
        photos: input.photos.map(photoMeta),
      },
    );
    const hashes = await Promise.all(
      input.photos.map(async (f, i) => {
        await putFile(start.uploads[i], f, this.fetchImpl);
        return sha256Hex(f);
      }),
    );
    const { job } = await this.post<{ job: JobRow }>("/api/studio/jobs/finish", { ticket: start.ticket, sha256: hashes });
    return job;
  }

  async sendMessage<K extends UserPayloadKind>(input: SendMessageInput<K>): Promise<MessageRow<K>> {
    assertPayload(input.kind, input.payload);
    const { message } = await this.post<{ message: MessageRow<K> }>(
      `/api/studio/jobs/${encodeURIComponent(input.jobId)}/messages`,
      { kind: input.kind, payload: input.payload, reply_to: input.replyTo ?? null, stage: input.stage ?? null, body: input.body ?? null },
    );
    this.bundles.delete(input.jobId);
    return message;
  }

  // ------------------------------------------------------------------ filaments

  async listFilaments() {
    return (await this.api<{ filaments: FilamentRow[] }>("/api/studio/filaments")).filaments;
  }
  async saveFilament(input: FilamentInput) {
    return (await this.post<{ filament: FilamentRow }>("/api/studio/filaments", input)).filament;
  }
  async uploadSwatch(filamentId: string, file: File) {
    const path = `/api/studio/filaments/${encodeURIComponent(filamentId)}/swatch`;
    const { upload } = await this.post<{ upload: UploadTarget }>(path, photoMeta(file));
    await putFile(upload, file, this.fetchImpl);
    return (await this.post<{ filament: FilamentRow }>(path, { key: upload.key }, "PUT")).filament;
  }

  // ------------------------------------------------------------------ workers and files

  async listWorkers() {
    return (await this.api<{ workers: WorkerRow[] }>("/api/studio/workers")).workers;
  }

  async uploadInput(jobId: string, path: string, body: Blob) {
    const { upload } = await this.post<{ upload: UploadTarget }>(
      `/api/studio/jobs/${encodeURIComponent(jobId)}/uploads`,
      { path, type: "text/plain", size: body.size },
    );
    await putFile(upload, body, this.fetchImpl);
  }

  async artifactUrl(jobId: string, path: string) {
    return `/api/studio/file?${new URLSearchParams({ bucket: "artifacts", job: jobId, path })}`;
  }
  async inputUrl(jobId: string, path: string) {
    return `/api/studio/file?${new URLSearchParams({ bucket: "inputs", job: jobId, path })}`;
  }
  async swatchUrl(key: string) {
    return `/api/studio/file?${new URLSearchParams({ bucket: "swatches", key })}`;
  }

  // ------------------------------------------------------------------ polling "realtime"

  private poll(fn: () => Promise<void>, ms: number): Unsubscribe {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      if (stopped) return;
      try {
        await fn();
      } catch {
        // transient; next tick retries
      }
      if (!stopped) timer = setTimeout(tick, ms);
    };
    timer = setTimeout(tick, ms);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }

  subscribeJob(jobId: string, cb: (e: RowEvent) => void): Unsubscribe {
    const seen = new Map<string, string>();
    let primed = false;
    const emitIfChanged = (key: string, row: object, event: RowEvent) => {
      const s = sig(row);
      if (seen.get(key) === s) return;
      seen.set(key, s);
      if (primed) cb(event);
    };
    const load = async () => {
      const b = await this.bundle(jobId, true);
      if (b.job) emitIfChanged(`job:${b.job.id}`, b.job, { table: "jobs", row: b.job });
      for (const r of b.runs) emitIfChanged(`run:${r.id}`, r, { table: "stage_runs", row: r });
      for (const m of b.messages) emitIfChanged(`msg:${m.id}`, m, { table: "messages", row: m });
      primed = true;
    };
    void load().catch(() => (primed = true));
    return this.poll(load, this.pollMs);
  }

  subscribeJobs(cb: (e: RowEvent) => void): Unsubscribe {
    const seen = new Map<string, string>();
    let primed = false;
    const load = async () => {
      for (const j of await this.listJobs()) {
        const s = sig(j);
        if (seen.get(j.id) !== s) {
          seen.set(j.id, s);
          if (primed) cb({ table: "jobs", row: j });
        }
      }
      primed = true;
    };
    void load().catch(() => (primed = true));
    return this.poll(load, this.pollMs * 2);
  }

  subscribeWorkers(cb: (e: RowEvent) => void): Unsubscribe {
    return this.poll(async () => {
      for (const w of await this.listWorkers()) cb({ table: "workers", row: w });
    }, WORKER_HEARTBEAT_S * 1000);
  }
}
