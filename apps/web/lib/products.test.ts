// @vitest-environment node
// SIZE_TIERS_MM must match contracts/src/p2p_contracts/job.py.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  DEFAULT_SIZE_TIER,
  SIZE_TIERS_MM,
  axisWord,
  sizeLabel,
  sizeRange,
  sizeSpoken,
  type SizeTier,
  type StandardProduct,
} from "./products";

const jobPy = readFileSync(join(__dirname, "..", "..", "..", "contracts/src/p2p_contracts/job.py"), "utf8");

function pySizeTiers(): Record<string, Record<string, number>> {
  const block = /SIZE_TIERS_MM[^=]*=\s*\{([\s\S]+?)\n\}/.exec(jobPy)?.[1];
  if (!block) throw new Error("SIZE_TIERS_MM not found in job.py");
  const out: Record<string, Record<string, number>> = {};
  for (const m of block.matchAll(/ProductType\.(\w+):\s*\{([^}]+)\}/g)) {
    const tiers: Record<string, number> = {};
    for (const t of m[2].matchAll(/SizeTier\.(\w+):\s*([0-9.]+)/g)) tiers[t[1].toLowerCase()] = Number(t[2]);
    out[m[1].toLowerCase()] = tiers;
  }
  return out;
}

describe("size tiers", () => {
  it("match job.py SIZE_TIERS_MM", () => {
    expect(pySizeTiers()).toEqual(SIZE_TIERS_MM);
  });

  it("default tier matches job.py DEFAULT_SIZE_TIER", () => {
    const py = /DEFAULT_SIZE_TIER = SizeTier\.(\w+)/.exec(jobPy)?.[1]?.toLowerCase();
    expect(py).toBe(DEFAULT_SIZE_TIER);
  });

  it("labels sizes by the tier axis", () => {
    expect(sizeLabel("keychain", "m")).toBe(`${SIZE_TIERS_MM.keychain.m} mm tall`);
    expect(sizeLabel("nameplate", "l")).toBe("150 mm wide");
    expect(sizeRange("nameplate")).toBe("80 to 150 mm wide");
    expect(sizeRange("figurine")).toBe("50 to 120 mm tall");
    expect(sizeSpoken("figurine", "xs" as SizeTier)).toBe("XS, 50 millimeters tall");
    for (const p of Object.keys(SIZE_TIERS_MM) as StandardProduct[]) {
      expect(axisWord(p)).toBe(p === "nameplate" ? "wide" : "tall");
    }
  });
});
