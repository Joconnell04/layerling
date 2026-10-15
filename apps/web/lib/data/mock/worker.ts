// Simulated worker for mock mode. Reacts to new jobs and user messages the way the real worker
// does through Supabase rows: stage_runs progress, assistant messages, spend, caps.

import {
  MAX_TWEAK_ROUNDS,
  MESSAGE_ROLES,
  PROFILES,
  STAGES,
  USD_PER_MESHY_CREDIT,
  type MessageKind,
  type PayloadFor,
} from "../../contracts";
import type {
  CheckItem,
  ConceptCandidate,
  ConceptCheck,
  ConceptsPayload,
  IntakeResult,
  JobResult,
  MeshReview,
  PaintPreview,
  ProductForm,
  ProofImage,
  Question,
  StageId,
  StageStatus,
  TweakRoute,
} from "../../contracts.gen";
import type { AnyMessage, JobRow, MessageRow, SpendVendor, StageRunRow } from "../../db.types";
import { PROOF_ORDER } from "../../fidelity";
import { asksFromText, formFromText, formIsChoice } from "../../intent";
import { currentPaint } from "../../paintEdit";
import { nameTextOf } from "../../products";
import PAINT_PREVIEW from "../../../public/mock/paint/edit/paint_preview.json";
import type { MockStore } from "./store";

/** The paint map an s09 attempt leaves (paint_state, region_map, face_regions). */
function paintOutputs(dir: string): { path: string; role: string }[] {
  return [
    { path: `${dir}/paint_state.json`, role: "paint_state" },
    { path: `${dir}/region_map.json`, role: "region_map" },
    { path: `${dir}/face_regions.npy`, role: "face_regions" },
  ];
}

export interface WorkerOptions {
  /** Milliseconds per simulated step. */
  stepMs: number;
  /** Replaces setTimeout; seeding uses a synchronous scheduler with a virtual clock. */
  schedule?: (fn: () => void, ms: number) => void;
}

const HAIKU = "claude-haiku-4-5-20251001";
const SONNET = "claude-sonnet-5";
const OPUS = "claude-opus-5-5";
const ORDER: StageId[] = STAGES.map((s) => s.id);

export class MockWorker {
  private timers = new Set<ReturnType<typeof setTimeout>>();
  schedule: (fn: () => void, ms: number) => void;

  constructor(
    private store: MockStore,
    private opts: WorkerOptions,
  ) {
    this.schedule =
      opts.schedule ??
      ((fn, ms) => {
        const t = setTimeout(() => {
          this.timers.delete(t);
          fn();
        }, ms);
        this.timers.add(t);
      });
  }

  dispose(): void {
    for (const t of this.timers) clearTimeout(t);
    this.timers.clear();
  }

  private after(steps: number, fn: () => void): void {
    this.schedule(fn, steps * this.opts.stepMs);
  }

  // ------------------------------------------------------------------ entry points

  onJobCreated(jobId: string): void {
    this.after(1, () => this.runStage(jobId, "s01_intake"));
  }

  onUserMessage(msg: AnyMessage): void {
    const job = this.store.jobs.get(msg.job_id);
    if (!job) return;
    // A palette edit works on a job that has a result (done, or in review after the slicer gate).
    if (msg.kind === "palette_edit") {
      if (job.result) this.after(1, () => this.paletteEdit(msg as MessageRow<"palette_edit">));
      return;
    }
    if (msg.kind === "paint_edit") {
      if (job.result) this.after(1, () => this.paintEdit(msg as MessageRow<"paint_edit">));
      return;
    }
    if (["cancelled", "failed", "done"].includes(job.status)) return;
    if (job.status === "paused" && msg.kind !== "resume") return;
    this.after(1, () => this.handle(msg));
  }

  /**
   * palette_edit: re-run the palette, then write and slice, as new attempts, and post a new result
   * with the chosen filaments. recolor keeps each slot's grams; refit spreads the model over the
   * new set (ASSUMED split: kept filaments keep their grams, an added color takes a quarter of the
   * largest slot).
   */
  private paletteEdit(msg: MessageRow<"palette_edit">): void {
    this.consume(msg);
    const jobId = msg.job_id;
    const before = this.job(jobId).result;
    if (!before) return;
    const p = msg.payload;
    const old = [...(before.filaments ?? [])].sort((a, b) => a.slot - b.slot);
    const ids = p.slots?.length ? p.slots : old.map((u) => u.filament_id ?? "");
    this.store.patchJob(jobId, { status: "running", stage: "s08_palette" });
    this.startRun(jobId, "s08_palette");
    this.after(1, () => {
      this.finishStage(jobId, "s08_palette", { data: { mode: p.mode, source: p.source ?? "owned", slots: ids } });
      const run = this.startRun(jobId, "s10_write");
      this.after(2, () => {
        const largest = old.reduce((m, u) => Math.max(m, u.grams), 0);
        const filaments = ids.map((id, i) => {
          const kept = p.mode === "recolor" ? old[i] : old.find((u) => u.filament_id === id);
          const grams = kept ? kept.grams : round4(largest / 4);
          return { slot: i + 1, filament_id: id || null, grams, meters: round4(grams * 0.33) };
        });
        const dir = `s10_write/a${run.attempt}`;
        const result: JobResult = {
          ...before,
          threemf: `${dir}/final.3mf`,
          filaments,
          baseline_only: false,
          slice_ok: true,
        };
        this.finishStage(jobId, "s10_write", { outputs: [{ path: result.threemf, role: "threemf" }] });
        this.say(jobId, "result", "s10_write", result, { reply_to: msg.id });
        this.store.patchJob(jobId, { status: "done", stage: "s10_write", result });
      });
    });
  }

