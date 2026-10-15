import { studioRoute } from "@/lib/server/http";
import { HttpError } from "@/lib/server/studio";
import { getTraceSource, traceJson } from "@/lib/server/traces";
import { callRows, filterCalls, spendSummary } from "@/lib/traces/derive";

export const runtime = "nodejs";

/**
 * Every model call across jobs and the spend summary. ?days=7|30 (default all), ?vendor=
 * claude|meshy|runpod, ?q= (model, purpose or job). The summary covers the day range only.
 */
export const GET = studioRoute("admin.traces.calls", async (req, ctx) => {
  const q = new URL(req.url).searchParams;
  const daysRaw = q.get("days");
  const days = daysRaw && daysRaw !== "all" ? Number(daysRaw) : null;
  if (days != null && (!Number.isInteger(days) || days < 1 || days > 3650)) throw new HttpError(400, "Bad days");
  const vendor = q.get("vendor");
  if (vendor && !["all", "claude", "meshy", "runpod"].includes(vendor)) throw new HttpError(400, "Bad vendor");
  const { jobs, runs } = await getTraceSource().calls(days);
  const ranged = filterCalls(callRows(jobs, runs), { days });
  const calls = filterCalls(ranged, { vendor, q: q.get("q")?.slice(0, 100) });
  ctx.log.info("trace calls", { count: calls.length, days });
  return traceJson({ calls, summary: spendSummary(ranged) });
});
