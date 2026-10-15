"use client";

// Stage 9 at a glance: iterations used, confidence, each checkpoint as the ideal paint beside the
// simulated print, and the sign-off checklist with pass or fail per item.

import type { Checklist } from "@/lib/contracts.gen";
import { pct } from "@/lib/format";
import { useArtifactJson } from "@/lib/hooks";
import { CHECKLIST_LABELS, type PaintInfo } from "@/lib/paintInfo";
import { ArtifactImage } from "../ArtifactImage";
import { cx } from "../ui";

/** Below this the job goes to Jackson's review queue (PLAN: low confidence routes to review). */
export const LOW_CONFIDENCE = 0.7;

export function PaintPanel({ jobId, info, showReason = true }: { jobId: string; info: PaintInfo; showReason?: boolean }) {
  const checklist = useArtifactJson<Checklist>(jobId, info.checklistPath);
  const confidence = info.confidence ?? checklist?.confidence ?? null;
  const low = confidence != null && confidence < LOW_CONFIDENCE;
  const items = checklist?.items ?? [];
  const passed = items.filter((i) => i.passed === true).length;
  const judged = items.filter((i) => i.passed != null).length;

  return (
    <section data-testid="paint-panel" aria-label="Paint" className="overflow-hidden rounded-xl border border-line bg-panel">
      <header className="flex items-center justify-between gap-4 border-b border-line px-[18px] py-2.5 text-sm text-muted">
        <span className="flex items-center gap-3">
          <span className="font-semibold text-fg">Paint</span>
          <span className="num" data-testid="paint-iterations">
            {info.iterations != null ? `Iteration ${info.iterations} of ${info.maxIterations}` : "Iterations unknown"}
          </span>
          {info.run.attempt > 1 && <span className="num">Attempt {info.run.attempt}</span>}
        </span>
        <span className="flex items-center gap-3">
          {checklist && (
            <span className="num" data-testid="paint-checklist-count">
              {passed} of {judged} pass
            </span>
          )}
          <span className={cx("num", low ? "text-bad" : confidence != null ? "text-ok" : undefined)} data-testid="paint-confidence">
            {confidence != null ? `Confidence ${pct(confidence)}` : "Confidence unknown"}
          </span>
        </span>
      </header>

      {showReason && info.reason && <p className="border-b border-line px-[18px] py-2 text-sm">{info.reason}</p>}

      {info.checkpoints.length > 0 && (
        <div className="flex gap-3 overflow-x-auto border-b border-line px-[18px] py-3" aria-label="Checkpoints">
          {info.checkpoints.map((c, i) => {
            const last = i === info.checkpoints.length - 1;
            return (
              <figure
                key={c.id}
                data-testid={`checkpoint-${c.id}`}
                className={cx("flex shrink-0 flex-col border", last ? "border-ink" : "border-line")}
              >
                <div className="grid grid-cols-2">
                  <Thumb jobId={jobId} path={c.before} label="Paint" />
                  <Thumb jobId={jobId} path={c.after} label="Print sim" border />
                </div>
                <figcaption className="flex max-w-[232px] gap-2 border-t border-line px-2 py-1 text-xs text-muted">
                  <span className={cx("num shrink-0", last && "text-fg")}>{String(c.iteration ?? i + 1).padStart(2, "0")}</span>
                  <span className="truncate normal-case tracking-normal" title={c.note ?? undefined}>
                    {c.note ?? ""}
                  </span>
                </figcaption>
              </figure>
            );
          })}
        </div>
      )}

      {items.length > 0 ? (
        <ul aria-label="Checklist" className="grid grid-cols-1 gap-x-6 px-[18px] py-3 md:grid-cols-2">
          {items.map((it) => (
            <li key={it.id} data-testid={`check-${it.id}`} data-passed={String(it.passed)} className="flex items-baseline gap-2.5 py-1 text-[14px]">
              <span
                className={cx(
                  "num w-9 shrink-0 text-xs font-semibold uppercase",
                  it.passed === true ? "text-ok" : it.passed === false ? "text-bad" : "text-muted",
                )}
              >
                {it.passed === true ? "Pass" : it.passed === false ? "Fail" : "n/a"}
              </span>
              <span className="min-w-0 flex-1">
                <span className="font-semibold">{CHECKLIST_LABELS[it.id] ?? it.id}</span>
                <span className="block truncate text-[13px] text-muted" title={it.evidence}>
                  {it.evidence}
                </span>
              </span>
              <span className="shrink-0 text-xs text-faint">{it.by}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="px-[18px] py-3 text-sm text-muted">{info.checklistPath ? "Loading checklist" : "No checklist yet"}</p>
      )}
    </section>
  );
}

function Thumb({ jobId, path, label, border }: { jobId: string; path: string | null; label: string; border?: boolean }) {
  return (
    <div className={cx("flex flex-col", border && "border-l border-line")}>
      <ArtifactImage jobId={jobId} path={path} alt={label} fit="contain" className="h-[96px] w-[116px] bg-transparent" />
      <span className="px-1.5 pb-1 text-[11px] text-muted">{label}</span>
    </div>
  );
}
