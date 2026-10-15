// First look: an optional quick sketch on the confirmation page, one per order.
// POST {receipt}: off unless FIRST_LOOK_ENABLED and MESHY_API_KEY (mock mode uses the fake and needs
// no key); one per order; a daily cap (America/New_York day) counted from spend rows; the spend
// row is inserted first as a reservation and backed out when another request won the race; a
// 10 minute signed URL for the first photo goes to Meshy Image to Image (never persisted).
// GET ?t=<task token>: reads meshy_events for the task (the meshy-webhook edge function writes
// them; Meshy is never polled); on SUCCEEDED downloads image_urls[0] once, stores it at
// orders/<order_id>/first-look.png and returns a 1 hour signed URL.

import { serverEnv, type ServerEnv } from "./env";
import { FIRST_LOOK_MODEL, firstLookPrompt } from "./firstLookPrompt";
import { verifyReceipt } from "./receipt";
import type { MeshyEventRow, ServerStore } from "./store";
import { signTicket, verifyTicket } from "./ticket";

export const MESHY_IMAGE_TO_IMAGE = "https://api.meshy.ai/openapi/v1/image-to-image";
export const FIRST_LOOK_KIND = "first_look";
const CREDITS = 3;
const USD_PER_CREDIT = 0.02;
const PHOTO_URL_TTL_S = 600;
const TASK_TTL_S = 3600;

export type FirstLookState =
  | { state: "off" }
  | { state: "capped" }
  | { state: "failed" }
  | { state: "printing"; task: string | null; progress?: number }
  | { state: "done"; url: string };

export interface MeshyImageApi {
  start(req: { prompt: string; imageUrl: string }): Promise<{ taskId: string }>;
}

/** Live Meshy. Only built when first look is enabled with a key, outside mock mode. */
export function liveMeshy(apiKey: string, fetchImpl: typeof fetch = fetch): MeshyImageApi {
  return {
    async start({ prompt, imageUrl }) {
      const res = await fetchImpl(MESHY_IMAGE_TO_IMAGE, {
        method: "POST",
        headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
        body: JSON.stringify({
          ai_model: FIRST_LOOK_MODEL,
          prompt,
          reference_image_urls: [imageUrl],
          aspect_ratio: "1:1",
          remove_background: true,
        }),
      });
      if (!res.ok) throw new Error(`meshy ${res.status}`);
      const body = (await res.json()) as { result?: string };
      if (!body.result) throw new Error("meshy: no task id");
      return { taskId: body.result };
    },
  };
}

/**
 * The fake used by tests and mock mode: returns a task id and, after `delayMs`, writes a
 * SUCCEEDED meshy_events row (as the webhook would) pointing at `imageUrl`.
 */
export function fakeMeshy(
  events: { meshyEvents: MeshyEventRow[] },
  imageUrl: string,
  delayMs = 8000,
  outcome: "SUCCEEDED" | "FAILED" = "SUCCEEDED",
): MeshyImageApi & { calls: { prompt: string; imageUrl: string }[] } {
  const calls: { prompt: string; imageUrl: string }[] = [];
  let seq = 0;
  return {
    calls,
    async start(req) {
      calls.push(req);
      const taskId = `fake-${crypto.randomUUID()}`;
      const write = () =>
        events.meshyEvents.push({
          id: ++seq,
          received_at: new Date().toISOString(),
          task_id: taskId,
          status: outcome,
          event: { status: outcome, progress: 100, image_urls: outcome === "SUCCEEDED" ? [imageUrl] : [] },
          processed_at: null,
        });
      if (delayMs <= 0) write();
      else setTimeout(write, delayMs);
      return { taskId };
    },
  };
}

/** Tests set a fake Meshy for the route here; undefined = choose from the environment. */
export function setFirstLookMeshy(api: MeshyImageApi | null | undefined) {
  (globalThis as unknown as { __p2pFirstLookMeshy?: MeshyImageApi | null }).__p2pFirstLookMeshy = api;
}

export interface FirstLookDeps {
  store: ServerStore;
  meshy: MeshyImageApi | null;
  env?: ServerEnv;
  fetchImpl?: typeof fetch;
  now?: () => number;
}

