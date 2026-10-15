// Copy the pricing workstream's costs/pricing.json into lib/pricing.generated.json (gitignored).
// Runs before dev, build and test. Writes {"missing": true} when the file does not exist yet, so
// the app shows "[PRICE]" instead of failing.
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const web = join(dirname(fileURLToPath(import.meta.url)), "..");
const src = join(web, "..", "..", "costs", "pricing.json");
const out = join(web, "lib", "pricing.generated.json");

let data = { missing: true };
if (existsSync(src)) {
  try {
    const parsed = JSON.parse(readFileSync(src, "utf8"));
    data = { missing: false, ...parsed };
  } catch (e) {
    console.warn(`pricing: ${src} is not valid JSON (${e.message}); using [PRICE]`);
  }
}
writeFileSync(out, JSON.stringify(data, null, 2) + "\n");
console.log(`pricing: ${data.missing ? "missing, using [PRICE]" : `${Object.keys(data.products ?? {}).length} products`}`);
