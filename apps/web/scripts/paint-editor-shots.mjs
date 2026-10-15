// Paint editor screenshots at 1440x900 from a running mock server (npm run dev:mock): the
// editor on the 3D keychain (Olive), a brush stroke and a fill with a catalog color, the worker's
// phase after Send, the preview (before and after, eight views, a layer), and Map paint on a
// thin-slice job. Writes docs/design/shots/studio/paint-editor/*.png.
// Usage: PLAYWRIGHT_CORE=/path/to/node_modules/playwright-core BASE_URL=http://localhost:3100 \
//          node scripts/paint-editor-shots.mjs
// Headless Chromium with SwiftShader WebGL. The mock worker answers with the fixture in
// public/mock/paint (scripts/gen_mock_paint_edit.py); nothing leaves the mock store.

import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_CORE ?? "playwright-core");
const here = dirname(fileURLToPath(import.meta.url));
const BASE = process.env.BASE_URL ?? "http://localhost:3100";
const OUT = resolve(here, "../../../docs/design/shots/studio/paint-editor");
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, colorScheme: "light" });
const page = await ctx.newPage();

async function shot(name) {
  await page.screenshot({ path: resolve(OUT, `${name}.png`), animations: "disabled" });
  console.log(`${name}.png`);
}

async function openJob(match) {
  await page.goto(`${BASE}/jobs`);
  const link = page.locator("a[href^='/jobs/']", { hasText: match }).first();
  await link.waitFor({ timeout: 60_000 });
  await page.goto(`${BASE}${await link.getAttribute("href")}`);
  await page.getByLabel("Display").getByRole("radio", { name: "Edit" }).click();
}

const tool = (name) => page.getByLabel("Tool").getByRole("radio", { name });

// 1. The editor on the shipped paint.
await openJob("Olive");
await page.getByTestId("paint-slots").waitFor();
await page.waitForTimeout(2000);
const box = await page.getByTestId("paint-canvas").boundingBox();

/** A canvas point whose hover readout matches (the model's pose depends on the camera). */
async function find(re) {
  for (let y = box.y + 80; y < box.y + box.height - 40; y += 14) {
    for (let x = box.x + 150; x < box.x + box.width - 150; x += 14) {
      await page.mouse.move(x, y);
      await page.waitForTimeout(20);
      const t = await page.getByTestId("paint-hover").textContent({ timeout: 50 }).catch(() => "");
      if (re.test(t)) return { x, y };
    }
  }
  throw new Error(`nothing under the cursor matches ${re}`);
}
const head = await find(/s1\.body/);
const collar = await find(/collar/);
await page.mouse.move(box.x + 40, box.y + box.height - 30);
await page.waitForTimeout(300);
await shot("editor");

// 2. A white blaze brushed on the head, the collar filled with a catalog color.
await page.getByRole("radio", { name: /Slot 2 White/ }).click();
await page.mouse.move(head.x + 10, head.y + 30);
await page.mouse.down();
for (let i = 0; i <= 12; i++) await page.mouse.move(head.x + 10 + i * 3, head.y + 30 + i, { steps: 2 });
await page.mouse.up();
await page.getByTestId("paint-editor").getByRole("button", { name: "Add color" }).click();
await page.waitForTimeout(300);
await shot("editor-picker");
await page.getByTestId("pick-bambu-pla-basic-orange").click();
await tool("Fill").click();
await page.mouse.click(collar.x, collar.y);
await tool("Brush").click();
await page.mouse.move(head.x + 34, head.y + 56);
await page.waitForTimeout(400);
await shot("editor-edits");

// 3. Send: the worker's phase in the editor, the preview panel and the result column.
await page.getByTestId("paint-send").click();
await page.getByTestId("paint-status").filter({ hasText: "Slicing" }).waitFor({ timeout: 30_000 });
await page.waitForTimeout(200);
await shot("editor-running");

// 4. Done: the new paint in the editor, then the preview (before and after, eight views, a layer).
await page.getByTestId("paint-edits").filter({ hasText: "No edits" }).waitFor({ timeout: 30_000 });
await page.getByTestId("palette-pending").waitFor({ state: "detached", timeout: 30_000 });
await page.waitForTimeout(1200);
await shot("editor-done");
await page.getByTestId("paint-preview").scrollIntoViewIfNeeded();
await page.getByTestId("preview-layers").locator("canvas").waitFor();
await page.waitForFunction(() => [...document.images].every((i) => i.complete && i.naturalWidth > 0), null, { timeout: 30_000 });
await page.evaluate(() => window.scrollBy(0, 120));
await page.waitForTimeout(600);
await shot("preview");
const slider = page.getByRole("slider", { name: "Layer" });
await slider.fill("40");
await page.getByLabel("Views").getByRole("radio", { name: "Paint" }).click();
await page.waitForTimeout(800);
await shot("preview-layer-paint");

// 5. A thin-slice job with no paint map.
await openJob("Biscuit");
await page.getByTestId("paint-empty").waitFor();
await page.waitForTimeout(500);
await shot("map-paint");

await browser.close();