  /**
   * paint_edit (worker: p2p_worker.paint_edit): a stale base gets a note; otherwise a new s09
   * attempt (progress edit, then preview) whose files are the mock paint edit fixture
   * (public/mock/paint/edit, made by the real pipeline), then s10 through the gate and a result
   * with the fixture's p2p_slicer numbers.
   */
  private paintEdit(msg: MessageRow<"paint_edit">): void {
    this.consume(msg);
    const jobId = msg.job_id;
    const before = this.job(jobId).result;
    if (!before) return;
    const p = msg.payload;
    const base = currentPaint(this.store.runsFor(jobId));
    if (p.base_attempt && p.base_attempt !== base) {
      this.say(jobId, "note", null, { text: `Paint changed since ${p.base_attempt}; reload` }, { reply_to: msg.id });
      return;
    }
    const run = this.startRun(jobId, "s09_paint");
    const dir = `s09_paint/a${run.attempt}`;
    const data = { paint_edit: { ...p, message_id: msg.id, base }, progress: "edit" };
    this.store.patchRun(run.id, { manifest: { ...run.manifest, data } });
    this.after(1, () => {
      this.store.patchRun(run.id, { manifest: { ...run.manifest, data: { ...data, progress: "preview" } } });
      this.after(2, () => {
        const views = PROOF_ORDER.flatMap((view) =>
          (["paint", "print"] as const).map((kind) => ({ path: `${dir}/preview/proof/${kind}_${view}.jpg`, role: `edit_proof_${kind}` })),
        );
        this.finishStage(jobId, "s09_paint", {
          data: { progress: "done", ops: p.ops?.length ?? 0 },
          outputs: [
            ...paintOutputs(dir),
            { path: `${dir}/paint_preview.json`, role: "paint_preview" },
            { path: `${dir}/final.3mf`, role: "preview_3mf" },
            { path: `${dir}/preview/toolpaths.json`, role: "toolpaths" },
            { path: `${dir}/preview/toolpaths.bin`, role: "toolpaths_bin" },
            ...views,
          ],
        });
        const gate = this.startRun(jobId, "s10_write");
        this.store.patchRun(gate.id, { manifest: { ...gate.manifest, data: { paint_edit_message: msg.id } } });
        this.after(2, () => {
          const out = `s10_write/a${gate.attempt}`;
          const after = (PAINT_PREVIEW as PaintPreview).after;
          const result: JobResult = {
            ...before,
            threemf: `${out}/final.3mf`,
            print_time_s: after?.print_time_s ?? before.print_time_s,
            filaments: (after?.filaments ?? []).map((f) => ({ slot: f.slot, filament_id: f.filament_id ?? null, grams: round4(f.grams), meters: f.meters ?? null })),
            baseline_only: false,
            slice_ok: true,
          };
          this.finishStage(jobId, "s10_write", {
            inputs: [{ path: `${dir}/paint_state.json`, role: "paint_state" }],
            outputs: [{ path: result.threemf, role: "final_3mf" }],
          });
          this.say(jobId, "result", "s10_write", result, { reply_to: msg.id });
          this.store.patchJob(jobId, { status: "done", stage: "s10_write", result });
        });
      });
    });
  }

