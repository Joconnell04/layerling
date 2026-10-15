// @vitest-environment node
import {
  accessCookieOptions,
  codeMatches,
  readCookie,
  signAccess,
  timingSafeEqual,
  verifyAccess,
} from "./accessCookie";
import { createLogger, redact } from "./log";
import { notifyOrder, orderEmail } from "./notify";
import { ORDER_COPY, ORDER_LIMITS, validateOrder } from "./orders";
import { RateLimiter } from "./rateLimit";
import { maskEmail, maskPhone, shortRef } from "./receipt";
import type { OrderRow } from "../db.types";
import { priceRange, quote } from "../pricing";

const SECRET = "0123456789abcdef0123456789abcdef";  // gitleaks:allow (test fake)
const DAY = 24 * 3600 * 1000;
const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

function b64urlDecode(s: string): Uint8Array {
  return new Uint8Array(Buffer.from(s, "base64url"));
}
function b64urlEncode(b: Uint8Array): string {
  return Buffer.from(b).toString("base64url");
}

describe("access cookie", () => {
  it("signs and verifies", async () => {
    const now = Date.parse("2026-09-24T00:00:00Z");
    const v = await signAccess(SECRET, now);
    expect(v).toMatch(/^v1\.\d+\.[\w-]+\.[\w-]+$/);
    const r = await verifyAccess(v, SECRET, now + DAY);
    expect(r).toEqual({ ok: true, exp: Math.floor(now / 1000) + 30 * 24 * 3600 });
  });

  it("expires after 30 days", async () => {
    const now = Date.parse("2026-09-24T00:00:00Z");
    const v = await signAccess(SECRET, now);
    expect((await verifyAccess(v, SECRET, now + 29 * DAY)).ok).toBe(true);
    expect(await verifyAccess(v, SECRET, now + 30 * DAY)).toEqual({ ok: false, reason: "expired" });
  });

  it("rejects tampering, a wrong secret and junk", async () => {
    const now = Date.now();
    const v = await signAccess(SECRET, now);
    const [ver, exp, nonce, sig] = v.split(".");
    const later = `${ver}.${Number(exp) + 999999}.${nonce}.${sig}`;
    expect(await verifyAccess(later, SECRET, now)).toEqual({ ok: false, reason: "bad_signature" });
    // Deterministic tamper: flip one bit in each byte of the decoded signature, re-encode.
    for (const i of [0, 15, 31]) {
      const bytes = b64urlDecode(sig);
      bytes[i] ^= 0x01;
      const tampered = `${ver}.${exp}.${nonce}.${b64urlEncode(bytes)}`;
      expect(await verifyAccess(tampered, SECRET, now)).toEqual({ ok: false, reason: "bad_signature" });
    }
    // Non-canonical spelling of the same 32 bytes (the last character's 2 unused bits set).
    const last = B64.indexOf(sig.at(-1)!);
    const alias = `${ver}.${exp}.${nonce}.${sig.slice(0, -1)}${B64[(last & ~0b11) | (last & 0b11 ? 0 : 0b01)]}`;
    expect(b64urlDecode(alias.split(".")[3])).toEqual(b64urlDecode(sig));
    expect(await verifyAccess(alias, SECRET, now)).toEqual({ ok: false, reason: "malformed" });
    // Truncated or padded signatures.
    expect((await verifyAccess(`${ver}.${exp}.${nonce}.${sig.slice(0, -2)}`, SECRET, now)).ok).toBe(false);
    expect((await verifyAccess(`${v}AAAA`, SECRET, now)).ok).toBe(false);
    expect(await verifyAccess(v, "another-secret-of-32-characters!", now)).toEqual({
      ok: false,
      reason: "bad_signature",
    });
    expect(await verifyAccess("v1.abc", SECRET, now)).toEqual({ ok: false, reason: "malformed" });
    expect(await verifyAccess(null, SECRET, now)).toEqual({ ok: false, reason: "missing" });
    expect(await verifyAccess(v, "short", now)).toEqual({ ok: false, reason: "no_secret" });
    await expect(signAccess("short")).rejects.toThrow(/16/);
  });

  it("compares codes in constant time and sets safe cookie options", async () => {
    expect(await codeMatches(" beagle-42 ", "beagle-42")).toBe(true);
    expect(await codeMatches("beagle-41", "beagle-42")).toBe(false);
    expect(await codeMatches("", null)).toBe(false);
    expect(await codeMatches("fox-maple-1", "beagle-42, fox-maple-1")).toBe(true);
    expect(await codeMatches("beagle-42", "beagle-42,fox-maple-1")).toBe(true);
    expect(await codeMatches("fox", "beagle-42,fox-maple-1")).toBe(false);
    expect(await codeMatches("", " , ")).toBe(false);
    expect(timingSafeEqual(new Uint8Array([1, 2]), new Uint8Array([1, 2]))).toBe(true);
    expect(timingSafeEqual(new Uint8Array([1, 2]), new Uint8Array([1, 3]))).toBe(false);
    expect(timingSafeEqual(new Uint8Array([1]), new Uint8Array([1, 2]))).toBe(false);

    const prod = accessCookieOptions(new Request("https://p2p.example/api/access"));
    expect(prod).toEqual({ httpOnly: true, secure: true, sameSite: "lax", path: "/", maxAge: 2592000 });
    expect(accessCookieOptions(new Request("http://localhost:3100/api/access")).secure).toBe(false);
    expect(
      readCookie(new Request("http://x/", { headers: { cookie: "a=1; p2p_access=v1.2.3.4" } }), "p2p_access"),
    ).toBe("v1.2.3.4");
  });
});

