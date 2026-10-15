import { studioRoute } from "@/lib/server/http";
import { startJobFromOrder } from "@/lib/server/studio";

export const runtime = "nodejs";
export const maxDuration = 60;

/** Start a job from an order: copy photos to inputs, set product, size and add-ons. */
export const POST = studioRoute<{ id: string }>("admin.orders.job", async (_req, ctx) => {
  const { job, order, created } = await startJobFromOrder(ctx.store, ctx.actor, ctx.params.id, ctx.log);
  return Response.json({ job_id: job.id, order, created });
});
