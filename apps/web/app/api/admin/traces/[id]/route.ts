import { studioRoute } from "@/lib/server/http";
import { HttpError } from "@/lib/server/studio";
import { UUID, getTraceSource, traceJson } from "@/lib/server/traces";
import { jobTrace } from "@/lib/traces/derive";

export const runtime = "nodejs";

/** One job's trace: request, attempts with calls, outputs, decisions (manifests reduced). */
export const GET = studioRoute<{ id: string }>("admin.traces.job", async (_req, ctx) => {
  const id = ctx.params.id;
  if (!UUID.test(id)) throw new HttpError(400, "Bad job id");
  const found = await getTraceSource().job(id);
  if (!found) throw new HttpError(404, "Job not found");
  return traceJson(jobTrace(found.job, found.runs));
});
