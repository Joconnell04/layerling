// Studio fidelity screenshots at 1440x900 from a running mock server (npm run dev:mock):
// new-job form (form choice and asks), intake (form question), concept pick (flat charm product,
// figure, checks), mesh gate (3D keychain from eight views), results (3D keychain print proof set,
// flat charm rendered from its preview). Writes docs/design/shots/studio/fidelity/*.png.
// Usage: PLAYWRIGHT_CORE=/path/to/node_modules/playwright-core BASE_URL=http://localhost:3100 \
//          node scripts/studio-fidelity-shots.mjs
// Headless Chromium with SwiftShader WebGL (the page renders GLBs from the eight proof views).
// Nothing is submitted: the new-job form is filled, not started.

import { createRequire } from "node:module";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_CORE ?? "playwright-core");
const here = dirname(fileURLToPath(import.meta.url));
const BASE = process.env.BASE_URL ?? "http://localhost:3100";
const OUT = resolve(here, "../../../docs/design/shots/studio/fidelity");
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, colorScheme: "light" });
const page = await ctx.newPage();

/** Wait until every image on the page has loaded. */
async function settled() {
  await page.waitForLoadState("networkidle");
  await page.waitForFunction(() => [...document.images].every((i) => i.complete && i.naturalWidth > 0), null, { timeout: 30_000 });
  await page.waitForTimeout(400);
}

async function jobHref(match) {
  await page.goto(`${BASE}/jobs`);
  const link = page.locator("a[href^='/jobs/']", { hasText: match }).first();
  await link.waitFor();
  return link.getAttribute("href");
}

async function shot(name) {
  await page.screenshot({ path: resolve(OUT, `${name}.png`) });
  console.log(`${name}.png`);
}

// 1. New job: a keychain whose request says 3D and asks for a sombrero and a ball.
await page.goto(`${BASE}/jobs/new`);
await page.getByTestId("photo-input").setInputFiles({
  name: "olive.svg",
  mimeType: "image/svg+xml",
  buffer: readFileSync(resolve(here, "../public/mock/photo-1.svg")),
});
await page.getByLabel("Request").fill("3d keychain of our frenchie Olive wearing a sombrero, holding a ball");
await page.getByRole("radio", { name: "Keychain" }).click();
await settled();
await shot("new-job-keychain-form");

// 2. Intake: the form question, answered like the others.
await page.goto(`${BASE}${await jobHref("cat Pip")}`);
await page.getByRole("group", { name: /Flat charm or 3D figure/ }).getByRole("radio", { name: "Flat charm" }).click();
await page.getByRole("group", { name: /Pose/ }).getByRole("radio", { name: "Sitting" }).click();
await settled();
await shot("intake-form-question");

// 3. Concept pick: the flat charm the customer gets, the figure alone, the check.
await page.goto(`${BASE}${await jobHref("Juniper")}`);
await page.getByTestId("concept-band").waitFor();
await settled();
await shot("concepts-flat-charm");

// 4. Mesh gate: the 3D keychain from all eight views next to the approved concept.
await page.goto(`${BASE}${await jobHref("Mochi")}`);
await page.getByTestId("proof-count").filter({ hasText: "8 of 8 views" }).waitFor({ timeout: 60_000 });
await settled();
await shot("mesh-review-3d-keychain");

// 5. Result: the shipped proof set of a 3D keychain, simulated print, then the model.
await page.goto(`${BASE}${await jobHref("Olive")}`);
await page.getByTestId("proof-gallery").waitFor();
await settled();
await shot("result-3d-keychain-print");
await page.getByRole("radiogroup", { name: "Proof" }).getByRole("radio", { name: "Model" }).click();
await settled();
await shot("result-3d-keychain-model");

// 6. Result: a thin slice flat charm, its preview rendered from every view.
await page.goto(`${BASE}${await jobHref("Biscuit")}`);
await page.getByTestId("proof-count").filter({ hasText: "8 of 8 views" }).waitFor({ timeout: 60_000 });
await settled();
await shot("result-flat-charm");

await browser.close();
