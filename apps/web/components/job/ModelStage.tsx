"use client";

// The job page's main view. Views: the approved concept next to the product from all eight proof
// views (the 3D model while it waits for approval; the simulated print, the paint and the model
// once a result exists). 3D: a large viewer with a Print and Model toggle. Under both, the size as
// one line of text and a small strip of images (render sheet, approved concept, photos). Paint:
// the paint editor (PaintEditor), once a result exists.

import { useEffect, useMemo, useState } from "react";
import type { AnyMessage, FilamentRow, JobRow, StageRunRow } from "@/lib/db.types";
import type { Gates } from "@/lib/derive";
import { approvedCandidate, proofSets, type ProofKind } from "@/lib/fidelity";
import type { Dimensions } from "@/lib/manifestInfo";
import { ArtifactImage } from "../ArtifactImage";
import { ViewToggle, type ModelInfo, type ViewMode } from "../ModelViewer";
import { Segmented, cx } from "../ui";
import { Viewer } from "../Viewer";
import { PaintEditor } from "./PaintEditor";
import { ProofGallery } from "./ProofGallery";

type Display = "views" | "3d" | "paint";

interface StripItem {
  id: string;
  label: string;
  path: string;
  bucket: "artifacts" | "inputs";
}

/** "W 21.4 · H 45 · T 8 mm" once s06 measured the piece, else the target. */
export function sizeText(dims: Dimensions, plate: boolean): string {
  if (dims.heightMm != null && dims.widthMm != null) {
    return dims.flat && dims.reliefMm != null
      ? `W ${dims.widthMm} · H ${dims.heightMm} · T ${dims.reliefMm} mm`
      : `W ${dims.widthMm} · H ${dims.heightMm} mm`;
  }
  if (dims.targetMm != null) return plate ? `${dims.targetMm} mm plate target` : `${dims.targetMm} mm target`;
  return "";
}

