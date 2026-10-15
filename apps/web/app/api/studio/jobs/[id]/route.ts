import { studioRoute } from "@/lib/server/http";

export const runtime = "nodejs";

/** One job with its stage runs, messages and spend: what the job page polls every 2 s. */
export const GET = studioRoute<{ id: string }>("studio.jobs.get", async (_req, ctx) => {
  const id = ctx.params.id;
  const [job, runs, messages, spend] = await Promise.all([
    ctx.store.getJob(id),
    ctx.store.listStageRuns(id),
    ctx.store.listMessages(id),
    ctx.store.listSpend(id),
  ]);
  return Response.json({ job, runs, messages, spend }, { headers: { "cache-control": "no-store" } });
});
