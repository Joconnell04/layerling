import {
  ACCESS_COOKIE,
  accessCookieOptions,
  codeMatches,
  readCookie,
  secretUsable,
  signAccess,
  verifyAccess,
} from "@/lib/server/accessCookie";
import { hasServiceStore, serverEnv } from "@/lib/server/env";
import { supabaseUser } from "@/lib/server/guard";
import { jsonError, route } from "@/lib/server/log";
import { limiters } from "@/lib/server/rateLimit";

export const runtime = "nodejs";

/** Session status for the browser: which data adapter to use. Never exposes the code. */
export const GET = route("access.status", async (req) => {
  const env = serverEnv();
  const code = (await verifyAccess(readCookie(req, ACCESS_COOKIE), env.cookieSecret)).ok;
  const user = code ? null : await supabaseUser(req.headers.get("cookie"));
  return Response.json(
    { access: env.mock || code || !!user, via: env.mock ? "mock" : code ? "code" : user ? "user" : null, server: hasServiceStore(env) },
    { headers: { "cache-control": "no-store" } },
  );
});

/** Exchange the access code for a signed, httpOnly session cookie. Wrong codes are rate-limited. */
export const POST = route("access.login", async (req, ctx) => {
  const env = serverEnv();
  if (!env.accessCode || !secretUsable(env.cookieSecret)) {
    ctx.log.error("access not configured");
    return jsonError(503, "Access not configured");
  }
  const gate = limiters.access.peek(ctx.ip);
  if (!gate.ok) {
    ctx.log.warn("access rate limited", { retry_after_s: gate.retryAfterS });
    return jsonError(429, "Too many attempts", { retry_after_s: gate.retryAfterS });
  }
  const body = (await req.json().catch(() => null)) as { code?: unknown } | null;
  const code = typeof body?.code === "string" ? body.code.slice(0, 200) : "";
  if (!(await codeMatches(code, env.accessCode))) {
    const r = limiters.access.hit(ctx.ip);
    ctx.log.warn("access denied", { remaining: r.remaining });
    return jsonError(401, "Wrong code", { remaining: r.remaining });
  }
  limiters.access.reset(ctx.ip);
  const value = await signAccess(env.cookieSecret);
  const o = accessCookieOptions(req);
  const cookie = [
    `${ACCESS_COOKIE}=${value}`,
    `Path=${o.path}`,
    `Max-Age=${o.maxAge}`,
    "HttpOnly",
    "SameSite=Lax",
    o.secure ? "Secure" : "",
  ]
    .filter(Boolean)
    .join("; ");
  ctx.log.info("access granted");
  return Response.json({ ok: true }, { headers: { "set-cookie": cookie, "cache-control": "no-store" } });
});

/** Sign out of an access-code session. */
export const DELETE = route("access.logout", async (req) => {
  const o = accessCookieOptions(req, 0);
  const cookie = [`${ACCESS_COOKIE}=`, "Path=/", "Max-Age=0", "HttpOnly", "SameSite=Lax", o.secure ? "Secure" : ""]
    .filter(Boolean)
    .join("; ");
  return Response.json({ ok: true }, { headers: { "set-cookie": cookie } });
});
