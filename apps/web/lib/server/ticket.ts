// Stateless, HMAC-signed tickets that carry validated form data between the "start" call (which
// hands out signed upload URLs) and the "complete" call (which writes the row). Needed because
// photos go straight to Storage: Vercel functions cap request bodies at 4.5 MB.

import { serverEnv } from "./env";

const enc = new TextEncoder();
const g = globalThis as unknown as { __p2pTicketKey?: string };

function b64url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromB64url(s: string): Uint8Array {
  const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

/** ACCESS_COOKIE_SECRET with a ticket-only suffix; a random per-process key in mock mode. */
function ticketKey(): string {
  const s = serverEnv().cookieSecret;
  if (s) return `${s}:ticket`;
  return (g.__p2pTicketKey ??= b64url(crypto.getRandomValues(new Uint8Array(32))));
}

async function hmac(key: string, data: string): Promise<Uint8Array> {
  const k = await crypto.subtle.importKey("raw", enc.encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", k, enc.encode(data)));
}

export async function signTicket<T extends object>(kind: string, data: T, ttlS = 2 * 3600, now = Date.now()): Promise<string> {
  const body = b64url(enc.encode(JSON.stringify({ k: kind, exp: Math.floor(now / 1000) + ttlS, d: data })));
  return `${body}.${b64url(await hmac(ticketKey(), body))}`;
}

export async function verifyTicket<T>(kind: string, token: unknown, now = Date.now()): Promise<T | null> {
  if (typeof token !== "string" || token.length > 20_000) return null;
  const [body, sig] = token.split(".");
  if (!body || !sig) return null;
  try {
    const expected = await hmac(ticketKey(), body);
    const given = fromB64url(sig);
    // Canonical encoding of exactly 32 bytes only (see accessCookie.verifyAccess).
    if (given.length !== expected.length || b64url(given) !== sig) return null;
    let diff = 0;
    for (let i = 0; i < given.length; i++) diff |= given[i] ^ expected[i];
    if (diff !== 0) return null;
    const parsed = JSON.parse(new TextDecoder().decode(fromB64url(body))) as { k: string; exp: number; d: T };
    if (parsed.k !== kind || parsed.exp * 1000 <= now) return null;
    return parsed.d;
  } catch {
    return null;
  }
}