  private handle(msg: AnyMessage): void {
    this.consume(msg);
    const jobId = msg.job_id;
    switch (msg.kind) {
      case "answers": {
        // Like s01: the form (answer > new-job form > request words > FORM_DEFAULTS) and the
        // custom asks go on the jobs row and into product.json.
        const job = this.job(jobId);
        const picked = msg.payload.answers.find((a) => a.question_id === "form")?.option_ids?.[0];
        const form = decideForm(job, picked);
        const custom_asks = asksFromText(job.prompt);
        this.store.patchJob(jobId, { form, custom_asks });
        const attempt = this.latestRun(jobId, "s01_intake")?.attempt ?? 1;
        this.finishStage(jobId, "s01_intake", {
          data: { answers: msg.payload.answers.length, form, custom_asks: custom_asks.length },
          outputs: [{ path: `s01_intake/a${attempt}/product.json`, role: "product_spec" }],
        });
        this.advance(jobId, "s01_intake");
        return;
      }
      case "concept_pick":
        if (msg.payload.approve) {
          this.finishStage(jobId, "s03_concept", {
            data: { picked: msg.payload.candidate_id },
          });
          this.advance(jobId, "s03_concept");
        }
        return;
      case "tweak":
        this.tweak(jobId, msg as MessageRow<"tweak">);
        return;
      case "mesh_decision":
        if (msg.payload.action === "approve") {
          this.finishStage(jobId, "s05_mesh", {});
          this.advance(jobId, "s05_mesh");
        } else {
          this.runMesh(jobId, msg.payload.action);
        }
        return;
      case "resume":
        this.resume(jobId, msg.payload);
        return;
      case "review_decision":
        if (msg.payload.action === "approve") {
          this.store.patchJob(jobId, { status: "running", review_reason: null });
          this.advance(jobId, "s09_paint");
        } else if (msg.payload.action === "reject") {
          this.store.patchJob(jobId, {
            status: "failed",
            review_reason: msg.payload.notes ?? "Rejected in review",
          });
        } else {
          this.store.patchJob(jobId, { status: "running", review_reason: null });
          this.runStage(jobId, "s09_paint");
        }
        return;
      default:
        return;
    }
  }

