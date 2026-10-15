import { readFileSync } from "node:fs";
import { join } from "node:path";
import { deltaE2000, filamentSlug, hexToLab, isHex, normalizeHex } from "./color";

const library = JSON.parse(
  readFileSync(join(__dirname, "..", "..", "..", "filaments", "library.example.json"), "utf8"),
) as { filaments: { id: string; brand: string; material: string; line: string; name: string; hex: string; lab: number[] }[] };

describe("hexToLab", () => {
  it("matches the Python library values within 0.05", () => {
    expect(library.filaments.length).toBeGreaterThan(5);
    for (const f of library.filaments) {
      const lab = hexToLab(f.hex);
      lab.forEach((v, i) => expect(Math.abs(v - f.lab[i])).toBeLessThan(0.05));
    }
  });

  it("handles white and black", () => {
    expect(hexToLab("#FFFFFF")).toEqual([100, 0, 0]);
    expect(hexToLab("000000")).toEqual([0, 0, 0]);
  });

  it("rejects bad hex", () => {
    expect(isHex("#12345")).toBe(false);
    expect(() => normalizeHex("zzz")).toThrow();
    expect(normalizeHex("abcdef")).toBe("#ABCDEF");
  });
});

describe("deltaE2000", () => {
  // Sharma et al. test data, pairs 1, 7, 17.
  it.each([
    [[50, 2.6772, -79.7751], [50, 0, -82.7485], 2.0425],
    [[50, 0, 0], [50, -1, 2], 2.3669],
    [[50, 2.5, 0], [56, -27, -3], 31.903],
  ] as const)("pair %#", (a, b, expected) => {
    expect(deltaE2000([...a], [...b])).toBeCloseTo(expected, 3);
  });
});

describe("filamentSlug", () => {
  it("rebuilds every example library id", () => {
    for (const f of library.filaments) {
      expect(filamentSlug(f.brand, f.material, f.line, f.name)).toBe(f.id);
    }
  });
});
