// @vitest-environment node
import { FixtureTraceSource } from "../server/traces";
import { attemptCalls, callRows, filterCalls, jobName, jobRows, jobTrace, spendSummary, wallSeconds } from "./derive";
import type { ManifestLike, TraceEntry } from "./types";

const JOB = "11111111-2222-3333-4444-555555555555";
const BRONCO = "1785a480-84b8-41f9-8f97-1005b6d7d0a6";
const FRENCHIE = "819cc491-c341-470a-bb97-63a194fa8dca";

const entry = (over: Partial<TraceEntry>): TraceEntry => ({
  seq: 1,
  kind: "meshy",
  ref: "task-a",
  path: "s03_concept/a1/trace/01-meshy.json",
  model: "gpt-image-2",
  purpose: "concept r1c1 gpt-image-2",
  status: "ok",
  started_at: "2026-09-24T04:52:14Z",
  duration_s: 39.2,
  credits: 12,
  usd: 0.24,
  usd_list: 0,
  summary: "A figure",
  images: ["input/photo_1.jpg"],
  files: ["s03_concept/a1/concept_r1c1.png"],
  ...over,
});

const task = (id: string, status: string, over: Record<string, unknown> = {}) => ({
  task_id: id,
  endpoint: "openapi/v1/image-to-image",
  purpose: `concept ${id}`,
  status,
  credits: 9,
  created_at: "2026-09-24T04:52:15Z",
  finished_at: status === "SUCCEEDED" ? "2026-09-24T04:53:18Z" : null,
  request: {
    ai_model: "nano-banana-pro",
    prompt: "The figure stands",
    aspect_ratio: "1:1",
    reference_image_urls: [`storage:inputs/${JOB}/input/photo_1.jpg`, "data:image/jpeg;sha256=c905ab333e6138f0;len=604948"],
  },
  result: null,
  ...over,
});

describe("attemptCalls merge rules", () => {
  it("index only: every call has an entry, nothing is derived", () => {
    const m: ManifestLike = {
      data: { trace: [entry({}), entry({ seq: 2, kind: "claude", ref: "sess-1", model: "claude-haiku-4-5-20251001", purpose: "route.tweak" })] },
      model_calls: [{ vendor: "anthropic", model: "claude-haiku-4-5-20251001", purpose: "route.tweak", started_at: "2026-09-24T04:55:00Z", duration_s: 3, request_id: "sess-1" }],
      meshy_tasks: [task("task-a", "SUCCEEDED")],
    };
    const calls = attemptCalls(JOB, m);
    expect(calls.map((c) => [c.seq, c.kind, !!c.derived])).toEqual([
      [1, "meshy", false],
      [2, "claude", false],
    ]);
  });

  it("index plus a pending Meshy task: the task is derived, pending, with its prompt and refs", () => {
    const m: ManifestLike = {
      data: { trace: [entry({})], steps: { "r1.candidates": [{ meshy_task_id: "task-b", image: "s03_concept/a1/concept_r1c2.png" }] } },
      meshy_tasks: [task("task-a", "SUCCEEDED"), task("task-b", "IN_PROGRESS")],
    };
    const calls = attemptCalls(JOB, m);
    expect(calls).toHaveLength(2);
    const b = calls[1];
    expect(b).toMatchObject({ kind: "meshy", ref: "task-b", status: "pending", derived: true, model: "nano-banana-pro", credits: 9, usd: 0.18, path: null });
    expect(b.images).toEqual(["input/photo_1.jpg", "unresolved:image/jpeg"]); // storage ref mapped
    expect(b.files).toEqual(["s03_concept/a1/concept_r1c2.png"]); // matched through the steps
    expect(b.detail?.text).toBe("The figure stands");
    expect(b.detail?.params).toEqual({ ai_model: "nano-banana-pro", aspect_ratio: "1:1" });
  });

  it("an old job with no trace: every call is derived from the manifest", () => {
    const m: ManifestLike = {
      model_calls: [
        { vendor: "anthropic", model: "claude-haiku-4-5-20251001", purpose: "intake", started_at: "2026-09-24T04:51:04Z", duration_s: 25.9, usd: 0, usd_list: 0.020074, request_id: "a6515ef7" },
      ],
      meshy_tasks: [
        task("t-3d", "SUCCEEDED", { endpoint: "openapi/v1/multi-image-to-3d", credits: 35, request: { ai_model: "latest" } }),
        task("t-bad", "FAILED", { result: { task_error: { message: "bad input" } }, credits: 0 }),
      ],
    };
    const calls = attemptCalls(JOB, m);
    expect(calls.map((c) => [c.kind, c.model, c.status, c.usd, c.usd_list, !!c.derived])).toEqual([
      ["claude", "claude-haiku-4-5-20251001", "ok", 0, 0.020074, true],
      ["meshy", "multi-image-to-3d", "ok", 0.7, 0, true],
      ["meshy", "nano-banana-pro", "error", 0, 0, true],
    ]);
    expect(calls[2].error).toBe("bad input");
  });
});

