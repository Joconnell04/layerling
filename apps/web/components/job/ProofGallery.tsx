"use client";

// Proof from every side: the approved concept next to the product from the eight contract
// PROOF_VIEWS, one kind at a time (the simulated print, the paint, the 3D model). Views come from
// the worker's proof renders, else the page renders the GLB itself (glbViews.ts). A view nothing
// rendered keeps its tile, so a missing side is visible rather than skipped.

import { useEffect, useState } from "react";
import type { ConceptCandidate } from "@/lib/contracts.gen";
import { PROOF_KIND_LABELS, PROOF_LABELS, PROOF_ORDER, type ProofKind, type ProofSet, type ProofView } from "@/lib/fidelity";
import { useArtifactUrl } from "@/lib/hooks";
import { ArtifactImage } from "../ArtifactImage";
import { Segmented, cx } from "../ui";
import { renderGlbViews, type GlbViews } from "./glbViews";

export function ProofGallery({
  jobId,
  sets,
  kind,
  onKind,
  concept,
  photo,
  controls,
}: {
  jobId: string;
  sets: ProofSet[];
  kind: ProofKind;
  onKind: (k: ProofKind) => void;
  /** The approved concept: what the customer said yes to. */
  concept: ConceptCandidate | null;
  /** Shown in the concept's place when no concept was approved (jobs from before s03 checks). */
  photo?: string | null;
  /** Extra controls for the header row (the Views and 3D switch). */
  controls?: React.ReactNode;
}) {
  const set = sets.find((s) => s.kind === kind) ?? sets[0];
  const rendered = useGlbSet(jobId, set?.source === "glb" ? set.glb : null);
  const images = (v: ProofView): { path?: string; url?: string } =>
    set?.source === "glb" ? { url: rendered.views?.[v] } : { path: set?.images[v] };
  const have = PROOF_ORDER.filter((v) => (set?.source === "glb" ? !!rendered.views?.[v] : !!set?.images[v])).length;
  const busy = set?.source === "glb" && rendered.state === "loading";

  return (
    <div className="@container" data-testid="proof-gallery" data-kind={set?.kind} data-source={set?.source}>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {controls}
        {sets.length > 1 && (
          <Segmented<ProofKind>
            label="Proof"
            value={set.kind}
            onChange={onKind}
            options={sets.map((s) => ({ value: s.kind, label: PROOF_KIND_LABELS[s.kind] }))}
          />
        )}
        {sets.length === 1 && (
          <span className="inline-flex h-9 items-center rounded-full border border-line-strong px-4 text-sm font-medium">
            {PROOF_KIND_LABELS[set.kind]}
          </span>
        )}
        <span className="num ml-auto text-sm text-muted" data-testid="proof-count">
          {busy ? "Rendering" : `${have} of ${PROOF_ORDER.length} views`}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-3 @min-[720px]:grid-cols-4">
        <figure className="col-span-2 row-span-2 flex min-w-0 flex-col" data-testid="proof-concept">
          <ArtifactImage
            jobId={jobId}
            path={concept?.image ?? photo}
            bucket={concept ? "artifacts" : "inputs"}
            alt={concept ? "Approved concept" : "Photo"}
            fit="contain"
            className="aspect-square w-full rounded-lg border-2 border-ink bg-canvas"
          />
          <figcaption className="pt-1.5 text-sm font-semibold">{concept ? "Concept" : "Photo"}</figcaption>
        </figure>
        {PROOF_ORDER.map((v) => (
          <ViewTile key={v} jobId={jobId} view={v} {...images(v)} busy={busy} />
        ))}
      </div>
    </div>
  );
}

function ViewTile({ jobId, view, path, url, busy }: { jobId: string; view: ProofView; path?: string; url?: string; busy: boolean }) {
  const label = PROOF_LABELS[view];
  const missing = !path && !url;
  return (
    <figure className="flex min-w-0 flex-col" data-testid={`proof-${view}`} data-missing={missing || undefined}>
      {path ? (
        <ArtifactImage jobId={jobId} path={path} alt={label} fit="contain" className="aspect-square w-full rounded-lg border border-line bg-canvas" />
      ) : url ? (
        <div className="aspect-square w-full overflow-hidden rounded-lg border border-line bg-canvas">
          {/* A data URL rendered in the page. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={url} alt={label} className="size-full object-contain" />
        </div>
      ) : (
        <div
          className={cx(
            "flex aspect-square w-full items-center justify-center rounded-lg border border-dashed border-line-strong bg-panel text-sm text-faint",
            busy && "animate-pulse bg-raised",
          )}
        >
          {busy ? "" : "No render"}
        </div>
      )}
      <figcaption className="pt-1.5 text-sm text-muted">{label}</figcaption>
    </figure>
  );
}

/** The eight views of a GLB rendered in the page; null while loading or without WebGL. */
function useGlbSet(jobId: string, glb: string | null | undefined): { state: "idle" | "loading" | "ready" | "failed"; views: GlbViews | null } {
  const url = useArtifactUrl(jobId, glb);
  const [out, setOut] = useState<{ url: string | null; views: GlbViews | null; done: boolean }>({ url: null, views: null, done: false });
  useEffect(() => {
    if (!url) return;
    let live = true;
    renderGlbViews(url).then((views) => live && setOut({ url, views, done: true }));
    return () => {
      live = false;
    };
  }, [url]);
  if (!glb) return { state: "idle", views: null };
  if (!url || out.url !== url || !out.done) return { state: "loading", views: null };
  return { state: out.views ? "ready" : "failed", views: out.views };
}
