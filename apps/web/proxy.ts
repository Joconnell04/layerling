// Studio gate (Next 16 proxy, Node runtime). Studio pages need a valid access-code cookie or a
// Supabase session; otherwise redirect to /access. API routes check the same things themselves.

import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";
import { ACCESS_COOKIE, verifyAccess } from "@/lib/server/accessCookie";

export async function proxy(req: NextRequest) {
  if (process.env.NEXT_PUBLIC_DATA === "mock" && process.env.NODE_ENV !== "production") {
    return NextResponse.next();
  }
  const code = await verifyAccess(req.cookies.get(ACCESS_COOKIE)?.value, process.env.ACCESS_COOKIE_SECRET);
  if (code.ok) return NextResponse.next();

  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anon = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (url && anon && req.cookies.getAll().some((c) => /^sb-.+-auth-token/.test(c.name))) {
    let res = NextResponse.next({ request: req });
    const sb = createServerClient(url, anon, {
      cookies: {
        getAll: () => req.cookies.getAll(),
        setAll: (list) => {
          for (const { name, value } of list) req.cookies.set(name, value);
          res = NextResponse.next({ request: req });
          for (const { name, value, options } of list) res.cookies.set(name, value, options);
        },
      },
    });
    const { data } = await sb.auth.getUser();
    if (data.user) return res;
  }

  const to = req.nextUrl.clone();
  to.pathname = "/access";
  to.search = `?next=${encodeURIComponent(req.nextUrl.pathname + req.nextUrl.search)}`;
  return NextResponse.redirect(to);
}

export const config = {
  matcher: ["/jobs/:path*", "/review/:path*", "/filaments/:path*", "/admin/:path*"],
};
