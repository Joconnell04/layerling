// @vitest-environment node
// app/tokens.css is generated from design/tokens.json (scripts/gen-tokens.mjs). Fails when the
// checked-in file is stale: run `npm run gen:tokens`.
import { readFileSync } from "node:fs";
import { TOKENS_CSS, readTokens, renderTokens } from "../scripts/gen-tokens.mjs";

describe("design tokens", () => {
  const tokens = readTokens();
  const css = readFileSync(TOKENS_CSS, "utf8");

  it("app/tokens.css matches design/tokens.json", () => {
    expect(css).toBe(renderTokens(tokens));
  });

  it("has every color, radius and type style", () => {
    for (const t of tokens.color.tokens) expect(css).toContain(`--color-${t.name}: ${t.value};`);
    for (const t of tokens.radius.tokens) expect(css).toContain(`--${t.name}: ${t.value};`);
    expect(css).toContain("--shadow-elev-sheet: 6px 6px 0 #e4dbcb;");
    expect(css).toContain("--text-display-xl: 88px;");
    expect(css).toContain("--text-display-xl-phone: 52px;");
    expect(css).toContain("--dur-print: 900ms;");
    expect(css).toContain("--ease-print: steps(18, jump-end);");
  });

  it("the studio aliases point at v1 tokens", () => {
    const globals = readFileSync(TOKENS_CSS.replace("tokens.css", "globals.css"), "utf8");
    for (const [old, v1] of [
      ["bg", "paper"],
      ["panel", "sheet"],
      ["raised", "tint"],
      ["fg", "ink"],
      ["muted", "ink-2"],
      ["faint", "ink-3"],
      ["accent", "brick"],
      ["accent-fg", "on-brick"],
      ["bad", "danger"],
    ]) {
      expect(globals).toContain(`--color-${old}: var(--color-${v1});`);
    }
  });
});
