// @vitest-environment node
// Route handlers end to end against the in-memory store (no Supabase, no email).
import { POST as orderStart } from "@/app/api/orders/start/route";
import { POST as orderFinish } from "@/app/api/orders/finish/route";
import { PUT as memoryUpload } from "@/app/api/uploads/memory/route";
import { DELETE as accessLogout, GET as accessStatus, POST as accessLogin } from "@/app/api/access/route";
import { GET as adminOrders } from "@/app/api/admin/orders/route";
import { PATCH as adminOrderPatch } from "@/app/api/admin/orders/[id]/route";
import { POST as adminOrderJob } from "@/app/api/admin/orders/[id]/job/route";
import { GET as studioJobs } from "@/app/api/studio/jobs/route";
import { POST as studioJobStart } from "@/app/api/studio/jobs/start/route";
import { POST as studioJobFinish } from "@/app/api/studio/jobs/finish/route";
import { GET as studioJob } from "@/app/api/studio/jobs/[id]/route";
import { POST as studioMessage } from "@/app/api/studio/jobs/[id]/messages/route";
import { GET as studioDownload } from "@/app/api/studio/jobs/[id]/download/route";
import { POST as studioUpload } from "@/app/api/studio/jobs/[id]/uploads/route";
import { priceRange, quote } from "../pricing";
import { verifyReceipt } from "./receipt";
import { resetMemoryStore, type MemoryServerStore } from "./store";

const CODE = "letmein-2026";
const SECRET = "test-cookie-secret-0123456789abcdef";
let ipSeq = 0;
let store: MemoryServerStore;

function req(path: string, init: RequestInit & { ip?: string; cookie?: string } = {}) {
  const headers = new Headers(init.headers);
  headers.set("x-forwarded-for", init.ip ?? `10.0.0.${++ipSeq}`);
  if (init.cookie) headers.set("cookie", init.cookie);
  return new Request(`https://p2p.example${path}`, { ...init, headers });
}
const json = (body: unknown) => ({
  method: "POST",
  body: JSON.stringify(body),
  headers: { "content-type": "application/json" },
});
const params = <P>(p: P) => ({ params: Promise.resolve(p) });

async function login(ip?: string): Promise<string> {
  const res = await accessLogin(req("/api/access", { ...json({ code: CODE }), ip }));
  expect(res.status).toBe(200);
  return res.headers.get("set-cookie")!.split(";")[0];
}

async function placeOrder(over: Record<string, unknown> = {}) {
  const start = await orderStart(
    req("/api/orders/start", json({
      name: "TEST ORDER",
      email: "test@example.com",
      product_type: "nameplate",
      size_tier: "m",
      request: "Beagle on a nameplate, name PEPPER",
      photos: [{ name: "a.jpg", type: "image/jpeg", size: 3 }],
      ...over,
    })),
  );
  expect(start.status).toBe(200);
  const s = await start.json();
  for (const u of s.uploads) {
    const put = await memoryUpload(
      req(u.url, { method: "PUT", body: new Uint8Array([1, 2, 3]), headers: u.headers }),
    );
    expect(put.status).toBe(200);
  }
  const fin = await orderFinish(req("/api/orders/finish", json({ ticket: s.ticket })));
  return { start: s, finish: fin, body: await fin.json() };
}

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_DATA", "");
  vi.stubEnv("SUPABASE_SERVICE_ROLE_KEY", "");
  vi.stubEnv("NEXT_PUBLIC_SUPABASE_URL", "");
  vi.stubEnv("ACCESS_CODE", CODE);
  vi.stubEnv("ACCESS_COOKIE_SECRET", SECRET);
  vi.stubEnv("AGENTMAIL_API_KEY", "");
  vi.stubEnv("ORDER_NOTIFY_EMAIL", "jackson@example.com");
  store = resetMemoryStore();
});
afterEach(() => vi.unstubAllEnvs());