export function ModelStage({
  job,
  gates,
  runs = [],
  mode,
  onMode,
  dims,
  painted = true,
  onModelInfo,
  messages = [],
  filaments = [],
}: {
  job: JobRow;
  gates: Gates;
  /** For the paint editor: its paint_edit messages and the worker's notes. */
  messages?: AnyMessage[];
  filaments?: FilamentRow[];
  /** Stage runs: proof renders are read from their outputs until the result carries them. */
  runs?: StageRunRow[];
  /** filament = the print, texture = Meshy's model. */
  mode: ViewMode;
  onMode: (m: ViewMode) => void;
  dims: Dimensions;
  /** False once the viewer found no filament colors in the preview GLB. */
  painted?: boolean;
  onModelInfo?: (i: ModelInfo) => void;
}) {
  const result = job.result ?? gates.result?.payload ?? null;
  const mesh = gates.mesh?.latest.payload ?? null;
  const preview = result?.preview_glb ?? null;
  const printPath = preview && painted ? preview : null;
  // Meshy's model; an unpainted preview stands in when there is no Meshy GLB.
  const modelPath = mesh?.glb ?? (preview && !painted ? preview : null);

  const approvedConcept = useMemo(() => approvedCandidate(gates), [gates]);
  const sets = useMemo(() => proofSets(job, runs, gates), [job, runs, gates]);
  const meshGate = gates.active === "mesh_review";
  // Proof from every side first, at the mesh gate and once a result exists.
  const [displayPick, setDisplayPick] = useState<Display | null>(null);
  // Paint: the paint editor, once a result exists (the job has something to repaint).
  const canPaint = !!result;
  const want = displayPick ?? (meshGate || result ? "views" : "3d");
  const display: Display = want === "paint" && canPaint ? "paint" : sets.length && want === "views" ? "views" : "3d";
  const [kindPick, setKindPick] = useState<ProofKind | null>(null);
  const kind: ProofKind = (kindPick && sets.some((x) => x.kind === kindPick) ? kindPick : sets[0]?.kind) ?? "model";

  const strip: StripItem[] = useMemo(() => {
    const list: StripItem[] = [];
    if (result?.render_sheet) list.push({ id: "sheet", label: "Sheet", path: result.render_sheet, bucket: "artifacts" });
    if (approvedConcept) list.push({ id: "concept", label: "Concept", path: approvedConcept.image, bucket: "artifacts" });
    job.photos.forEach((p, i) => list.push({ id: p.path, label: `Photo ${i + 1}`, path: p.path, bucket: "inputs" }));
    return list;
  }, [result?.render_sheet, approvedConcept, job.photos]);

  const has3d = !!(printPath || modelPath);
  // An image picked from the strip replaces the viewer until a 3D view is picked again.
  const [image, setImage] = useState<string | null>(null);
  const modelKey = `${mesh?.glb ?? ""}|${preview ?? ""}|${painted}`;
  useEffect(() => {
    // No painted preview: the render sheet shows what prints. Nothing in 3D: the first image.
    setImage(printPath ? null : result?.render_sheet ? "sheet" : has3d ? null : (strip[0]?.id ?? null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelKey]);

  const views = display === "views" && !image;
  const paint = display === "paint" && !image;
  const view3d: "print" | "model" | null =
    image || views || paint ? null : printPath && (mode === "filament" || !modelPath) ? "print" : modelPath ? "model" : null;
  const shown = image ? strip.find((s) => s.id === image) : null;
  const plate = job.product_type === "nameplate";
  const size = sizeText(dims, plate);
  const displays = [
    sets.length > 0 && { value: "views" as const, label: "Views" },
    has3d && { value: "3d" as const, label: "3D" },
    canPaint && { value: "paint" as const, label: "Edit" },
  ].filter((o): o is { value: Display; label: string } => !!o);
  const displaySwitch = displays.length > 1 && (
    <Segmented<Display | "none">
      label="Display"
      value={image ? "none" : display}
      onChange={(d) => {
        setImage(null);
        setDisplayPick(d as Display);
      }}
      options={displays}
    />
  );

  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid="model-stage">
      {paint ? (
        <PaintEditor job={job} runs={runs} messages={messages} filaments={filaments} controls={displaySwitch} />
      ) : views ? (
        <div className="rounded-xl border border-line bg-panel p-4">
          <ProofGallery
            jobId={job.id}
            sets={sets}
            kind={kind}
            onKind={setKindPick}
            concept={approvedConcept}
            photo={job.photos[0]?.path}
            controls={displaySwitch}
          />
        </div>
      ) : (
      <div className="relative h-[min(64vh,620px)] min-h-[420px] overflow-hidden rounded-xl border border-line bg-panel">
        {view3d ? (
          <div className="absolute inset-0" data-testid="stage-view" data-view={view3d}>
            <Viewer
              key={view3d}
              jobId={job.id}
              texturePath={view3d === "model" ? modelPath : null}
              filamentPath={view3d === "print" ? printPath : null}
              mode={view3d === "print" ? "filament" : "texture"}
              onInfo={onModelInfo}
              height="fill"
              bare
            />
          </div>
        ) : shown ? (
          <ArtifactImage
            jobId={job.id}
            path={shown.path}
            bucket={shown.bucket}
            alt={shown.label}
            fit="contain"
            className="absolute inset-6 bg-transparent"
          />
        ) : (
          <div className="absolute inset-10 animate-pulse rounded-lg bg-raised/60" aria-label="Loading" />
        )}
        {has3d && (
          <div className="absolute left-4 top-4 flex items-center gap-2">
            {displaySwitch}
            {printPath && modelPath ? (
              <ViewToggle
                value={view3d === "print" ? "filament" : view3d === "model" ? "texture" : ("none" as ViewMode)}
                onChange={(m) => {
                  setImage(null);
                  setDisplayPick("3d");
                  onMode(m);
                }}
                textureUrl="t"
                filamentUrl="f"
              />
            ) : null}
            {image && !displaySwitch && (
              <button
                type="button"
                onClick={() => setImage(null)}
                className="h-9 rounded-full border border-ink bg-panel px-4 text-sm font-medium hover:bg-hover"
              >
                Show 3D
              </button>
            )}
            {preview && !printPath && (
              <span data-testid="texture-only" className="inline-flex h-9 items-center rounded-full border border-line-strong bg-panel px-4 text-sm text-muted">
                Texture only
              </span>
            )}
          </div>
        )}
      </div>
      )}

      <div className="flex items-center gap-4">
        {strip.length > 0 && (
          <div className="flex min-w-0 gap-2 overflow-x-auto" role="tablist" aria-label="Images">
            {strip.map((it) => {
              const on = image === it.id;
              return (
                <button
                  key={it.id}
                  type="button"
                  role="tab"
                  aria-selected={on}
                  aria-label={it.label}
                  title={it.label}
                  onClick={() => setImage(on && has3d ? null : it.id)}
                  className={cx(
                    "size-16 shrink-0 overflow-hidden rounded-md border bg-panel",
                    on ? "border-ink ring-2 ring-ink/15" : "border-line hover:border-line-strong",
                  )}
                >
                  <ArtifactImage jobId={job.id} path={it.path} bucket={it.bucket} alt="" fit={it.bucket === "inputs" ? "cover" : "contain"} className="size-full bg-transparent" />
                </button>
              );
            })}
          </div>
        )}
        {size && (
          <span className="num ml-auto shrink-0 text-sm text-muted" data-testid="stage-size">
            {size}
          </span>
        )}
      </div>
    </div>
  );
}
