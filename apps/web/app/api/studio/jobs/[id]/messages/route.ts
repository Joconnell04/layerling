import { readJson, studioRoute } from "@/lib/server/http";
import { sendUserMessage, type MessageBody } from "@/lib/server/studio";

export const runtime = "nodejs";

/** Insert a user message (answers, concept_pick, tweak, mesh_decision, review_decision). */
export const POST = studioRoute<{ id: string }>("studio.messages.send", async (req, ctx) => {
  const body = await readJson<MessageBody>(req);
  const message = await sendUserMessage(ctx.store, ctx.actor, ctx.params.id, body);
  ctx.log.info("message sent", { job_id: ctx.params.id, kind: message.kind });
  return Response.json({ message });
});
