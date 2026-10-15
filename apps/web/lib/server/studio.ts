// Studio operations shared by the API routes: create a job from uploads or from an order, send a
// user message, save a filament. All writes go through a ServerStore (service role or memory).

import { MESSAGE_ROLES, PRODUCT_DEFAULTS, type Profile, type UserPayloadKind } from "../contracts";
import type { AddOn, ProductForm, ProductType, StageId } from "../contracts.gen";
import type { AnyMessage, FilamentRow, JobPhoto, JobRow, OrderRow } from "../db.types";
import { filamentRowFrom } from "../data/filaments";
import type { FilamentInput } from "../data/types";
import { formIsChoice } from "../intent";
import { PRODUCT_LABELS, addonsFor } from "../products";
import { validatePayload } from "../validate";
import { ownership, type Actor } from "./guard";
import type { Logger } from "./log";
import { PHOTO_TYPES, photoType } from "./orders";
import type { ServerStore } from "./store";

export class HttpError extends Error {
  constructor(
    public status: number,
    message: string,
    public details?: Record<string, unknown>,
  ) {
    super(message);
  }
}

async function sha256Hex(blob: Blob): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export interface JobFields {
  prompt: string;
  productType: ProductType;
  targetSizeMm: number | null;
  profile: Profile;
  addons: AddOn[];
  title?: string | null;
  /** Keychains and magnets: flat charm or 3D figure; absent, intake asks. */
  form?: ProductForm | null;
}

/** Carried between /api/studio/jobs/start and /finish (signed, see ticket.ts). */
export interface JobTicket {
  id: string;
  fields: JobFields;
  photos: { path: string; key: string; size: number }[];
}

export interface Upload {
  blob: Blob;
  type: string;
  ext: string;
}

/**
 * Upload photos to inputs/<job_id>/input/photo_<n>.<ext>, insert the job as draft with its photos,
 * add the prompt message, then queue it so the worker only sees complete jobs.
 */
export async function createJob(
  store: ServerStore,
  actor: Actor,
  fields: JobFields,
  uploads: Upload[],
  log: Logger,
): Promise<JobRow> {
  if (!fields.prompt.trim()) throw new HttpError(400, "Prompt required");
  if (!uploads.length) throw new HttpError(400, "Add at least one photo");
  const id = crypto.randomUUID();
  const photos: JobPhoto[] = [];
  for (const [i, u] of uploads.entries()) {
    const path = `input/photo_${i + 1}.${u.ext}`;
    await store.upload("inputs", `${id}/${path}`, u.blob, u.type);
    photos.push({ path, sha256: await sha256Hex(u.blob) });
  }
  return insertJobRows(store, actor, id, fields, photos, log);
}

/** Insert a job whose photos are already in inputs/<id>/input/: draft, prompt message, queued. */
export async function insertJobRows(
  store: ServerStore,
  actor: Actor,
  id: string,
  fields: JobFields,
  photos: JobPhoto[],
  log: Logger,
): Promise<JobRow> {
  if (!fields.prompt.trim()) throw new HttpError(400, "Prompt required");
  if (!photos.length) throw new HttpError(400, "Add at least one photo");
  const own = ownership(actor);
  const draft = await store.insertJob({
    id,
    ...own,
    title: fields.title ?? null,
    prompt: fields.prompt.trim(),
    product_type: fields.productType,
    profile: fields.profile,
    target_size_mm: fields.targetSizeMm,
    addons: fields.addons,
    photos,
    status: "draft",
    ...(fields.form ? { form: fields.form } : {}),
  });
  await store.insertMessage({
    job_id: id,
    owner_id: own.owner_id,
    role: "user",
    kind: "prompt",
    stage: null,
    body: draft.prompt,
    payload: {},
    reply_to: null,
  } as never);
  const job = await store.updateJob(id, { status: "queued" });
  log.info("job created", { job_id: id, product: fields.productType, photos: photos.length, access: own.access });
  return job;
}

/** Check new-job fields (same names as NewJobInput). `get` reads a string field. */
export function parseJobFields(get: (k: string) => unknown, addonsIn: unknown): JobFields {
  const s = (k: string) => {
    const v = get(k);
    return typeof v === "string" ? v.trim() : typeof v === "number" ? String(v) : "";
  };
  const productType = s("productType") as ProductType;
  if (!(productType in PRODUCT_DEFAULTS)) throw new HttpError(400, "Bad product type");
  const profile = (s("profile") || "thin_slice") as Profile;
  if (profile !== "thin_slice" && profile !== "full") throw new HttpError(400, "Bad profile");
  const size = s("targetSizeMm");
  const targetSizeMm = size ? Number(size) : null;
  if (targetSizeMm != null && (!Number.isFinite(targetSizeMm) || targetSizeMm < 10 || targetSizeMm > 300)) {
    throw new HttpError(400, "Size 10 to 300 mm");
  }
  let addons: AddOn[] = addonsFor(productType);
  if (addonsIn != null && addonsIn !== "") {
    try {
      addons = (typeof addonsIn === "string" ? JSON.parse(addonsIn) : addonsIn) as AddOn[];
    } catch {
      throw new HttpError(400, "Bad add-ons");
    }
    // AddOn has no schema of its own; IntakeResult carries addons, so validate through it.
    const v = validatePayload("questions", { subjects: [], questions: [], product_type: productType, addons });
    if (!v.ok) throw new HttpError(400, "Bad add-ons", { errors: v.errors });
  }
  const prompt = s("prompt");
  if (!prompt) throw new HttpError(400, "Prompt required");
  if (prompt.length > 2000) throw new HttpError(400, "Prompt too long");
  const formIn = s("form");
  if (formIn && formIn !== "flat" && formIn !== "figure") throw new HttpError(400, "Bad form");
  // Only keychains and magnets have a form choice; every other product is a 3D figure.
  const form = formIn && formIsChoice(productType) ? (formIn as ProductForm) : null;
  return { prompt, productType, targetSizeMm, profile, addons, title: s("title").slice(0, 120) || null, form };
}

