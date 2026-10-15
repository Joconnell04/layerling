import { MESSAGE_ROLES, type Profile, type UserPayloadKind } from "../../contracts";
import type { AddOn, MessagePayloadMap, ProductForm, ProductType, StageId } from "../../contracts.gen";
import type { AnyMessage, JobPhoto, JobRow, MessageRow } from "../../db.types";
import type { MockStore } from "./store";

export interface JobDraft {
  prompt: string;
  productType: ProductType;
  targetSizeMm: number | null;
  profile: Profile;
  photos: JobPhoto[];
  title?: string | null;
  addons?: AddOn[];
  form?: ProductForm | null;
}

export function buildJob(store: MockStore, ownerId: string | null, d: JobDraft): JobRow {
  const now = store.iso();
  return {
    id: store.newId(),
    owner_id: ownerId,
    access: ownerId ? "owner" : "code",
    created_at: now,
    updated_at: now,
    title: d.title ?? null,
    prompt: d.prompt.trim(),
    product_type: d.productType,
    profile: d.profile,
    status: "queued",
    stage: null,
    target_size_mm: d.targetSizeMm,
    form: d.form ?? null,
    custom_asks: [],
    addons: d.addons ?? [],
    settings: {},
    photos: d.photos,
    budget_usd: 10,
    budget_credits: 400,
    spent_usd: 0,
    spent_credits: 0,
    confidence: null,
    review_reason: null,
    pause_reason: null,
    result: null,
  };
}

export function buildPromptMessage(store: MockStore, job: JobRow): MessageRow<"prompt"> {
  return {
    id: store.newId(),
    job_id: job.id,
    owner_id: job.owner_id,
    created_at: store.iso(),
    role: "user",
    kind: "prompt",
    stage: null,
    body: job.prompt,
    payload: {},
    reply_to: null,
    consumed_at: null,
  };
}

export function buildUserMessage<K extends UserPayloadKind>(
  store: MockStore,
  job: JobRow,
  kind: K,
  payload: MessagePayloadMap[K],
  opts: { replyTo?: string | null; stage?: StageId | null; body?: string | null } = {},
): MessageRow<K> {
  return {
    id: store.newId(),
    job_id: job.id,
    owner_id: job.owner_id,
    created_at: store.iso(),
    role: MESSAGE_ROLES[kind],
    kind,
    stage: opts.stage ?? job.stage,
    body: opts.body ?? null,
    payload: structuredClone(payload) as MessageRow<K>["payload"],
    reply_to: opts.replyTo ?? null,
    consumed_at: null,
  };
}

export const asAny = (m: MessageRow) => m as unknown as AnyMessage;
