import { readJson, studioRoute } from "@/lib/server/http";
import { HttpError, insertJobRows, type JobTicket } from "@/lib/server/studio";
import { verifyTicket } from "@/lib/server/ticket";

export const runtime = "nodejs";

/** New job, step 2: check the uploaded photos, insert the job and its prompt message, queue it. */
export const POST = studioRoute("studio.jobs.finish", async (req, ctx) => {
  const body = await readJson<{ ticket?: unknown; sha256?: unknown }>(req);
  const t = await verifyTicket<JobTicket>("job", body.ticket);
  if (!t) throw new HttpError(400, "Invalid or expired ticket");
  const existing = await ctx.store.getJob(t.id);
  if (existing) return Response.json({ job: existing });
  const objects = await ctx.store.listObjects("inputs", `${t.id}/input`);
  const have = new Set(objects.map((o) => o.key));
  const missing = t.photos.filter((p) => !have.has(p.key));
  if (missing.length) throw new HttpError(400, "Photos not uploaded", { missing: missing.map((p) => p.path) });
  const sha = Array.isArray(body.sha256) ? body.sha256 : [];
  const photos = t.photos.map((p, i) => {
    const h = sha[i];
    return typeof h === "string" && /^[0-9a-f]{64}$/.test(h) ? { path: p.path, sha256: h } : { path: p.path };
  });
  const job = await insertJobRows(ctx.store, ctx.actor, t.id, t.fields, photos, ctx.log);
  return Response.json({ job });
});
