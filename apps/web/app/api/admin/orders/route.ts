import { studioRoute } from "@/lib/server/http";

export const runtime = "nodejs";

/** Orders, newest first, with signed photo URLs (1 h). */
export const GET = studioRoute("admin.orders.list", async (_req, ctx) => {
  const orders = await ctx.store.listOrders();
  const withUrls = await Promise.all(
    orders.map(async (o) => ({
      ...o,
      photo_urls: await Promise.all(o.photos.map((k) => ctx.store.signedUrl("orders", k).catch(() => null))),
    })),
  );
  ctx.log.info("orders listed", { count: orders.length });
  return Response.json({ orders: withUrls }, { headers: { "cache-control": "no-store" } });
});
