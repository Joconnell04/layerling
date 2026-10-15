import type { RowEvent } from "./types";
import { chooseMode } from "./index";
import { ServerDataClient } from "./server";

function job(updated_at: string) {
  return { id: "j1", updated_at, status: "running", addons: [], photos: [] };
}

describe("ServerDataClient", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("polls the job bundle every 2 s and emits only changed rows", async () => {
    let n = 0;
    const bundles = [
      { job: job("t1"), runs: [{ id: "r1", updated_at: "a" }], messages: [{ id: "m1", consumed_at: null }], spend: [] },
      { job: job("t1"), runs: [{ id: "r1", updated_at: "a" }], messages: [{ id: "m1", consumed_at: null }], spend: [] },
      {
        job: job("t2"),
        runs: [{ id: "r1", updated_at: "b" }],
        messages: [{ id: "m1", consumed_at: "now" }, { id: "m2", consumed_at: null }],
        spend: [],
      },
    ];
    const fetchImpl = vi.fn(async (_url: string) => Response.json(bundles[Math.min(n++, bundles.length - 1)]));
    const c = new ServerDataClient(fetchImpl as unknown as typeof fetch, 2000);
    const events: RowEvent[] = [];
    const off = c.subscribeJob("j1", (e) => events.push(e));
    await vi.advanceTimersByTimeAsync(10);
    expect(events).toEqual([]); // first load primes, no events
    await vi.advanceTimersByTimeAsync(2000);
    expect(events).toEqual([]); // unchanged
    await vi.advanceTimersByTimeAsync(2000);
    expect(events.map((e) => `${e.table}:${(e.row as { id: string }).id}`)).toEqual([
      "jobs:j1",
      "stage_runs:r1",
      "messages:m1",
      "messages:m2",
    ]);
    off();
    const calls = fetchImpl.mock.calls.length;
    await vi.advanceTimersByTimeAsync(6000);
    expect(fetchImpl.mock.calls.length).toBe(calls);
    expect(fetchImpl.mock.calls[0][0]).toBe("/api/studio/jobs/j1");
  });

  it("creates a job through start, signed PUTs and finish", async () => {
    vi.useRealTimers();
    const calls: string[] = [];
    const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (url === "/api/studio/jobs/start") {
        return Response.json({
          job_id: "j9",
          ticket: "t",
          uploads: [{ key: "j9/input/photo_1.png", url: "https://s.example/u", method: "PUT", headers: {} }],
        });
      }
      if (url === "https://s.example/u") return new Response(null, { status: 200 });
      if (url === "/api/studio/jobs/finish") return Response.json({ job: { id: "j9", status: "queued" } });
      return new Response("x", { status: 404 });
    });
    const c = new ServerDataClient(fetchImpl as unknown as typeof fetch);
    const created = await c.createJob({
      prompt: "A cat",
      productType: "magnet",
      targetSizeMm: 60,
      profile: "thin_slice",
      photos: [new File([new Uint8Array([1])], "cat.png", { type: "image/png" })],
    });
    expect(created.id).toBe("j9");
    expect(calls).toEqual(["POST /api/studio/jobs/start", "PUT https://s.example/u", "POST /api/studio/jobs/finish"]);
    const start = JSON.parse(fetchImpl.mock.calls[0][1]!.body as string);
    expect(start.addons).toEqual([{ kind: "flat_back" }, { kind: "magnet_pocket" }]);
    expect(await c.artifactUrl("j9", "s05_mesh/a1/model.glb")).toBe(
      "/api/studio/file?bucket=artifacts&job=j9&path=s05_mesh%2Fa1%2Fmodel.glb",
    );
  });
});

describe("chooseMode", () => {
  const sb = { NEXT_PUBLIC_SUPABASE_URL: "https://x.supabase.co", NEXT_PUBLIC_SUPABASE_ANON_KEY: "k" };
  it("prefers mock, then a Supabase session, then an access-code session", () => {
    expect(chooseMode({ NEXT_PUBLIC_DATA: "mock", ...sb }, null, true)).toBe("mock");
    expect(chooseMode(sb, { access: true, via: "code", server: true }, true)).toBe("supabase");
    expect(chooseMode(sb, { access: true, via: "code", server: true }, false)).toBe("server");
    expect(chooseMode({}, { access: true, via: "code", server: true }, false)).toBe("server");
    expect(chooseMode(sb, { access: false, via: null, server: true }, false)).toBe("supabase");
    expect(chooseMode({}, { access: false, via: null, server: true }, false)).toBe("server");
    expect(chooseMode({}, null, false)).toBe("mock");
  });
});