describe("order flow", () => {
  const start = async (body: Record<string, unknown>, ip?: string) => {
    const res = await orderStart(req("/api/orders/start", { ...json(body), ip }));
    return { status: res.status, body: await res.json() };
  };
  const base = {
    name: "TEST ORDER",
    email: "test@example.com",
    product_type: "keychain",
    size_tier: "m",
    photos: [{ name: "a.png", type: "image/png", size: 10 }],
  };

  it("validates, stores the order with its tier, returns a signed receipt, and never emails a test address", async () => {
    const { finish, body, start: s } = await placeOrder();
    expect(finish.status).toBe(200);
    expect(finish.headers.get("x-request-id")).toBeTruthy();
    expect(s.uploads[0].key).toBe(`${s.order_id}/photo_1.jpg`);
    const row = store.orders.get(body.order_id)!;
    expect(row).toMatchObject({
      name: "TEST ORDER",
      status: "new",
      product_type: "nameplate",
      size_tier: "m",
      size_mm: 120,
      quantity: 1,
      name_text: null,
      photos: [`${s.order_id}/photo_1.jpg`],
    });
    const r = await verifyReceipt(body.receipt);
    expect(r).toEqual({ id: body.order_id, quote: quote("nameplate", "m") });
    await vi.waitFor(() => expect(store.orders.get(body.order_id)!.notify_error).toBe("test order"));
    // Finishing twice is idempotent and hands back the same kind of receipt.
    const again = await (await orderFinish(req("/api/orders/finish", json({ ticket: s.ticket })))).json();
    expect(again.order_id).toBe(body.order_id);
    expect((await verifyReceipt(again.receipt))?.id).toBe(body.order_id);
    expect(store.orders.size).toBe(1);
    expect(await verifyReceipt(`${body.receipt}x`)).toBeNull();
    expect(await verifyReceipt(body.receipt, Date.now() + 31 * 24 * 3600 * 1000)).toBeNull();
  });

  it("rejects invalid fields, missing photos and forged tickets", async () => {
    const bad = await start({ name: "", email: "x", photos: [] });
    expect(bad.status).toBe(400);
    expect(Object.keys(bad.body.errors)).toEqual(expect.arrayContaining(["name", "email", "photos", "product_type"]));

    const s = await start(base);
    expect(s.status).toBe(200);
    const noPhoto = await orderFinish(req("/api/orders/finish", json({ ticket: s.body.ticket })));
    expect(noPhoto.status).toBe(400);
    const forged = await orderFinish(req("/api/orders/finish", json({ ticket: `${s.body.ticket}x` })));
    expect(forged.status).toBe(400);
  });

  it("needs an email or a phone, and checks each one given", async () => {
    const none = await start({ ...base, email: "", phone: "" });
    expect(none.body.errors).toEqual({ contact: "Add an email or a phone number" });
    expect((await start({ ...base, email: "", phone: "555 010 1234" })).status).toBe(200);
    expect((await start({ ...base, email: "nope" })).body.errors).toEqual({ email: "Check the email address" });
    expect((await start({ ...base, phone: "call me" })).body.errors).toEqual({ phone: "Check the phone number" });
  });

  it("needs a size tier for standard products and derives size_mm from it", async () => {
    expect((await start({ ...base, size_tier: undefined })).body.errors).toEqual({ size_tier: "Pick a size" });
    expect((await start({ ...base, size_tier: "xl" })).body.errors).toEqual({ size_tier: "Pick a size" });
    const s = await start({ ...base, product_type: "figurine", size_tier: "L", size_mm: 999 });
    expect(s.status).toBe(200);
    await Promise.all(
      s.body.uploads.map((u: { url: string; headers: Record<string, string> }) =>
        memoryUpload(req(u.url, { method: "PUT", body: new Uint8Array([1]), headers: u.headers })),
      ),
    );
    const fin = await (await orderFinish(req("/api/orders/finish", json({ ticket: s.body.ticket })))).json();
    expect(store.orders.get(fin.order_id)).toMatchObject({ product_type: "figurine", size_tier: "l", size_mm: 120 });
  });

  it("makes the request optional for standard products and 10 characters for Other", async () => {
    expect((await start({ ...base, request: "" })).status).toBe(200);
    const short = await start({ ...base, product_type: "other", size_tier: undefined, request: "a planter" });
    expect(short.body.errors).toEqual({ request: "Tell us a bit more, at least 10 characters" });
    const ok = await start({ ...base, product_type: "other", size_tier: undefined, request: "a planter shaped like my dog" });
    expect(ok.status).toBe(200);
    expect((await start({ ...base, request: "x".repeat(2001) })).body.errors).toEqual({ request: "Max 2000 characters" });
  });

  it("checks the price the form showed against pricing.json", async () => {
    const q = quote("keychain", "l");
    expect((await start({ ...base, size_tier: "l", price: q.amount })).status).toBe(200);
    const stale = await start({ ...base, size_tier: "l", price: (q.amount ?? 0) + 1 });
    expect(stale.status).toBe(400);
    expect(stale.body.errors).toEqual({ price: "Price changed" });
    expect(stale.body.quote).toEqual(q);
    const other = priceRange("other");
    const o = await start({ ...base, product_type: "other", size_tier: undefined, request: "a planter shaped like my dog", price: other.amount });
    expect(o.status).toBe(200);
  });

  it("allows one to four photos of the accepted types under 15 MB", async () => {
    const p = (type: string, size = 10, name = "a.jpg") => ({ name, type, size });
    expect((await start({ ...base, photos: [] })).body.errors).toEqual({ photos: "Add at least one photo" });
    expect((await start({ ...base, photos: Array(5).fill(p("image/jpeg")) })).body.errors).toEqual({ photos: "Max 4 photos" });
    expect((await start({ ...base, photos: Array(4).fill(p("image/jpeg")) })).status).toBe(200);
    expect((await start({ ...base, photos: [p("image/gif")] })).body.errors).toEqual({ photos: "Photos must be JPEG, PNG, HEIC or WebP" });
    expect((await start({ ...base, photos: [p("image/jpeg", 15 * 1024 * 1024 + 1)] })).body.errors).toEqual({
      photos: "Each photo must be under 15 MB",
    });
    expect((await start({ ...base, photos: [p("", 10, "IMG_1.HEIC")] })).status).toBe(200);
  });

  it("rate-limits order starts per IP", async () => {
    const codes = [];
    for (let i = 0; i < 6; i++) codes.push((await start(base, "203.0.113.9")).status);
    expect(codes).toEqual([200, 200, 200, 200, 200, 429]);
  });

  it("emails through AgentMail for a real address, never for test addresses", async () => {
    vi.stubEnv("AGENTMAIL_API_KEY", "am_test");
    vi.stubEnv("AGENTMAIL_INBOX_ID", "orders@agentmail.to");
    const calls: string[] = [];
    const real = globalThis.fetch;
    globalThis.fetch = (async (url: string | URL | Request) => {
      calls.push(String(url));
      return Response.json({ message_id: "m1", thread_id: "t1" });
    }) as typeof fetch;
    try {
      const test = await placeOrder();
      await vi.waitFor(() => expect(store.orders.get(test.body.order_id)!.notify_error).toBe("test order"));
      expect(calls).toEqual([]);
      const realOrder = await placeOrder({ email: "someone@customer.dev" });
      await vi.waitFor(() => expect(store.orders.get(realOrder.body.order_id)!.notified_at).not.toBeNull());
      expect(calls).toEqual(["https://api.agentmail.to/v0/inboxes/orders%40agentmail.to/messages/send"]);
    } finally {
      globalThis.fetch = real;
    }
  });

  it("records 'no provider' when AgentMail is not configured", async () => {
    const { body } = await placeOrder({ email: "someone@customer.dev" });
    await vi.waitFor(() => expect(store.orders.get(body.order_id)!.notify_error).toBe("no provider"));
  });

  it("stores honeypot orders as spam without notifying", async () => {
    const { body } = await placeOrder({ website: "http://spam.example" });
    expect(store.orders.get(body.order_id)!.status).toBe("spam");
    expect(store.orders.get(body.order_id)!.notify_error).toBeNull();
  });
});