export const MAX_JOB_PHOTOS = 8;

/** Photo metadata from the browser for a signed-upload job: type and size checks. */
export function jobPhotoMeta(raw: unknown): { type: string; ext: string; size: number }[] {
  const list = Array.isArray(raw) ? raw : [];
  if (!list.length) throw new HttpError(400, "Add at least one photo");
  if (list.length > MAX_JOB_PHOTOS) throw new HttpError(400, `Max ${MAX_JOB_PHOTOS} photos`);
  return list.map((p) => {
    const meta = (p ?? {}) as { name?: string; type?: string; size?: number };
    const t = photoType({ name: meta.name, type: meta.type });
    if (!t) throw new HttpError(400, "Photos must be JPEG, PNG, HEIC or WebP");
    const size = Number(meta.size ?? 0);
    if (!(size > 0) || size > 15 * 1024 * 1024) throw new HttpError(400, "Each photo must be under 15 MB");
    return { ...t, size };
  });
}

/** Create a job from an order: copy its photos into inputs, set product, size and add-ons. */
export async function startJobFromOrder(
  store: ServerStore,
  actor: Actor,
  orderId: string,
  log: Logger,
): Promise<{ job: JobRow; order: OrderRow; created: boolean }> {
  const order = await store.getOrder(orderId);
  if (!order) throw new HttpError(404, "Order not found");
  if (order.job_id) {
    const existing = await store.getJob(order.job_id);
    if (existing) return { job: existing, order, created: false };
  }
  if (!order.photos.length) throw new HttpError(400, "Order has no photos");
  const uploads: Upload[] = [];
  for (const key of order.photos) {
    const blob = await store.download("orders", key);
    const ext = /\.([a-z0-9]+)$/i.exec(key)?.[1]?.toLowerCase() ?? "jpg";
    const type = Object.entries(PHOTO_TYPES).find(([, e]) => e === ext)?.[0] ?? (blob.type || "image/jpeg");
    uploads.push({ blob, type, ext });
  }
  const job = await createJob(
    store,
    actor,
    {
      prompt: order.request || PRODUCT_LABELS[order.product_type],
      productType: order.product_type,
      targetSizeMm: order.size_mm ?? PRODUCT_DEFAULTS[order.product_type].target_size_mm,
      profile: "thin_slice",
      addons: addonsFor(order.product_type, order.name_text),
      title: `${PRODUCT_LABELS[order.product_type]} for ${order.name}`.slice(0, 120),
    },
    uploads,
    log,
  );
  const updated = await store.updateOrder(order.id, { job_id: job.id, status: "accepted" });
  log.info("order started", { order_id: order.id, job_id: job.id });
  return { job, order: updated, created: true };
}

export interface MessageBody {
  kind: UserPayloadKind;
  payload: unknown;
  reply_to?: string | null;
  stage?: StageId | null;
  body?: string | null;
}

const USER_KINDS = Object.entries(MESSAGE_ROLES)
  .filter(([k, r]) => r === "user" && k !== "prompt")
  .map(([k]) => k);

export async function sendUserMessage(
  store: ServerStore,
  actor: Actor,
  jobId: string,
  msg: MessageBody,
): Promise<AnyMessage> {
  if (!USER_KINDS.includes(msg.kind)) throw new HttpError(400, "Not a user message kind");
  const v = validatePayload(msg.kind, msg.payload);
  if (!v.ok) throw new HttpError(400, "Invalid payload", { errors: v.errors });
  const job = await store.getJob(jobId);
  if (!job) throw new HttpError(404, "Job not found");
  return store.insertMessage({
    job_id: jobId,
    owner_id: ownership(actor).owner_id ?? job.owner_id,
    role: "user",
    kind: msg.kind,
    stage: msg.stage ?? job.stage,
    body: msg.body ?? null,
    payload: msg.payload,
    reply_to: msg.reply_to ?? null,
  } as never);
}

export async function saveFilament(store: ServerStore, actor: Actor, input: FilamentInput): Promise<FilamentRow> {
  const existing = input.id ? ((await store.getFilament(input.id)) ?? undefined) : undefined;
  if (input.id && !existing) throw new HttpError(404, "Filament not found");
  const row = filamentRowFrom(input, existing, new Date().toISOString(), ownership(actor).owner_id);
  if (!input.id && (await store.getFilament(row.id))) throw new HttpError(409, `${row.id} exists`);
  return store.upsertFilament(row, !input.id);
}