  /** Clear a pause: raise caps if given, then rerun the paused stage. */
  private resume(jobId: string, p: { cap_usd?: number | null; cap_credits?: number | null }): void {
    const job = this.job(jobId);
    if (job.status !== "paused") return;
    const paused = this.store
      .runsFor(jobId)
      .filter((r) => r.status === "paused")
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0];
    this.store.patchJob(jobId, {
      status: "running",
      pause_reason: null,
      budget_usd: p.cap_usd ?? job.budget_usd,
      budget_credits: p.cap_credits ?? job.budget_credits,
    });
    const stage = paused?.stage ?? job.stage;
    if (!stage) return;
    if (paused) this.store.patchRun(paused.id, { status: "failed", finished_at: this.store.iso() });
    this.after(1, () => (stage === "s05_mesh" ? this.runMesh(jobId, "regenerate") : this.runStage(jobId, stage)));
  }

  // ------------------------------------------------------------------ rows

  private consume(msg: AnyMessage): void {
    const cur = this.store.messages.get(msg.id);
    if (cur && !cur.consumed_at) this.store.putMessage({ ...cur, consumed_at: this.store.iso() });
  }

  private job(jobId: string): JobRow {
    const j = this.store.jobs.get(jobId);
    if (!j) throw new Error(`job ${jobId} not found`);
    return j;
  }

  private latestRun(jobId: string, stage: StageId): StageRunRow | undefined {
    return this.store
      .runsFor(jobId)
      .filter((r) => r.stage === stage)
      .sort((a, b) => b.attempt - a.attempt)[0];
  }

  private startRun(jobId: string, stage: StageId, status: StageStatus = "running"): StageRunRow {
    const prev = this.latestRun(jobId, stage);
    const now = this.store.iso();
    const run: StageRunRow = {
      id: this.store.newId(),
      job_id: jobId,
      owner_id: this.job(jobId).owner_id,
      stage,
      attempt: (prev?.attempt ?? 0) + 1,
      status,
      started_at: status === "skipped" ? null : now,
      finished_at: status === "skipped" ? now : null,
      usd: 0,
      credits: 0,
      confidence: null,
      manifest: {},
      updated_at: now,
    };
    this.store.putRun(run);
    if (status !== "skipped") {
      this.store.patchJob(jobId, { status: "running", stage });
    }
    return run;
  }

  private setRun(jobId: string, stage: StageId, patch: Partial<StageRunRow>): void {
    const run = this.latestRun(jobId, stage);
    if (run) this.store.patchRun(run.id, patch);
  }

  private finishStage(
    jobId: string,
    stage: StageId,
    opts: {
      confidence?: number;
      data?: Record<string, unknown>;
      outputs?: { path: string; role: string }[];
      inputs?: { path: string; role: string }[];
    },
  ): void {
    const run = this.latestRun(jobId, stage);
    if (!run) return;
    const finished = this.store.now();
    const started = run.started_at ? new Date(run.started_at) : finished;
    this.store.patchRun(run.id, {
      status: "succeeded",
      finished_at: finished.toISOString(),
      confidence: opts.confidence ?? run.confidence,
      manifest: {
        ...run.manifest,
        job_id: jobId,
        stage,
        attempt: run.attempt,
        status: "succeeded",
        duration_s: (finished.getTime() - started.getTime()) / 1000,
        inputs: [
          ...(run.manifest.inputs ?? []),
          ...(opts.inputs ?? []).map((o) => ({ ...o, sha256: "mock" })),
        ],
        outputs: [
          ...(run.manifest.outputs ?? []),
          ...(opts.outputs ?? []).map((o) => ({ ...o, sha256: "mock" })),
        ],
        data: { ...(run.manifest.data ?? {}), ...(opts.data ?? {}) },
      },
    });
  }

  private say<K extends MessageKind>(
    jobId: string,
    kind: K,
    stage: StageId | null,
    payload: PayloadFor<K>,
    extra: { body?: string | null; reply_to?: string | null } = {},
  ): MessageRow<K> {
    const msg: MessageRow<K> = {
      id: this.store.newId(),
      job_id: jobId,
      owner_id: this.job(jobId).owner_id,
      created_at: this.store.iso(),
      role: MESSAGE_ROLES[kind],
      kind,
      stage,
      body: extra.body ?? null,
      payload,
      reply_to: extra.reply_to ?? null,
      consumed_at: null,
    };
    this.store.putMessage(msg as AnyMessage);
    return msg;
  }

  /** Records spend on the job and the stage run. Returns false when a cap paused the job. */
  private spend(
    jobId: string,
    stage: StageId,
    vendor: SpendVendor,
    kind: string,
    amount: { usd?: number; credits?: number },
  ): boolean {
    const credits = amount.credits ?? 0;
    const usd = round4((amount.usd ?? 0) + credits * USD_PER_MESHY_CREDIT);
    const job = this.job(jobId);
    this.store.addSpend({
      owner_id: job.owner_id,
      at: this.store.iso(),
      job_id: jobId,
      stage,
      vendor,
      kind,
      usd,
      credits,
      ref: vendor === "meshy" ? `mock-${this.store.newId().slice(-8)}` : null,
      test_mode: true,
    });
    const run = this.latestRun(jobId, stage);
    if (run) {
      this.store.patchRun(run.id, { usd: round4(run.usd + usd), credits: run.credits + credits });
    }
    const spentUsd = round4(job.spent_usd + usd);
    const spentCredits = job.spent_credits + credits;
    this.store.patchJob(jobId, { spent_usd: spentUsd, spent_credits: spentCredits });
    const overUsd = spentUsd >= job.budget_usd;
    const overCredits = spentCredits >= job.budget_credits;
    if (overUsd || overCredits) {
      const reason = overCredits
        ? `Credit cap ${job.budget_credits}`
        : `USD cap $${job.budget_usd.toFixed(2)}`;
      this.store.patchJob(jobId, { status: "paused", pause_reason: reason });
      this.setRun(jobId, stage, { status: "paused" });
      return false;
    }
    return true;
  }

  private waitUser(jobId: string, stage: StageId): void {
    this.setRun(jobId, stage, { status: "waiting_user" });
    this.store.patchJob(jobId, { status: "waiting_user", stage });
  }

  private alive(jobId: string): boolean {
    const j = this.store.jobs.get(jobId);
    return !!j && !["paused", "cancelled", "failed", "done"].includes(j.status);
  }

  // ------------------------------------------------------------------ stage graph

  private advance(jobId: string, from: StageId): void {
    const profile = PROFILES[this.job(jobId).profile];
    for (let i = ORDER.indexOf(from) + 1; i < ORDER.length; i++) {
      const stage = ORDER[i];
      if (profile.includes(stage)) {
        this.after(1, () => this.runStage(jobId, stage));
        return;
      }
      if (!this.latestRun(jobId, stage)) this.startRun(jobId, stage, "skipped");
    }
  }

  private runStage(jobId: string, stage: StageId): void {
    if (!this.alive(jobId)) return;
    switch (stage) {
      case "s01_intake":
        return this.runIntake(jobId);
      case "s02_color_spec":
        return this.runAuto(jobId, stage, 2, () =>
          this.spend(jobId, stage, "anthropic", "messages", { usd: 0.031 }),
        );
      case "s03_concept":
        return this.runConcepts(jobId, 1);
      case "s04_multiview":
        return this.runAuto(jobId, stage, 3, () =>
          // one generate_multi_view task on the approved nano-banana-pro, billed once (9)
          this.spend(jobId, stage, "meshy", "image-to-image", { credits: 9 }),
        );
      case "s05_mesh":
        return this.runMesh(jobId, "generate");
      case "s06_printability":
        return this.runAuto(
          jobId,
          stage,
          2,
          () => this.spend(jobId, stage, "meshy", "analyze", { credits: 0 }),
          undefined,
          (attempt) => ({
            // Like the real s06: Meshy's printability report, the geometry, the warnings list.
            data: { geometry: "p2p_geometry", warnings: 2, printability: "warning" },
            outputs: [
              { path: `s06_printability/a${attempt}/printability.json`, role: "printability" },
              { path: `s06_printability/a${attempt}/geometry.json`, role: "geometry" },
              { path: `s06_printability/a${attempt}/warnings.json`, role: "geometry_warnings" },
            ],
          }),
        );
      case "s07_baseline":
        return this.runAuto(
          jobId,
          stage,
          3,
          () => this.spend(jobId, stage, "meshy", "multi-color", { credits: 10 }),
          () => {
            if (this.job(jobId).profile === "thin_slice") this.finishJob(jobId, stage);
            else this.advance(jobId, stage);
          },
        );
      case "s08_palette":
        return this.runAuto(jobId, stage, 1, () => true);
      case "s09_paint":
        return this.runPaint(jobId);
      case "s10_write":
        return this.runAuto(
          jobId,
          stage,
          2,
          () => true,
          () => this.finishJob(jobId, stage),
        );
    }
  }

  private runAuto(
    jobId: string,
    stage: StageId,
    steps: number,
    work: () => boolean,
    then?: () => void,
    finish?: (attempt: number) => { data?: Record<string, unknown>; outputs?: { path: string; role: string }[] },
  ): void {
    const run = this.startRun(jobId, stage);
    this.after(steps, () => {
      if (!this.alive(jobId)) return;
      if (!work()) return;
      this.finishStage(jobId, stage, finish ? finish(run.attempt) : {});
      if (then) then();
      else this.advance(jobId, stage);
    });
  }

  private runIntake(jobId: string): void {
    this.startRun(jobId, "s01_intake");
    this.after(2, () => {
      if (!this.spend(jobId, "s01_intake", "anthropic", "messages", { usd: 0.004 })) return;
      const job = this.job(jobId);
      this.say(jobId, "questions", "s01_intake", intakeFor(job));
      this.waitUser(jobId, "s01_intake");
    });
  }

  private conceptsSoFar(jobId: string): MessageRow<"concepts">[] {
    return this.store
      .messagesFor(jobId)
      .filter((m): m is MessageRow<"concepts"> => m.kind === "concepts");
  }

  private runConcepts(jobId: string, round: number, basePrompt?: string): void {
    const run = this.latestRun(jobId, "s03_concept");
    if (!run || run.status === "succeeded" || round === 1) this.startRun(jobId, "s03_concept");
    else this.setRun(jobId, "s03_concept", { status: "running" });
    this.store.patchJob(jobId, { status: "running", stage: "s03_concept" });
    this.after(1, () => this.setRun(jobId, "s03_concept", { status: "waiting_vendor" }));
    this.after(3, () => {
      if (!this.alive(jobId)) return;
      if (!this.spend(jobId, "s03_concept", "meshy", "image-to-image", { credits: 21 })) return;
      const job = this.job(jobId);
      const attempt = this.latestRun(jobId, "s03_concept")?.attempt ?? 1;
      const prompt = basePrompt ?? conceptPrompt(job.prompt);
      const form = job.form ?? decideForm(job);
      const dir = `s03_concept/a${attempt}`;
      // Like s03: the figure render, then the product the customer approves (loop, plate or name
      // added by an edit; a flat charm from the Creative Lab prototype), then the check.
      const candidates: ConceptCandidate[] = [
        ["gpt-image-2", 1],
        ["nano-banana-pro", 2],
      ].map(([model, n]) => ({
        id: `r${round}c${n}`,
        round,
        image_model: model as string,
        image: `${dir}/concept_r${round}c${n}.png`,
        figure_image: `${dir}/figure_r${round}c${n}.png`,
        form,
        product_source: form === "flat" ? "prototype" : job.addons.length ? "edit" : "figure",
        product_task_id: `mock-${this.store.newId().slice(-8)}`,
        meshy_task_id: `mock-${this.store.newId().slice(-8)}`,
        prompt,
        check: conceptCheck(job, form, n as number, round),
      }));
      const payload: ConceptsPayload = {
        round,
        candidates,
        rounds_left: Math.max(0, MAX_TWEAK_ROUNDS - (round - 1)),
        form,
      };
      this.say(jobId, "concepts", "s03_concept", payload);
      this.waitUser(jobId, "s03_concept");
    });
  }

  private tweak(jobId: string, msg: MessageRow<"tweak">): void {
    const last = this.conceptsSoFar(jobId).at(-1);
    const round = (last?.payload.round ?? 0) + 1;
    const roundsLeft = last?.payload.rounds_left ?? MAX_TWEAK_ROUNDS;
    this.setRun(jobId, "s03_concept", { status: "running" });
    this.store.patchJob(jobId, { status: "running" });
    this.after(1, () => {
      if (!this.spend(jobId, "s03_concept", "anthropic", "messages", { usd: 0.002 })) return;
      const text = msg.payload.text.trim();
      const followup = text.endsWith("?");
      const route: TweakRoute = followup ? "ask_followup" : "rerender_concept";
      this.say(
        jobId,
        "tweak_decision",
        "s03_concept",
        {
          route,
          instruction: followup ? text : `Re-render ${msg.payload.candidate_id ?? "concept"}: ${text}`,
          followup_question: followup ? "Which subject should change?" : null,
          model: HAIKU,
        },
        { reply_to: msg.id },
      );
      if (followup) {
        this.waitUser(jobId, "s03_concept");
        return;
      }
      if (roundsLeft <= 0) {
        this.say(jobId, "note", "s03_concept", { text: "No tweak rounds left" });
        this.waitUser(jobId, "s03_concept");
        return;
      }
      const base = last?.payload.candidates.find((c) => c.id === msg.payload.candidate_id);
      this.runConcepts(jobId, round, `${base?.prompt ?? conceptPrompt(this.job(jobId).prompt)} Change: ${text}`);
    });
  }

  private runMesh(jobId: string, action: "generate" | "retexture" | "regenerate"): void {
    const run = this.startRun(jobId, "s05_mesh");
    this.after(1, () => this.setRun(jobId, "s05_mesh", { status: "waiting_vendor" }));
    this.after(4, () => {
      if (!this.alive(jobId)) return;
      const credits = action === "retexture" ? 10 : 35;
      const kind = action === "retexture" ? "retexture" : "multi-image-to-3d";
      if (!this.spend(jobId, "s05_mesh", "meshy", kind, { credits })) return;
      const dir = `s05_mesh/a${run.attempt}`;
      const tag = `m${run.attempt}`;
      // Meshy's multi-view thumbnails are four views; the page renders the GLB from all eight.
      const payload: MeshReview = {
        glb: `${dir}/mesh_${tag}.glb`,
        thumbnails: ["front", "right", "back", "left"].map((v) => `${dir}/thumb_${tag}_${v}.png`),
        meshy_task_id: `mock-${this.store.newId().slice(-8)}`,
        face_count: 48210 + run.attempt * 311,
        actions: ["approve", "retexture", "regenerate"],
        retexture_credits: 10,
        regenerate_credits: 35,
      };
      this.say(jobId, "mesh_review", "s05_mesh", payload);
      this.waitUser(jobId, "s05_mesh");
    });
  }

  private runPaint(jobId: string): void {
    const run = this.startRun(jobId, "s09_paint");
    this.after(4, () => {
      if (!this.alive(jobId)) return;
      if (!this.spend(jobId, "s09_paint", "anthropic", "agent", { usd: 1.62 })) return;
      const confidence = run.attempt === 1 ? 0.58 : 0.86;
      const dir = `s09_paint/a${run.attempt}`;
      const notes = [
        "First pass from the palette",
        "Eyes and nose to black",
        "Belly patch to white",
        "Collar to red",
        "Despeckled islands under 0.8 mm2",
        "Left eye merged in simulation",
      ];
      const iterations = run.attempt === 1 ? 6 : 4;
      const checkpoints = notes.slice(0, iterations).map((note, i) => {
        const n = String(i + 1).padStart(2, "0");
        return { id: `ckpt_${n}`, iteration: i + 1, note, before: `${dir}/ckpt_${n}_ideal.png`, after: `${dir}/ckpt_${n}_sim.png` };
      });
      this.finishStage(jobId, "s09_paint", {
        confidence,
        data: { iterations, max_iterations: 8, checkpoints },
        outputs: [{ path: `${dir}/checklist.json`, role: "checklist" }, ...paintOutputs(dir)],
      });
      if (confidence < 0.7) {
        this.store.patchJob(jobId, {
          status: "review",
          confidence,
          review_reason: "Low paint confidence",
        });
        this.say(jobId, "review_request", "s09_paint", {
          reason: "Left eye region under 2 mm; merged in simulation",
          confidence,
          checklist: `${dir}/checklist.json`,
          images: [`${dir}/sim_front.png`, `${dir}/render_sheet.png`],
        });
        return;
      }
      this.store.patchJob(jobId, { confidence });
      this.advance(jobId, "s09_paint");
    });
  }

  private finishJob(jobId: string, stage: StageId): void {
    const job = this.job(jobId);
    const attempt = this.latestRun(jobId, stage)?.attempt ?? 1;
    const dir = `${stage}/a${attempt}`;
    const baseline = job.profile === "thin_slice";
    // The form the customer chose: a flat charm prints as a relief on a black back, a 3D
    // keychain as the figure with its loop (the fidelity back half keeps the form).
    const form = job.form ?? decideForm(job);
    const flat = formIsChoice(job.product_type) && form === "flat";
    const kc3d = job.product_type === "keychain" && !flat;
    // s10 ships the proof set (contracts JobResult.proof_images); s07's thin slice result has none.
    const proof: ProofImage[] = baseline
      ? []
      : (["model", "paint", "print"] as const).flatMap((kind) =>
          PROOF_ORDER.map((view) => ({ kind, view, path: `${dir}/proof/${kind}_${view}.png` })),
        );
    if (proof.length) {
      // s10 writes the paint s09 left (its paint_state input), like the worker's WriteStage.
      const paint = this.latestRun(jobId, "s09_paint")?.manifest.outputs?.find((o) => o.role === "paint_state");
      this.finishStage(jobId, stage, {
        outputs: proof.map((p) => ({ path: p.path, role: `proof_${p.kind}` })),
        inputs: paint ? [{ path: paint.path, role: "paint_state" }] : [],
      });
    }
    const result: JobResult = {
      threemf: `${dir}/${baseline ? "baseline" : "final"}.3mf`,
      render_sheet: `${dir}/${flat ? "render_sheet_flat" : "render_sheet"}.png`,
      preview_glb: `${dir}/${flat ? "preview_flat" : kc3d ? "preview_kc3d" : "preview"}.glb`,
      proof_images: proof,
      print_time_s: baseline ? 5820 : 6240,
      filaments: [
        { slot: 1, filament_id: "generic-pla-brown", grams: 9.4, meters: 3.08 },
        { slot: 2, filament_id: "generic-pla-white", grams: 6.2, meters: 2.03 },
        { slot: 3, filament_id: "generic-pla-black", grams: flat ? 3.1 : 0.6, meters: flat ? 1.04 : 0.2 },
        { slot: 4, filament_id: "bambu-pla-basic-red", grams: 0.4, meters: 0.13 },
      ],
      baseline_only: baseline,
      slice_ok: true,
    };
    if (flat) {
      // The palette step's spool suggestion (proof run 1: no gray loaded, gray went to brown),
      // with the catalog color that fixes it.
      this.say(jobId, "spool_suggestion", stage, {
        suggestion: "gray PLA",
        target_hex: "#8E9089",
        target_lab: [60.1, -0.9, 3.2],
        delta_e_now: 26.9,
        delta_e_with: 3.1,
        regions: ["muzzle"],
        area_pct: 6.5,
        filament_id: "bambu-pla-basic-gray",
      });
    }
    this.say(jobId, "result", stage, result);
    this.store.patchJob(jobId, { status: "done", stage, result });
  }
}

