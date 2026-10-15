// @vitest-environment node
import { existsSync } from "node:fs";
import { join } from "node:path";
import { LINEUP_PRODUCTS, SIZE_TIERS, SIZE_TIERS_MM } from "./products";
import { frontRender, heroSrc, placeFront, renderAxisMm } from "./renders";

describe("product renders", () => {
  it("every standard product has a hero and a front render on disk", () => {
    for (const p of LINEUP_PRODUCTS) {
      for (const src of [heroSrc(p), frontRender(p).src]) {
        expect(existsSync(join(__dirname, "..", "public", src))).toBe(true);
      }
    }
  });

  it("front renders are 10 px per mm on the tier axis", () => {
    for (const p of LINEUP_PRODUCTS) {
      const f = frontRender(p);
      const [x0, y0, x1, y1] = f.box;
      const px = p === "nameplate" ? x1 - x0 : y1 - y0;
      // The 4 degree camera elevation adds a sliver of the top: within 2 percent.
      expect(Math.abs(px / f.pxPerMm / renderAxisMm(p) - 1)).toBeLessThan(0.02);
    }
  });

  it("placeFront draws every tier at one px per mm", () => {
    for (const p of LINEUP_PRODUCTS) {
      for (const t of SIZE_TIERS) {
        const d = placeFront(p, t, 4);
        const drawn = p === "nameplate" ? d.objectWidth : d.objectHeight;
        expect(Math.abs(drawn / 4 / SIZE_TIERS_MM[p][t] - 1)).toBeLessThan(0.02);
        expect(d.offsetBottom).toBeLessThan(0);
      }
    }
  });
});
