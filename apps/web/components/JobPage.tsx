"use client";

// Job page: a large 3D viewer with a small image strip on the left, the one decision waiting on
// you (or the result, its colors and the download) on the right. During the concept pick the
// candidates take the viewer's place so likeness can be judged.

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import type { JobRow } from "@/lib/db.types";
import { costView, deriveGates, type Gates } from "@/lib/derive";
import { ago } from "@/lib/format";
import { useArtifactJson, useFilaments, useJob } from "@/lib/hooks";
import {
  dimensions,
  jobWarnings,
  outputPath,
  type GeometryInfo,
  type PrintabilityInfo,
  type Warning,
} from "@/lib/manifestInfo";
import { jobAsks, jobForm } from "@/lib/fidelity";
import { formIsChoice, formLabel } from "@/lib/intent";
import { editBusy, editStatus } from "@/lib/paintEdit";
import { paintInfo } from "@/lib/paintInfo";
import { spoolSuggestions } from "@/lib/spool";
import { ADDON_LABELS, PRODUCT_LABELS, nameTextOf } from "@/lib/products";
import { ConceptBand } from "./job/ConceptBand";
import { DecisionRail } from "./job/DecisionRail";
import { ModelStage } from "./job/ModelStage";
import { PaintPanel } from "./job/PaintPanel";
import { PaintPreview } from "./job/PaintPreview";
import { MessageLog } from "./MessageLog";
import type { ModelInfo, ViewMode } from "./ModelViewer";
import { QuestionGate } from "./QuestionGate";
import { JOB_STATUS } from "./status";
import { Empty, Tip, cx } from "./ui";

const GENERIC = /^(me|person|subject|dog|cat|rabbit|bird|horse|pet|object)$/i;

/** The name the page talks about: name text, else a named subject, else a name in the prompt. */
export function subjectName(job: JobRow, gates: Gates): string | null {
  const text = nameTextOf(job.addons);
  if (text) return text.length > 1 && text === text.toUpperCase() ? text.charAt(0) + text.slice(1).toLowerCase() : text;
  const subject = gates.questions?.message.payload.subjects.find((s) => !GENERIC.test(s.name));
  if (subject) return subject.name;
  const named = /\b(?:named|called)\s+([A-Z][a-z]+)|\b(?:(?:my|our) \w+ )([A-Z][a-z]+)\b|\b([A-Z][a-z]+) the\b|\bof ([A-Z][a-z]+)\b/.exec(job.prompt);
  return named?.[1] ?? named?.[2] ?? named?.[3] ?? named?.[4] ?? null;
}

