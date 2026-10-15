import { assertPayload, validatePayload } from "./validate";

describe("validatePayload", () => {
  it("accepts contract-shaped user payloads", () => {
    expect(
      validatePayload("answers", {
        answers: [{ question_id: "q1", option_ids: ["a"], text: null }],
      }).ok,
    ).toBe(true);
    expect(validatePayload("concept_pick", { candidate_id: "r1c1", approve: true }).ok).toBe(true);
    expect(validatePayload("tweak", { text: "bigger ears", candidate_id: "r1c2" }).ok).toBe(true);
    expect(validatePayload("mesh_decision", { action: "retexture", text: "warmer" }).ok).toBe(true);
    expect(validatePayload("review_decision", { action: "rerun_paint" }).ok).toBe(true);
    expect(validatePayload("prompt", {}).ok).toBe(true);
    expect(validatePayload("resume", { cap_credits: 800, cap_usd: null, note: "ok" }).ok).toBe(true);
    expect(validatePayload("resume", { cap_credits: "lots" }).ok).toBe(false);
  });

  it("rejects extra fields (extra=forbid) and bad enums", () => {
    expect(validatePayload("concept_pick", { candidate_id: "r1c1", approve: true, x: 1 }).ok).toBe(
      false,
    );
    expect(validatePayload("mesh_decision", { action: "delete" }).ok).toBe(false);
    expect(validatePayload("review_decision", {}).ok).toBe(false);
    expect(() => assertPayload("tweak", { candidate_id: "r1c1" })).toThrow(/Invalid tweak/);
  });

  it("checks assistant payload limits", () => {
    const q = (n: number) => ({
      id: `q${n}`,
      text: "Pose?",
      options: [
        { id: "a", label: "Sitting" },
        { id: "b", label: "Standing" },
      ],
    });
    const base = { subjects: [], product_type: "figurine" };
    expect(validatePayload("questions", { ...base, questions: [q(1), q(2)] }).ok).toBe(true);
    expect(
      validatePayload("questions", { ...base, questions: [q(1), q(2), q(3), q(4), q(5)] }).ok,
    ).toBe(false);
  });
});
