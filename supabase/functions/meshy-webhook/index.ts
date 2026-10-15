// Meshy webhook receiver. Meshy POSTs the full task object on every status change to every
// enabled webhook URL. Meshy does not sign payloads, so the URL carries a secret token
// (?token=...) and we compare its SHA-256 here. The worker confirms each event with one GET of
// the task before acting on it.
import { createClient } from "npm:@supabase/supabase-js@2";

// sha256 of MESHY_WEBHOOK_TOKEN, set as a function secret:
//   supabase secrets set MESHY_WEBHOOK_TOKEN_SHA256=$(printf %s "$MESHY_WEBHOOK_TOKEN" | shasum -a 256 | cut -d' ' -f1)
// The token itself lives only in .env and the Meshy dashboard.
const TOKEN_SHA256 = Deno.env.get("MESHY_WEBHOOK_TOKEN_SHA256") ?? "";

async function sha256Hex(s: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

Deno.serve(async (req: Request) => {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });
  const token = new URL(req.url).searchParams.get("token") ?? "";
  if (!TOKEN_SHA256 || (await sha256Hex(token)) !== TOKEN_SHA256) return new Response("forbidden", { status: 403 });

  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return new Response("bad json", { status: 400 });
  }
  const taskId = String(body?.id ?? body?.task_id ?? "unknown");
  const status = typeof body?.status === "string" ? body.status : null;

  const sb = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    { auth: { persistSession: false } },
  );
  const { error } = await sb.from("meshy_events").insert({ task_id: taskId, status, event: body });
  if (error) return new Response("store failed", { status: 500 });
  return new Response("ok", { status: 200 });
});
