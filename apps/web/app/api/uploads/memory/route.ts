import { jsonError } from "@/lib/server/log";
import { publicRoute } from "@/lib/server/http";
import { MemoryServerStore, type Bucket } from "@/lib/server/store";

export const runtime = "nodejs";

const BUCKETS: Bucket[] = ["orders", "inputs", "swatches"];

/** Upload target for the in-memory store (mock mode and tests only; 404 with a real store). */
export const PUT = publicRoute("uploads.memory", async (req, ctx) => {
  if (!(ctx.store instanceof MemoryServerStore)) return jsonError(404, "not found");
  const url = new URL(req.url);
  const bucket = url.searchParams.get("bucket") as Bucket;
  const key = url.searchParams.get("key") ?? "";
  if (!BUCKETS.includes(bucket) || !/^[\w-]+(\/[\w.-]+)+$/.test(key)) return jsonError(400, "bad key");
  const blob = await req.blob();
  if (blob.size > 15 * 1024 * 1024) return jsonError(413, "too large");
  await ctx.store.upload(bucket, key, blob, req.headers.get("content-type") ?? "application/octet-stream");
  ctx.log.info("memory upload", { bucket, bytes: blob.size });
  return new Response(null, { status: 200 });
});
