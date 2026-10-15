import type { StageId } from "@/lib/contracts.gen";
import { studioRoute } from "@/lib/server/http";
import { HttpError } from "@/lib/server/studio";
import { UUID, getTraceSource, traceJson } from "@/lib/server/traces";

export const runtime = "nodejs";

/** One full stage manifest, redacted: ?stage=s03_concept&attempt=1. */
export const GET = studioRoute<{ id: string }>("admin.traces.raw", async (req, ctx) => {
  const id = ctx.params.id;
  const q = new URL(req.url).searchParams;
  const stage = q.get("stage") ?? "";
  const attempt = Number(q.get("attempt"));
  if (!UUID.test(id)) throw new HttpError(400, "Bad job id");
  if (!/^s\d\d_[a-z0-9_]+$/.test(stage) || !Number.isInteger(attempt) || attempt < 1) {
    throw new HttpError(400, "Bad stage or attempt");
  }
  const manifest = await getTraceSource().manifest(id, stage as StageId, attempt);
  if (!manifest) throw new HttpError(404, "Manifest not found");
  return traceJson({ manifest });
});
