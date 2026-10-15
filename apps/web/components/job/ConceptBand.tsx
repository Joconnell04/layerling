"use client";

// Concept pick: the original photo next to both candidates, large enough to judge likeness
// (each candidate at least 420 px wide on desktop). Each candidate is the product the customer
// will get (the figure with its loop, plate and name, or the flat charm), with the figure alone
// small beside its check results in a few words. Space is reserved and skeletons show while
// renders load or a new round is rendering, so there is never a blank state.
// Layout follows the band's own width (container queries), so it never scrolls sideways: photo,
// A and B in one row when there is room for 420 px candidates, else the photo on a short row
// above A and B, else one column.

import type { ConceptCandidate } from "@/lib/contracts";
import type { JobRow } from "@/lib/db.types";
import type { Gates } from "@/lib/derive";
import { checkChips, type CheckChip } from "@/lib/fidelity";
import { ArtifactImage } from "../ArtifactImage";
import { Tip, cx } from "../ui";

export function ConceptBand({
  job,
  gates,
  base,
  onBase,
}: {
  job: JobRow;
  gates: Gates;
  base: string | null;
  onBase: (id: string) => void;
}) {
  const c = gates.concepts;
  const cands: (ConceptCandidate | null)[] = c?.latest.payload.candidates ?? [null, null];
  const approved = c?.approved?.payload.candidate_id;
  const rendering = !c || c.pending;
  const selected = base ?? cands[0]?.id ?? null;
  const photo = job.photos[0]?.path;

  return (
    <div className="@container">
      <div
        data-testid="concept-band"
        className="grid grid-cols-1 gap-4 rounded-xl border border-line bg-panel p-4 @min-[600px]:grid-cols-2 @min-[1070px]:[grid-template-columns:minmax(160px,0.6fr)_minmax(420px,1fr)_minmax(420px,1fr)]"
        role="radiogroup"
        aria-label="Candidates"
      >
        <figure className="flex min-w-0 items-end gap-3 @min-[600px]:col-span-2 @min-[1070px]:col-span-1 @min-[1070px]:flex-col @min-[1070px]:items-stretch @min-[1070px]:gap-0">
          <ArtifactImage
            jobId={job.id}
            path={photo}
            bucket="inputs"
            alt="Original photo"
            className="aspect-[3/4] w-[108px] shrink-0 @min-[1070px]:w-full"
          />
          <figcaption className="pt-2 text-sm text-muted">Photo</figcaption>
        </figure>
        {cands.map((cand, i) => {
          const on = !!cand && selected === cand.id;
          const letter = String.fromCharCode(65 + i);
          return (
            <div
              key={cand?.id ?? `slot-${i}`}
              className={cx(
                "flex min-w-0 flex-col gap-2 rounded-lg border-2 p-2",
                cand && approved === cand.id ? "border-ok" : on && c?.open ? "border-ink" : "border-transparent",
              )}
            >
              <div className="flex min-h-7 min-w-0 items-start gap-2">
                <span className="w-4 shrink-0 pt-0.5 text-base font-semibold">{letter}</span>
                <CheckChips chips={checkChips(cand?.check)} />
              </div>
              <button
                type="button"
                role="radio"
                aria-checked={on}
                aria-label={cand ? `Use ${cand.id} as base` : `Candidate ${letter} rendering`}
                data-testid={cand ? `candidate-${cand.id}` : undefined}
                disabled={!cand || !c?.open}
                onClick={() => cand && onBase(cand.id)}
                className="relative block w-full text-left disabled:cursor-default"
              >
                {/* The product: what the customer gets. */}
                <ArtifactImage jobId={job.id} path={cand?.image} alt={cand ? `Concept ${cand.id}` : ""} fit="contain" className="aspect-square w-full rounded-md bg-canvas" />
                {/* The figure alone, small: the 3D input (a flat charm's source). */}
                {cand?.figure_image && cand.figure_image !== cand.image && (
                  <span className="absolute right-2 top-2 flex w-[24%] flex-col items-stretch" data-testid={`figure-${cand.id}`}>
                    <ArtifactImage
                      jobId={job.id}
                      path={cand.figure_image}
                      alt={`Figure ${cand.id}`}
                      fit="contain"
                      className="aspect-square w-full rounded-md border border-line-strong bg-panel"
                    />
                    <span className="pt-1 text-center text-xs font-medium text-muted">Figure</span>
                  </span>
                )}
                {rendering && (
                  <span className="absolute inset-0 flex items-center justify-center rounded-md bg-panel/60 text-sm font-semibold">
                    Rendering
                  </span>
                )}
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** The concept check in a few words per item; the full item and the model's note on hover. */
export function CheckChips({ chips }: { chips: CheckChip[] }) {
  if (!chips.length) return <span className="min-w-0 flex-1" />;
  return (
    <ul aria-label="Checks" className="flex min-w-0 flex-1 flex-wrap gap-1.5">
      {chips.map((c) => (
        <li key={c.id} data-testid={`check-${c.id}`} data-passed={c.passed}>
          <Tip label={c.detail || c.text}>
            <span
              tabIndex={0}
              className={cx(
                "inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-[13px] font-medium",
                c.passed ? "border-line-strong text-fg" : "border-bad/60 text-bad",
              )}
            >
              <span aria-hidden className={cx("size-1.5 rounded-full", c.passed ? "bg-ok" : "bg-bad")} />
              {c.text}
            </span>
          </Tip>
        </li>
      ))}
    </ul>
  );
}
