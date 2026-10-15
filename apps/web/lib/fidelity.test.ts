import type { ConceptCheck } from "./contracts.gen";
import type { AnyMessage, JobRow, StageRunRow } from "./db.types";
import { deriveGates } from "./derive";
import { checkChips, jobForm, proofSets, viewOf } from "./fidelity";

let seq = 0;
const msg = (kind: string, payload: unknown, over: Partial<AnyMessage> = {}) =>
  ({
    id: `m${++seq}`,
    job_id: "j",
    owner_id: null,
    created_at: `2026-09-24T10:00:${String(seq).padStart(2, "0")}Z`,
    role: ["answers", "concept_pick", "mesh_decision"].includes(kind) ? "user" : "assistant",
    kind,
    stage: null,
    body: null,
    payload,
    reply_to: null,
    consumed_at: null,
    ...over,
  }) as AnyMessage;

const job = (over: Partial<JobRow> = {}) =>
  ({ id: "j", product_type: "keychain", prompt: "keychain of my frenchie", result: null, form: null, ...over }) as JobRow;

const run = (stage: StageRunRow["stage"], outputs: { path: string; role: string }[], attempt = 1) =>
  ({ stage, attempt, status: "succeeded", manifest: { outputs } }) as unknown as StageRunRow;

describe("jobForm", () => {
  it("takes the row, then the answer, then the request's words", () => {
    expect(jobForm(job({ product_type: "figurine" }), deriveGates([]))).toBe("figure");
    expect(jobForm(job({ form: "flat" }), deriveGates([]))).toBe("flat");
    expect(jobForm(job(), deriveGates([]))).toBeNull();
    expect(jobForm(job({ prompt: "3d keychain of my cat" }), deriveGates([]))).toBe("figure");
    const q = msg("questions", { subjects: [], questions: [], product_type: "keychain" });
    const a = msg("answers", { answers: [{ question_id: "form", option_ids: ["flat"] }] }, { reply_to: q.id });
    expect(jobForm(job(), deriveGates([q, a]))).toBe("flat");
  });
});

describe("checkChips", () => {
  it("says each check in a few words, failures first", () => {
    const check: ConceptCheck = {
      passed: false,
      text_read: "BISCUIT",
      items: [
        { id: "part.keyring_loop", kind: "part", label: "A keyring loop on top of the head", on: "product", passed: true },
        { id: "text", kind: "text", label: "The name reads exactly BISCUIT", on: "product", passed: true },
        { id: "ask.1", kind: "ask", label: "wearing a sombrero", on: "figure", passed: false, note: "No hat" },
        { id: "no_extra_base", kind: "no_extra_base", label: "No base", on: "figure", passed: true },
        { id: "no_extra_base.product", kind: "no_extra_base", label: "Nothing else", on: "product", passed: true },
        { id: "figure_identical", kind: "figure_identical", label: "Unchanged", on: "product", passed: true, by: "code" },
      ],
    };
    const chips = checkChips(check);
    expect(chips.map((c) => c.text)).toEqual(["Sombrero missing", "Loop checked", "Name checked", "No extra base", "Figure kept"]);
    expect(chips[0]).toMatchObject({ passed: false, detail: "wearing a sombrero. No hat" });
    expect(chips.find((c) => c.text === "Name checked")!.detail).toContain("Read: BISCUIT");
    expect(checkChips(null)).toEqual([]);
    for (const c of chips) expect(c.text.split(" ").length).toBeLessThanOrEqual(3);
  });
});

describe("proof sets", () => {
  it("reads the view from the file name", () => {
    expect(viewOf("s10_write/a1/proof/print_front_3q.png")).toBe("front_3q");
    expect(viewOf("s05_mesh/a1/thumb_m1_left.png")).toBe("left");
    expect(viewOf("s09_paint/a2/proof/final/model_back_3q.png")).toBe("back_3q");
    expect(viewOf("s10_write/a1/proof/print_sheet.png")).toBeNull();
  });

  const views = ["front", "front_3q", "left_3q", "left", "right", "back", "back_3q", "top"];

  it("prefers the contract proof_images, then stage outputs by role", () => {
    const result = {
      threemf: "s10_write/a1/final.3mf",
      proof_images: views.flatMap((v) => [
        { kind: "print" as const, view: v, path: `s10_write/a1/proof/print_${v}.png` },
        { kind: "model" as const, view: v, path: `s10_write/a1/proof/model_${v}.png` },
      ]),
    };
    const sets = proofSets(job({ result }), [], deriveGates([]));
    expect(sets.map((s) => [s.kind, s.source])).toEqual([
      ["print", "proof"],
      ["model", "proof"],
    ]);
    expect(Object.keys(sets[0].images)).toHaveLength(8);

    // No contract field yet (a result from before fidelity/back): the s09 outputs.
    const runs = [run("s09_paint", views.map((v) => ({ path: `s09_paint/a1/proof/final/paint_${v}.png`, role: "proof_paint" })))];
    const fromRuns = proofSets(job({ result: { threemf: "x.3mf", preview_glb: "s07_baseline/a1/preview.glb" } }), runs, deriveGates([]));
    expect(fromRuns.map((s) => [s.kind, s.source])).toEqual([["paint", "proof"]]);
  });

  it("renders the mesh waiting for approval from every view, never an older proof set", () => {
    const review = msg("mesh_review", {
      glb: "s05_mesh/a2/mesh_m2.glb",
      thumbnails: ["s05_mesh/a2/thumb_m2_front.png", "s05_mesh/a2/thumb_m2_back.png"],
      meshy_task_id: "t",
    });
    const old = [run("s10_write", views.map((v) => ({ path: `s10_write/a1/proof/model_${v}.png`, role: "proof_model" })))];
    const sets = proofSets(job(), old, deriveGates([review]));
    expect(sets).toEqual([{ kind: "model", source: "glb", images: {}, glb: "s05_mesh/a2/mesh_m2.glb" }]);
  });

  it("falls back to the preview GLB for paint on a thin slice result", () => {
    const sets = proofSets(job({ result: { threemf: "a.3mf", preview_glb: "s07_baseline/a1/preview.glb" } }), [], deriveGates([]));
    expect(sets).toEqual([{ kind: "paint", source: "glb", images: {}, glb: "s07_baseline/a1/preview.glb" }]);
  });
});