describe("views over the fixtures", () => {
  const src = new FixtureTraceSource();

  it("names jobs: title, else the name text, else the prompt", () => {
    expect(jobName({ title: " Rex ", addons: [], prompt: "p" })).toBe("Rex");
    expect(jobName({ title: null, addons: [{ kind: "nameplate", text: { text: "Bronco" } }], prompt: "p" })).toBe("Bronco");
    expect(jobName({ title: null, addons: [{ kind: "keyring_loop", text: null }], prompt: "keychain of my frenchie" })).toBe("keychain of my frenchie");
  });

  it("job rows count stage time of current attempts, not the gap before a rerun", async () => {
    const { jobs, runs } = await src.jobs();
    const rows = jobRows(jobs, runs);
    expect(rows.map((r) => r.name)).toEqual(["keychain of my frenchie", "Bronco"]);
    const bronco = rows.find((r) => r.id === BRONCO)!;
    expect(bronco).toMatchObject({ credits: 66, usd: 1.32, status: "done", stage: "s07_baseline" });
    expect(Math.round(bronco.wall_s!)).toBe(479); // 7m 59s
    expect(wallSeconds([])).toBeNull();
  });

  it("the Bronco trace: request, attempts in stage order, stale a1, slicer numbers, skipped stages", async () => {
    const found = (await src.job(BRONCO))!;
    const t = jobTrace(found.job, found.runs);
    expect(t.request).toMatchObject({ prompt: "nameplate for my beagle Bronco", name_text: "Bronco", size_mm: 120, photos: ["input/photo_1.jpg"] });
    expect(t.attempts.map((a) => a.key)).toEqual([
      "s01_intake-a1", "s03_concept-a1", "s05_mesh-a1", "s06_printability-a1", "s07_baseline-a1", "s07_baseline-a2",
    ]);
    expect(t.attempts.find((a) => a.key === "s07_baseline-a1")!.stale).toBe(true);
    expect(t.attempts.find((a) => a.key === "s07_baseline-a2")!.slicer).toMatchObject({ ok: true, print_time_s: 33716, slots_used: 3, errors: 0 });
    expect(t.skipped).toEqual(["s02_color_spec", "s04_multiview", "s08_palette", "s09_paint", "s10_write"]);
    expect(t.totals).toMatchObject({ credits: 66, calls: 6 });
    expect(t.totals.usd_list).toBeCloseTo(0.020074, 6);
    const concept = t.attempts.find((a) => a.stage === "s03_concept")!;
    expect(concept.calls.map((c) => c.model)).toEqual(["gpt-image-2", "nano-banana-pro"]);
    expect(concept.decisions.some((d) => d.key.startsWith("meshy.get."))).toBe(false);
  });

  it("calls across jobs: one row per model call, the summary and filters", async () => {
    const { jobs, runs } = await src.calls();
    const rows = callRows(jobs, runs);
    expect(rows).toHaveLength(12);
    expect(rows.every((r) => r.kind !== "slicer")).toBe(true);
    const s = spendSummary(rows);
    expect(s).toMatchObject({ calls: 12, credits: 132, usd: 2.64 });
    expect(s.usd_list).toBeCloseTo(0.1026, 4);
    expect(s.by_model[0]).toMatchObject({ model: "multi-image-to-3d", calls: 2, credits: 70, usd: 1.4 });
    expect(filterCalls(rows, { vendor: "claude" })).toHaveLength(2);
    expect(filterCalls(rows, { q: "frenchie" }).every((r) => r.job_id === FRENCHIE)).toBe(true);
    expect(filterCalls(rows, { days: 1, now: Date.parse("2027-01-01") })).toHaveLength(0);
  });
});
