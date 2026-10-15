// One data interface for the app. Two adapters: mock (in-memory, simulated worker) and supabase.

import type { AddOn, MessagePayloadMap, ProductForm, ProductType, StageId } from "../contracts.gen";
import type { Profile, UserPayloadKind } from "../contracts";
import type {
  AnyMessage,
  FilamentRow,
  JobRow,
  MessageRow,
  SpendRow,
  StageRunRow,
  WorkerRow,
} from "../db.types";

export type Unsubscribe = () => void;

export interface SessionUser {
  id: string;
  email: string | null;
}

export interface NewJobInput {
  prompt: string;
  productType: ProductType;
  targetSizeMm: number | null;
  profile: Profile;
  photos: File[];
  title?: string | null;
  /** Exact-geometry add-ons; use addonsFor() to build them from the product and name text. */
  addons?: AddOn[];
  /** Keychains and magnets: flat charm or 3D figure. Null or absent: intake asks the customer. */
  form?: ProductForm | null;
}

export interface SendMessageInput<K extends UserPayloadKind> {
  jobId: string;
  kind: K;
  payload: MessagePayloadMap[K];
  replyTo?: string | null;
  stage?: StageId | null;
  body?: string | null;
}

export interface FilamentInput {
  id?: string; // omitted for a new spool: derived from brand, material, line, name
  brand: string;
  line: string;
  name: string;
  material: string;
  hex: string;
  owned: boolean;
  spools: number;
  ams_slot: number | null;
  td_mm?: number | null;
  notes?: string | null;
}

export type RowEvent =
  | { table: "jobs"; row: JobRow }
  | { table: "stage_runs"; row: StageRunRow }
  | { table: "messages"; row: AnyMessage }
  | { table: "workers"; row: WorkerRow };

export type DataMode = "mock" | "supabase" | "server";

export interface DataClient {
  readonly mode: DataMode;

  // auth
  getUser(): Promise<SessionUser | null>;
  signIn(email: string, password: string): Promise<SessionUser>;
  signOut(): Promise<void>;
  onAuthChange(cb: (user: SessionUser | null) => void): Unsubscribe;

  // jobs
  listJobs(): Promise<JobRow[]>;
  getJob(id: string): Promise<JobRow | null>;
  createJob(input: NewJobInput): Promise<JobRow>;
  listStageRuns(jobId: string): Promise<StageRunRow[]>;
  listMessages(jobId: string): Promise<AnyMessage[]>;
  listMessagesByKind<K extends keyof MessagePayloadMap>(
    jobIds: string[],
    kind: K,
  ): Promise<MessageRow<K>[]>;
  listSpend(jobId?: string): Promise<SpendRow[]>;
  sendMessage<K extends UserPayloadKind>(input: SendMessageInput<K>): Promise<MessageRow<K>>;

  // filaments
  listFilaments(): Promise<FilamentRow[]>;
  saveFilament(input: FilamentInput): Promise<FilamentRow>;
  uploadSwatch(filamentId: string, file: File): Promise<FilamentRow>;

  // workers
  listWorkers(): Promise<WorkerRow[]>;

  // storage
  /** A file the web adds to a running job (bucket inputs), e.g. input/paint/<name>.txt. */
  uploadInput(jobId: string, path: string, body: Blob): Promise<void>;
  artifactUrl(jobId: string, path: string): Promise<string>;
  inputUrl(jobId: string, path: string): Promise<string>;
  swatchUrl(path: string): Promise<string>;

  // realtime
  subscribeJob(jobId: string, cb: (e: RowEvent) => void): Unsubscribe;
  subscribeJobs(cb: (e: RowEvent) => void): Unsubscribe;
  subscribeWorkers(cb: (e: RowEvent) => void): Unsubscribe;
}

/** Storage key for a job-relative path in any bucket (contracts job.storage_key): `<job_id>/<path>`. */
export function storageKey(jobId: string, path: string): string {
  return `${jobId}/${path.replace(/^\/+/, "")}`;
}

/** Bucket `inputs`: input/photo_1.jpg -> <job_id>/input/photo_1.jpg. */
export const inputStorageKey = storageKey;

/** Paint strokes the editor uploads for paint_edit faces_path (bucket inputs). */
export const PAINT_STROKE_PATH = /^input\/paint\/[A-Za-z0-9-]{1,64}\.txt$/;
/** Bucket `artifacts`: s03_concept/a1/r1c1.png -> <job_id>/s03_concept/a1/r1c1.png. */
export const artifactStorageKey = storageKey;

export function photoExt(file: File): string {
  const fromName = /\.([a-z0-9]{2,5})$/i.exec(file.name)?.[1]?.toLowerCase();
  if (fromName) return fromName === "jpeg" ? "jpg" : fromName;
  const fromType = file.type.split("/")[1];
  return fromType === "jpeg" ? "jpg" : fromType || "bin";
}

export async function sha256Hex(file: Blob): Promise<string> {
  const buf = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buf);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
