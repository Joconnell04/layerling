// @vitest-environment node
// First look with a fake Meshy and the in-memory store. Live Meshy is never called.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { GET as poll, POST as start } from "@/app/api/orders/first-look/route";
import { fakeMeshy, firstLookKey, nyDayStart, setFirstLookMeshy } from "./firstLook";
import { FORBIDDEN, NANO_OPENING, PRODUCT_LINES, PROMPT_RULES, firstLookPrompt } from "./firstLookPrompt";
import { signReceipt } from "./receipt";
import { resetMemoryStore, type MemoryServerStore } from "./store";

let store: MemoryServerStore;

async function order(id = crypto.randomUUID()) {
  await store.insertOrder({
    id,
    name: "TEST ORDER",
    email: "test@example.com",
    phone: null,
    product_type: "keychain",
    size_tier: "m",
    size_mm: 52,
    quantity: 1,
    name_text: null,
    request: "pink collar",
    photos: [`${id}/photo_1.jpg`],
    source: "web",
  });
  await store.upload("orders", `${id}/photo_1.jpg`, new Blob([new Uint8Array([1, 2, 3])]), "image/jpeg");
  return { id, receipt: await signReceipt({ id, quote: { amount: 31.99, label: "$31.99", kind: "size" } }) };
}

const post = async (receipt: string) =>
  (await start(new Request("https://p2p.example/api/orders/first-look", { method: "POST", body: JSON.stringify({ receipt }) }))).json();
const get = async (t: string) => (await poll(new Request(`https://p2p.example/api/orders/first-look?t=${encodeURIComponent(t)}`))).json();

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_DATA", "");
  vi.stubEnv("SUPABASE_SERVICE_ROLE_KEY", "");
  vi.stubEnv("ACCESS_COOKIE_SECRET", "test-cookie-secret-0123456789abcdef");
  vi.stubEnv("MESHY_API_KEY", "");
  vi.stubEnv("MESHY_MODE", "");
  vi.stubEnv("FIRST_LOOK_ENABLED", "");
  vi.stubEnv("FIRST_LOOK_DAILY_CAP", "");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (String(url).startsWith("https://api.meshy.ai")) throw new Error("live Meshy called");
      return new Response(new Uint8Array([137, 80, 78, 71]), { headers: { "content-type": "image/png" } });
    }),
  );
  store = resetMemoryStore();
});
afterEach(() => {
  setFirstLookMeshy(undefined);
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("first look", () => {
  it("is off unless FIRST_LOOK_ENABLED is set, and rejects bad receipts", async () => {
    const fake = fakeMeshy(store, "https://p2p.example/samples/beagle-concept.png", 0);
    setFirstLookMeshy(fake);
    const o = await order();
    expect(await post(o.receipt)).toEqual({ state: "off" });
    expect(fake.calls).toHaveLength(0);
    expect(store.spend).toHaveLength(0);
    const bad = await start(new Request("https://p2p.example/api/orders/first-look", { method: "POST", body: JSON.stringify({ receipt: "x.y" }) }));
    expect(bad.status).toBe(400);
  });

  it("reserves a spend row, starts one task per order, and stores the PNG on success", async () => {
    vi.stubEnv("FIRST_LOOK_ENABLED", "true");
    const fake = fakeMeshy(store, "https://p2p.example/samples/beagle-concept.png", 0);
    setFirstLookMeshy(fake);
    const o = await order();
    const s = await post(o.receipt);
    expect(s.state).toBe("printing");
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].prompt).toBe(firstLookPrompt("keychain", "pink collar"));
    expect(store.spend).toMatchObject([{ vendor: "meshy", kind: "first_look", credits: 3, usd: 0.06, ref: o.id, test_mode: true }]);

    const done = await get(s.task);
    expect(done.state).toBe("done");
    expect(store.files.has(`orders/${firstLookKey(o.id)}`)).toBe(true);

    // One per order: a second start does not call Meshy again and shows the stored image.
    const again = await post(o.receipt);
    expect(again.state).toBe("done");
    expect(fake.calls).toHaveLength(1);
    expect(store.spend).toHaveLength(1);
  });

  it("stops at the daily cap", async () => {
    vi.stubEnv("FIRST_LOOK_ENABLED", "1");
    vi.stubEnv("FIRST_LOOK_DAILY_CAP", "1");
    setFirstLookMeshy(fakeMeshy(store, "https://p2p.example/x.png", 60_000));
    expect((await post((await order()).receipt)).state).toBe("printing");
    expect(await post((await order()).receipt)).toEqual({ state: "capped" });
    expect(store.spend).toHaveLength(1);
  });

  it("backs out a reservation that lost the race", async () => {
    vi.stubEnv("FIRST_LOOK_ENABLED", "1");
    const fake = fakeMeshy(store, "https://p2p.example/x.png", 60_000);
    setFirstLookMeshy(fake);
    const o = await order();
    const [a, b] = await Promise.all([post(o.receipt), post(o.receipt)]);
    expect([a.state, b.state]).toEqual(["printing", "printing"]);
    expect(store.spend.filter((s) => s.ref === o.id)).toHaveLength(1);
    expect(fake.calls).toHaveLength(1);
  });

  it("reports a failed task", async () => {
    vi.stubEnv("FIRST_LOOK_ENABLED", "1");
    setFirstLookMeshy(fakeMeshy(store, "https://p2p.example/x.png", 0, "FAILED"));
    const s = await post((await order()).receipt);
    expect(await get(s.task)).toEqual({ state: "failed" });
  });

  it("counts the day in New York time", () => {
    // 03:30 UTC on a summer day is 23:30 the previous day in New York (UTC-4).
    expect(nyDayStart(Date.UTC(2026, 6, 2, 3, 30))).toBe("2026-07-01T04:00:00.000Z");
    expect(nyDayStart(Date.UTC(2026, 0, 15, 12))).toBe("2026-01-15T05:00:00.000Z");
  });

  it("matches the worker's concept prompt rules (worker/src/p2p_worker/stages/prompts.py)", () => {
    const py = readFileSync(resolve(__dirname, "../../../../worker/src/p2p_worker/stages/prompts.py"), "utf8");
    const joined = (name: string) => {
      const m = new RegExp(`^${name} = \\(([\\s\\S]*?)\\)\\n`, "m").exec(py);
      return [...(m?.[1] ?? "").matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((x) => x[1]).join("");
    };
    expect(PROMPT_RULES).toBe(joined("PROMPT_RULES"));
    expect(NANO_OPENING).toBe(joined("NANO_OPENING"));
    const forbidden = /^FORBIDDEN = \(([\s\S]*?)\)\n/m.exec(py)?.[1] ?? "";
    expect([...forbidden.matchAll(/"([^"]*)"/g)].map((x) => x[1])).toEqual([...FORBIDDEN]);
    const lines = /^PRODUCT_LINES = \{([\s\S]*?)\n\}/m.exec(py)?.[1] ?? "";
    for (const [product, line] of Object.entries(PRODUCT_LINES)) {
      const block = new RegExp(`"${product}": \\(?([\\s\\S]*?)\\)?,\\n    (?:"|$)`).exec(lines + '\n    "')?.[1] ?? "";
      expect([...block.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((x) => x[1]).join("")).toBe(line);
    }
    expect(py).toContain('return f"{NANO_OPENING} {prompt} {NANO_CLOSING}"');
    expect(py).toContain('NANO_CLOSING = "Avoid: " + ", ".join(FORBIDDEN) + "."');
  });
});
