import { studioRoute } from "@/lib/server/http";

export const runtime = "nodejs";

/** Worker heartbeats. */
export const GET = studioRoute("studio.workers", async (_req, ctx) => {
  const workers = await ctx.store.listWorkers();
  return Response.json({ workers }, { headers: { "cache-control": "no-store" } });
});
