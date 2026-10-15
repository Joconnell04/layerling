// Order notification through AgentMail's HTTP API:
// POST https://api.agentmail.to/v0/inboxes/{inbox_id}/messages/send, Authorization: Bearer <key>,
// JSON {to, subject, text, html, reply_to}. Returns {message_id, thread_id}.
// Never throws: the caller records notified_at or notify_error and the order stands either way.

import type { OrderRow } from "../db.types";
import { PRODUCT_LABELS } from "../products";

export const AGENTMAIL_BASE = "https://api.agentmail.to/v0";
const TIMEOUT_MS = 10_000;

export interface NotifyConfig {
  apiKey: string | null;
  inboxId: string | null;
  to: string | null;
  /** Absolute origin for the admin link, e.g. https://layerling.example */
  appUrl?: string | null;
}

export type NotifyResult = { ok: true; messageId: string | null } | { ok: false; error: string };

export function orderEmail(order: OrderRow, appUrl?: string | null): { subject: string; text: string; html: string } {
  const product = PRODUCT_LABELS[order.product_type] ?? order.product_type;
  const tier = order.size_tier ? order.size_tier.toUpperCase() : "";
  const subject = `layerling order: ${product}${tier ? ` ${tier}` : ""}${order.name_text ? ` "${order.name_text}"` : ""} for ${order.name}`;
  const rows: [string, string][] = [
    ["Name", order.name],
    ["Email", order.email ?? ""],
    ["Phone", order.phone ?? ""],
    ["Product", product],
    ["Size", [tier, order.size_mm ? `${order.size_mm} mm` : ""].filter(Boolean).join(", ")],
    ["Name text", order.name_text ?? ""],
    ["Photos", String(order.photos.length)],
    ["Source", order.source ?? ""],
    ["Order", order.id],
  ];
  const admin = appUrl ? `${appUrl.replace(/\/$/, "")}/admin/orders` : null;
  const text = [
    ...rows.filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`),
    ...(order.request ? ["", order.request] : []),
    ...(admin ? ["", admin] : []),
  ].join("\n");
  const esc = (s: string) =>
    s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
  const html = [
    "<table>",
    ...rows.filter(([, v]) => v).map(([k, v]) => `<tr><td><b>${esc(k)}</b></td><td>${esc(v)}</td></tr>`),
    "</table>",
    order.request ? `<p>${esc(order.request).replace(/\n/g, "<br>")}</p>` : "",
    admin ? `<p><a href="${esc(admin)}">Orders</a></p>` : "",
  ].join("");
  return { subject, text, html };
}

export async function notifyOrder(
  order: OrderRow,
  cfg: NotifyConfig,
  fetchImpl: typeof fetch = fetch,
): Promise<NotifyResult> {
  if (!cfg.apiKey || !cfg.inboxId) return { ok: false, error: "no provider" };
  if (!cfg.to) return { ok: false, error: "no recipient" };
  const { subject, text, html } = orderEmail(order, cfg.appUrl);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const res = await fetchImpl(`${AGENTMAIL_BASE}/inboxes/${encodeURIComponent(cfg.inboxId)}/messages/send`, {
      method: "POST",
      headers: { Authorization: `Bearer ${cfg.apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({ to: [cfg.to], subject, text, html, ...(order.email ? { reply_to: [order.email] } : {}) }),
      signal: ctrl.signal,
    });
    if (!res.ok) {
      const detail = (await res.text().catch(() => "")).slice(0, 200);
      return { ok: false, error: `agentmail ${res.status}${detail ? `: ${detail}` : ""}` };
    }
    const body = (await res.json().catch(() => ({}))) as { message_id?: string };
    return { ok: true, messageId: body.message_id ?? null };
  } catch (e) {
    const err = e as Error;
    return { ok: false, error: err.name === "AbortError" ? "agentmail timeout" : `agentmail ${err.message}` };
  } finally {
    clearTimeout(timer);
  }
}
