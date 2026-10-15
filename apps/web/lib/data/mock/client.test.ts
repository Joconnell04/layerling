import { validatePayload } from "../../validate";
import type { AnyMessage, MessageRow } from "../../db.types";
import type { RowEvent } from "../types";
import { MockDataClient } from "./client";

const STEP = 100;

function photo(name = "dog.jpg") {
  return new File([new Uint8Array([1, 2, 3])], name, { type: "image/jpeg" });
}

async function tick(steps: number) {
  await vi.advanceTimersByTimeAsync(steps * STEP);
}

function lastOf<K extends AnyMessage["kind"]>(msgs: AnyMessage[], kind: K) {
  return msgs.filter((m) => m.kind === kind).at(-1) as MessageRow<K> | undefined;
}

describe("MockDataClient seed", () => {
  it("seeds jobs in each state with valid assistant payloads", async () => {
    const c = new MockDataClient({ stepMs: STEP, heartbeatMs: null });
    const jobs = await c.listJobs();
    expect(jobs.map((j) => j.status).sort()).toEqual(
      ["done", "done", "paused", "review", "waiting_user", "waiting_user", "waiting_user", "waiting_user", "waiting_user", "waiting_user"].sort(),
    );
    const keychain = jobs.find((j) => /Biscuit/.test(j.prompt))!;
    // The form each keychain got: picked at intake, or said in the request.
    expect(keychain.form).toBe("flat");
    expect(jobs.find((j) => /Olive/.test(j.prompt))!.form).toBe("figure");
    expect(keychain.addons).toEqual([
      { kind: "keyring_loop" },
      { kind: "nameplate", text: { text: "BISCUIT" } },
    ]);
    const plate = jobs.find((j) => j.product_type === "nameplate")!;
    expect(plate.addons).toEqual([{ kind: "nameplate", text: { text: "PEPPER" } }]);
    const paused = jobs.find((j) => j.status === "paused")!;
    expect(paused.pause_reason).toMatch(/Credit cap/);
    expect(paused.spent_credits).toBeGreaterThanOrEqual(400);

    for (const j of jobs) {
      for (const m of await c.listMessages(j.id)) {
        const r = validatePayload(m.kind, m.payload);
        expect(r.errors, `${m.kind}`).toEqual([]);
      }
    }
    const filaments = await c.listFilaments();
    expect(filaments.map((f) => f.hex)).toEqual(
      expect.arrayContaining(["#161616", "#7C4B00", "#F72323", "#0D6284", "#FFFFFF"]),
    );
    c.dispose();
  });

  it("keeps seeded ids stable across loads", async () => {
    const a = new MockDataClient({ heartbeatMs: null });
    const b = new MockDataClient({ heartbeatMs: null });
    expect((await a.listJobs()).map((j) => j.id)).toEqual((await b.listJobs()).map((j) => j.id));
  });
});

