// Copy the server keys the web app needs from the repo root .env into apps/web/.env.local.
// Prints key names only, never values. Run: node scripts/sync-env.mjs
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const web = join(dirname(fileURLToPath(import.meta.url)), "..");
const rootEnv = join(web, "..", "..", ".env");
const local = join(web, ".env.local");

const KEYS = [
  "SUPABASE_URL",
  "SUPABASE_SERVICE_ROLE_KEY",
  "ACCESS_CODE",
  "ACCESS_COOKIE_SECRET",
  "ORDER_NOTIFY_EMAIL",
  "AGENTMAIL_API_KEY",
  "AGENTMAIL_INBOX_ID",
];

function parse(text) {
  const out = new Map();
  for (const line of text.split("\n")) {
    const m = /^([A-Z0-9_]+)=(.*)$/.exec(line.trim());
    if (!m) continue;
    const value = m[2].replace(/\s+#.*$/, "").trim().replace(/^["']|["']$/g, "");
    out.set(m[1], value);
  }
  return out;
}

// Worktrees live under <repo>/.claude/worktrees/<name>/; fall back to the main checkout's .env.
const candidates = [rootEnv, join(web, "..", "..", "..", "..", "..", ".env")];
const src = candidates.find((p) => existsSync(p));
if (!src) {
  console.error("no root .env found");
  process.exit(1);
}
const root = parse(readFileSync(src, "utf8"));
const lines = existsSync(local) ? readFileSync(local, "utf8").split("\n") : [];
const kept = lines.filter((l) => !KEYS.some((k) => l.startsWith(`${k}=`)));
const added = [];
for (const k of KEYS) {
  const v = root.get(k);
  if (v) {
    kept.push(`${k}=${v}`);
    added.push(k);
  }
}
writeFileSync(local, kept.filter((l, i, a) => l !== "" || i < a.length - 1).join("\n") + "\n");
console.log(`copied: ${added.join(", ") || "none"}`);
console.log(`missing: ${KEYS.filter((k) => !added.includes(k)).join(", ") || "none"}`);
