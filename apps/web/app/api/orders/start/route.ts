import { startOrder } from "@/lib/server/orderFlow";
import { publicRoute } from "@/lib/server/http";

export const runtime = "nodejs";

/** Public order form, step 1: validate, rate-limit by IP, hand out signed photo upload URLs. */
export const POST = publicRoute("orders.start", (req, ctx) => startOrder(req, ctx));