export function JobPage({ jobId }: { jobId: string }) {
  const { job, runs, messages, spend, loading, notFound, error } = useJob(jobId);
  const { filaments } = useFilaments();
  const [mode, setMode] = useState<ViewMode>("texture");
  const [modeTouched, setModeTouched] = useState(false);
  const [painted, setPainted] = useState<boolean | null>(null);
  const [base, setBase] = useState<string | null>(null);

  const gates = useMemo(() => deriveGates(messages), [messages]);
  const cost = useMemo(() => (job ? costView(job, spend) : null), [job, spend]);
  const result = job?.result ?? gates.result?.payload ?? null;
  const previewGlb = result?.preview_glb ?? null;

  const geometry = useArtifactJson<GeometryInfo>(job?.id, outputPath(runs, "geometry"));
  const fileWarnings = useArtifactJson<unknown>(job?.id, outputPath(runs, "geometry_warnings"));
  const printability = useArtifactJson<PrintabilityInfo>(job?.id, outputPath(runs, "printability"));
  const warnings = useMemo(
    () => jobWarnings({ runs, files: fileWarnings, geometry, printability }),
    [fileWarnings, geometry, printability, runs],
  );
  const suggestions = useMemo(() => spoolSuggestions(messages), [messages]);
  const paint = useMemo(() => paintInfo(runs, messages), [runs, messages]);
  const paintBusy = useMemo(() => editBusy(editStatus(messages, runs)), [messages, runs]);

  // A new preview GLB: assume painted until the viewer says otherwise; filament view by default.
  useEffect(() => setPainted(null), [previewGlb]);
  useEffect(() => {
    if (!modeTouched && previewGlb) setMode("filament");
  }, [previewGlb, modeTouched]);

  const onModelInfo = (i: ModelInfo) => {
    if (i.mode !== "filament") return;
    setPainted(i.hasColors);
    if (!i.hasColors) setMode("texture");
  };

  if (loading) return <Empty>Loading</Empty>;
  if (error) return <Empty>{error}</Empty>;
  if (notFound || !job || !cost) {
    return (
      <Empty>
        Not found.{" "}
        <Link href="/jobs" className="text-accent underline">
          Jobs
        </Link>
      </Empty>
    );
  }

  const name = subjectName(job, gates);
  const title = name ?? job.title ?? job.prompt;
  const mesh = gates.mesh?.latest.payload;
  const hasModel = !!(mesh || previewGlb);
  // The concept band shows while picking, while a round renders, and while s03 runs before any round.
  const conceptMode =
    !hasModel && (gates.active === "concepts" || !!gates.concepts?.pending || (job.stage === "s03_concept" && !gates.concepts));
  const filamentUsable = !!previewGlb && painted !== false;
  const dims = dimensions(geometry, job.target_size_mm);

  const form = formIsChoice(job.product_type) ? jobForm(job, gates) : null;
  const asks = jobAsks(job, gates);
  const meta = [
    PRODUCT_LABELS[job.product_type],
    job.target_size_mm != null ? `${job.target_size_mm} mm` : null,
    form ? formLabel(form, job.product_type) : null,
    ...job.addons.filter((a) => !a.text?.text).map((a) => ADDON_LABELS[a.kind]?.toLowerCase()),
    job.profile === "full" ? "full pipeline" : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const header = (
    <header className="flex min-w-0 flex-col gap-2">
      <h1 className="font-display truncate text-[40px] font-bold leading-none tracking-[-0.025em]">{title}</h1>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted">
        <StatusWord job={job} />
        <span data-testid="job-meta">{meta}</span>
        {nameTextOf(job.addons) && (
          <span data-testid="name-text" className="rounded-full border border-line-strong px-2 text-xs font-semibold tracking-wide text-fg">
            {nameTextOf(job.addons)}
          </span>
        )}
        {asks
          .filter((a) => a.kind !== "text" && a.buildable !== false)
          .map((a) => (
            <span key={`${a.kind}-${a.text}`} data-testid="job-ask" className="rounded-full border border-line-strong px-2 text-xs text-fg">
              {a.text}
            </span>
          ))}
        <Tip label={new Date(job.created_at).toLocaleString()}>
          <span className="num">{ago(job.created_at)}</span>
        </Tip>
      </div>
    </header>
  );

  const banner = (job.status === "paused" || job.status === "failed") && (
    <div role="status" className={cx("rounded-lg border px-4 py-2.5 text-sm font-semibold", job.status === "paused" ? "border-pause text-pause" : "border-bad text-bad")}>
      {job.status === "paused" ? "Paused" : "Failed"}: {job.status === "paused" ? job.pause_reason : job.review_reason}
    </div>
  );

  const main = conceptMode ? (
    <ConceptBand job={job} gates={gates} base={base} onBase={setBase} />
  ) : (
    <ModelStage
      job={job}
      gates={gates}
      runs={runs}
      mode={filamentUsable ? mode : "texture"}
      onMode={(m) => {
        setMode(m);
        setModeTouched(true);
      }}
      dims={dims}
      painted={painted !== false}
      onModelInfo={onModelInfo}
      messages={messages}
      filaments={filaments ?? []}
    />
  );

  return (
    <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_400px]">
      <section className="flex min-w-0 flex-col gap-5">
        {header}
        {banner}
        {main}
        {!conceptMode && <PaintPreview jobId={job.id} runs={runs} messages={messages} filaments={filaments ?? []} />}
        <WarningsList warnings={warnings} />
        {paint && <PaintPanel jobId={job.id} info={paint} />}
        {gates.questions && gates.active !== "questions" && (
          <QuestionGate key={gates.questions.message.id} jobId={job.id} gate={gates.questions} />
        )}
        <MessageLog messages={messages} />
      </section>
      <aside className="lg:sticky lg:top-20 lg:self-start">
        <div className="flex min-h-[min(calc(100dvh-7rem),760px)] flex-col rounded-2xl border border-line bg-panel p-6">
          <DecisionRail
            job={job}
            gates={gates}
            name={name}
            filaments={filaments ?? []}
            cost={cost}
            spend={spend}
            base={base}
            suggestions={suggestions}
            messages={messages}
            paintBusy={paintBusy}
          />
        </div>
      </aside>
    </div>
  );
}

/** Printability and geometry warnings from the manifests, behind a disclosure. */
export function WarningsList({ warnings }: { warnings: Warning[] }) {
  if (!warnings.length) return null;
  return (
    <details className="rounded-xl border border-pause/40 bg-panel px-4 py-3" data-testid="warnings">
      <summary className="cursor-pointer text-sm font-semibold text-pause">Warnings ({warnings.length})</summary>
      <ul className="mt-2 flex list-disc flex-col gap-1 pl-5 text-sm">
        {warnings.map((w, i) => (
          <li key={i}>{w.text}</li>
        ))}
      </ul>
    </details>
  );
}

function StatusWord({ job }: { job: JobRow }) {
  const s = JOB_STATUS[job.status] ?? JOB_STATUS.queued;
  const tone = { accent: "text-accent", ok: "text-ok", run: "text-run", review: "text-review", bad: "text-bad", pause: "text-pause", faint: "text-muted" }[s.tone];
  return <span className={cx("font-semibold", tone)}>{s.label}</span>;
}
