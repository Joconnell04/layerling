import { MESSAGE_ROLES } from "@/lib/contracts";
import type { MessagePayloadMap } from "@/lib/contracts.gen";
import { studioRoute } from "@/lib/server/http";
import { HttpError } from "@/lib/server/studio";

export const runtime = "nodejs";

/** Messages of one kind across jobs (the review queue). ?kind=review_request&jobs=a,b */
export const GET = studioRoute("studio.messages.byKind", async (req, ctx) => {
  const url = new URL(req.url);
  const kind = url.searchParams.get("kind") ?? "";
  if (!(kind in MESSAGE_ROLES) || kind === "prompt") throw new HttpError(400, "Bad kind");
  const jobs = (url.searchParams.get("jobs") ?? "").split(",").filter(Boolean).slice(0, 100);
  const messages = await ctx.store.listMessagesByKind(jobs, kind as keyof MessagePayloadMap);
  return Response.json({ messages }, { headers: { "cache-control": "no-store" } });
});
