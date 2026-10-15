import { readJson, studioRoute } from "@/lib/server/http";
import { photoType } from "@/lib/server/orders";
import { HttpError } from "@/lib/server/studio";

export const runtime = "nodejs";

const keyFor = (owner: string | null, id: string, ext: string) => `${owner ?? "code"}/${id}.${ext}`;

/** Swatch photo, step 1: signed upload URL in bucket swatches. Body {name, type, size}. */
export const POST = studioRoute<{ id: string }>("studio.swatch.start", async (req, ctx) => {
  const f = await ctx.store.getFilament(ctx.params.id);
  if (!f) throw new HttpError(404, "Filament not found");
  const meta = await readJson<{ name?: string; type?: string; size?: number }>(req);
  const t = photoType(meta);
  if (!t) throw new HttpError(400, "JPEG, PNG, HEIC or WebP");
  if (!(Number(meta.size) > 0) || Number(meta.size) > 15 * 1024 * 1024) throw new HttpError(400, "Under 15 MB");
  const owner = ctx.actor.kind === "user" ? ctx.actor.userId : null;
  const upload = await ctx.store.createUploadUrl("swatches", keyFor(owner, f.id, t.ext), t.type);
  return Response.json({ upload });
});

/** Swatch photo, step 2: point the filament at the uploaded object. Body {key}. */
export const PUT = studioRoute<{ id: string }>("studio.swatch.set", async (req, ctx) => {
  const f = await ctx.store.getFilament(ctx.params.id);
  if (!f) throw new HttpError(404, "Filament not found");
  const { key } = await readJson<{ key?: string }>(req);
  const owner = ctx.actor.kind === "user" ? ctx.actor.userId : null;
  if (typeof key !== "string" || !key.startsWith(`${owner ?? "code"}/${f.id}.`)) throw new HttpError(400, "Bad key");
  const folder = key.slice(0, key.lastIndexOf("/"));
  const objects = await ctx.store.listObjects("swatches", folder);
  if (!objects.some((o) => o.key === key)) throw new HttpError(400, "Swatch not uploaded");
  const filament = await ctx.store.upsertFilament({ ...f, swatch_path: key }, false);
  return Response.json({ filament });
});
