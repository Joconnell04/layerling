import type { SupabaseClient } from "@supabase/supabase-js";
import type { RowEvent } from "./types";
import { normalizeJob, SupabaseDataClient } from "./supabase";

type Call = { table: string; op: string; args: unknown[] };

/** Minimal chainable stand-in for supabase-js: records calls, answers from a queue per table. */
function fakeSupabase() {
  const calls: Call[] = [];
  const uploads: { bucket: string; key: string }[] = [];
  const signs: { bucket: string; key: string }[] = [];
  const listeners: { filter: Record<string, string>; cb: (p: unknown) => void }[] = [];
  const answers: Record<string, unknown[]> = {};
  let seq = 0;

  const builder = (table: string) => {
    const q: Record<string, unknown> = {};
    const chain = (op: string) =>
      (...args: unknown[]) => {
        calls.push({ table, op, args });
        return q;
      };
    for (const op of ["select", "insert", "update", "eq", "in", "order", "limit", "single", "maybeSingle"]) {
      q[op] = chain(op);
    }
    q.then = (resolve: (v: unknown) => void) => {
      const queue = answers[table] ?? [];
      const data = queue.length ? queue.shift() : null;
      resolve({ data, error: null });
    };
    return q;
  };

  const sb = {
    from: (table: string) => builder(table),
    auth: {
      getSession: async () => ({ data: { session: { user: { id: "u1", email: "j@x.com" } } } }),
    },
    storage: {
      from: (bucket: string) => ({
        upload: async (key: string) => {
          uploads.push({ bucket, key });
          return { data: { path: key }, error: null };
        },
        createSignedUrl: async (key: string) => {
          signs.push({ bucket, key });
          return { data: { signedUrl: `https://signed/${bucket}/${key}?t=${++seq}` }, error: null };
        },
      }),
    },
    channel: () => {
      const ch = {
        on: (_type: string, filter: Record<string, string>, cb: (p: unknown) => void) => {
          listeners.push({ filter, cb });
          return ch;
        },
        subscribe: () => ch,
      };
      return ch;
    },
    removeChannel: async () => "ok",
  };
  return { sb: sb as unknown as SupabaseClient, calls, uploads, signs, listeners, answers };
}

const jobRow = (over: Record<string, unknown> = {}) => ({
  id: "j1",
  owner_id: "u1",
  prompt: "p",
  status: "draft",
  budget_usd: "10.00",
  spent_usd: "1.2500",
  budget_credits: 400,
  spent_credits: 21,
  settings: {},
  photos: [],
  ...over,
});

