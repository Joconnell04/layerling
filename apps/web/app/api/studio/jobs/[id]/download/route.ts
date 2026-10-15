import type { MessageRow } from "@/lib/db.types";
import { downloadName } from "@/lib/download";
import { studioRoute } from "@/lib/server/http";
import { HttpError } from "@/lib/server/studio";

export const runtime = "nodejs";

const THREEMF = "application/vnd.ms-package.3dmanufacturing-3dmodel+xml";

/**
 * The job's 3MF as "<name>-<product>.3mf". With Supabase this redirects to a short signed URL
 * whose response carries Content-Disposition with that filename (Storage sets it from the
 * `download` parameter), so large files never pass through a function body (Vercel caps function
 * responses at 4.5 MB; the M1 3MF was 10.8 MB). The memory store streams the bytes directly.
 */
export const GET = studioRoute<{ id: string }>("studio.jobs.download", async (req, ctx) => {
  const job = await ctx.store.getJob(ctx.params.id);
  if (!job) throw new HttpError(404, "Job not found");
  // The worker posts the result message before it writes jobs.result; use the message meanwhile.
  let path = job.result?.threemf;
  if (!path) {
    const msgs = await ctx.store.listMessages(job.id);
    const last = msgs.filter((m) => m.kind === "result").at(-1) as MessageRow<"result"> | undefined;
    path = last?.payload.threemf;
  }
  if (!path) throw new HttpError(404, "No 3MF yet");
  const name = new URL(req.url).searchParams.get("name");
  const filename = downloadName(job, name);
  const key = `${job.id}/${path.replace(/^\/+/, "")}`;
  ctx.log.info("download", { job_id: job.id, filename, slice_ok: job.result?.slice_ok ?? null });

  const signed = await ctx.store.signedDownloadUrl("artifacts", key, filename);
  if (signed) {
    return new Response(null, { status: 302, headers: { location: signed, "cache-control": "private, no-store" } });
  }
  const blob = await ctx.store.download("artifacts", key);
  return new Response(blob.stream(), {
    headers: {
      "content-type": THREEMF,
      "content-length": String(blob.size),
      "content-disposition": `attachment; filename="${filename}"`,
      "cache-control": "private, no-store",
    },
  });
});
