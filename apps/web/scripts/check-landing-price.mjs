// After `next build`: the prerendered pages must show the cost model's prices. Reads the same
// lib/pricing.generated.json the build used, and fails when
//   - the landing lacks a product's "from $X" (X = its lowest per-size price, else `price`),
//   - the sizes page lacks a per-size price the file has,
//   - either page shows "[PRICE]".
// Skips when the cost model is missing or a page was not prerendered.
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const web = join(dirname(fileURLToPath(import.meta.url)), "..");
const pricingPath = join(web, "lib", "pricing.generated.json");
const htmlPath = join(web, ".next", "server", "app", "index.html");
const sizesPath = join(web, ".next", "server", "app", "sizes.html");

if (!existsSync(pricingPath) || !existsSync(htmlPath)) {
  console.log("landing price check: skipped (no pricing file or no prerendered landing page)");
  process.exit(0);
}
const pricing = JSON.parse(readFileSync(pricingPath, "utf8"));
if (pricing.missing) {
  console.log("landing price check: skipped (costs/pricing.json missing, [PRICE] expected)");
  process.exit(0);
}
// React escapes nothing in "$31.99", but keep the check tolerant of split text nodes.
const read = (p) => readFileSync(p, "utf8").replace(/<!-- -->/g, "");
const html = read(htmlPath);
const money = (n) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: pricing.currency ?? "USD",
    maximumFractionDigits: Number.isInteger(n) ? 0 : 2,
  }).format(n);
const valid = (n) => typeof n === "number" && Number.isFinite(n) && n > 0;
const sizePrices = (p) =>
  Object.entries(p.sizes ?? {})
    .map(([tier, s]) => [tier, typeof s === "number" ? s : s?.price])
    .filter(([, n]) => valid(n));

const missing = [];
let sizeChecks = 0;
const sizesHtml = existsSync(sizesPath) ? read(sizesPath) : null;
for (const [product, p] of Object.entries(pricing.products ?? {})) {
  const sizes = sizePrices(p);
  const base = typeof p.from === "number" ? p.from : p.price;
  const from = sizes.length ? Math.min(...sizes.map(([, n]) => n)) : base;
  if (!valid(from)) continue;
  if (!html.includes(money(from))) missing.push(`landing ${product} ${money(from)}`);
  if (sizesHtml) {
    for (const [tier, n] of sizes) {
      sizeChecks++;
      if (!sizesHtml.includes(money(n))) missing.push(`sizes ${product} ${tier} ${money(n)}`);
    }
  }
}
const placeholders = (html.match(/\[PRICE\]/g) ?? []).length + ((sizesHtml ?? "").match(/\[PRICE\]/g) ?? []).length;
if (missing.length || placeholders) {
  console.error(`landing price check: FAILED. Missing: ${missing.join(", ") || "none"}; [PRICE] shown ${placeholders} times`);
  process.exit(1);
}
console.log(
  `landing price check: ok (${Object.keys(pricing.products ?? {}).length} products, ${sizeChecks} size prices on /sizes${sizesHtml ? "" : " (not prerendered)"})`,
);
