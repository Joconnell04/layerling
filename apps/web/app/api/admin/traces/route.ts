import { studioRoute } from "@/lib/server/http";
import { getTraceSource, traceJson } from "@/lib/server/traces";
import { jobRows } from "@/lib/traces/derive";

export const runtime = "nodejs";

/** Trace job list: one row per job, newest first. */
export const GET = studioRoute("admin.traces.list", async (_req, ctx) => {
  const { jobs, runs } = await getTraceSource().jobs();
  const rows = jobRows(jobs, runs);
  ctx.log.info("trace jobs", { count: rows.length });
  return traceJson({ jobs: rows });
});