function orderForm(over: Record<string, string | null> = {}, photos: File[] = [jpeg()]) {
  const f = new FormData();
  const base: Record<string, string> = {
    name: "  Test   Person ",
    email: "test@example.com",
    phone: "+1 (555) 010-0000",
    product_type: "nameplate",
    size_tier: "m",
    request: "Nameplate with my beagle sitting on it",
  };
  for (const [k, v] of Object.entries({ ...base, ...over })) if (v != null) f.set(k, v);
  for (const p of photos) f.append("photos", p);
  return f;
}

function jpeg(bytes = 1024, name = "dog.jpg", type = "image/jpeg") {
  return new File([new Uint8Array(bytes)], name, { type });
}

describe("order validation", () => {
  it("accepts a valid order, derives size_mm from the tier, and quotes from pricing.json", () => {
    const r = validateOrder(orderForm({ size_mm: "999", quantity: "7", name_text: "PEPPER" }));
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.value).toMatchObject({
      name: "Test Person",
      email: "test@example.com",
      product_type: "nameplate",
      size_tier: "m",
      size_mm: 120,
      quantity: 1,
      name_text: null,
      spam: false,
      quote: quote("nameplate", "m"),
    });
    expect(r.value.photos[0]).toMatchObject({ type: "image/jpeg", ext: "jpg" });
  });

  it("rejects bad fields with per-field errors", () => {
    const r = validateOrder(orderForm({ name: "", email: "nope", product_type: "rocket", phone: "abc" }));
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(Object.keys(r.errors).sort()).toEqual(["email", "name", "phone", "product_type"].sort());
  });

  it("Other needs no tier and at least 10 characters of request", () => {
    const short = validateOrder(orderForm({ product_type: "other", size_tier: null, request: "cake" }));
    expect(!short.ok && short.errors).toEqual({ request: ORDER_COPY.otherRequest });
    const ok = validateOrder(orderForm({ product_type: "other", size_tier: null, request: "a cake topper of us" }));
    expect(ok.ok && ok.value).toMatchObject({ size_tier: null, size_mm: null, quote: priceRange("other") });
  });

  it("checks photo count, size and type", () => {
    const none = validateOrder(orderForm({}, []));
    expect(!none.ok && none.errors.photos).toMatch(/at least one/);
    const five = validateOrder(orderForm({}, [jpeg(), jpeg(), jpeg(), jpeg(), jpeg()]));
    expect(!five.ok && five.errors.photos).toMatch(/Max 4/);
    const big = validateOrder(orderForm({}, [jpeg(ORDER_LIMITS.maxPhotoBytes + 1)]));
    expect(!big.ok && big.errors.photos).toMatch(/15 MB/);
    const gif = validateOrder(orderForm({}, [jpeg(10, "a.gif", "image/gif")]));
    expect(!gif.ok && gif.errors.photos).toMatch(/JPEG/);
    const heic = validateOrder(orderForm({}, [jpeg(10, "IMG_1.HEIC", "")]));
    expect(heic.ok && heic.value.photos[0]).toMatchObject({ type: "image/heic", ext: "heic" });
  });

  it("flags the honeypot as spam", () => {
    const r = validateOrder(orderForm({ website: "http://spam" }));
    expect(r.ok && r.value.spam).toBe(true);
  });
});

describe("receipt", () => {
  it("masks contact details for the confirmation page", () => {
    expect(maskEmail("maria@gmail.com")).toBe("m•••@gmail.com");
    expect(maskEmail("bad")).toBe("•••");
    expect(maskPhone("+1 (555) 010-1234")).toBe("(•••) •••-1234");
    expect(shortRef("98edb3f8-1111-4111-8111-111111111111")).toBe("98edb3f8");
  });
});

