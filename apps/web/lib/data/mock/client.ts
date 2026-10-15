// Mock adapter: in-memory tables, a simulated worker, and realtime-like events on timers.

import { WORKER_HEARTBEAT_S, type UserPayloadKind } from "../../contracts";
import type { MessagePayloadMap } from "../../contracts.gen";
import type { AnyMessage, FilamentRow, JobRow, MessageRow, WorkerRow } from "../../db.types";
import { assertPayload } from "../../validate";
import {
  PAINT_STROKE_PATH,
  photoExt,
  type DataClient,
  type FilamentInput,
  type NewJobInput,
  type RowEvent,
  type SendMessageInput,
  type SessionUser,
  type Unsubscribe,
} from "../types";
import { addonsFor } from "../../products";
import { filamentRowFrom } from "../filaments";
import MOCK_CHECKLIST from "../../../public/mock/checklist.json";
import { mockAssetUrl, mockPaintUrl, mockSet } from "./assets";
import { asAny, buildJob, buildPromptMessage, buildUserMessage } from "./build";
import { seedStore } from "./seed";
import { MOCK_USER_ID, MockStore } from "./store";
import { MockWorker } from "./worker";

export interface MockOptions {
  stepMs?: number;
  seed?: boolean;
  heartbeatMs?: number | null;
}

const USER: SessionUser = { id: MOCK_USER_ID, email: "jackson@local" };

// ASSUMED sample warnings, shaped like s06 warnings.json (a list of strings, geometry wording).
const MOCK_WARNINGS = ["Loop hole 3 mm raised to 3.5 mm", "Thin strokes in the name, switched to the heaviest weight"];

// Shaped like s06 printability.json (Meshy Analyze Printability); the counts are proof run 1's.
const MOCK_PRINTABILITY = {
  status: "warning",
  issue_count: 1,
  error_count: 0,
  warning_count: 1,
  metrics: { degenerate_faces: 164801, holes: 0, non_manifold_edges: 0, is_watertight: true, volume: 0.74 },
};

function jsonDataUrl(v: unknown): string {
  return `data:application/json,${encodeURIComponent(JSON.stringify(v))}`;
}

/**
 * A geometry.json in the print frame (mm, Z up, base on z = 0) that matches the job's product
 * and size. Keychains and magnets lie flat face up like the real s06 output; the keychain's
 * numbers match public/mock/preview-flat.glb.
 */
export function mockGeometry(job: JobRow) {
  const t = job.target_size_mm ?? 90;
  // A flat charm lies face up; a 3D keychain stands like the figure it is.
  const set = mockSet(job);
  const flat = set === "flat" || set === "relief";
  // [x width, y depth or face-on height, z height or relief thickness]
  const [w, d, h] =
    job.product_type === "nameplate"
      ? [t, t * 0.3, t * 0.55]
      : set === "kc3d"
        ? [t * 0.52, t * 0.4, t]
        : job.product_type === "keychain"
        ? [t * KEYCHAIN_W, t, t * KEYCHAIN_T]
        : job.product_type === "magnet"
          ? [t * 0.8, t, t * 0.15]
          : [t * 0.7, t * 0.6, t];
  const r = (v: number) => Math.round(v * 10) / 10;
  return {
    orientation: flat ? "flat_face_up" : "upright",
    bbox_min_mm: [r(-w / 2), r(-d / 2), 0],
    bbox_max_mm: [r(w / 2), r(d / 2), r(h)],
    warnings: [],
  };
}
// preview-flat.glb proportions (scripts/gen-mock-assets.mjs: 21.42 x 45.00 x 8.00 mm): width and
// relief thickness over length.
const KEYCHAIN_W = 21.42 / 45;
const KEYCHAIN_T = 8.0 / 45;
const clone = <T>(v: T): T => structuredClone(v);

