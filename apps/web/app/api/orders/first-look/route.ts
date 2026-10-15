import { serverEnv } from "@/lib/server/env";
import { fakeMeshy, liveMeshy, pollFirstLook, startFirstLook, type MeshyImageApi } from "@/lib/server/firstLook";
import { publicRoute } from "@/lib/server/http";
import { jsonError } from "@/lib/server/log";
import type { MemoryServerStore, ServerStore } from "@/lib/server/store";

export const runtime = "nodejs";
export const maxDuration = 30;

// Tests set __p2pFirstLookMeshy through setFirstLookMeshy in lib/server/firstLook.
const g = globalThis as unknown as { __p2pFirstLookMeshy?: MeshyImageApi | null; __p2pFakeMeshy?: MeshyImageApi };

function meshyFor(req: Request, store: ServerStore): MeshyImageApi | null {
  if (g.__p2pFirstLookMeshy !== undefined) return g.__p2pFirstLookMeshy;
  const env = serverEnv();
  if (!env.firstLookEnabled) return null;
  // Mock mode never calls Meshy: the fake returns the sample concept after about 8 s.
  if (env.mock || store.kind === "memory") {
    const origin = new URL(req.url).origin;
    return (g.__p2pFakeMeshy ??= fakeMeshy(store as MemoryServerStore, `${origin}/samples/beagle-concept.png`, 8000));
  }
  return env.meshyKey ? liveMeshy(env.meshyKey) : null;
}

/** Start the one first look for an order: {receipt} in, a state out. */
export const POST = publicRoute("orders.first_look", async (req, ctx) => {
  const body = (await req.json().catch(() => null)) as { receipt?: unknown } | null;
  const state = await startFirstLook(body?.receipt, { store: ctx.store, meshy: meshyFor(req, ctx.store) });
  if (!state) return jsonError(400, "Invalid or expired receipt");
  ctx.log.info("first look", { state: state.state });
  return Response.json(state);
});

/** Poll a first look task: ?t=<task token>. Reads webhook rows only, never Meshy. */
export const GET = publicRoute("orders.first_look_poll", async (req, ctx) => {
  const state = await pollFirstLook(new URL(req.url).searchParams.get("t"), { store: ctx.store, meshy: null });
  if (!state) return jsonError(400, "Invalid or expired task");
  return Response.json(state);
});
