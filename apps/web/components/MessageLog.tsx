"use client";

import { useState } from "react";
import { STAGE_BY_ID } from "@/lib/contracts";
import type { AnyMessage } from "@/lib/db.types";
import { pct, titleCase } from "@/lib/format";
import { validatePayload } from "@/lib/validate";
import { Button, Panel, Tip, cx } from "./ui";

export function summarize(m: AnyMessage): string {
  switch (m.kind) {
    case "prompt":
      return m.body ?? "";
    case "questions":
      return `${m.payload.questions.length} questions, ${m.payload.subjects.length} subjects`;
    case "answers":
      return `${m.payload.answers.length} answers`;
    case "concepts":
      return `Round ${m.payload.round}, ${m.payload.candidates.length} candidates`;
    case "concept_pick":
      return `${m.payload.approve ? "Approve" : "Base"} ${m.payload.candidate_id}`;
    case "tweak":
      return m.payload.text;
    case "tweak_decision":
      return `${m.payload.route.replace(/_/g, " ")}: ${m.payload.instruction}`;
    case "mesh_review":
      return m.payload.glb.split("/").slice(0, 2).join("/");
    case "mesh_decision":
      return [titleCase(m.payload.action), m.payload.text].filter(Boolean).join(": ");
    case "result":
      return `${m.payload.threemf.split("/").pop()}${m.payload.slice_ok ? ", slices" : ""}`;
    case "review_request":
      return `${m.payload.reason}${m.payload.confidence != null ? ` (${pct(m.payload.confidence)})` : ""}`;
    case "review_decision":
      return titleCase(m.payload.action);
    case "note":
      return m.payload.text;
    case "resume": {
      const caps = [
        m.payload.cap_usd != null ? `cap $${m.payload.cap_usd}` : null,
        m.payload.cap_credits != null ? `cap ${m.payload.cap_credits} cr` : null,
      ].filter(Boolean);
      return ["Resume", ...caps, m.payload.note].filter(Boolean).join(", ");
    }
    case "spool_suggestion": {
      const p = m.payload;
      const fix = p.delta_e_with != null ? ` → ${p.delta_e_with.toFixed(1)}` : "";
      return `Add ${p.suggestion}, dE00 ${p.delta_e_now.toFixed(1)}${fix}`;
    }
    case "palette_edit": {
      const p = m.payload;
      const colors = p.slots?.length ? `${p.slots.length} colors` : "auto colors";
      const any = p.source === "any" ? "any spool" : null;
      return [p.mode === "recolor" ? "Recolor" : "Refit", colors, any, p.note]
        .filter(Boolean)
        .join(", ");
    }
    case "paint_edit": {
      const p = m.payload;
      const n = p.ops?.length ?? 0;
      const any = p.source === "any" ? "any spool" : null;
      return [n ? `Paint, ${n} ${n === 1 ? "edit" : "edits"}` : "Paint preview", p.base_attempt, any, p.note]
        .filter(Boolean)
        .join(", ");
    }
  }
}

export function MessageLog({ messages }: { messages: AnyMessage[] }) {
  const [all, setAll] = useState(false);
  const [open, setOpen] = useState(false);
  const shown = all ? messages : messages.slice(-10);
  return (
    <Panel
      title={
        <>
          Log <span className="num text-xs font-normal text-faint">{messages.length}</span>
        </>
      }
      collapsed={!open}
      onToggle={() => setOpen((o) => !o)}
      right={
        open &&
        messages.length > 10 && (
          <Button size="sm" variant="ghost" onClick={() => setAll((a) => !a)}>
            {all ? "Recent" : "All"}
          </Button>
        )
      }
      bodyClassName="p-0"
    >
      <ul className="divide-y divide-line/60">
        {shown.map((m) => {
          const valid = validatePayload(m.kind, m.payload);
          return (
            <li key={m.id} className="grid h-8 grid-cols-[44px_110px_minmax(0,1fr)_auto] items-center gap-2 px-3 text-xs">
              <span className="num text-faint">
                {new Date(m.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}
              </span>
              <span className={cx("truncate", m.role === "user" ? "text-accent" : "text-muted")}>{m.kind.replace(/_/g, " ")}</span>
              <span className="truncate text-fg">{summarize(m)}</span>
              <span className="flex items-center gap-2 text-faint">
                {m.stage && <span>{STAGE_BY_ID[m.stage]?.number}</span>}
                {!valid.ok && (
                  <Tip label={valid.errors.join("; ")}>
                    <span className="text-bad">Schema</span>
                  </Tip>
                )}
                {m.role === "user" && m.kind !== "prompt" && (
                  <Tip label={m.consumed_at ? "Worker acted" : "Waiting for worker"}>
                    <span className={m.consumed_at ? "text-ok" : "text-faint"}>{m.consumed_at ? "✓" : "…"}</span>
                  </Tip>
                )}
              </span>
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}
