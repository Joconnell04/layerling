// Picks the adapter:
//   mock      NEXT_PUBLIC_DATA=mock, or nothing configured
//   supabase  a Supabase auth session exists (Jackson's login, RLS)
//   server    an access-code session: /api/studio routes with the service role, polling
// The server reports the session at GET /api/access; the code itself never reaches the browser.

import type { DataClient, DataMode } from "./types";

export type { DataMode };

export function dataMode(env: Record<string, string | undefined> = publicEnv()): "mock" | "live" {
  return env.NEXT_PUBLIC_DATA === "mock" ? "mock" : "live";
}

// Next inlines NEXT_PUBLIC_* only for literal property access.
function publicEnv(): Record<string, string | undefined> {
  return {
    NEXT_PUBLIC_DATA: process.env.NEXT_PUBLIC_DATA,
    NEXT_PUBLIC_SUPABASE_URL: process.env.NEXT_PUBLIC_SUPABASE_URL,
    NEXT_PUBLIC_SUPABASE_ANON_KEY: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY,
  };
}

export interface SessionStatus {
  access: boolean;
  via: "mock" | "code" | "user" | null;
  server: boolean;
}

export async function sessionStatus(fetchImpl: typeof fetch = fetch): Promise<SessionStatus | null> {
  try {
    const res = await fetchImpl("/api/access", { cache: "no-store", credentials: "same-origin" });
    return res.ok ? ((await res.json()) as SessionStatus) : null;
  } catch {
    return null;
  }
}

/** Decide the adapter from build-time env and the server's view of this browser's session. */
export function chooseMode(
  env: Record<string, string | undefined>,
  status: SessionStatus | null,
  hasSupabaseSession: boolean,
): DataMode {
  if (env.NEXT_PUBLIC_DATA === "mock") return "mock";
  const supabase = !!env.NEXT_PUBLIC_SUPABASE_URL && !!env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (supabase && hasSupabaseSession) return "supabase";
  if (status?.via === "code") return "server";
  if (status?.via === "mock") return "mock";
  if (supabase) return "supabase";
  if (status?.server) return "server";
  return "mock";
}

let client: Promise<DataClient> | null = null;

/** Browser-only singleton. Adapters load lazily so each build only ships what it uses. */
export function getDataClient(): Promise<DataClient> {
  if (!client) client = createClient();
  return client;
}

/** Drop the cached adapter (after sign in or out). */
export function resetDataClient(): void {
  client = null;
}

async function createClient(): Promise<DataClient> {
  const env = publicEnv();
  if (env.NEXT_PUBLIC_DATA === "mock") {
    const m = await import("./mock/client");
    return new m.MockDataClient();
  }
  let supabaseClient: DataClient | null = null;
  let hasSession = false;
  if (env.NEXT_PUBLIC_SUPABASE_URL && env.NEXT_PUBLIC_SUPABASE_ANON_KEY) {
    const s = await import("./supabase");
    supabaseClient = s.SupabaseDataClient.fromEnv(env.NEXT_PUBLIC_SUPABASE_URL, env.NEXT_PUBLIC_SUPABASE_ANON_KEY);
    hasSession = !!(await supabaseClient.getUser());
  }
  const status = hasSession ? null : await sessionStatus();
  const mode = chooseMode(env, status, hasSession);
  if (mode === "supabase" && supabaseClient) return supabaseClient;
  if (mode === "server") {
    const s = await import("./server");
    return new s.ServerDataClient();
  }
  const m = await import("./mock/client");
  return new m.MockDataClient();
}

export type { DataClient } from "./types";
