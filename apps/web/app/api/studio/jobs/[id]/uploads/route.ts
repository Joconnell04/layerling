import { PAINT_STROKE_PATH } from "@/lib/data/types";
import { readJson, studioRoute } from "@/lib/server/http";
import { HttpError } from "@/lib/server/studio";

export const runtime = "nodejs";

/** A large paint stroke for paint_edit faces_path: a signed upload URL into bucket inputs. */
export const POST = studioRoute<{ id: string }>("studio.jobs.upload", async (req, ctx) => {
  const body = await readJson<{ path?: string; size?: number }>(req);
  const path = String(body.path ?? "");
  if (!PAINT_STROKE_PATH.test(path)) throw new HttpError(400, "Path must be input/paint/<name>.txt");
  if (typeof body.size === "number" && body.size > 20 * 1024 * 1024) throw new HttpError(413, "Stroke file over 20 MB");
  const job = await ctx.store.getJob(ctx.params.id);
  if (!job) throw new HttpError(404, "Job not found");
  const upload = await ctx.store.createUploadUrl("inputs", `${job.id}/${path}`, "text/plain");
  ctx.log.info("stroke upload", { job_id: job.id, path });
  return Response.json({ upload });
});
