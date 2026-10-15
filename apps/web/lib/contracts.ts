// Hand-written mirrors of contracts that have no JSON Schema (stage graph, message registry, caps).
// Tests in lib/contracts.test.ts parse the Python sources and fail if these drift.

import type { AddOnKind, MessagePayloadMap, ProductType, StageId, StageStatus } from "./contracts.gen";

export type * from "./contracts.gen";

export type Gate = "none" | "user" | "auto";
export type Profile = "thin_slice" | "full";

export interface StageInfo {
  id: StageId;
  number: number;
  name: string;
  gate: Gate;
  milestone: string;
}

// contracts/src/p2p_contracts/stages.py STAGES
export const STAGES: readonly StageInfo[] = [
  { id: "s01_intake", number: 1, name: "Intake", gate: "user", milestone: "M1" },
  { id: "s02_color_spec", number: 2, name: "Color spec", gate: "none", milestone: "M3" },
  { id: "s03_concept", number: 3, name: "Concept", gate: "user", milestone: "M1" },
  { id: "s04_multiview", number: 4, name: "Multi-view", gate: "auto", milestone: "M3" },
  { id: "s05_mesh", number: 5, name: "3D model", gate: "user", milestone: "M1" },
  { id: "s06_printability", number: 6, name: "Printability", gate: "auto", milestone: "M3" },
  { id: "s07_baseline", number: 7, name: "Baseline + labels", gate: "auto", milestone: "M1" },
  { id: "s08_palette", number: 8, name: "Palette", gate: "auto", milestone: "M3" },
  { id: "s09_paint", number: 9, name: "Paint agent", gate: "auto", milestone: "M3" },
  { id: "s10_write", number: 10, name: "Write + slice", gate: "auto", milestone: "M3" },
];

export const STAGE_BY_ID: Record<StageId, StageInfo> = Object.fromEntries(
  STAGES.map((s) => [s.id, s]),
) as Record<StageId, StageInfo>;

// stages.py PROFILE_THIN_SLICE / PROFILE_FULL
export const PROFILES: Record<Profile, readonly StageId[]> = {
  thin_slice: [
    "s01_intake", "s03_concept", "s04_multiview", "s05_mesh", "s06_printability", "s07_baseline",
  ],
  full: STAGES.map((s) => s.id),
};

export const STAGE_STATUSES: readonly StageStatus[] = [
  "pending",
  "running",
  "waiting_user",
  "waiting_vendor",
  "succeeded",
  "failed",
  "paused",
  "skipped",
  "stale",
];

// payloads.py MESSAGE_PAYLOADS: kind -> role
export type MessageKind = "prompt" | keyof MessagePayloadMap;
export type MessageRole = "user" | "assistant" | "system";

export const MESSAGE_ROLES = {
  prompt: "user",
  questions: "assistant",
  answers: "user",
  concepts: "assistant",
  concept_pick: "user",
  tweak: "user",
  tweak_decision: "assistant",
  mesh_review: "assistant",
  mesh_decision: "user",
  result: "assistant",
  review_request: "assistant",
  review_decision: "user",
  note: "assistant",
  resume: "user",
  spool_suggestion: "assistant",
  palette_edit: "user",
  paint_edit: "user",
} as const satisfies Record<MessageKind, MessageRole>;

export type UserPayloadKind = {
  [K in keyof MessagePayloadMap]: (typeof MESSAGE_ROLES)[K] extends "user" ? K : never;
}[keyof MessagePayloadMap];

export type AssistantPayloadKind = Exclude<keyof MessagePayloadMap, UserPayloadKind>;

export type PayloadFor<K extends MessageKind> = K extends keyof MessagePayloadMap
  ? MessagePayloadMap[K]
  : Record<string, never>;

// job.py caps and worker heartbeat
export const JOB_CAP_USD = 10.0;
export const JOB_CAP_CREDITS = 400;
export const MAX_TWEAK_ROUNDS = 5;
export const USD_PER_MESHY_CREDIT = 0.02;
export const WORKER_HEARTBEAT_S = 15;
export const WORKER_ONLINE_S = 60;

// job.py PRODUCT_DEFAULTS
export const PRODUCT_DEFAULTS: Record<ProductType, { target_size_mm: number; addons: AddOnKind[] }> = {
  figurine: { target_size_mm: 90, addons: ["round_base"] },
  keychain: { target_size_mm: 52, addons: ["keyring_loop"] },
  nameplate: { target_size_mm: 120, addons: ["nameplate"] },
  ornament: { target_size_mm: 70, addons: ["hanging_loop"] },
  magnet: { target_size_mm: 60, addons: ["flat_back", "magnet_pocket"] },
  other: { target_size_mm: 90, addons: [] },
};

// TextSpec.text max_length
export const NAME_TEXT_MAX = 24;
/** Products whose add-ons can carry name text. */
export const NAME_TEXT_PRODUCTS: readonly ProductType[] = ["nameplate", "keychain"];
