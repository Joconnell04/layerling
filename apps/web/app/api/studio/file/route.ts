import { studioRoute } from "@/lib/server/http";
import type { Bucket } from "@/lib/server/store";
import { HttpError } from "@/lib/server/studio";

export const runtime = "nodejs";

/**
 * Redirect to a signed Storage URL (1 h). Job files: ?bucket=artifacts|inputs&job=<id>&path=<rel>.
 * Swatches: ?bucket=swatches&key=<owner>/<filament>.<ext>.
 */
export const GET = studioRoute("studio.file", async (req, ctx) => {
  const q = new URL(req.url).searchParams;
  const bucket = q.get("bucket") as Bucket;
  let key: string;
  if (bucket === "artifacts" || bucket === "inputs") {
    const job = q.get("job") ?? "";
    const path = q.get("path") ?? "";
    if (!/^[0-9a-f-]{36}$/i.test(job) || !path || path.includes("..") || path.startsWith("/")) {
      throw new HttpError(400, "Bad path");
    }
    key = `${job}/${path}`;
  } else if (bucket === "swatches") {
    key = q.get("key") ?? "";
    if (!/^[\w-]+\/[\w.-]+$/.test(key) || key.includes("..")) throw new HttpError(400, "Bad key");
  } else {
    throw new HttpError(400, "Bad bucket");
  }
  const url = await ctx.store.signedUrl(bucket, key);
  return new Response(null, { status: 302, headers: { location: url, "cache-control": "private, max-age=300" } });
});