export class MockDataClient implements DataClient {
  readonly mode = "mock" as const;
  readonly store = new MockStore();
  readonly worker: MockWorker;
  private heartbeat: ReturnType<typeof setInterval> | null = null;

  constructor(opts: MockOptions = {}) {
    const stepMs = opts.stepMs ?? 900;
    if (opts.seed ?? true) seedStore(this.store);
    this.worker = new MockWorker(this.store, { stepMs });
    this.store.putWorker({
      id: "mock-worker",
      last_seen: new Date().toISOString(),
      version: "0.1.0-mock",
      meshy_mode: "test",
      info: { host: "mock", stages: "all" },
    });
    const hb = opts.heartbeatMs === undefined ? WORKER_HEARTBEAT_S * 1000 : opts.heartbeatMs;
    if (hb) {
      this.heartbeat = setInterval(() => {
        const w = this.store.workers.get("mock-worker");
        if (w) this.store.putWorker({ ...w, last_seen: new Date().toISOString() });
      }, hb);
    }
  }

  dispose(): void {
    this.worker.dispose();
    if (this.heartbeat) clearInterval(this.heartbeat);
  }

  // ------------------------------------------------------------------ auth (always signed in)

  async getUser() {
    return USER;
  }
  async signIn() {
    return USER;
  }
  async signOut() {}
  onAuthChange(): Unsubscribe {
    return () => {};
  }

  // ------------------------------------------------------------------ jobs

  async listJobs(): Promise<JobRow[]> {
    return [...this.store.jobs.values()]
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
      .map(clone);
  }

  async getJob(id: string): Promise<JobRow | null> {
    const j = this.store.jobs.get(id);
    return j ? clone(j) : null;
  }

  async createJob(input: NewJobInput): Promise<JobRow> {
    if (!input.prompt.trim()) throw new Error("Prompt required");
    if (input.photos.length === 0) throw new Error("Add at least one photo");
    const id = this.store.newId();
    const photos = input.photos.map((file, i) => {
      const path = `input/photo_${i + 1}.${photoExt(file)}`;
      this.store.blobs.set(`inputs/${id}/${path}`, URL.createObjectURL(file));
      return { path };
    });
    const addons = input.addons ?? addonsFor(input.productType);
    const job = { ...buildJob(this.store, USER.id, { ...input, photos, addons }), id };
    this.store.putJob(job);
    this.store.putMessage(asAny(buildPromptMessage(this.store, job)));
    this.worker.onJobCreated(id);
    return clone(job);
  }

  async listStageRuns(jobId: string) {
    return this.store
      .runsFor(jobId)
      .sort((a, b) => a.stage.localeCompare(b.stage) || a.attempt - b.attempt)
      .map(clone);
  }

  async listMessages(jobId: string): Promise<AnyMessage[]> {
    return this.store.messagesFor(jobId).map(clone);
  }

  async listMessagesByKind<K extends keyof MessagePayloadMap>(
    jobIds: string[],
    kind: K,
  ): Promise<MessageRow<K>[]> {
    const ids = new Set(jobIds);
    return [...this.store.messages.values()]
      .filter((m) => ids.has(m.job_id) && m.kind === kind)
      .sort((a, b) => a.created_at.localeCompare(b.created_at))
      .map((m) => clone(m) as unknown as MessageRow<K>);
  }

  async listSpend(jobId?: string) {
    return this.store.spend
      .filter((s) => !jobId || s.job_id === jobId)
      .sort((a, b) => b.at.localeCompare(a.at))
      .map(clone);
  }

  async sendMessage<K extends UserPayloadKind>(input: SendMessageInput<K>): Promise<MessageRow<K>> {
    assertPayload(input.kind, input.payload);
    const job = this.store.jobs.get(input.jobId);
    if (!job) throw new Error("Job not found");
    const msg = buildUserMessage(this.store, job, input.kind, input.payload, {
      replyTo: input.replyTo,
      stage: input.stage,
      body: input.body,
    });
    this.store.putMessage(msg as unknown as AnyMessage);
    this.worker.onUserMessage(msg as unknown as AnyMessage);
    return clone(msg);
  }

