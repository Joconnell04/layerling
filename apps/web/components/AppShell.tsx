"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";
import { resetDataClient } from "@/lib/data";
import { useJobs, useSession } from "@/lib/hooks";
import { LayerlingLogo } from "./brand/LayerlingMark";
import { Button, Popover, cx } from "./ui";

const NAV = [
  { href: "/jobs", label: "Jobs" },
  { href: "/jobs/new", label: "New job" },
  { href: "/review", label: "Review" },
  { href: "/filaments", label: "Filaments" },
  { href: "/admin/orders", label: "Orders" },
  { href: "/admin/traces", label: "Traces" },
] as const;

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname() ?? "/";
  const router = useRouter();
  const { user, client } = useSession();
  const isLogin = pathname === "/login";

  // Auth guard (the proxy already blocks studio pages without a session): Supabase mode needs a
  // user; access-code sessions come through the server adapter; mock mode never shows login.
  useEffect(() => {
    if (!client || user === undefined) return;
    if (client.mode !== "supabase" && isLogin) router.replace("/jobs");
    if (client.mode === "supabase" && !user && !isLogin) router.replace("/access");
    if (client.mode === "supabase" && user && isLogin) router.replace("/jobs");
    if (client.mode === "server" && !user) router.replace("/access");
  }, [client, user, isLogin, router]);

  const signOut = async () => {
    if (!client) return;
    await client.signOut();
    resetDataClient();
    window.location.assign("/");
  };

  const ready = client && user !== undefined && (client.mode === "mock" || user || isLogin);

  return (
    <div className="flex min-h-dvh flex-col">
      {!isLogin && (
        <header className="sticky top-0 z-30 border-b border-line bg-bg/90 backdrop-blur">
          <div className="mx-auto flex h-14 max-w-[1440px] items-center gap-6 px-6">
            <Link href="/jobs" className="flex shrink-0 items-center hover:text-fg" aria-label="layerling studio">
              <LayerlingLogo size={30} />
            </Link>
            <nav className="flex min-w-0 items-center gap-0.5 overflow-x-auto">
              {NAV.map((n) => (
                <NavItem key={n.href} {...n} active={isActive(pathname, n.href)} />
              ))}
            </nav>
            <div className="ml-auto flex items-center gap-1">
              {client && client.mode !== "mock" && user && (
                <Popover
                  trigger={({ toggle }) => (
                    <Button variant="ghost" size="sm" onClick={toggle} aria-label="Account">
                      {user.email ?? "Studio"}
                    </Button>
                  )}
                >
                  {(close) => (
                    <Button
                      variant="ghost"
                      className="w-full justify-start"
                      onClick={() => {
                        close();
                        void signOut();
                      }}
                    >
                      Sign out
                    </Button>
                  )}
                </Popover>
              )}
            </div>
          </div>
        </header>
      )}
      <main className="mx-auto w-full max-w-[1440px] flex-1 px-6 py-6">{ready ? children : null}</main>
    </div>
  );
}

function isActive(pathname: string, href: string): boolean {
  if (href === "/jobs") return pathname === "/jobs" || (pathname.startsWith("/jobs/") && pathname !== "/jobs/new");
  return pathname === href || pathname.startsWith(`${href}/`);
}

function NavItem({ href, label, active }: { href: string; label: string; active: boolean }) {
  const review = href === "/review";
  return (
    <Link
      href={href}
      className={cx(
        "inline-flex h-8 items-center gap-2 rounded-full px-3.5 text-sm transition-colors",
        active ? "bg-ink text-bg hover:text-bg" : "text-fg hover:bg-hover",
      )}
    >
      {label}
      {review && <ReviewCount />}
    </Link>
  );
}

function ReviewCount() {
  const { jobs } = useJobs();
  const n = (jobs ?? []).filter((j) => j.status === "review").length;
  if (!n) return null;
  return (
    <span className="num rounded-full bg-accent px-1.5 text-2xs font-semibold text-accent-fg">{n}</span>
  );
}