describe("rate limiter", () => {
  it("allows max hits per window, then blocks with a retry time", () => {
    let t = 0;
    const rl = new RateLimiter(3, 60_000, () => t);
    expect([rl.hit("a").ok, rl.hit("a").ok, rl.hit("a").ok]).toEqual([true, true, true]);
    const blocked = rl.hit("a");
    expect(blocked).toEqual({ ok: false, remaining: 0, retryAfterS: 60 });
    expect(rl.hit("b").ok).toBe(true); // separate key
    t = 30_000;
    expect(rl.peek("a").retryAfterS).toBe(30);
    t = 60_001;
    expect(rl.hit("a")).toMatchObject({ ok: true, remaining: 2 });
  });

  it("resets a key and bounds memory", () => {
    const rl = new RateLimiter(1, 1000, () => 0, 3);
    rl.hit("a");
    expect(rl.peek("a").ok).toBe(false);
    rl.reset("a");
    expect(rl.peek("a").ok).toBe(true);
    for (const k of ["k1", "k2", "k3", "k4", "k5"]) rl.hit(k);
    expect(rl.size()).toBeLessThanOrEqual(3);
  });
});

const order: OrderRow = {
  id: "11111111-1111-4111-8111-111111111111",
  created_at: "2026-09-24T00:00:00Z",
  updated_at: "2026-09-24T00:00:00Z",
  status: "new",
  name: "Test <Person>",
  email: "test@example.com",
  phone: null,
  product_type: "nameplate",
  size_tier: "m",
  size_mm: 120,
  quantity: 1,
  name_text: "PEPPER",
  request: "Beagle on a plate\nsecond line",
  photos: ["x/photo_1.jpg", "x/photo_2.jpg"],
  source: "flyer",
  notified_at: null,
  notify_error: null,
  job_id: null,
};

describe("notifyOrder", () => {
  const cfg = { apiKey: "am_test_key", inboxId: "orders@agentmail.to", to: "jackson@example.com", appUrl: "https://p2p.example/" };

  it("posts to the AgentMail send endpoint with bearer auth", async () => {
    const fetchMock = vi.fn(async () => Response.json({ message_id: "m1", thread_id: "t1" }));
    const r = await notifyOrder(order, cfg, fetchMock as unknown as typeof fetch);
    expect(r).toEqual({ ok: true, messageId: "m1" });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.agentmail.to/v0/inboxes/orders%40agentmail.to/messages/send");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer am_test_key");
    const body = JSON.parse(init.body as string);
    expect(body.to).toEqual(["jackson@example.com"]);
    expect(body.reply_to).toEqual(["test@example.com"]);
    expect(body.subject).toBe('layerling order: Nameplate M "PEPPER" for Test <Person>');
    expect(body.text).toContain("Size: M, 120 mm");
    expect(body.text).toContain("Photos: 2");
    expect(body.text).toContain("https://p2p.example/admin/orders");
    expect(body.html).toContain("Test &lt;Person&gt;");
  });

  it("reports provider errors, timeouts and missing config without throwing", async () => {
    const bad = vi.fn(async () => new Response("quota", { status: 429 }));
    expect(await notifyOrder(order, cfg, bad as unknown as typeof fetch)).toEqual({ ok: false, error: "agentmail 429: quota" });
    const boom = vi.fn(async () => {
      throw new TypeError("network down");
    });
    expect(await notifyOrder(order, cfg, boom as unknown as typeof fetch)).toEqual({
      ok: false,
      error: "agentmail network down",
    });
    const never = vi.fn();
    expect(await notifyOrder(order, { ...cfg, apiKey: null }, never as unknown as typeof fetch)).toEqual({
      ok: false,
      error: "no provider",
    });
    expect(never).not.toHaveBeenCalled();
  });

  it("builds a plain text email with every field", () => {
    const e = orderEmail(order, null);
    expect(e.text).toMatch(/^Name: Test <Person>\nEmail: test@example.com/);
    expect(e.text).not.toContain("admin/orders");
  });
});

describe("structured log", () => {
  it("writes JSON lines and redacts secrets and bytes", () => {
    const lines: string[] = [];
    const log = createLogger({ req_id: "r1" }, (l) => lines.push(l));
    log.info("order", {
      access_code: "letmein",
      authorization: "Bearer x",
      SUPABASE_SERVICE_ROLE_KEY: "k",
      photo: new Uint8Array(2048),
      nested: { cookie: "p2p_access=abc", ok: 1 },
    });
    const rec = JSON.parse(lines[0]);
    expect(rec).toMatchObject({
      level: "info",
      msg: "order",
      req_id: "r1",
      access_code: "[redacted]",
      authorization: "[redacted]",
      SUPABASE_SERVICE_ROLE_KEY: "[redacted]",
      photo: { bytes: 2048 },
      nested: { cookie: "[redacted]", ok: 1 },
    });
    expect(lines[0]).not.toContain("letmein");
    expect(redact(new Error("x"))).toEqual({ name: "Error", message: "x" });
  });
});
