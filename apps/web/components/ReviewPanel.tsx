"use client";

import { useState } from "react";
import type { MessageRow } from "@/lib/db.types";
import { pct } from "@/lib/format";
import { useArtifactUrl, useData } from "@/lib/hooks";
import { ArtifactImage } from "./ArtifactImage";
import { Button, ErrorLine, Input, Panel, Tag } from "./ui";

type Action = "approve" | "rerun_paint" | "reject";

export function ReviewPanel({
  jobId,
  request,
  decision,
  compact,
  title,
  bare,
}: {
  jobId: string;
  request: MessageRow<"review_request">;
  decision: MessageRow<"review_decision"> | null;
  compact?: boolean;
  title?: React.ReactNode;
  /** Decision rail: stacked pills, no panel frame. */
  bare?: boolean;
}) {
  const client = useData();
  const p = request.payload;
  const checklistUrl = useArtifactUrl(jobId, p.checklist);
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState<Action | null>(null);
  const [error, setError] = useState<string | null>(null);
  const open = !decision;

  const decide = async (action: Action) => {
    if (!client || busy) return;
    setBusy(action);
    setError(null);
    try {
      await client.sendMessage({
        jobId,
        kind: "review_decision",
        payload: { action, notes: notes.trim() || null },
        replyTo: request.id,
        stage: request.stage,
      });
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  };

  if (bare) {
    return (
      <div className="flex flex-col gap-3.5">
        <p className="text-base text-fg">{p.reason}</p>
        {p.confidence != null && (
          <p className="num text-sm text-muted">
            Confidence <span className="font-semibold text-fg">{pct(p.confidence)}</span>
            {checklistUrl && (
              <a href={checklistUrl} target="_blank" rel="noreferrer" className="ml-3 underline">
                Checklist
              </a>
            )}
          </p>
        )}
        {open ? (
          <>
            <button
              type="button"
              disabled={!!busy}
              onClick={() => void decide("approve")}
              className="h-[54px] rounded-full bg-accent text-[17px] font-semibold text-accent-fg hover:brightness-110 disabled:opacity-50"
            >
              Approve
            </button>
            <div className="grid grid-cols-2 gap-2.5">
              <Button className="h-[46px] text-[15px]" disabled={!!busy} onClick={() => void decide("rerun_paint")}>
                Rerun paint
              </Button>
              <Button variant="danger" className="h-[46px] text-[15px]" disabled={!!busy} onClick={() => void decide("reject")}>
                Reject
              </Button>
            </div>
            <label htmlFor="review-notes" className="text-sm font-semibold">
              Notes
            </label>
            <textarea
              id="review-notes"
              aria-label="Notes"
              rows={2}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              className="resize-none rounded-[10px] border border-line-strong bg-panel px-3.5 py-3 text-[15px] focus:border-ink focus:outline-none"
            />
          </>
        ) : (
          <p className="text-sm font-semibold">{decision?.payload.action.replace("_", " ")}</p>
        )}
        <ErrorLine>{error}</ErrorLine>
      </div>
    );
  }

  return (
    <Panel
      id="review"
      active={open}
      title={title ?? "Review"}
      right={
        <>
          {p.confidence != null && (
            <Tag className={p.confidence < 0.7 ? "text-pause" : "text-ok"}>
              <span className="num">Confidence {pct(p.confidence)}</span>
            </Tag>
          )}
          {checklistUrl && (
            <a href={checklistUrl} target="_blank" rel="noreferrer">
              <Button size="sm" variant="ghost" tabIndex={-1}>
                Checklist
              </Button>
            </a>
          )}
          {decision && <Tag className="text-fg">{decision.payload.action.replace("_", " ")}</Tag>}
        </>
      }
    >
      <div className="space-y-3">
        <p className="text-sm text-fg">{p.reason}</p>
        {p.images && p.images.length > 0 && (
          <div className={compact ? "grid grid-cols-4 gap-2" : "grid grid-cols-2 gap-2"}>
            {p.images.map((img) => (
              <ArtifactImage
                key={img}
                jobId={jobId}
                path={img}
                alt={img.split("/").pop() ?? img}
                fit="contain"
                className="aspect-[3/2] rounded-md border border-line"
              />
            ))}
          </div>
        )}
        {open && (
          <div className="flex flex-wrap items-center gap-2">
            <Input aria-label="Notes" placeholder="Notes" value={notes} onChange={(e) => setNotes(e.target.value)} className="w-64 flex-1" />
            <Button variant="danger" disabled={!!busy} onClick={() => void decide("reject")}>
              Reject
            </Button>
            <Button disabled={!!busy} onClick={() => void decide("rerun_paint")}>
              Rerun paint
            </Button>
            <Button variant="primary" disabled={!!busy} onClick={() => void decide("approve")}>
              Approve
            </Button>
          </div>
        )}
        {decision?.payload.notes && <p className="text-xs text-muted">{decision.payload.notes}</p>}
        <ErrorLine>{error}</ErrorLine>
      </div>
    </Panel>
  );
}
