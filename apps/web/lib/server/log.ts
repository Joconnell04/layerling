// Structured logs: one JSON object per line, with a request id. Secret-looking fields are redacted
// and binary values are replaced by their size, so photo bytes and keys never reach the log.

export type Fields = Record<string, unknown>;
export type Level = "debug" | "info" | "warn" | "error";

export interface Logger {
  debug(msg: string, fields?: Fields): void;
  info(msg: string, fields?: Fields): void;
  warn(msg: string, fields?: Fields): void;
  error(msg: string, fields?: Fields): void;
  child(fields: Fields): Logger;
}

const SECRET = /(secret|password|passwd|token|api[_-]?key|service[_-]?role|authorization|cookie|access[_-]?code|^code$|signature)/i;

export function redact(value: unknown, key = "", depth = 0): unknown {
  if (key && SECRET.test(key)) return "[redacted]";
  if (value == null) return value;
  if (typeof value === "string") return value.length > 500 ? `${value.slice(0, 500)}…` : value;
  if (typeof value !== "object") return value;
  if (value instanceof Error) return { name: value.name, message: value.message };
  if (typeof Blob !== "undefined" && value instanceof Blob) return { bytes: value.size, type: value.type };
  if (ArrayBuffer.isView(value)) return { bytes: value.byteLength };
  if (value instanceof ArrayBuffer) return { bytes: value.byteLength };
  if (depth > 4) return "[depth]";
  if (Array.isArray(value)) return value.slice(0, 50).map((x) => redact(x, "", depth + 1));
  const out: Fields = {};
  for (const [k, x] of Object.entries(value as Fields)) out[k] = redact(x, k, depth + 1);
  return out;
}

export type Sink = (line: string) => void;
// Quiet under vitest; set LOG_IN_TESTS=1 to see lines.
const defaultSink: Sink = (line) => {
  if (process.env.VITEST && !process.env.LOG_IN_TESTS) return;
  console.log(line);
};

export function createLogger(base: Fields = {}, sink: Sink = defaultSink): Logger {
  const emit = (level: Level, msg: string, fields?: Fields) => {
    const rec = { t: new Date().toISOString(), level, msg, ...(redact(base) as Fields), ...(redact(fields ?? {}) as Fields) };
    try {
      sink(JSON.stringify(rec));
    } catch {
      sink(JSON.stringify({ t: rec.t, level, msg, log_error: "unserializable fields" }));
    }
  };
  return {
    debug: (m, f) => emit("debug", m, f),
    info: (m, f) => emit("info", m, f),
    warn: (m, f) => emit("warn", m, f),
    error: (m, f) => emit("error", m, f),
    child: (f) => createLogger({ ...base, ...f }, sink),
  };
}

export function requestId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : Math.random().toString(16).slice(2) + Date.now().toString(16);
}

/** Client IP from proxy headers (Vercel sets x-forwarded-for). */
export function clientIp(req: Request): string {
  const fwd = req.headers.get("x-forwarded-for");
  if (fwd) return fwd.split(",")[0].trim();
  return req.headers.get("x-real-ip") ?? "unknown";
}

/** Short, non-reversible IP tag for logs. */
export async function ipTag(ip: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`p2p:${ip}`));
  return [...new Uint8Array(d).slice(0, 6)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export interface RouteCtx<P = Record<string, string>> {
  reqId: string;
  log: Logger;
  ip: string;
  params: P;
}

type RouteParams<P> = { params: Promise<P> };

/**
 * Wrap a route handler: request id, start and finish log lines with status and duration,
 * x-request-id header, and a JSON 500 for uncaught errors.
 */
export function route<P = Record<string, string>>(
  name: string,
  handler: (req: Request, ctx: RouteCtx<P>) => Promise<Response>,
  sink?: Sink,
) {
  return async (req: Request, routeParams?: RouteParams<P>): Promise<Response> => {
    const reqId = req.headers.get("x-request-id")?.slice(0, 64) || requestId();
    const ip = clientIp(req);
    const log = createLogger({ route: name, req_id: reqId }, sink);
    const started = Date.now();
    const url = new URL(req.url);
    log.info("request", { method: req.method, path: url.pathname, ip: await ipTag(ip) });
    let res: Response;
    try {
      const params = (routeParams ? await routeParams.params : {}) as P;
      res = await handler(req, { reqId, log, ip, params });
    } catch (e) {
      log.error("unhandled", { error: e });
      res = Response.json({ error: "internal", request_id: reqId }, { status: 500 });
    }
    const headers = new Headers(res.headers);
    headers.set("x-request-id", reqId);
    const out = new Response(res.body, { status: res.status, statusText: res.statusText, headers });
    log.info("response", { status: res.status, ms: Date.now() - started });
    return out;
  };
}

export function jsonError(status: number, error: string, extra: Fields = {}): Response {
  return Response.json({ error, ...extra }, { status });
}
