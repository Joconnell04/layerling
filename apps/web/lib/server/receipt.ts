// The confirmation link. completeOrder signs a receipt (kind "receipt", 30 days) holding the order
// id and the quote the server computed at order time; /order/thanks?r=<receipt> verifies it and
// loads the order with the service store. An invalid or expired receipt shows the generic page.

import type { PriceQuote } from "../pricing";
import { signTicket, verifyTicket } from "./ticket";

export const RECEIPT_TTL_S = 30 * 24 * 3600;

export interface Receipt {
  id: string;
  quote: PriceQuote;
}

export function signReceipt(r: Receipt, now = Date.now()): Promise<string> {
  return signTicket<Receipt>("receipt", r, RECEIPT_TTL_S, now);
}

export async function verifyReceipt(token: unknown, now = Date.now()): Promise<Receipt | null> {
  const r = await verifyTicket<Receipt>("receipt", token, now);
  if (!r || typeof r.id !== "string" || !/^[0-9a-f-]{36}$/i.test(r.id)) return null;
  return r;
}

/** "m•••@gmail.com": the first letter of the user part, the whole domain. */
export function maskEmail(email: string): string {
  const at = email.lastIndexOf("@");
  if (at < 1) return "•••";
  return `${email[0]}•••${email.slice(at)}`;
}

/** "(•••) •••-1234": the last four digits only. */
export function maskPhone(phone: string): string {
  const digits = phone.replace(/\D/g, "");
  return `(•••) •••-${digits.slice(-4).padStart(4, "•")}`;
}

/** "Order 98edb3f8": the first 8 hex characters of the id. */
export function shortRef(id: string): string {
  return id.replace(/-/g, "").slice(0, 8);
}