/** Midnight in New York for the day containing `now`, as an ISO string. */
export function nyDayStart(now: number): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(now));
  const get = (t: string) => Number(parts.find((p) => p.type === t)?.value ?? 0);
  const wall = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second"));
  const offset = wall - Math.floor(now / 1000) * 1000;
  return new Date(Date.UTC(get("year"), get("month") - 1, get("day")) - offset).toISOString();
}

export const firstLookKey = (orderId: string) => `${orderId}/first-look.png`;

async function storedImage(store: ServerStore, orderId: string): Promise<string | null> {
  const objects = await store.listObjects("orders", orderId).catch(() => []);
  if (!objects.some((o) => o.key === firstLookKey(orderId))) return null;
  return store.signedUrl("orders", firstLookKey(orderId), 3600);
}

/** What the confirmation page shows on load: the stored image, else nothing yet. */
export async function firstLookOnLoad(store: ServerStore, orderId: string): Promise<string | null> {
  return storedImage(store, orderId);
}

export async function startFirstLook(receipt: unknown, deps: FirstLookDeps): Promise<FirstLookState | null> {
  const r = await verifyReceipt(receipt);
  if (!r) return null;
  const env = deps.env ?? serverEnv();
  if (!env.firstLookEnabled || !deps.meshy) return { state: "off" };
  const { store } = deps;
  const now = deps.now?.() ?? Date.now();

  const existing = await store.listSpendByKind(FIRST_LOOK_KIND, { ref: r.id });
  if (existing.length) {
    const url = await storedImage(store, r.id);
    return url ? { state: "done", url } : { state: "printing", task: null };
  }
  const since = nyDayStart(now);
  const cap = env.firstLookDailyCap;
  if (cap <= 0 || (await store.listSpendByKind(FIRST_LOOK_KIND, { since })).length >= cap) return { state: "capped" };

  const order = await store.getOrder(r.id);
  if (!order || !order.photos.length) return { state: "failed" };

  // Reserve first, then check nobody else did.
  const mine = await store.insertSpend({
    vendor: "meshy",
    kind: FIRST_LOOK_KIND,
    credits: CREDITS,
    usd: CREDITS * USD_PER_CREDIT,
    ref: r.id,
    test_mode: env.meshyMode !== "live",
    ...({ billing: "credits", usd_list: CREDITS * USD_PER_CREDIT } as object),
  });
  const forOrder = await store.listSpendByKind(FIRST_LOOK_KIND, { ref: r.id });
  if (forOrder[0]?.id !== mine.id) {
    await store.deleteSpend(mine.id);
    return { state: "printing", task: null };
  }
  const today = await store.listSpendByKind(FIRST_LOOK_KIND, { since });
  if (today.findIndex((s) => s.id === mine.id) >= cap) {
    await store.deleteSpend(mine.id);
    return { state: "capped" };
  }

  try {
    const imageUrl = await store.signedUrl("orders", order.photos[0], PHOTO_URL_TTL_S);
    const { taskId } = await deps.meshy.start({ prompt: firstLookPrompt(order.product_type, order.request), imageUrl });
    const task = await signTicket("first_look", { order_id: r.id, task_id: taskId }, TASK_TTL_S);
    return { state: "printing", task };
  } catch {
    // The reservation stays: one per order, and a failed call may still have spent credits.
    return { state: "failed" };
  }
}

export async function pollFirstLook(token: unknown, deps: FirstLookDeps): Promise<FirstLookState | null> {
  const t = await verifyTicket<{ order_id: string; task_id: string }>("first_look", token);
  if (!t) return null;
  const { store } = deps;
  const stored = await storedImage(store, t.order_id);
  if (stored) return { state: "done", url: stored };
  const events = await store.listMeshyEvents(t.task_id);
  const latest = events[0];
  const status = (latest?.status ?? latest?.event?.status ?? "").toUpperCase();
  if (status === "SUCCEEDED") {
    const src = latest.event.image_urls?.[0];
    if (!src) return { state: "failed" };
    const res = await (deps.fetchImpl ?? fetch)(src);
    if (!res.ok) return { state: "failed" };
    await store.upload("orders", firstLookKey(t.order_id), await res.blob(), "image/png");
    return { state: "done", url: await store.signedUrl("orders", firstLookKey(t.order_id), 3600) };
  }
  if (status === "FAILED" || status === "EXPIRED" || status === "CANCELED") return { state: "failed" };
  return { state: "printing", task: token as string, progress: Number(latest?.event?.progress ?? 0) || 0 };
}
