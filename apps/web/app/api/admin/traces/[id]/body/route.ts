import { studioRoute } from "@/lib/server/http";
import { HttpError } from "@/lib/server/studio";
import { BODY_PATH, UUID, getTraceSource, traceJson } from "@/lib/server/traces";

export const runtime = "nodejs";

/** One trace body, redacted: ?path=s03_concept/a1/trace/01-meshy.json. */
export const GET = studioRoute<{ id: string }>("admin.traces.body", async (req, ctx) => {
  const id = ctx.params.id;
  const p = new URL(req.url).searchParams.get("path") ?? "";
  if (!UUID.test(id)) throw new HttpError(400, "Bad job id");
  if (!BODY_PATH.test(p) || p.includes("..")) throw new HttpError(400, "Bad path");
  const body = await getTraceSource().body(id, p);
  if (body == null) throw new HttpError(404, "Trace body not found");
  return traceJson({ body });
});
