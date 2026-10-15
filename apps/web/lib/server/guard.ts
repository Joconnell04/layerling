// Who is calling a studio route: an access-code session (signed cookie), a Supabase user (auth
// cookies from @supabase/ssr), or mock mode during local development.

import { createServerClient, parseCookieHeader } from "@supabase/ssr";
import { ACCESS_COOKIE, readCookie, verifyAccess } from "./accessCookie";
import { serverEnv } from "./env";

export type Actor =
  | { kind: "mock"; userId: null }
  | { kind: "code"; userId: null }
  | { kind: "user"; userId: string; email: string | null };

export async function supabaseUser(cookieHeader: string | null): Promise<{ id: string; email: string | null } | null> {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anon = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !anon || !cookieHeader || !/sb-[^=]+-auth-token/.test(cookieHeader)) return null;
  const sb = createServerClient(url, anon, {
    cookies: {
      getAll: () => parseCookieHeader(cookieHeader).map((c) => ({ name: c.name, value: c.value ?? "" })),
      setAll: () => {},
    },
  });
  const { data, error } = await sb.auth.getUser();
  if (error || !data.user) return null;
  return { id: data.user.id, email: data.user.email ?? null };
}

export async function getActor(req: Request): Promise<Actor | null> {
  const env = serverEnv();
  if (env.mock) return { kind: "mock", userId: null };
  const v = await verifyAccess(readCookie(req, ACCESS_COOKIE), env.cookieSecret);
  if (v.ok) return { kind: "code", userId: null };
  const user = await supabaseUser(req.headers.get("cookie"));
  if (user) return { kind: "user", userId: user.id, email: user.email };
  return null;
}

/** owner_id and access for rows created by this actor. */
export function ownership(actor: Actor): { owner_id: string | null; access: "owner" | "code" } {
  return actor.kind === "user" ? { owner_id: actor.userId, access: "owner" } : { owner_id: null, access: "code" };
}
