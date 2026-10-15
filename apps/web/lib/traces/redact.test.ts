import { SECRET_PATTERN, manifestSafe, redact, redactText } from "./redact";

const JOB = "1785a480-84b8-41f9-8f97-1005b6d7d0a6";
const JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJzdG9yYWdlIn0.c2lnbmF0dXJlLXZhbHVl";  // gitleaks:allow (test fake)
const SUPA = `https://abc.supabase.co/storage/v1/object/sign/artifacts/${JOB}/s03_concept/a1/approved%20concept.png?token=${JWT}`;
const MESHY = "https://assets.meshy.ai/t/1/output/model.glb?Expires=1790484954&Signature=AbC~d_e&Key-Pair-Id=KXYZ";

describe("redact", () => {
  it("turns signed URLs into storage refs or bare URLs, whole-string like manifest_safe", () => {
    expect(manifestSafe(SUPA)).toBe(`storage:artifacts/${JOB}/s03_concept/a1/approved concept.png`);
    expect(manifestSafe(MESHY)).toBe("https://assets.meshy.ai/t/1/output/model.glb");
    expect(manifestSafe("data:image/png;base64,iVBORw0KGgo=")).toBe("data:image/png;redacted;len=12");
    const safe = "data:image/jpeg;sha256=c905ab333e6138f0;len=604948";
    expect(manifestSafe(safe)).toBe(safe);
    expect(manifestSafe("storage:inputs/x/input/photo_1.jpg")).toBe("storage:inputs/x/input/photo_1.jpg");
  });

  it("removes keys, JWTs, bearer tokens, signed URLs, data URIs and configured secrets anywhere", () => {
    const text =
      "keys sk-ant-api03-abcdefghijklmnop msy_abcdefghijklmnop sb_secret_abcdefghijklmnop " +
      `sb_publishable_abcdefghijk jwt ${JWT} auth Bearer x, the-configured-secret-value url ${SUPA} ` +
      `and ${MESHY} img data:image/png;base64,iVBORw0KGgoAAAANSUhEUg== half eyJhbGciOiJIUzI1NiJ9 token=abc`;
    const out = redact(
      { a: [{ deep: text }], manifest: { request: { reference_image_urls: [SUPA] }, result: { model_urls: { glb: MESHY } } } },
      ["the-configured-secret-value"],
    );
    const s = JSON.stringify(out);
    expect(s).not.toMatch(SECRET_PATTERN);
    for (const bad of ["sk-ant", "msy_abc", "sb_secret", "sb_publishable_", "Bearer x", "AbC~d_e", "base64,", "the-configured-secret-value"]) {
      expect(s).not.toContain(bad);
    }
    expect(out.manifest.request.reference_image_urls[0]).toBe(`storage:artifacts/${JOB}/s03_concept/a1/approved concept.png`);
    expect(out.manifest.result.model_urls.glb).toBe("https://assets.meshy.ai/t/1/output/model.glb");
  });

  it("drops secret-looking keys and long base64 blobs, keeps ordinary values", () => {
    const out = redact({ headers: { Authorization: "x", apikey: "y" }, blob: "A".repeat(3000), n: 3, ok: true, none: null });
    expect(out).toEqual({ headers: { Authorization: "[redacted]", apikey: "[redacted]" }, blob: "<base64 3000 chars>", n: 3, ok: true, none: null });
    expect(redactText("Request: nameplate for my beagle Bronco")).toBe("Request: nameplate for my beagle Bronco");
  });
});
