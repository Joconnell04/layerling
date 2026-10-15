// Row types for supabase/migrations/20260923000001_init.sql. jsonb columns use the contract types.
// lib/contracts.test.ts checks the enum and check-constraint values against the migration.

import type {
  AddOn,
  CustomAsk,
  JobResult,
  JobStatus,
  Manifest,
  PrintSettings,
  ProductForm,
  ProductType,
  StageId,
  StageStatus,
} from "./contracts.gen";
import type { MessageKind, MessageRole, PayloadFor, Profile } from "./contracts";

export interface JobPhoto {
  path: string; // job-relative, input/photo_<n>.<ext>
  sha256?: string;
  subject_ids?: string[];
}

export type JobAccess = "owner" | "code";

export interface JobRow {
  id: string;
  owner_id: string | null; // null for jobs created from an access-code session
  access: JobAccess;
  created_at: string;
  updated_at: string;
  title: string | null;
  prompt: string;
  product_type: ProductType;
  profile: Profile;
  status: JobStatus;
  stage: StageId | null;
  target_size_mm: number | null;
  /**
   * Migration 20260924000011 (may be missing on a project): flat charm or 3D figure. The new-job
   * form sets it for keychains and magnets (then intake does not ask); s01 writes the decision.
   */
  form?: ProductForm | null;
  /** Migration 20260924000011: props, outfits, poses and names, written by s01. */
  custom_asks?: CustomAsk[];
  addons: AddOn[];
  settings: Partial<PrintSettings>;
  photos: JobPhoto[];
  budget_usd: number;
  budget_credits: number;
  spent_usd: number;
  spent_credits: number;
  confidence: number | null;
  review_reason: string | null;
  pause_reason: string | null;
  result: JobResult | null;
}

export interface StageRunRow {
  id: string;
  job_id: string;
  owner_id: string | null;
  stage: StageId;
  attempt: number;
  status: StageStatus;
  started_at: string | null;
  finished_at: string | null;
  usd: number;
  credits: number;
  confidence: number | null;
  manifest: Partial<Manifest>;
  updated_at: string;
}

export interface MessageRow<K extends MessageKind = MessageKind> {
  id: string;
  job_id: string;
  owner_id: string | null;
  created_at: string;
  role: MessageRole;
  kind: K;
  stage: StageId | null;
  body: string | null;
  payload: PayloadFor<K>;
  reply_to: string | null;
  consumed_at: string | null;
}

export type AnyMessage = { [K in MessageKind]: MessageRow<K> }[MessageKind];

export type LabSource = "nominal" | "measured";

export interface FilamentRow {
  id: string;
  owner_id: string | null;
  brand: string;
  line: string;
  name: string;
  material: string;
  hex: string;
  lab_l: number;
  lab_a: number;
  lab_b: number;
  lab_source: LabSource;
  td_mm: number | null;
  owned: boolean;
  spools: number;
  ams_slot: number | null;
  swatch_path: string | null;
  bambu_filament_id: string | null;
  slicer_preset: string | null;
  density_g_cm3: number;
  notes: string | null;
  updated_at: string;
}

export type SpendVendor = "anthropic" | "meshy" | "runpod";

export interface SpendRow {
  id: number;
  owner_id: string | null;
  at: string;
  job_id: string | null;
  stage: StageId | null;
  vendor: SpendVendor;
  kind: string;
  usd: number;
  credits: number;
  ref: string | null;
  test_mode: boolean;
}

export interface WorkerRow {
  id: string;
  last_seen: string;
  version: string | null;
  meshy_mode: "test" | "live" | null;
  info: Record<string, unknown>;
}

export const PRODUCT_TYPES: readonly ProductType[] = [
  "figurine",
  "keychain",
  "nameplate",
  "ornament",
  "magnet",
  "other",
];
export const JOB_ACCESS: readonly JobAccess[] = ["owner", "code"];

// supabase/migrations/20260924000004_orders_access.sql
export type OrderStatus = "new" | "contacted" | "quoted" | "accepted" | "declined" | "done" | "spam";
export const ORDER_STATUSES: readonly OrderStatus[] = [
  "new",
  "contacted",
  "quoted",
  "accepted",
  "declined",
  "done",
  "spam",
];

export interface OrderRow {
  id: string;
  created_at: string;
  updated_at: string;
  status: OrderStatus;
  name: string;
  /** Email or phone, at least one (migration 20260924000007). */
  email: string | null;
  phone: string | null;
  product_type: ProductType;
  /** xs, s, m or l; null for Other (migration 20260924000007). */
  size_tier: "xs" | "s" | "m" | "l" | null;
  size_mm: number | null;
  quantity: number;
  name_text: string | null;
  /** Optional for standard products, at least 10 characters for Other. */
  request: string | null;
  photos: string[]; // storage keys in bucket `orders`: <order_id>/photo_<n>.<ext>
  source: string | null;
  notified_at: string | null;
  notify_error: string | null;
  job_id: string | null;
}
export const PROFILE_VALUES: readonly Profile[] = ["thin_slice", "full"];
export const SPEND_VENDORS: readonly SpendVendor[] = ["anthropic", "meshy", "runpod"];
export const JOB_STATUSES: readonly JobStatus[] = [
  "draft",
  "queued",
  "running",
  "waiting_user",
  "paused",
  "review",
  "done",
  "failed",
  "cancelled",
];
