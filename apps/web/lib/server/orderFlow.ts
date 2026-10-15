// Order submission in two calls, so 15 MB photos never pass through a function body:
//   start:    validate fields and photo metadata, rate-limit by IP, return an order id, signed
//             upload URLs for orders/<order_id>/photo_<n>.<ext>, and a signed ticket.
//   (browser PUTs each photo to its URL)
//   complete: verify the ticket and the uploaded objects, insert public.orders, then notify.
// Email never blocks the order: notifyOrder runs after the response and records its outcome.

import type { OrderRow } from "../db.types";
import { serverEnv } from "./env";
import { jsonError, type RouteCtx } from "./log";
import { notifyOrder, type NotifyResult } from "./notify";
import type { PriceQuote } from "../pricing";
import { ORDER_LIMITS, orderPhotoKey, validateOrderJson, type OrderInput } from "./orders";
import { limiters } from "./rateLimit";
import { signReceipt } from "./receipt";
import type { ServerStore } from "./store";
import { signTicket, verifyTicket } from "./ticket";

type Ctx = RouteCtx<unknown> & { store: ServerStore };
export type Schedule = (task: () => Promise<void>) => void;

interface OrderTicket {
  id: string;
  order: Omit<OrderInput, "photos" | "spam" | "quote">;
  quote: PriceQuote;
  spam: boolean;
  photos: { key: string; type: string; size: number }[];
}

export async function startOrder(req: Request, ctx: Ctx): Promise<Response> {
  const gate = limiters.orders.peek(ctx.ip);
  if (!gate.ok) {
    ctx.log.warn("rate limited", { retry_after_s: gate.retryAfterS });
    return jsonError(429, "Too many orders", { retry_after_s: gate.retryAfterS });
  }
  const body = await req.json().catch(() => null);
  const v = validateOrderJson(body);
  if (!v.ok) {
    ctx.log.info("invalid order", { fields: Object.keys(v.errors) });
    return jsonError(400, "invalid", { errors: v.errors, quote: v.quote });
  }
  limiters.orders.hit(ctx.ip);
  const id = crypto.randomUUID();
  const { photos, spam, quote, ...order } = v.value;
  const targets = await Promise.all(
    photos.map((p, i) => ctx.store.createUploadUrl("orders", orderPhotoKey(id, i + 1, p.ext), p.type)),
  );
  const ticket = await signTicket<OrderTicket>("order", {
    id,
    order,
    quote,
    spam,
    photos: photos.map((p, i) => ({ key: targets[i].key, type: p.type, size: p.size })),
  });
  ctx.log.info("order started", { order_id: id, product: order.product_type, photos: photos.length, spam });
  return Response.json({ order_id: id, ticket, uploads: targets });
}

export async function completeOrder(req: Request, ctx: Ctx, schedule: Schedule): Promise<Response> {
  const body = (await req.json().catch(() => null)) as { ticket?: unknown } | null;
  const t = await verifyTicket<OrderTicket>("order", body?.ticket);
  if (!t) return jsonError(400, "Invalid or expired ticket");

  const existing = await ctx.store.getOrder(t.id);
  if (existing) {
    const receipt = await signReceipt({ id: existing.id, quote: t.quote });
    return Response.json({ order_id: existing.id, status: existing.status, receipt });
  }

  const objects = await ctx.store.listObjects("orders", t.id);
  const sizes = new Map(objects.map((o) => [o.key, o.size]));
  const missing = t.photos.filter((p) => !sizes.has(p.key));
  if (missing.length) {
    ctx.log.warn("photos missing", { order_id: t.id, missing: missing.length });
    return jsonError(400, "Photos not uploaded", { missing: missing.map((p) => p.key) });
  }
  if (t.photos.some((p) => (sizes.get(p.key) ?? 0) > ORDER_LIMITS.maxPhotoBytes)) {
    await ctx.store.remove("orders", t.photos.map((p) => p.key));
    return jsonError(400, "Each photo must be under 15 MB");
  }

  const row = await ctx.store.insertOrder({
    id: t.id,
    ...t.order,
    status: t.spam ? "spam" : "new",
    photos: t.photos.map((p) => p.key),
  });
  ctx.log.info("order stored", { order_id: row.id, status: row.status });

  if (!t.spam) {
    const origin = new URL(req.url).origin;
    const log = ctx.log;
    const store = ctx.store;
    schedule(async () => {
      const r = await notify(row, origin);
      await recordNotify(store, row.id, r).catch((e) => log.error("notify record failed", { error: e }));
      log.info("order notify", { order_id: row.id, ok: r.ok, error: r.ok ? undefined : r.error });
    });
  }
  const receipt = await signReceipt({ id: row.id, quote: t.quote });
  return Response.json({ order_id: row.id, status: row.status, receipt });
}

/** RFC 2606 reserved domains: test orders never send email. */
export function isTestAddress(email: string): boolean {
  return /@(example\.(com|org|net)|[^@]+\.(test|example|invalid))$/i.test(email.trim());
}

async function notify(order: OrderRow, origin: string): Promise<NotifyResult> {
  if (order.email && isTestAddress(order.email)) return { ok: false, error: "test order" };
  const env = serverEnv();
  return notifyOrder(order, {
    apiKey: env.agentmailKey,
    inboxId: env.agentmailInbox,
    to: env.notifyEmail,
    appUrl: origin,
  });
}

export async function recordNotify(store: ServerStore, orderId: string, r: NotifyResult): Promise<void> {
  await store.updateOrder(
    orderId,
    r.ok ? { notified_at: new Date().toISOString(), notify_error: null } : { notify_error: r.error.slice(0, 500) },
  );
}

/** Run a task after the response when inside a request, else inline without awaiting. */
export function scheduleAfter(afterFn: (task: () => Promise<void>) => void): Schedule {
  return (task) => {
    try {
      afterFn(task);
    } catch {
      void task();
    }
  };
}
