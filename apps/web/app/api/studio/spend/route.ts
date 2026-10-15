import { studioRoute } from "@/lib/server/http";

export const runtime = "nodejs";

/** Spend ledger, optionally for one job. ?job=<id> */
export const GET = studioRoute("studio.spend", async (req, ctx) => {
  const job = new URL(req.url).searchParams.get("job") ?? undefined;
  const spend = await ctx.store.listSpend(job);
  return Response.json({ spend }, { headers: { "cache-control": "no-store" } });
});
