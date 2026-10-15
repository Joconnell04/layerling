// Access-code session cookie: `v1.<exp>.<nonce>.<sig>`, sig = HMAC-SHA256(secret, "v1.<exp>.<nonce>")
// in base64url. Web Crypto only, so it runs in route handlers, the proxy and tests alike.
// The access code itself never reaches the client; the cookie is httpOnly.

export const ACCESS_COOKIE = "p2p_access";
export const ACCESS_TTL_S = 30 * 24 * 60 * 60;
const MIN_SECRET = 16;

const enc = new TextEncoder();

function b64url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromB64url(s: string): Uint8Array | null {
  try {
    const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));
    return Uint8Array.from(bin, (c) => c.charCodeAt(0));
  } catch {
    return null;
  }
}

/** Constant-time comparison for equal-length byte arrays (length itself is not secret here). */
export function timingSafeEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

async function hmac(secret: string, data: string): Promise<Uint8Array> {
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(data)));
}

async function sha256(data: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(data)));
}

export function secretUsable(secret: string | null | undefined): secret is string {
  return !!secret && secret.length >= MIN_SECRET;
}

export async function signAccess(
  secret: string,
  nowMs: number = Date.now(),
  ttlS: number = ACCESS_TTL_S,
): Promise<string> {
  if (!secretUsable(secret)) throw new Error("ACCESS_COOKIE_SECRET missing or shorter than 16 characters");
  const exp = Math.floor(nowMs / 1000) + ttlS;
  const nonce = b64url(crypto.getRandomValues(new Uint8Array(12)));
  const body = `v1.${exp}.${nonce}`;
  return `${body}.${b64url(await hmac(secret, body))}`;
}

export type VerifyResult = { ok: true; exp: number } | { ok: false; reason: "missing" | "malformed" | "bad_signature" | "expired" | "no_secret" };

export async function verifyAccess(
  value: string | null | undefined,
  secret: string | null | undefined,
  nowMs: number = Date.now(),
): Promise<VerifyResult> {
  if (!secretUsable(secret)) return { ok: false, reason: "no_secret" };
  if (!value) return { ok: false, reason: "missing" };
  const parts = value.split(".");
  if (parts.length !== 4 || parts[0] !== "v1" || !/^\d{1,12}$/.test(parts[1])) {
    return { ok: false, reason: "malformed" };
  }
  // The signature must be the canonical base64url of exactly 32 bytes: the last character of a
  // 43-character encoding carries 2 unused bits, so non-canonical spellings of a valid signature
  // would otherwise decode to the same bytes and pass.
  const sig = fromB64url(parts[3]);
  if (!sig || sig.length !== 32 || b64url(sig) !== parts[3]) return { ok: false, reason: "malformed" };
  const expected = await hmac(secret, parts.slice(0, 3).join("."));
  // Constant time over the raw 32-byte HMACs, never over encoded strings.
  if (!timingSafeEqual(sig, expected)) return { ok: false, reason: "bad_signature" };
  const exp = Number(parts[1]);
  if (exp * 1000 <= nowMs) return { ok: false, reason: "expired" };
  return { ok: true, exp };
}

/**
 * Compare a submitted code with ACCESS_CODE in constant time. Both sides are hashed first so the
 * comparison length does not depend on either input.
 */
export async function codeMatches(input: string, expected: string | null | undefined): Promise<boolean> {
  if (!expected) return false;
  // ACCESS_CODE may hold several codes separated by commas; every one is checked so timing does
  // not reveal which matched.
  const codes = expected.split(",").map((c) => c.trim()).filter(Boolean);
  if (codes.length === 0) return false;
  const a = await sha256(`p2p-code:${input.trim()}`);
  let ok = false;
  for (const code of codes) {
    const b = await sha256(`p2p-code:${code}`);
    if (timingSafeEqual(a, b)) ok = true;
  }
  return ok;
}

export interface CookieOptions {
  httpOnly: true;
  secure: boolean;
  sameSite: "lax";
  path: "/";
  maxAge: number;
}

/** Secure everywhere except plain-http localhost during development. */
export function accessCookieOptions(req: Request, maxAge: number = ACCESS_TTL_S): CookieOptions {
  const url = new URL(req.url);
  const local = url.protocol === "http:" && ["localhost", "127.0.0.1"].includes(url.hostname);
  return { httpOnly: true, secure: !local, sameSite: "lax", path: "/", maxAge };
}

export function readCookie(req: Request, name: string): string | null {
  const header = req.headers.get("cookie");
  if (!header) return null;
  for (const part of header.split(";")) {
    const i = part.indexOf("=");
    if (i === -1) continue;
    if (part.slice(0, i).trim() === name) return decodeURIComponent(part.slice(i + 1).trim());
  }
  return null;
}
