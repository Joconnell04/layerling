// Route wrappers: logging and request id (route()), HttpError to JSON, and the studio guard.

import { getActor, type Actor } from "./guard";
import { jsonError, route, type RouteCtx, type Sink } from "./log";
import { HttpError } from "./studio";
import { StoreUnavailable, getStore, type ServerStore } from "./store";

export interface StudioCtx<P> extends RouteCtx<P> {
  actor: Actor;
  store: ServerStore;
}

function toResponse(e: unknown, ctx: RouteCtx<unknown>): Response {
  if (e instanceof StoreUnavailable) {
    ctx.log.error("store unavailable");
    return jsonError(503, e.message, { request_id: ctx.reqId });
  }
  if (e instanceof HttpError) {
    ctx.log.warn("rejected", { status: e.status, error: e.message });
    return jsonError(e.status, e.message, { ...(e.details ?? {}), request_id: ctx.reqId });
  }
  throw e;
}

export function publicRoute<P = Record<string, string>>(
  name: string,
  handler: (req: Request, ctx: RouteCtx<P> & { store: ServerStore }) => Promise<Response>,
  sink?: Sink,
) {
  return route<P>(
    name,
    async (req, ctx) => {
      try {
        const store = getStore();
        return await handler(req, { ...ctx, store });
      } catch (e) {
        return toResponse(e, ctx as RouteCtx<unknown>);
      }
    },
    sink,
  );
}

/** Studio routes: valid access cookie, Supabase session, or mock mode in development. */
export function studioRoute<P = Record<string, string>>(
  name: string,
  handler: (req: Request, ctx: StudioCtx<P>) => Promise<Response>,
  sink?: Sink,
) {
  return route<P>(
    name,
    async (req, ctx) => {
      const actor = await getActor(req);
      if (!actor) {
        ctx.log.warn("unauthorized");
        return jsonError(401, "unauthorized", { request_id: ctx.reqId });
      }
      try {
        const store = getStore();
        return await handler(req, { ...ctx, actor, store });
      } catch (e) {
        return toResponse(e, ctx as RouteCtx<unknown>);
      }
    },
    sink,
  );
}

export async function readJson<T>(req: Request): Promise<T> {
  try {
    return (await req.json()) as T;
  } catch {
    throw new HttpError(400, "Bad JSON");
  }
}

export async function readForm(req: Request): Promise<FormData> {
  try {
    return await req.formData();
  } catch {
    throw new HttpError(400, "Bad form data");
  }
}
