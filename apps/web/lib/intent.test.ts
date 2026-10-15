// The web port of worker/src/p2p_worker/intent.py: the worker's own cases
// (worker/tests/test_front_fidelity.py) and every golden prompt, plus drift guards on the lists.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { FORM_CHOICE_PRODUCTS, askKeyWord, asksFromText, formFromText, formLabel } from "./intent";

const repo = join(__dirname, "..", "..", "..");
const read = (p: string) => readFileSync(join(repo, p), "utf8");

describe("formFromText", () => {
  it("reads the form only when the request names it", () => {
    expect(formFromText("3d keychain of my cat")).toBe("figure");
    expect(formFromText("flat keychain of my calico")).toBe("flat");
    expect(formFromText("keychain of my frenchie")).toBeNull();
    expect(formFromText("magnet with a flat back of my dog")).toBeNull(); // a back, not a form
    expect(formFromText("3D figure or flat, not sure")).toBeNull();
  });

  it("labels the form in the intake question's words", () => {
    expect(formLabel("figure", "keychain")).toBe("3D figure");
    expect(formLabel("flat", "keychain")).toBe("Flat charm");
    expect(formLabel("flat", "magnet")).toBe("Flat relief");
  });
});

describe("asksFromText", () => {
  const pairs = (t: string) => asksFromText(t).map((a) => [a.kind, a.text]);

  it("finds props, outfits and names in casual requests", () => {
    expect(pairs("keychain of my frenchie wearing a sombrero lol")).toEqual([["outfit", "wearing a sombrero"]]);
    expect(pairs("nameplate for my beagle bronco with his name, give him a bow tie")).toEqual([["outfit", "wearing a bow tie"]]);
    expect(asksFromText("my golden holding a ball pls").map((a) => a.text)).toEqual(["holding a ball"]);
    expect(asksFromText("keychain of me holding my chihuahua")).toEqual([]); // a subject, not a prop
    expect(pairs("keychain of my teddy with his name Otis")).toEqual([["text", "Otis"]]);
    expect(pairs("my cat sitting in a santa hat")).toEqual([
      ["outfit", "wearing a santa hat"],
      ["pose", "sitting"],
    ]);
  });

  it("finds every ask the golden set expects, and the form it states", () => {
    const sets = JSON.parse(read("evals/golden/sets.json")) as {
      id: string;
      prompt: string;
      product_type: string;
      expect: { form: string | null; custom_asks: string[] };
    }[];
    expect(sets.length).toBeGreaterThan(0);
    for (const s of sets) {
      if ((FORM_CHOICE_PRODUCTS as readonly string[]).includes(s.product_type)) {
        expect([s.id, formFromText(s.prompt)]).toEqual([s.id, s.expect.form]);
      }
      const found = asksFromText(s.prompt)
        .map((a) => a.text)
        .join(" ");
      for (const want of s.expect.custom_asks) expect([s.id, found]).toEqual([s.id, expect.stringContaining(want)]);
    }
  });

  it("names an ask by its key word", () => {
    expect(askKeyWord("wearing a sombrero")).toBe("Sombrero");
    expect(askKeyWord("holding a ball")).toBe("Ball");
    expect(askKeyWord("sitting")).toBe("Sitting");
  });

  it("keeps the worker's word lists", () => {
    const py = read("worker/src/p2p_worker/intent.py");
    for (const word of ["sombrero", "bandana", "frisbee", "chihuahua", "medallion", "curled"]) {
      expect(py).toContain(word);
      expect(read("apps/web/lib/intent.ts")).toContain(word);
    }
    const jobPy = read("contracts/src/p2p_contracts/job.py");
    expect(jobPy).toMatch(/FORM_CHOICE_PRODUCTS[^=]*= \(ProductType\.KEYCHAIN, ProductType\.MAGNET\)/);
  });
});
