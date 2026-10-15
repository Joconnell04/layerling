// Server-only environment. Read on every call so tests and Vercel env changes apply. Never log
// values; describeEnv() reports presence only.

export interface ServerEnv {
  supabaseUrl: string | null;
  serviceRoleKey: string | null;
  accessCode: string | null;
  cookieSecret: string | null;
  notifyEmail: string | null;
  agentmailKey: string | null;
  agentmailInbox: string | null;
  /** First look: off unless FIRST_LOOK_ENABLED is 1 or true and MESHY_API_KEY is set. */
  firstLookEnabled: boolean;
  firstLookDailyCap: number;
  meshyKey: string | null;
  /** "live" only after Jackson approves spend; anything else is test mode. */
  meshyMode: string;
  /** In-memory store and no auth gate: NEXT_PUBLIC_DATA=mock outside production. */
  mock: boolean;
}

const v = (k: string): string | null => {
  const x = process.env[k];
  return x && x.trim() ? x.trim() : null;
};

export function serverEnv(): ServerEnv {
  return {
    supabaseUrl: v("SUPABASE_URL") ?? v("NEXT_PUBLIC_SUPABASE_URL"),
    serviceRoleKey: v("SUPABASE_SERVICE_ROLE_KEY"),
    accessCode: v("ACCESS_CODE"),
    cookieSecret: v("ACCESS_COOKIE_SECRET"),
    notifyEmail: v("ORDER_NOTIFY_EMAIL"),
    agentmailKey: v("AGENTMAIL_API_KEY"),
    agentmailInbox: v("AGENTMAIL_INBOX_ID"),
    firstLookEnabled: /^(1|true)$/i.test(v("FIRST_LOOK_ENABLED") ?? ""),
    firstLookDailyCap: Math.max(0, Math.floor(Number(v("FIRST_LOOK_DAILY_CAP") ?? 5)) || 0),
    meshyKey: v("MESHY_API_KEY"),
    meshyMode: v("MESHY_MODE") ?? "test",
    mock: process.env.NEXT_PUBLIC_DATA === "mock" && process.env.NODE_ENV !== "production",
  };
}

/** Whether the server can reach Supabase with the service role. */
export function hasServiceStore(env: ServerEnv = serverEnv()): boolean {
  return !env.mock && !!env.supabaseUrl && !!env.serviceRoleKey;
}

export function describeEnv(env: ServerEnv = serverEnv()): Record<string, boolean> {
  return {
    supabase: !!env.supabaseUrl,
    service_role: !!env.serviceRoleKey,
    access_code: !!env.accessCode,
    cookie_secret: !!env.cookieSecret,
    notify_email: !!env.notifyEmail,
    agentmail: !!env.agentmailKey && !!env.agentmailInbox,
    first_look: env.firstLookEnabled,
    meshy: !!env.meshyKey,
    mock: env.mock,
  };
}
