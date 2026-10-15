// Redaction for everything the trace API returns: a port of the worker's rules
// (util.manifest_safe, logs.Redactor, trace.redact), applied to every string, recursively.
// Old manifests (the Bronco run) still hold signed Meshy URLs and a Supabase ?token= URL; this
// removes them on read.

const SAFE_DATA = /^data:[\w.+/-]+;sha256=[0-9a-f]{16};len=\d+$/;
const SIGNED_STORAGE = /^https?:\/\/[^/]+\/storage\/v1\/object\/(?:sign|authenticated|public)\/([^/?]+)\/([^?]+)/;

const PATTERNS: [RegExp, string][] = [
  [/data:[\w.+/-]+;base64,[A-Za-z0-9+/=_-]+/g, "data:<redacted>"],
  [/(https?:\/\/[^\s"'<>?]+)\?[^\s"'<>]+/g, "$1?<redacted>"],
  [/bearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer <redacted>"],
  [/eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}/g, "<jwt>"],
  [/\b(?:msy|sk-ant|sb_secret|sb_publishable)[_-][A-Za-z0-9_-]{6,}/g, "<key>"],
  [/\b(token|apikey|api_key|signature|key)=[^\s&"']+/gi, "$1=<redacted>"],
  // worker trace.redact extras: a JWT cut short, and `token=` left by the rule above
  [/eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]*){0,2}/g, "<jwt>"],
  [/\b(token|signature|expires|key-pair-id)=[^\s&"'<>]*/gi, "$1:<redacted>"],
];
const LONG_B64 = /^[A-Za-z0-9+/=_-]{2000,}$/;
const SECRET_KEY = /^(authorization|apikey|api_key|x-api-key|cookie|set-cookie|password|service_role_key)$/i;

function decode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/** util.manifest_safe for one string. */
export function manifestSafe(s: string): string {
  if (SAFE_DATA.test(s)) return s;
  if (s.startsWith("data:")) {
    const comma = s.indexOf(",");
    const mime = (comma < 0 ? s : s.slice(0, comma)).split(";")[0];
    return `${mime};redacted;len=${comma < 0 ? 0 : s.length - comma - 1}`;
  }
  if (/^https?:\/\//i.test(s)) {
    const m = SIGNED_STORAGE.exec(s);
    if (m) return `storage:${m[1]}/${decode(m[2])}`;
    return s.split("?")[0];
  }
  return s;
}

export function redactText(input: string, secrets: readonly string[] = []): string {
  let s = manifestSafe(input);
  for (const secret of secrets) {
    if (secret && secret.length >= 8 && s.includes(secret)) s = s.split(secret).join("<secret>");
  }
  for (const [re, repl] of PATTERNS) s = s.replace(re, repl);
  if (s.length >= 2000 && LONG_B64.test(s.trim())) return `<base64 ${s.length} chars>`;
  return s;
}

/** A copy of any JSON value with no keys, tokens, JWTs, signed URLs, data URIs or secrets. */
export function redact<T>(value: T, secrets: readonly string[] = []): T {
  const walk = (v: unknown, depth: number): unknown => {
    if (typeof v === "string") return redactText(v, secrets);
    if (v == null || typeof v !== "object") return v;
    if (depth > 40) return "[depth]";
    if (Array.isArray(v)) return v.map((x) => walk(x, depth + 1));
    const out: Record<string, unknown> = {};
    for (const [k, x] of Object.entries(v as Record<string, unknown>)) {
      out[k] = SECRET_KEY.test(k) ? "[redacted]" : walk(x, depth + 1);
    }
    return out;
  };
  return walk(value, 0) as T;
}

/** What the security checks grep for; a response that matches must never leave the server. */
export const SECRET_PATTERN = /Signature=|Expires=|token=|eyJ[A-Za-z0-9_-]{10,}|msy_|sk-ant|sb_secret/;
