import { studioRoute } from "@/lib/server/http";
import { HttpError } from "@/lib/server/studio";
import { SIGNED_URL_TTL_S, UUID, getTraceSource, type TraceBucket } from "@/lib/server/traces";

export const runtime = "nodejs";

/**
 * A job file: 302 to a 5 minute signed Storage URL (the fixture file in mock mode).
 * ?bucket=inputs|artifacts&path=<job-relative path>, or path=storage:<bucket>/<job_id>/<path>.
 * The signed URL never appears in JSON or page text.
 */
export const GET = studioRoute<{ id: string }>("admin.traces.file", async (req, ctx) => {
  const id = ctx.params.id;
  const q = new URL(req.url).searchParams;
  let bucket = q.get("bucket") ?? "";
  let p = q.get("path") ?? "";
  if (!UUID.test(id)) throw new HttpError(400, "Bad job id");
  const ref = /^storage:(inputs|artifacts)\/([0-9a-f-]{36})\/(.+)$/i.exec(p);
  if (ref) {
    if (ref[2].toLowerCase() !== id.toLowerCase()) throw new HttpError(400, "Bad path");
    bucket = ref[1];
    p = ref[3];
  }
  if (bucket !== "inputs" && bucket !== "artifacts") throw new HttpError(400, "Bad bucket");
  if (!p || p.includes("..") || p.startsWith("/") || p.includes("\\") || p.length > 300) {
    throw new HttpError(400, "Bad path");
  }
  const hit = await getTraceSource().file(id, bucket as TraceBucket, p);
  if (!hit) throw new HttpError(404, "File not found");
  if ("redirect" in hit) {
    return new Response(null, {
      status: 302,
      headers: { location: hit.redirect, "cache-control": `private, max-age=${SIGNED_URL_TTL_S - 60}` },
    });
  }
  return new Response(hit.body as BodyInit, {
    headers: { "content-type": hit.type, "cache-control": "private, max-age=300" },
  });
});
