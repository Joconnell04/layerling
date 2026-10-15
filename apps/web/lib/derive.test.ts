import type { AnyMessage, JobRow, StageRunRow } from "./db.types";
import { costView, deriveGates, stageProgress, stageViews, workerState } from "./derive";
import { ago, duration } from "./format";
import { MockDataClient } from "./data/mock/client";

const job = (over: Partial<JobRow> = {}): JobRow => ({
  id: "j",
  owner_id: "u",
  access: "owner",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  title: null,
  prompt: "p",
  product_type: "figurine",
  profile: "thin_slice",
  status: "running",
  stage: "s03_concept",
  target_size_mm: null,
  addons: [],
  settings: {},
  photos: [],
  budget_usd: 10,
  budget_credits: 400,
  spent_usd: 0,
  spent_credits: 0,
  confidence: null,
  review_reason: null,
  pause_reason: null,
  result: null,
  ...over,
});

const run = (stage: StageRunRow["stage"], attempt: number, status: StageRunRow["status"], over: Partial<StageRunRow> = {}): StageRunRow => ({
  id: `${stage}-${attempt}`,
  job_id: "j",
  owner_id: "u",
  stage,
  attempt,
  status,
  started_at: "2026-01-01T00:00:00Z",
  finished_at: "2026-01-01T00:01:30Z",
  usd: 0.1,
  credits: 5,
  confidence: null,
  manifest: {},
  updated_at: "2026-01-01T00:00:00Z",
  ...over,
});

let seq = 0;
const msg = (kind: AnyMessage["kind"], role: AnyMessage["role"], payload: unknown, over: Partial<AnyMessage> = {}): AnyMessage =>
  ({
    id: `m${++seq}`,
    job_id: "j",
    owner_id: "u",
    created_at: `2026-01-01T00:${String(seq).padStart(2, "0")}:00Z`,
    role,
    kind,
    stage: null,
    body: null,
    payload,
    reply_to: null,
    consumed_at: null,
    ...over,
  }) as AnyMessage;

describe("stageViews", () => {
  it("dims stages outside the profile and uses the latest attempt", () => {
    const views = stageViews(job(), [
      run("s01_intake", 1, "succeeded"),
      run("s03_concept", 1, "failed"),
      run("s03_concept", 2, "waiting_user", { finished_at: null }),
    ], Date.parse("2026-01-01T00:02:00Z"));
    expect(views).toHaveLength(10);
    const by = Object.fromEntries(views.map((v) => [v.info.id, v]));
    expect(by.s02_color_spec.status).toBe("skipped");
    expect(by.s02_color_spec.inProfile).toBe(false);
    expect(by.s05_mesh.status).toBe("pending");
    expect(by.s03_concept.status).toBe("waiting_user");
    expect(by.s03_concept.attempts).toBe(2);
    expect(by.s03_concept.credits).toBe(10);
    expect(by.s03_concept.durationS).toBe(120);
    expect(by.s03_concept.current).toBe(true);
    expect(stageProgress(job())).toEqual([2, 6]); // the thin slice runs s04 too
    expect(stageProgress(job({ status: "done", profile: "full" }))).toEqual([10, 10]);
  });
});

describe("deriveGates", () => {
  it("tracks the open gate through a concept tweak loop", () => {
    const q = msg("questions", "assistant", { subjects: [], questions: [{ id: "a", text: "?", options: [] }], product_type: "x" });
    const a = msg("answers", "user", { answers: [] }, { reply_to: q.id });
    const c1 = msg("concepts", "assistant", { round: 1, candidates: [], rounds_left: 5 });
    let g = deriveGates([q, a, c1]);
    expect(g.questions?.open).toBe(false);
    expect(g.questions?.answer?.id).toBe(a.id);
    expect(g.active).toBe("concepts");

    const t = msg("tweak", "user", { text: "x" });
    g = deriveGates([q, a, c1, t]);
    expect(g.concepts?.pending).toBe(true);
    expect(g.active).toBeNull();

    const d = msg("tweak_decision", "assistant", { route: "rerender_concept", instruction: "", model: "h" });
    const c2 = msg("concepts", "assistant", { round: 2, candidates: [], rounds_left: 4 });
    g = deriveGates([q, a, c1, t, d, c2]);
    expect(g.concepts?.rounds).toHaveLength(2);
    expect(g.concepts?.thread.map((m) => m.kind)).toEqual(["tweak", "tweak_decision"]);
    expect(g.active).toBe("concepts");

    const pick = msg("concept_pick", "user", { candidate_id: "r2c1", approve: true });
    const mr = msg("mesh_review", "assistant", { glb: "a.glb", meshy_task_id: "t" });
    g = deriveGates([q, a, c1, t, d, c2, pick, mr]);
    expect(g.concepts?.approved?.id).toBe(pick.id);
    expect(g.active).toBe("mesh_review");

    const md = msg("mesh_decision", "user", { action: "approve" });
    g = deriveGates([q, a, c1, t, d, c2, pick, mr, md]);
    expect(g.mesh?.decision?.payload.action).toBe("approve");
    expect(g.active).toBeNull();
  });

  it("matches the seeded review job", async () => {
    const c = new MockDataClient({ heartbeatMs: null });
    const review = (await c.listJobs()).find((j) => j.status === "review")!;
    const g = deriveGates(await c.listMessages(review.id));
    expect(g.active).toBe("review_request");
    expect(g.mesh?.reviews).toHaveLength(2);
    expect(g.concepts?.rounds).toHaveLength(2);
    expect(g.result).toBeNull();
  });
});

describe("costView", () => {
  it("levels against the $10 and 400 credit caps", () => {
    expect(costView(job({ spent_usd: 2, spent_credits: 100 })).level).toBe("ok");
    expect(costView(job({ spent_usd: 2, spent_credits: 320 })).level).toBe("warn");
    const over = costView(job({ spent_usd: 10.2, spent_credits: 10 }), [
      { id: 1, owner_id: null, at: "", job_id: "j", stage: null, vendor: "meshy", kind: "x", usd: 0.4, credits: 20, ref: null, test_mode: true },
      { id: 2, owner_id: null, at: "", job_id: "j", stage: null, vendor: "meshy", kind: "x", usd: 0.2, credits: 10, ref: null, test_mode: true },
    ]);
    expect(over.level).toBe("over");
    expect(over.byVendor).toEqual([{ vendor: "meshy", usd: 0.6000000000000001, credits: 30, events: 2 }]);
  });
});

describe("format and worker state", () => {
  it("formats", () => {
    expect(duration(5820)).toBe("1h 37m");
    expect(duration(95)).toBe("1m 35s");
    expect(ago("2026-01-01T00:00:00Z", Date.parse("2026-01-01T03:00:00Z"))).toBe("3h");
  });
  it("classifies heartbeats", () => {
    const now = Date.parse("2026-01-01T00:10:00Z");
    expect(workerState("2026-01-01T00:09:30Z", now)).toBe("online");
    expect(workerState("2026-01-01T00:07:00Z", now)).toBe("stale");
    expect(workerState("2026-01-01T00:00:00Z", now)).toBe("offline");
    expect(workerState(null, now)).toBe("offline");
  });
});
