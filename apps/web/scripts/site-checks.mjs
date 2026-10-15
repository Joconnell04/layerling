// Public site checks against a running mock server (npm run dev:mock, FIRST_LOOK_ENABLED=true):
//   1. layout shift: a PerformanceObserver sums layout-shift entries while the landing scrolls
//      top to bottom and back; fails above 0.02.
//   2. reduced motion: with prefers-reduced-motion, every print-in body is fully shown (no
//      clip-path, no animation) and the parallax images do not move.
//   3. screenshots at 1440x900 and 390x844 into docs/design/shots/ (--shots).
// Usage: PLAYWRIGHT_CORE=/path/to/node_modules/playwright-core node scripts/site-checks.mjs [--shots]
// Chromium comes from PLAYWRIGHT_BROWSERS_PATH (preinstalled at /opt/pw-browsers).
// Test orders go to test@example.com on the local in-memory store only; nothing is emailed.

import { createRequire } from "node:module";
import { mkdirSync, readdirSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_CORE ?? "playwright-core");
const here = dirname(fileURLToPath(import.meta.url));
const BASE = process.env.BASE_URL ?? "http://localhost:3100";
const SHOTS = resolve(here, "../../../docs/design/shots");
const wantShots = process.argv.includes("--shots");

function chromePath() {
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH ?? "/opt/pw-browsers";
  const dir = readdirSync(root).find((d) => /^chromium-\d+$/.test(d));
  const p = dir && resolve(root, dir, "chrome-linux/chrome");
  return p && existsSync(p) ? p : undefined;
}

const browser = await chromium.launch({ executablePath: chromePath() });
const results = [];
const fail = (msg) => {
  results.push(`FAIL ${msg}`);
  process.exitCode = 1;
};

async function page(width, height, opts = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1, ...opts });
  return { ctx, p: await ctx.newPage() };
}

async function scrollThrough(p) {
  await p.evaluate(async () => {
    const step = Math.max(200, Math.floor(innerHeight / 2));
    for (let y = 0; y <= document.body.scrollHeight; y += step) {
      scrollTo(0, y);
      await new Promise((r) => setTimeout(r, 90));
    }
    await new Promise((r) => setTimeout(r, 400));
    scrollTo(0, 0);
    await new Promise((r) => setTimeout(r, 300));
  });
}

// ------------------------------------------------------------------ 1. layout shift
for (const [w, h] of [
  [1440, 900],
  [390, 844],
]) {
  const { ctx, p } = await page(w, h);
  await p.addInitScript(() => {
    window.__cls = 0;
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) if (!e.hadRecentInput) window.__cls += e.value;
    }).observe({ type: "layout-shift", buffered: true });
  });
  await p.goto(`${BASE}/`, { waitUntil: "networkidle" });
  await scrollThrough(p);
  const cls = await p.evaluate(() => window.__cls);
  results.push(`CLS landing ${w}x${h}: ${cls.toFixed(4)}`);
  if (cls > 0.02) fail(`CLS ${cls.toFixed(4)} above 0.02 at ${w}`);
  await ctx.close();
}

// ------------------------------------------------------------------ 2. reduced motion
{
  const { ctx, p } = await page(1440, 900, { reducedMotion: "reduce" });
  await p.goto(`${BASE}/`, { waitUntil: "networkidle" });
  const state = await p.evaluate(() => {
    const bodies = [...document.querySelectorAll(".print-in > .print-in-body")];
    const hidden = bodies.filter((b) => {
      const s = getComputedStyle(b);
      return (s.clipPath && s.clipPath !== "none" && !/inset\(0(px)?( 0(px)?)*\)/.test(s.clipPath)) || s.animationName !== "none";
    });
    const moving = [...document.querySelectorAll(".drift")].filter((el) => getComputedStyle(el).animationName !== "none");
    const reveals = [...document.querySelectorAll("[data-reveal]")].filter((el) => Number(getComputedStyle(el).opacity) < 1);
    return { bodies: bodies.length, hidden: hidden.length, moving: moving.length, reveals: reveals.length };
  });
  results.push(`reduced motion: ${state.bodies} print-in blocks, ${state.hidden} hidden, ${state.moving} parallax moving, ${state.reveals} reveals hidden`);
  if (!state.bodies) fail("no print-in blocks found");
  if (state.hidden || state.moving || state.reveals) fail("motion still active under prefers-reduced-motion");
  await ctx.close();
}

// ------------------------------------------------------------------ 3. screenshots
async function placeTestOrder(product, tier, request) {
  const start = await fetch(`${BASE}/api/orders/start`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      name: "Maria Test",
      email: "maria@example.com",
      product_type: product,
      size_tier: tier,
      request,
      source: "shots",
      photos: [{ name: "beagle.jpg", type: "image/jpeg", size: 1000 }],
    }),
  }).then((r) => r.json());
  if (!start.uploads) throw new Error(`order start failed: ${JSON.stringify(start)}`);
  const photo = await fetch(`${BASE}/samples/beagle-photo.jpg`).then((r) => r.arrayBuffer());
  for (const u of start.uploads) {
    await fetch(u.url.startsWith("http") ? u.url : `${BASE}${u.url}`, { method: "PUT", headers: u.headers, body: photo });
  }
  const fin = await fetch(`${BASE}/api/orders/finish`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ticket: start.ticket }),
  }).then((r) => r.json());
  return fin.receipt;
}

if (wantShots) {
  mkdirSync(SHOTS, { recursive: true });
  const widths = [
    [1440, 900],
    [390, 844],
  ];
  const receipt = await placeTestOrder("keychain", "m", "Our beagle Biscuit, red collar");
  for (const [w, h] of widths) {
    const shot = async (name, url, fn, full = false, reduced = false) => {
      const { ctx, p } = await page(w, h, reduced ? { reducedMotion: "reduce" } : {});
      await p.goto(`${BASE}${url}`, { waitUntil: "networkidle" });
      if (fn) await fn(p);
      await p.waitForTimeout(500);
      await p.screenshot({ path: `${SHOTS}/${name}-${w}.png`, fullPage: full });
      results.push(`shot ${name}-${w}.png`);
      await ctx.close();
    };
    await shot("landing-top", "/");
    await shot("landing-full", "/", null, true, true);
    await shot("order-keychain", "/order?product=keychain", async (p) => {
      await p.locator("#request").scrollIntoViewIfNeeded();
      await p.waitForTimeout(1300);
    });
    await shot("order-other-error", "/order?product=other", async (p) => {
      await p.fill("#request", "a planter");
      await p.click("text=Send order");
      await p.locator("#request").scrollIntoViewIfNeeded();
    });
    await shot("size-dialog", "/order?product=keychain", async (p) => {
      await p.click("text=Compare sizes");
      await p.waitForTimeout(400);
    });
    await shot("sizes", "/sizes", null, true, true);
    await shot("confirmation-printing", `/order/thanks?r=${encodeURIComponent(receipt)}&view=printing`, null, true);
    await shot("confirmation-done", `/order/thanks?r=${encodeURIComponent(receipt)}&view=done`, null, true, true);
  }
}

await browser.close();
console.log(results.join("\n"));