function round4(v: number): number {
  return Math.round(v * 10000) / 10000;
}

function conceptPrompt(request: string): string {
  return (
    `One connected sculpted figurine on a shared base: ${request}. ` +
    "Chunky proportions, no loose whiskers or thin tails, neutral background, flat even lighting, 3/4 hero view."
  );
}

// Subjects can be people, pets, or objects ("me and my dog").
const SUBJECT_WORDS: [RegExp, string, string][] = [
  [/\b(me|myself)\b/i, "person", "Me"],
  [/\b(wife|husband|partner|mom|mum|dad|son|daughter|kid|child|baby|friend|man|woman|girl|boy|person)\b/i, "person", "Person"],
  [/\bdogs?\b|\bpupp(y|ies)\b/i, "dog", "Dog"],
  [/\bcats?\b|\bkittens?\b/i, "cat", "Cat"],
  [/\brabbits?\b|\bbunn(y|ies)\b/i, "rabbit", "Rabbit"],
  [/\bchickens?\b|\bbirds?\b|\bparrots?\b/i, "bird", "Bird"],
  [/\bhorses?\b/i, "horse", "Horse"],
];

export function subjectsFromPrompt(prompt: string): IntakeResult["subjects"] {
  const found = SUBJECT_WORDS.filter(([re]) => re.test(prompt));
  const list = found.length ? found : [[/./, "object", "Subject"] as [RegExp, string, string]];
  return list.map(([, kind, name], i) => ({
    id: `s${i + 1}`,
    name,
    kind,
    description: `${name.toLowerCase()} in photo ${i + 1}`,
    photo_indices: [i],
  }));
}