  // ------------------------------------------------------------------ filaments

  async listFilaments(): Promise<FilamentRow[]> {
    return [...this.store.filaments.values()]
      .sort((a, b) => Number(b.owned) - Number(a.owned) || a.name.localeCompare(b.name))
      .map(clone);
  }

  async saveFilament(input: FilamentInput): Promise<FilamentRow> {
    const row = filamentRowFrom(
      input,
      this.store.filaments.get(input.id ?? ""),
      this.store.iso(),
      USER.id,
    );
    if (!input.id && this.store.filaments.has(row.id)) throw new Error(`${row.id} exists`);
    this.store.putFilament(row);
    return clone(row);
  }

  async uploadSwatch(filamentId: string, file: File): Promise<FilamentRow> {
    const f = this.store.filaments.get(filamentId);
    if (!f) throw new Error("Filament not found");
    const key = `${USER.id}/${filamentId}.${photoExt(file)}`;
    this.store.blobs.set(`swatches/${key}`, URL.createObjectURL(file));
    const row = this.store.putFilament({ ...f, swatch_path: key, updated_at: this.store.iso() });
    return clone(row);
  }

  // ------------------------------------------------------------------ workers

  async listWorkers(): Promise<WorkerRow[]> {
    return [...this.store.workers.values()].map(clone);
  }

  // ------------------------------------------------------------------ storage

  async artifactUrl(jobId: string, path: string): Promise<string> {
    const job = this.store.jobs.get(jobId);
    if (job && path.endsWith("/geometry.json")) return jsonDataUrl(mockGeometry(job));
    if (path.endsWith("/warnings.json")) return jsonDataUrl(MOCK_WARNINGS);
    if (path.endsWith("/printability.json")) return jsonDataUrl(MOCK_PRINTABILITY);
    if (path.endsWith("/checklist.json")) return jsonDataUrl(MOCK_CHECKLIST);
    const attempt = /^(s\d\d_[a-z]+)\/a(\d+)\//.exec(path);
    const run = attempt
      ? [...this.store.runs.values()].find((r) => r.job_id === jobId && r.stage === attempt[1] && r.attempt === Number(attempt[2]))
      : undefined;
    return mockPaintUrl(path, !!run?.manifest.data?.paint_edit) ?? mockAssetUrl(path, job);
  }

  async uploadInput(jobId: string, path: string, body: Blob): Promise<void> {
    if (!PAINT_STROKE_PATH.test(path)) throw new Error(`Not an editor upload path: ${path}`);
    this.store.blobs.set(`inputs/${jobId}/${path}`, typeof URL.createObjectURL === "function" ? URL.createObjectURL(body) : path);
  }

  async inputUrl(jobId: string, path: string): Promise<string> {
    return this.store.blobs.get(`inputs/${jobId}/${path}`) ?? mockAssetUrl(path);
  }

  async swatchUrl(path: string): Promise<string> {
    return this.store.blobs.get(`swatches/${path}`) ?? "/mock/swatch.svg";
  }

  // ------------------------------------------------------------------ realtime

  subscribeJob(jobId: string, cb: (e: RowEvent) => void): Unsubscribe {
    return this.store.on((e) => {
      if (e.table === "workers") return;
      const id = e.table === "jobs" ? e.row.id : e.row.job_id;
      if (id === jobId) cb(e);
    });
  }

  subscribeJobs(cb: (e: RowEvent) => void): Unsubscribe {
    return this.store.on((e) => {
      if (e.table === "jobs") cb(e);
    });
  }

  subscribeWorkers(cb: (e: RowEvent) => void): Unsubscribe {
    return this.store.on((e) => {
      if (e.table === "workers") cb(e);
    });
  }
}
