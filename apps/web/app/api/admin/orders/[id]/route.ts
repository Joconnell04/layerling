import { ORDER_STATUSES, type OrderStatus } from "@/lib/db.types";
import { readJson, studioRoute } from "@/lib/server/http";
import { HttpError } from "@/lib/server/studio";

export const runtime = "nodejs";

/** Change an order's status. */
export const PATCH = studioRoute<{ id: string }>("admin.orders.update", async (req, ctx) => {
  const body = await readJson<{ status?: string }>(req);
  if (!ORDER_STATUSES.includes(body.status as OrderStatus)) throw new HttpError(400, "Bad status");
  if (!(await ctx.store.getOrder(ctx.params.id))) throw new HttpError(404, "Order not found");
  const order = await ctx.store.updateOrder(ctx.params.id, { status: body.status as OrderStatus });
  ctx.log.info("order status", { order_id: order.id, status: order.status });
  return Response.json({ order });
});