describe("access", () => {
  it("sets a signed httpOnly cookie for the right code only", async () => {
    const ip = "198.51.100.7";
    const wrong = await accessLogin(req("/api/access", { ...json({ code: "nope" }), ip }));
    expect(wrong.status).toBe(401);
    expect(wrong.headers.get("set-cookie")).toBeNull();
    const res = await accessLogin(req("/api/access", { ...json({ code: CODE }), ip }));
    const set = res.headers.get("set-cookie")!;
    expect(set).toMatch(/^p2p_access=v1\.\d+\./);
    expect(set).toContain("HttpOnly");
    expect(set).toContain("Secure");
    expect(set).toContain("SameSite=Lax");
    expect(set).toContain("Max-Age=2592000");
    expect(set).not.toContain(CODE);
    const status = await (await accessStatus(req("/api/access", { cookie: set.split(";")[0] }))).json();
    expect(status).toMatchObject({ access: true, via: "code" });
    const out = await accessLogout(req("/api/access", { method: "DELETE" }));
    expect(out.headers.get("set-cookie")).toContain("Max-Age=0");
  });

  it("locks out after 5 wrong codes", async () => {
    const ip = "198.51.100.99";
    const codes = [];
    for (let i = 0; i < 6; i++) codes.push((await accessLogin(req("/api/access", { ...json({ code: "bad" }), ip }))).status);
    expect(codes).toEqual([401, 401, 401, 401, 401, 429]);
    expect((await accessLogin(req("/api/access", { ...json({ code: CODE }), ip }))).status).toBe(429);
  });
});

