"use client";

// What a paint edit prints like: time, filament and purge before and after (both p2p_slicer),
// the simulated print (or the paint) from the eight proof views, and the toolpaths one layer at
// a time. While the worker runs, its phase (Painting, Slicing, Gate check).

import { useEffect, useMemo, useRef, useState } from "react";
import type { PaintPreview as Preview, ProofImage } from "@/lib/contracts.gen";
import type { AnyMessage, FilamentRow, StageRunRow } from "@/lib/db.types";
import { PROOF_LABELS, PROOF_ORDER, type ProofView } from "@/lib/fidelity";
import { filamentLabel } from "@/lib/filamentLabel";
import { duration, grams } from "@/lib/format";
import { useArtifactJson, useArtifactUrl } from "@/lib/hooks";
import { PHASE_LABELS, editBusy, editStatus, latestPreview, previewNumbers, type NumberRow } from "@/lib/paintEdit";
import { drawLayer, parseToolpaths, type ToolpathIndex, type Toolpaths } from "@/lib/toolpaths";
import { ArtifactImage } from "../ArtifactImage";
import { Segmented, cx } from "../ui";

/** The simulator's backdrop (p2p_sim.proof), so white and black filament both read. */
const PLATE = "#6e737c";

export function PaintPreview({ jobId, runs, messages, filaments }: { jobId: string; runs: StageRunRow[]; messages: AnyMessage[]; filaments: FilamentRow[] }) {
  const status = useMemo(() => editStatus(messages, runs), [messages, runs]);
  const latest = useMemo(() => latestPreview(runs), [runs]);
  const preview = useArtifactJson<Preview>(jobId, latest?.path);
  const busy = editBusy(status);
  if (!status && !latest) return null;
  const attempt = latest ? latest.path.split("/").slice(0, 2).join("/") : null;

  return (
    <section aria-label="Paint preview" data-testid="paint-preview" className="overflow-hidden rounded-xl border border-line bg-panel">
      <header className="flex items-center gap-3 border-b border-line px-[18px] py-2.5 text-sm">
        <span className="font-semibold">Paint preview</span>
        {attempt && <span className="num text-muted">{attempt}</span>}
        {preview?.ops ? <span className="num text-muted">{preview.ops} {preview.ops === 1 ? "edit" : "edits"} · {(preview.changed_faces ?? 0).toLocaleString()} faces</span> : null}
        {status && busy && (
          <span role="status" data-testid="preview-phase" className="pulse ml-auto font-semibold text-accent">
            {PHASE_LABELS[status.phase]}
          </span>
        )}
        {status && !busy && status.phase !== "done" && (
          <span className="ml-auto font-semibold text-bad" title={status.note ?? undefined}>
            {PHASE_LABELS[status.phase]}
          </span>
        )}
      </header>
      {preview ? (
        <div className="flex flex-col gap-4 p-[18px]">
          <Numbers rows={previewNumbers(preview)} />
          <div className="grid gap-6 xl:grid-cols-[minmax(0,1.45fr)_minmax(0,1fr)]">
            <Views jobId={jobId} images={preview.proof_images ?? []} />
            <Layers jobId={jobId} path={preview.toolpaths ?? null} filaments={filaments} />
          </div>
          {!!preview.warnings?.length && (
            <ul className="flex flex-col gap-0.5 text-xs text-muted" data-testid="preview-warnings">
              {preview.warnings.slice(0, 3).map((w) => (
                <li key={w} className="truncate" title={w}>
                  {w}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <div className="flex h-40 items-center justify-center text-sm text-muted">{busy ? "Working" : latest ? "Loading" : "No preview"}</div>
      )}
    </section>
  );
}

function fmt(row: NumberRow, v: number | null): string {
  if (v == null) return "?";
  return row.key === "time" ? duration(v) : grams(v);
}

function delta(row: NumberRow): { text: string; tone: string } | null {
  if (row.before == null || row.after == null) return null;
  const d = row.after - row.before;
  const tiny = row.key === "time" ? Math.abs(d) < 30 : Math.abs(d) < 0.05;
  if (tiny) return { text: "Same", tone: "text-muted" };
  const sign = d < 0 ? "−" : "+";
  const text = row.key === "time" ? `${sign}${duration(Math.abs(d))}` : `${sign}${Math.abs(d).toFixed(1)} g`;
  return { text, tone: d < 0 ? "text-ok" : "text-bad" };
}

function Numbers({ rows }: { rows: NumberRow[] }) {
  return (
    <dl className="grid grid-cols-3 gap-2" aria-label="Before and after" data-testid="preview-numbers">
      {rows.map((r) => {
        const dl = delta(r);
        return (
          <div key={r.key} className="flex flex-col gap-0.5 rounded-lg bg-bg px-3.5 py-2.5" data-testid={`preview-${r.key}`}>
            <dt className="text-xs text-muted">{r.label}</dt>
            <dd className="num flex items-baseline gap-2">
              <span className="text-lg font-semibold">{fmt(r, r.after)}</span>
              {dl && <span className={cx("text-sm font-semibold", dl.tone)}>{dl.text}</span>}
            </dd>
            <dd className="num text-xs text-muted">Was {fmt(r, r.before)}</dd>
          </div>
        );
      })}
    </dl>
  );
}

function Views({ jobId, images }: { jobId: string; images: ProofImage[] }) {
  const kinds = (["print", "paint"] as const).filter((k) => images.some((i) => i.kind === k));
  const [pick, setPick] = useState<"print" | "paint">("print");
  const kind = kinds.includes(pick) ? pick : (kinds[0] ?? "print");
  const byView = new Map(images.filter((i) => i.kind === kind).map((i) => [i.view as ProofView, i.path]));
  return (
    <div className="flex min-w-0 flex-col gap-2.5" data-testid="preview-views">
      <div className="flex items-center gap-2">
        <Segmented<"print" | "paint">
          size="sm"
          label="Views"
          value={kind}
          onChange={setPick}
          options={[
            { value: "print", label: "Print", disabled: !kinds.includes("print") },
            { value: "paint", label: "Paint", disabled: !kinds.includes("paint") },
          ]}
        />
        <span className="num ml-auto text-xs text-muted">{byView.size} of 8 views</span>
      </div>
      <div className="grid grid-cols-4 gap-2">
        {PROOF_ORDER.map((v) => (
          <figure key={v} className="flex min-w-0 flex-col" data-testid={`preview-view-${v}`}>
            {byView.get(v) ? (
              <ArtifactImage jobId={jobId} path={byView.get(v)} alt={PROOF_LABELS[v]} fit="contain" className="aspect-square w-full rounded-md" />
            ) : (
              <div className="flex aspect-square w-full items-center justify-center rounded-md border border-dashed border-line-strong text-xs text-faint">No render</div>
            )}
            <figcaption className="pt-1 text-xs text-muted">{PROOF_LABELS[v]}</figcaption>
          </figure>
        ))}
      </div>
    </div>
  );
}

function useToolpaths(jobId: string, path: string | null): Toolpaths | null {
  const index = useArtifactJson<ToolpathIndex>(jobId, path);
  const binPath = path && index ? `${path.slice(0, path.lastIndexOf("/"))}/${index.bin}` : null;
  const binUrl = useArtifactUrl(jobId, binPath);
  const [out, setOut] = useState<{ url: string; t: Toolpaths } | null>(null);
  useEffect(() => {
    if (!binUrl || !index) return;
    let live = true;
    fetch(binUrl, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.arrayBuffer() : Promise.reject(new Error(String(r.status)))))
      .then((buf) => live && setOut({ url: binUrl, t: parseToolpaths(index, buf) }))
      .catch(() => live && setOut(null));
    return () => {
      live = false;
    };
  }, [binUrl, index]);
  return out && out.url === binUrl ? out.t : null;
}

function Layers({ jobId, path, filaments }: { jobId: string; path: string | null; filaments: FilamentRow[] }) {
  const t = useToolpaths(jobId, path);
  const layers = t?.index.layers ?? [];
  const busiest = useMemo(() => {
    let best = 0;
    layers.forEach((l, i) => {
      if (l.slots.length > (layers[best]?.slots.length ?? 0)) best = i;
    });
    return best;
  }, [layers]);
  const [pick, setPick] = useState<number | null>(null);
  const i = Math.min(pick ?? busiest, Math.max(0, layers.length - 1));
  const canvas = useRef<HTMLCanvasElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(360);
  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setW(Math.max(160, Math.floor(el.clientWidth))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  useEffect(() => {
    const c = canvas.current;
    const ctx = c?.getContext("2d");
    if (!c || !ctx || !t) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    c.width = w * dpr;
    c.height = w * dpr;
    drawLayer(ctx, t, i, { width: w, height: w, dpr, background: PLATE, under: "rgba(255,255,255,0.16)" });
  }, [t, i, w]);
  const layer = layers[i];
  const byId = new Map(filaments.map((f) => [f.id, f]));
  return (
    <div className="mx-auto flex w-full min-w-0 max-w-[320px] flex-col gap-2.5" data-testid="preview-layers">
      <div className="flex items-center gap-2 text-xs text-muted">
        <span className="text-sm font-semibold text-fg">Layers</span>
        {layer && (
          <span className="num ml-auto" data-testid="layer-readout">
            Layer {i + 1} of {layers.length} · {layer.z.toFixed(2)} mm
          </span>
        )}
      </div>
      <div ref={box} className="w-full">
        {t ? (
          <canvas ref={canvas} style={{ width: w, height: w }} className="block rounded-md" aria-label={layer ? `Toolpaths, layer ${i + 1}` : "Toolpaths"} />
        ) : (
          <div className="flex aspect-square w-full items-center justify-center rounded-md border border-dashed border-line-strong text-xs text-faint">
            {path ? "Loading" : "No toolpaths"}
          </div>
        )}
      </div>
      {layers.length > 0 && (
        <>
          <input
            type="range"
            min={1}
            max={layers.length}
            value={i + 1}
            onChange={(e) => setPick(Number(e.target.value) - 1)}
            aria-label="Layer"
            className="w-full accent-[var(--color-brick)]"
          />
          <div className="flex min-h-5 items-center gap-2 text-xs text-muted">
            {layer?.slots.map((s) => {
              const slot = t?.index.slots.find((x) => x.slot === s);
              const f = filamentLabel(slot?.filament_id, byId, s);
              return (
                <span key={s} className="flex items-center gap-1.5">
                  <span className="size-3 rounded-full border border-ink/25" style={{ background: slot?.hex ?? f.hex ?? undefined }} />
                  {f.name}
                </span>
              );
            })}
            {layer && layer.changes > 0 && <span className="num ml-auto">{layer.changes} {layer.changes === 1 ? "change" : "changes"}</span>}
            {layer?.time_s != null && <span className={cx("num", !(layer.changes > 0) && "ml-auto")}>{duration(layer.time_s)}</span>}
          </div>
        </>
      )}
    </div>
  );
}