// contracts job.py FORM_DEFAULTS: only when a keychain or magnet ends intake without an answer.
const FORM_DEFAULTS: Partial<Record<JobRow["product_type"], ProductForm>> = { keychain: "figure", magnet: "flat" };

/** s01 decide_form: answer > new-job form > request words > default. Every other product: figure. */
export function decideForm(job: JobRow, answered?: string | null): ProductForm {
  if (!formIsChoice(job.product_type)) return "figure";
  if (answered === "flat" || answered === "figure") return answered;
  return job.form ?? formFromText(job.prompt) ?? FORM_DEFAULTS[job.product_type] ?? "figure";
}

/** question_bank.yaml: keychain and magnet `form` questions, asked when nothing set the form. */
function formQuestion(job: JobRow): Question | null {
  if (!formIsChoice(job.product_type) || job.form || formFromText(job.prompt)) return null;
  return job.product_type === "magnet"
    ? {
        id: "form",
        text: "Flat relief or 3D figure?",
        options: [
          { id: "flat", label: "Flat relief" },
          { id: "figure", label: "3D figure" },
        ],
        multi: false,
        allow_text: false,
      }
    : {
        id: "form",
        text: "Flat charm or 3D figure?",
        options: [
          { id: "figure", label: "3D figure" },
          { id: "flat", label: "Flat charm" },
        ],
        multi: false,
        allow_text: false,
      };
}