describe("studio and admin routes", () => {
  it("require the access cookie", async () => {
    expect((await studioJobs(req("/api/studio/jobs"))).status).toBe(401);
    expect((await adminOrders(req("/api/admin/orders"))).status).toBe(401);
    const bad = "p2p_access=v1.9999999999.abc.def";
    expect((await studioJobs(req("/api/studio/jobs", { cookie: bad }))).status).toBe(401);
  });

  it("lists orders, changes status and starts a job from an order", async () => {
    const cookie = await login();
    const { body } = await placeOrder();
    const list = await (await adminOrders(req("/api/admin/orders", { cookie }))).json();
    expect(list.orders).toHaveLength(1);
    expect(list.orders[0].photo_urls[0]).toMatch(/^data:image\/jpeg;base64,/);

    const patched = await adminOrderPatch(
      req(`/api/admin/orders/${body.order_id}`, { method: "PATCH", body: JSON.stringify({ status: "contacted" }), cookie }),
      params({ id: body.order_id }),
    );
    expect((await patched.json()).order.status).toBe("contacted");

    const started = await (await adminOrderJob(req(`/api/admin/orders/${body.order_id}/job`, { method: "POST", cookie }), params({ id: body.order_id }))).json();
    expect(started.created).toBe(true);
    const job = store.jobs.get(started.job_id)!;
    expect(job).toMatchObject({
      owner_id: null,
      access: "code",
      status: "queued",
      product_type: "nameplate",
      target_size_mm: 120,
      addons: [{ kind: "nameplate" }],
    });
    expect(job.photos[0].path).toBe("input/photo_1.jpg");
    expect(store.files.has(`inputs/${job.id}/input/photo_1.jpg`)).toBe(true);
    expect(store.orders.get(body.order_id)).toMatchObject({ job_id: job.id, status: "accepted" });
    expect(store.messages.filter((m) => m.job_id === job.id).map((m) => m.kind)).toEqual(["prompt"]);

    const again = await (await adminOrderJob(req(`/api/admin/orders/${body.order_id}/job`, { method: "POST", cookie }), params({ id: body.order_id }))).json();
    expect(again).toMatchObject({ job_id: job.id, created: false });
  });

  it("creates a job through signed uploads and sends validated messages", async () => {
    const cookie = await login();
    const start = await studioJobStart(
      req("/api/studio/jobs/start", {
        ...json({
          prompt: "Keychain of me and my dog",
          productType: "keychain",
          targetSizeMm: 45,
          profile: "thin_slice",
          addons: [{ kind: "keyring_loop" }, { kind: "nameplate", text: { text: "BISCUIT" } }],
          form: "flat",
          photos: [{ name: "a.heic", type: "", size: 3 }],
        }),
        cookie,
      }),
    );
    const s = await start.json();
    expect(start.status).toBe(200);
    expect(s.uploads[0].key).toBe(`${s.job_id}/input/photo_1.heic`);
    await memoryUpload(req(s.uploads[0].url, { method: "PUT", body: new Uint8Array([9, 9, 9]), headers: s.uploads[0].headers }));
    const fin = await studioJobFinish(req("/api/studio/jobs/finish", { ...json({ ticket: s.ticket, sha256: ["a".repeat(64)] }), cookie }));
    const { job } = await fin.json();
    expect(job).toMatchObject({ id: s.job_id, status: "queued", access: "code", photos: [{ path: "input/photo_1.heic", sha256: "a".repeat(64) }] });
    // The form the new-job form picked rides on the row (then intake does not ask).
    expect(job.form).toBe("flat");

    const bundle = await (await studioJob(req(`/api/studio/jobs/${job.id}`, { cookie }), params({ id: job.id }))).json();
    expect(bundle.messages.map((m: { kind: string }) => m.kind)).toEqual(["prompt"]);

    const bad = await studioMessage(
      req(`/api/studio/jobs/${job.id}/messages`, { ...json({ kind: "mesh_decision", payload: { action: "delete" } }), cookie }),
      params({ id: job.id }),
    );
    expect(bad.status).toBe(400);
    const good = await studioMessage(
      req(`/api/studio/jobs/${job.id}/messages`, { ...json({ kind: "mesh_decision", payload: { action: "approve" } }), cookie }),
      params({ id: job.id }),
    );
    expect((await good.json()).message).toMatchObject({ kind: "mesh_decision", role: "user", owner_id: null });

    const badAddons = await studioJobStart(
      req("/api/studio/jobs/start", {
        ...json({ prompt: "x", productType: "keychain", addons: [{ kind: "rocket" }], photos: [{ name: "a.jpg", type: "image/jpeg", size: 1 }] }),
        cookie,
      }),
    );
    expect(badAddons.status).toBe(400);
    const badForm = await studioJobStart(
      req("/api/studio/jobs/start", {
        ...json({ prompt: "x", productType: "keychain", form: "round", photos: [{ name: "a.jpg", type: "image/jpeg", size: 1 }] }),
        cookie,
      }),
    );
    expect(badForm.status).toBe(400);

    // The paint editor: a large stroke goes to bucket inputs, then paint_edit names it.
    const up = (path: string) =>
      studioUpload(req(`/api/studio/jobs/${job.id}/uploads`, { ...json({ path, size: 12 }), cookie }), params({ id: job.id }));
    expect((await up("input/photo_1.jpg")).status).toBe(400);
    expect((await up("input/paint/../x.txt")).status).toBe(400);
    const { upload } = await (await up("input/paint/stroke-1.txt")).json();
    expect(upload.key).toBe(`${job.id}/input/paint/stroke-1.txt`);
    await memoryUpload(req(upload.url, { method: "PUT", body: "0-9,15", headers: upload.headers }));
    expect(store.files.has(`inputs/${job.id}/input/paint/stroke-1.txt`)).toBe(true);
    const edit = await studioMessage(
      req(`/api/studio/jobs/${job.id}/messages`, {
        ...json({
          kind: "paint_edit",
          payload: {
            ops: [
              { op: "brush_faces", faces_path: "input/paint/stroke-1.txt", filament_id: "generic-pla-black" },
              { op: "fill_region", region_ids: [3], slot: 1 },
            ],
            base_attempt: "s09_paint/a1",
          },
          stage: "s09_paint",
        }),
        cookie,
      }),
      params({ id: job.id }),
    );
    expect((await edit.json()).message).toMatchObject({ kind: "paint_edit", role: "user", stage: "s09_paint" });
    const badEdit = await studioMessage(
      req(`/api/studio/jobs/${job.id}/messages`, { ...json({ kind: "paint_edit", payload: { ops: [{ op: "brush_faces", faces: "1-2, 3", slot: 1 }] } }), cookie }),
      params({ id: job.id }),
    );
    expect(badEdit.status).toBe(400);
  });

  it("serves the 3MF as <name>-<product>.3mf, streamed from memory or redirected to a signed URL", async () => {
    const cookie = await login();
    const { body } = await placeOrder();
    const { job_id } = await (await adminOrderJob(req(`/api/admin/orders/${body.order_id}/job`, { method: "POST", cookie }), params({ id: body.order_id }))).json();
    const get = (c?: string, q = "") => studioDownload(req(`/api/studio/jobs/${job_id}/download${q}`, { cookie: c }), params({ id: job_id }));

    expect((await get()).status).toBe(401);
    expect((await get(cookie)).status).toBe(404);

    const bytes = new Uint8Array([80, 75, 3, 4, 9]);
    await store.upload("artifacts", `${job_id}/s10_export/a1/model.3mf`, new Blob([bytes]), "model/3mf");
    // Result message first (the worker writes jobs.result after it).
    store.messages.push({
      ...store.messages.find((m) => m.job_id === job_id)!,
      id: "result-1",
      role: "worker",
      kind: "result",
      payload: {
        threemf: "s10_export/a1/model.3mf",
        render_sheet: null,
        preview_glb: null,
        print_time_s: 3600,
        filaments: [],
        baseline_only: true,
        slice_ok: true,
      },
    } as never);

    const res = await get(cookie);
    expect(res.status).toBe(200);
    expect(res.headers.get("content-disposition")).toBe('attachment; filename="nameplate-for-test-order-nameplate.3mf"');
    expect(res.headers.get("content-type")).toBe("application/vnd.ms-package.3dmanufacturing-3dmodel+xml");
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(bytes);

    // A subject name from the page wins over the name text.
    const named = await get(cookie, "?name=Miss%20Pepper");
    expect(named.headers.get("content-disposition")).toBe('attachment; filename="miss-pepper-nameplate.3mf"');

    // Supabase: redirect to a signed URL that carries Content-Disposition (too big for a function body).
    const spy = vi.spyOn(store, "signedDownloadUrl").mockResolvedValue("https://storage.example/signed?download=pepper-nameplate.3mf");
    const redirect = await get(cookie);
    expect(redirect.status).toBe(302);
    expect(redirect.headers.get("location")).toBe("https://storage.example/signed?download=pepper-nameplate.3mf");
    expect(spy).toHaveBeenCalledWith("artifacts", `${job_id}/s10_export/a1/model.3mf`, "nameplate-for-test-order-nameplate.3mf");
  });
});