describe("SupabaseDataClient", () => {
  it("creates a job as draft, uploads photos to inputs/<job>/, then queues it", async () => {
    const f = fakeSupabase();
    f.answers.jobs = [jobRow(), jobRow({ status: "queued" })];
    const c = new SupabaseDataClient(f.sb);
    const job = await c.createJob({
      prompt: "  me and my dog ",
      productType: "keychain",
      targetSizeMm: 50,
      profile: "thin_slice",
      photos: [
        new File([new Uint8Array([1])], "a.JPEG", { type: "image/jpeg" }),
        new File([new Uint8Array([2])], "b.png", { type: "image/png" }),
      ],
    });
    expect(job.status).toBe("queued");
    expect(job.spent_usd).toBe(1.25);

    const insert = f.calls.find((c) => c.table === "jobs" && c.op === "insert")!;
    expect(insert.args[0]).toMatchObject({
      status: "draft",
      prompt: "me and my dog",
      product_type: "keychain",
      profile: "thin_slice",
      target_size_mm: 50,
      addons: [{ kind: "keyring_loop" }],
    });
    expect(f.uploads).toEqual([
      { bucket: "inputs", key: "j1/input/photo_1.jpg" },
      { bucket: "inputs", key: "j1/input/photo_2.png" },
    ]);
    const prompt = f.calls.find((c) => c.table === "messages" && c.op === "insert")!;
    expect(prompt.args[0]).toMatchObject({ job_id: "j1", role: "user", kind: "prompt", payload: {} });
    const update = f.calls.find((c) => c.table === "jobs" && c.op === "update")!;
    const upd = update.args[0] as { status: string; photos: { path: string; sha256: string }[] };
    expect(upd.status).toBe("queued");
    expect(upd.photos.map((p) => p.path)).toEqual(["input/photo_1.jpg", "input/photo_2.png"]);
    expect(upd.photos[0].sha256).toMatch(/^[0-9a-f]{64}$/);

    const order = f.calls.filter((c) => ["insert", "update"].includes(c.op)).map((c) => `${c.table}.${c.op}`);
    expect(order).toEqual(["jobs.insert", "messages.insert", "jobs.update"]);
  });

  it("inserts user messages with role, stage and reply_to, after validating", async () => {
    const f = fakeSupabase();
    f.answers.messages = [{ id: "m2", kind: "concept_pick" }];
    const c = new SupabaseDataClient(f.sb);
    await c.sendMessage({
      jobId: "j1",
      kind: "concept_pick",
      payload: { candidate_id: "r1c2", approve: true },
      replyTo: "m1",
      stage: "s03_concept",
    });
    const insert = f.calls.find((x) => x.op === "insert")!;
    expect(insert.args[0]).toEqual({
      job_id: "j1",
      owner_id: "u1",
      role: "user",
      kind: "concept_pick",
      stage: "s03_concept",
      body: null,
      payload: { candidate_id: "r1c2", approve: true },
      reply_to: "m1",
    });
    await expect(
      // @ts-expect-error missing approve on purpose
      c.sendMessage({ jobId: "j1", kind: "concept_pick", payload: { candidate_id: "r1c2" } }),
    ).rejects.toThrow(/Invalid concept_pick/);
  });

  it("signs artifact URLs with the job prefix and caches them", async () => {
    const f = fakeSupabase();
    const c = new SupabaseDataClient(f.sb);
    const a = await c.artifactUrl("j1", "s03_concept/a1/r1c1.png");
    const b = await c.artifactUrl("j1", "s03_concept/a1/r1c1.png");
    expect(a).toBe(b);
    expect(f.signs).toEqual([{ bucket: "artifacts", key: "j1/s03_concept/a1/r1c1.png" }]);
    await c.inputUrl("j1", "input/photo_1.jpg");
    expect(f.signs[1]).toEqual({ bucket: "inputs", key: "j1/input/photo_1.jpg" });
  });

  it("subscribes to one job's rows and normalizes realtime payloads", () => {
    const f = fakeSupabase();
    const c = new SupabaseDataClient(f.sb);
    const events: RowEvent[] = [];
    const off = c.subscribeJob("j1", (e) => events.push(e));
    expect(f.listeners.map((l) => [l.filter.table, l.filter.filter])).toEqual([
      ["jobs", "id=eq.j1"],
      ["stage_runs", "job_id=eq.j1"],
      ["messages", "job_id=eq.j1"],
    ]);
    f.listeners[0].cb({ eventType: "UPDATE", new: jobRow({ spent_usd: "2.5000" }) });
    f.listeners[1].cb({ eventType: "INSERT", new: { id: "r", usd: "0.4200", credits: 21 } });
    f.listeners[2].cb({ eventType: "DELETE", old: { id: "m" }, new: {} });
    expect(events).toHaveLength(2);
    expect(events[0].table === "jobs" && events[0].row.spent_usd).toBe(2.5);
    expect(events[1].table === "stage_runs" && events[1].row.usd).toBe(0.42);
    off();
  });

  it("saves a new spool with LAB computed from hex", async () => {
    const f = fakeSupabase();
    f.answers.filaments = [{ id: "generic-pla-black" }];
    const c = new SupabaseDataClient(f.sb);
    await c.saveFilament({
      brand: "Generic",
      line: "PLA",
      material: "PLA",
      name: "Black",
      hex: "#161616",
      owned: true,
      spools: 1,
      ams_slot: 1,
    });
    const insert = f.calls.find((x) => x.table === "filaments" && x.op === "insert")!;
    const row = insert.args[0] as Record<string, unknown>;
    expect(row.id).toBe("generic-pla-black");
    expect(row.owner_id).toBe("u1");
    expect(row.lab_l).toBeCloseTo(7.2, 0);
    expect(row).not.toHaveProperty("updated_at");
  });

  it("coerces numeric strings on job rows", () => {
    expect(normalizeJob(jobRow()).budget_usd).toBe(10);
  });
});
