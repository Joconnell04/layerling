import { after } from "next/server";
import { completeOrder, scheduleAfter } from "@/lib/server/orderFlow";
import { publicRoute } from "@/lib/server/http";

export const runtime = "nodejs";
export const maxDuration = 30;

/** Public order form, step 2: check the uploaded photos, store the order, email after responding. */
export const POST = publicRoute("orders.finish", (req, ctx) => completeOrder(req, ctx, scheduleAfter(after)));
