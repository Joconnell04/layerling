import type { FilamentInput } from "@/lib/data/types";
import { readJson, studioRoute } from "@/lib/server/http";
import { saveFilament } from "@/lib/server/studio";

export const runtime = "nodejs";

export const GET = studioRoute("studio.filaments.list", async (_req, ctx) => {
  const filaments = await ctx.store.listFilaments();
  return Response.json({ filaments }, { headers: { "cache-control": "no-store" } });
});

/** Add a spool (no id) or edit one (id). LAB is computed from hex server-side too. */
export const POST = studioRoute("studio.filaments.save", async (req, ctx) => {
  const input = await readJson<FilamentInput>(req);
  const filament = await saveFilament(ctx.store, ctx.actor, input);
  ctx.log.info("filament saved", { filament_id: filament.id });
  return Response.json({ filament });
});