describe("MockDataClient live flow", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("runs a thin slice job end to end through the user gates", async () => {
    const c = new MockDataClient({ stepMs: STEP, seed: false, heartbeatMs: null });
    const job = await c.createJob({
      prompt: "Me and my dog on a bench",
      productType: "figurine",
      targetSizeMm: 80,
      profile: "thin_slice",
      photos: [photo(), photo("me.png")],
    });
    expect(job.status).toBe("queued");
    expect(job.photos.map((p) => p.path)).toEqual(["input/photo_1.jpg", "input/photo_2.png"]);
    expect(await c.inputUrl(job.id, "input/photo_1.jpg")).toMatch(/^blob:/);

    const events: RowEvent[] = [];
    const off = c.subscribeJob(job.id, (e) => events.push(e));

    await tick(4);
    let msgs = await c.listMessages(job.id);
    const q = lastOf(msgs, "questions")!;
    expect(q.payload.subjects.map((s) => s.kind)).toEqual(["person", "dog"]);
    expect((await c.getJob(job.id))!.status).toBe("waiting_user");

    await c.sendMessage({
      jobId: job.id,
      kind: "answers",
      replyTo: q.id,
      stage: "s01_intake",
      payload: {
        answers: q.payload.questions.map((x) => ({ question_id: x.id, option_ids: [x.options[1].id] })),
      },
    });
    await tick(8);
    msgs = await c.listMessages(job.id);
    expect(lastOf(msgs, "answers")!.consumed_at).not.toBeNull();
    const concepts = lastOf(msgs, "concepts")!;
    expect(concepts.payload.candidates).toHaveLength(2);
    expect(concepts.payload.round).toBe(1);

    await c.sendMessage({
      jobId: job.id,
      kind: "tweak",
      replyTo: concepts.id,
      payload: { text: "Longer ears", candidate_id: "r1c2" },
    });
    await tick(8);
    msgs = await c.listMessages(job.id);
    expect(lastOf(msgs, "tweak_decision")!.payload.route).toBe("rerender_concept");
    const round2 = lastOf(msgs, "concepts")!;
    expect(round2.payload.round).toBe(2);
    expect(round2.payload.rounds_left).toBe(4);

    await c.sendMessage({
      jobId: job.id,
      kind: "concept_pick",
      replyTo: round2.id,
      payload: { candidate_id: "r2c1", approve: true },
    });
    await tick(10);
    msgs = await c.listMessages(job.id);
    const mesh = lastOf(msgs, "mesh_review")!;
    expect(mesh.payload.glb).toBe("s05_mesh/a1/mesh_m1.glb");

    await c.sendMessage({
      jobId: job.id,
      kind: "mesh_decision",
      replyTo: mesh.id,
      payload: { action: "approve" },
    });
    await tick(10);
    const done = (await c.getJob(job.id))!;
    expect(done.status).toBe("done");
    expect(done.result?.baseline_only).toBe(true);
    expect(done.result?.threemf).toBe("s07_baseline/a1/baseline.3mf");

    const runs = await c.listStageRuns(job.id);
    const status = Object.fromEntries(runs.map((r) => [r.stage, r.status]));
    expect(status).toMatchObject({
      s01_intake: "succeeded",
      s02_color_spec: "skipped",
      s03_concept: "succeeded",
      s04_multiview: "succeeded",
      s05_mesh: "succeeded",
      s06_printability: "succeeded",
      s07_baseline: "succeeded",
    });
    expect(done.spent_credits).toBe(21 + 21 + 9 + 35 + 10);
    expect((await c.listSpend(job.id)).length).toBeGreaterThan(4);

    expect(events.some((e) => e.table === "stage_runs")).toBe(true);
    expect(events.some((e) => e.table === "messages" && e.row.kind === "result")).toBe(true);
    off();
    c.dispose();
  });

  it("sends a full profile job to review and finishes it on approve", async () => {
    const c = new MockDataClient({ stepMs: STEP, seed: false, heartbeatMs: null });
    const job = await c.createJob({
      prompt: "A cat",
      productType: "keychain",
      targetSizeMm: 50,
      profile: "full",
      photos: [photo()],
    });
    await tick(4);
    const q = lastOf(await c.listMessages(job.id), "questions")!;
    await c.sendMessage({
      jobId: job.id,
      kind: "answers",
      replyTo: q.id,
      payload: { answers: [{ question_id: "pose", option_ids: ["sit"] }] },
    });
    await tick(12);
    const concepts = lastOf(await c.listMessages(job.id), "concepts")!;
    await c.sendMessage({
      jobId: job.id,
      kind: "concept_pick",
      payload: { candidate_id: concepts.payload.candidates[0].id, approve: true },
    });
    await tick(14);
    const mesh = lastOf(await c.listMessages(job.id), "mesh_review")!;
    await c.sendMessage({ jobId: job.id, kind: "mesh_decision", payload: { action: "approve" } });
    await tick(20);
    expect(mesh).toBeDefined();
    const review = (await c.getJob(job.id))!;
    expect(review.status).toBe("review");
    const req = lastOf(await c.listMessages(job.id), "review_request")!;
    expect(req.payload.confidence).toBeLessThan(0.7);

    await c.sendMessage({
      jobId: job.id,
      kind: "review_decision",
      replyTo: req.id,
      payload: { action: "approve", notes: null },
    });
    await tick(8);
    const done = (await c.getJob(job.id))!;
    expect(done.status).toBe("done");
    expect(done.result?.baseline_only).toBe(false);
    expect(done.stage).toBe("s10_write");
    c.dispose();
  });

  it("rejects payloads that break the contract", async () => {
    const c = new MockDataClient({ seed: false, heartbeatMs: null });
    const job = await c.createJob({
      prompt: "x",
      productType: "other",
      targetSizeMm: null,
      profile: "thin_slice",
      photos: [photo()],
    });
    await expect(
      c.sendMessage({
        jobId: job.id,
        kind: "mesh_decision",
        // @ts-expect-error invalid action on purpose
        payload: { action: "delete" },
      }),
    ).rejects.toThrow(/Invalid mesh_decision/);
    await expect(
      c.createJob({ prompt: "x", productType: "other", targetSizeMm: null, profile: "full", photos: [] }),
    ).rejects.toThrow(/photo/);
    c.dispose();
  });

  it("adds a spool with LAB from hex and a slug id", async () => {
    const c = new MockDataClient({ seed: false, heartbeatMs: null });
    const f = await c.saveFilament({
      brand: "Bambu Lab",
      line: "PLA Matte",
      material: "PLA",
      name: "Ash Gray",
      hex: "#9b9ea0",
      owned: true,
      spools: 2,
      ams_slot: 5,
    });
    expect(f.id).toBe("bambu-pla-matte-ash-gray");
    expect(f.hex).toBe("#9B9EA0");
    expect(f.lab_l).toBeGreaterThan(60);
    expect(f.lab_source).toBe("nominal");
    await expect(
      c.saveFilament({ brand: "Bambu Lab", line: "PLA Matte", material: "PLA", name: "Ash Gray", hex: "#000000", owned: true, spools: 1, ams_slot: null }),
    ).rejects.toThrow(/exists/);
    const withSwatch = await c.uploadSwatch(f.id, photo("swatch.jpg"));
    expect(withSwatch.swatch_path).toMatch(/bambu-pla-matte-ash-gray\.jpg$/);
    c.dispose();
  });
});