/**
 * s03's check, shaped like product_plan.check_items plus the code checks: the parts the product
 * shows, the name, each ask, the markings, no extra base, the figure kept. Round 1 candidate B
 * misses the markings (the check the mock art can show failing).
 */
export function conceptCheck(job: JobRow, form: ProductForm, n: number, round: number): ConceptCheck {
  const flat = form === "flat";
  const figOn = flat ? "product" : "figure";
  const name = nameTextOf(job.addons);
  const items: CheckItem[] = [];
  if (flat) items.push({ id: "part.relief", kind: "part", label: "A flat relief charm seen from the front", on: "product", passed: true, by: "model" });
  for (const a of job.addons) {
    if (a.kind === "keyring_loop" || a.kind === "hanging_loop") {
      const what = a.kind === "keyring_loop" ? "keyring loop" : "hanging loop";
      items.push({ id: `part.${a.kind}`, kind: "part", label: `A ${what} ${flat ? "at the charm's top edge" : "on top of the head"}`, on: "product", passed: true, by: "model" });
    } else if (a.kind === "nameplate") {
      items.push({ id: "part.nameplate", kind: "part", label: "A rounded name plate with the figure standing on one end", on: "product", passed: true, by: "model" });
    } else if (a.kind === "round_base" || a.kind === "oval_base") {
      items.push({ id: `part.${a.kind}`, kind: "part", label: "A round base under the figure", on: "product", passed: true, by: "model" });
    }
  }
  if (name) items.push({ id: "text", kind: "text", label: `The name reads exactly ${name}`, on: "product", passed: true, by: "model" });
  asksFromText(job.prompt)
    .filter((a) => a.kind !== "text")
    .forEach((a, i) => items.push({ id: `ask.${i + 1}`, kind: "ask", label: a.text, on: figOn, passed: true, by: "model" }));
  const markingsOk = !(round === 1 && n === 2);
  items.push({
    id: "markings",
    kind: "markings",
    label: "Coat colors and markings true to the photos: brown coat; cream muzzle and chest",
    on: figOn,
    passed: markingsOk,
    by: "model",
    note: markingsOk ? null : "Chest patch smaller than in the photos",
  });
  items.push({ id: "no_extra_base", kind: "no_extra_base", label: "No base, plinth, stand or ground under the figure", on: figOn, passed: true, by: "model" });
  if (!flat && job.addons.length) {
    items.push({ id: "figure_identical", kind: "figure_identical", label: "The figure is unchanged in the product", on: "product", passed: true, by: "code" });
  }
  return {
    passed: items.every((i) => i.passed),
    items,
    text_read: name,
    model: HAIKU,
    figure_identical: !flat && job.addons.length ? 0.998 : null,
  };
}

