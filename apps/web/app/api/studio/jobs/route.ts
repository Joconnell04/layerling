import { studioRoute } from "@/lib/server/http";

export const runtime = "nodejs";

/** Jobs, newest first. Access-code sessions see every job (service role). */
export const GET = studioRoute("studio.jobs.list", async (_req, ctx) => {
  const jobs = await ctx.store.listJobs();
  return Response.json({ jobs }, { headers: { "cache-control": "no-store" } });
});
