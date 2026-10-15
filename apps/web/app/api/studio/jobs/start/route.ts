import { readJson, studioRoute } from "@/lib/server/http";
import { jobPhotoMeta, parseJobFields, type JobTicket } from "@/lib/server/studio";
import { signTicket } from "@/lib/server/ticket";

export const runtime = "nodejs";

/** New job, step 1: check fields and photo metadata, return signed upload URLs into inputs. */
export const POST = studioRoute("studio.jobs.start", async (req, ctx) => {
  const body = await readJson<Record<string, unknown>>(req);
  const fields = parseJobFields((k) => body[k], body.addons);
  const meta = jobPhotoMeta(body.photos);
  const id = crypto.randomUUID();
  const photos = meta.map((m, i) => {
    const path = `input/photo_${i + 1}.${m.ext}`;
    return { path, key: `${id}/${path}`, size: m.size, type: m.type };
  });
  const uploads = await Promise.all(photos.map((p) => ctx.store.createUploadUrl("inputs", p.key, p.type)));
  const ticket = await signTicket<JobTicket>("job", {
    id,
    fields,
    photos: photos.map(({ path, key, size }) => ({ path, key, size })),
  });
  ctx.log.info("job upload started", { job_id: id, photos: photos.length });
  return Response.json({ job_id: id, ticket, uploads });
});