export function intakeFor(job: JobRow): IntakeResult {
  const subjects = subjectsFromPrompt(job.prompt);
  const name = nameTextOf(job.addons);
  const form = formQuestion(job);
  const questions: IntakeResult["questions"] = [
    ...(form ? [form] : []),
    {
      id: "pose",
      text: "Pose?",
      options:
        subjects.length > 1
          ? [
              { id: "hug", label: "Hugging" },
              { id: "side", label: "Side by side" },
              { id: "stack", label: "Stacked" },
            ]
          : [
              { id: "sit", label: "Sitting" },
              { id: "stand", label: "Standing" },
              { id: "lie", label: "Lying down" },
            ],
      multi: false,
      allow_text: true,
    },
    {
      id: "style",
      text: "Style?",
      options: [
        { id: "cartoon", label: "Cartoon" },
        { id: "realistic", label: "Realistic" },
        { id: "chibi", label: "Chibi" },
      ],
      multi: false,
      allow_text: false,
    },
    thirdQuestion(job, name),
  ];
  return {
    subjects,
    // At most 4 (payloads.IntakeResult): the form question replaces the style question.
    questions: form ? questions.filter((q) => q.id !== "style") : questions,
    product_type: job.product_type,
    target_size_mm: job.target_size_mm,
    addons: job.addons,
    notes: null,
    form: formIsChoice(job.product_type) ? (job.form ?? formFromText(job.prompt)) : null,
    custom_asks: asksFromText(job.prompt),
  };
}

function thirdQuestion(job: JobRow, name: string | null): Question {
  switch (job.product_type) {
    case "keychain":
      return {
        id: "loop",
        text: "Keyring loop?",
        options: [
          { id: "top", label: "Top" },
          { id: "side", label: "Side" },
          { id: "base", label: "Base edge" },
        ],
        multi: false,
        allow_text: false,
      };
    case "nameplate":
      return {
        id: "plate",
        text: name ? `Plate style for ${name}?` : "Plate style?",
        options: [
          { id: "front", label: "Name in front" },
          { id: "under", label: "Figure on plate" },
          { id: "arch", label: "Arched plate" },
        ],
        multi: false,
        allow_text: true,
      };
    case "magnet":
      return {
        id: "framing",
        text: "What should it show?",
        options: [
          { id: "head_only", label: "Just the head" },
          { id: "full_body", label: "Full body" },
        ],
        multi: false,
        allow_text: false,
      };
    default:
      return {
        id: "extras",
        text: "Extras?",
        options: [
          { id: "name", label: "Name on base" },
          { id: "accessory", label: "Accessory" },
          { id: "none", label: "None" },
        ],
        multi: true,
        allow_text: true,
      };
  }
}

export const MOCK_MODELS = { HAIKU, SONNET, OPUS };
