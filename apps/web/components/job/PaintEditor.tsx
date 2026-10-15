"use client";

// The paint editor: the paint behind the result in three.js (every face in its filament),
// tools Brush, Fill and Pick, the palette slots with the catalog picker, undo within the
// session, and Send, which posts the contract paint_edit. The worker answers through the rows
// (lib/paintEdit editStatus); the preview panel under the stage shows the result.

import { useEffect, useMemo, useRef, useState } from "react";
import { FaceGrid, faceGeometry, type FaceGeometry } from "@/lib/brush";
import type { AnyMessage, FilamentRow, JobRow, StageRunRow } from "@/lib/db.types";
import { filamentLabel } from "@/lib/filamentLabel";
import { useData } from "@/lib/hooks";
import { outputPath } from "@/lib/manifestInfo";
import {
  PHASE_LABELS,
  applyOp,
  buildPaintEdit,
  changedFaces,
  currentPaint,
  editBusy,
  editStatus,
  faceFilament,
  faceSlots,
  finalPalette,
  modelFrom,
  replay,
  type EditorOp,
  type PaintModel,
} from "@/lib/paintEdit";
import { ErrorLine, Segmented, cx } from "../ui";
import { PaintScene, linearRgb } from "./paintScene";
import { FilamentPicker } from "./ResultRail";
import { usePaintFiles } from "./usePaintFiles";

type Tool = "brush" | "fill" | "pick";

interface Draft {
  ops: EditorOp[];
  /** Colors added to the slots but not painted yet. */
  extra: string[];
  active: string | null;
}

const EMPTY: Draft = { ops: [], extra: [], active: null };
/** Drafts outlive the editor while the stage shows Views or 3D. */
const drafts = new Map<string, Draft>();

const BTN = "h-9 rounded-full px-3.5 text-sm font-semibold enabled:hover:bg-hover disabled:opacity-40";

export function PaintEditor({
  job,
  runs,
  messages,
  filaments,
  controls,
}: {
  job: JobRow;
  runs: StageRunRow[];
  messages: AnyMessage[];
  filaments: FilamentRow[];
  /** The stage's display switch, shown in the toolbar. */
  controls?: React.ReactNode;
}) {
  const client = useData();
  const folder = useMemo(() => currentPaint(runs), [runs]);
  const status = useMemo(() => editStatus(messages, runs), [messages, runs]);
  const busy = editBusy(status);
  const { files, error: loadError, loading } = usePaintFiles(job.id, folder, outputPath(runs, "geometry"));
  const base = useMemo(() => (files ? modelFrom(files.state, files.map, files.faceRegions) : null), [files]);
  const byId = useMemo(() => new Map(filaments.map((f) => [f.id, f])), [filaments]);
  const hexOf = (id: string) => filamentLabel(id, byId).hex ?? "#9a9a9a";
  const cap = job.settings.ams_slots ?? 5;

  const key = `${job.id}|${folder ?? ""}`;
  const [draft, setDraftState] = useState<Draft>(() => drafts.get(key) ?? EMPTY);
  const [draftKey, setDraftKey] = useState(key);
  if (draftKey !== key) {
    setDraftKey(key);
    setDraftState(drafts.get(key) ?? EMPTY);
  }
  const setDraft = (d: Draft | ((cur: Draft) => Draft)) =>
    setDraftState((cur) => {
      const next = typeof d === "function" ? d(cur) : d;
      drafts.set(key, next);
      return next;
    });

  // Model after the ops: appended ops apply incrementally, undo replays from the base.
  const cache = useRef<{ base: PaintModel; ops: EditorOp[]; model: PaintModel } | null>(null);
  const model = useMemo(() => {
    if (!base) return null;
    const c = cache.current;
    let m: PaintModel;
    if (c && c.base === base && draft.ops.length === c.ops.length + 1 && c.ops.every((o, i) => o === draft.ops[i])) {
      m = applyOp(c.model, draft.ops[draft.ops.length - 1]);
    } else if (c && c.base === base && c.ops.length === draft.ops.length && c.ops.every((o, i) => o === draft.ops[i])) {
      m = c.model;
    } else m = replay(base, draft.ops);
    cache.current = { base, ops: draft.ops, model: m };
    return m;
  }, [base, draft.ops]);

  const slots = useMemo(() => {
    if (!base || !model) return [];
    const shown = finalPalette(base, model);
    return [...shown, ...draft.extra.filter((x) => !shown.includes(x))];
  }, [base, model, draft.extra]);
  const active = draft.active && slots.includes(draft.active) ? draft.active : (slots[0] ?? null);
  const changed = useMemo(() => (base && model && draft.ops.length ? changedFaces(base, model) : 0), [base, model, draft.ops.length]);

  const [tool, setToolState] = useState<Tool>("brush");
  // Pick is an eyedropper: after a pick the tool goes back to the one before it.
  const lastPaintTool = useRef<Tool>("brush");
  const setTool = (t: Tool) => {
    if (t !== "pick") lastPaintTool.current = t;
    setToolState(t);
  };
  const [size, setSize] = useState(2);
  const [picker, setPicker] = useState<string | "new" | null>(null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hover, setHover] = useState<{ x: number; y: number; r: number; text: string } | null>(null);
  const [glError, setGlError] = useState(false);

  // ------------------------------------------------------------------ scene
  const host = useRef<HTMLDivElement>(null);
  const scene = useRef<PaintScene | null>(null);
  const geo = useRef<FaceGeometry | null>(null);
  const grid = useRef<FaceGrid | null>(null);
  useEffect(() => {
    if (!host.current) return;
    try {
      scene.current = new PaintScene(host.current);
    } catch {
      setGlError(true);
      return;
    }
    return () => {
      scene.current?.dispose();
      scene.current = null;
    };
  }, []);
  useEffect(() => {
    const s = scene.current;
    if (!files || !s) return;
    s.setMesh(
      files.figure,
      files.parts.map((p) => ({ mesh: p.mesh, hex: hexOf(p.filament ?? files.state.default_filament) })),
    );
    geo.current = faceGeometry(files.figure.positions, files.figure.indices);
    grid.current = new FaceGrid(geo.current, 2);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [files]);
  const palKey = [...new Set([...(model?.palette ?? []), ...draft.extra])].join("|");
  useEffect(() => {
    const s = scene.current;
    if (!model || !s) return;
    const pal = palKey.split("|");
    s.setFaceColors(faceSlots(model, pal), pal.map((id) => linearRgb(hexOf(id))));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [model, palKey, files]);

  // ------------------------------------------------------------------ pointer
  const stroke = useRef<{ mask: Uint8Array; faces: number[]; x: number; y: number } | null>(null);
  const click = useRef<{ x: number; y: number; face: number } | null>(null);
  const frame = useRef(0);
  const ringPx = useRef(8);
  const editable = !!model && !busy && !sending && !glError;

  const centroid = (f: number): [number, number, number] => {
    const c = geo.current!.centroids;
    return [c[f * 3], c[f * 3 + 1], c[f * 3 + 2]];
  };
  const dab = (face: number) => {
    const st = stroke.current;
    if (!st || !grid.current || !scene.current || !active) return;
    const fresh = grid.current.query(centroid(face), size, scene.current.viewDir()).filter((f) => !st.mask[f]);
    for (const f of fresh) st.mask[f] = 1;
    st.faces.push(...fresh);
    scene.current.paintFaces(fresh, linearRgb(hexOf(active)));
  };
  const describe = (face: number) => {
    if (!model) return "";
    const rid = model.faceRegions[face];
    const label = rid ? (model.labels.get(rid) ?? `region ${rid}`) : "unlabeled";
    return `${label} · ${filamentLabel(faceFilament(model, face), byId).name}`;
  };

  const onPointerDownCapture = (e: React.PointerEvent<HTMLDivElement>) => {
    const s = scene.current;
    if (e.button !== 0 || !editable || !s || !model) return;
    const face = s.pick(e.clientX, e.clientY);
    if (face == null) return; // the background orbits
    if (tool === "brush") {
      if (!active) return;
      e.stopPropagation();
      e.preventDefault();
      s.controls.enabled = false;
      host.current?.setPointerCapture?.(e.pointerId);
      stroke.current = { mask: new Uint8Array(model.faceRegions.length), faces: [], x: e.clientX, y: e.clientY };
      dab(face);
    } else {
      click.current = { x: e.clientX, y: e.clientY, face };
    }
  };

  /** Dabs along the drag from the last traced point to (x, y), about half a brush apart (each
   * is one GPU pick). Returns the face under (x, y). */
  const trace = (x: number, y: number): number | null => {
    const s = scene.current;
    const st = stroke.current;
    if (!s || !st) return null;
    const d = Math.hypot(x - st.x, y - st.y);
    const n = Math.max(1, Math.min(8, Math.ceil(d / Math.max(3, ringPx.current / 2))));
    let face: number | null = null;
    for (let i = 1; i <= n; i++) {
      face = s.pick(st.x + ((x - st.x) * i) / n, st.y + ((y - st.y) * i) / n);
      if (face != null) dab(face);
    }
    st.x = x;
    st.y = y;
    return face;
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const x = e.clientX;
    const y = e.clientY;
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => {
      const s = scene.current;
      if (!s || !model || !geo.current) return;
      const face = stroke.current ? trace(x, y) : s.pick(x, y);
      const rect = host.current?.getBoundingClientRect();
      if (face == null || !rect) return setHover(null);
      const r = tool === "brush" ? s.pxPerMm(centroid(face)) * size : 0;
      if (r) ringPx.current = r;
      setHover({ x: x - rect.left, y: y - rect.top, r, text: describe(face) });
    });
  };

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    const s = scene.current;
    const st = stroke.current;
    if (st) {
      // The last stretch of a fast drag may still wait for its frame: trace it now.
      cancelAnimationFrame(frame.current);
      if (st.x !== e.clientX || st.y !== e.clientY) trace(e.clientX, e.clientY);
      stroke.current = null;
      if (s) s.controls.enabled = true;
      host.current?.releasePointerCapture?.(e.pointerId);
      if (st.faces.length && active) {
        const op: EditorOp = { op: { op: "brush_faces", filament_id: active }, faces: Uint32Array.from(st.faces).sort() };
        setDraft((d) => ({ ...d, ops: [...d.ops, op] }));
      }
      return;
    }
    const c = click.current;
    click.current = null;
    if (!c || !model || Math.hypot(e.clientX - c.x, e.clientY - c.y) > 5) return;
    if (tool === "pick") {
      setDraft((d) => ({ ...d, active: faceFilament(model, c.face) }));
      setTool(lastPaintTool.current);
      return;
    }
    const rid = model.faceRegions[c.face];
    if (!rid || !active || model.assignment.get(rid) === active) return;
    setDraft((d) => ({ ...d, ops: [...d.ops, { op: { op: "fill_region", region_ids: [rid], filament_id: active } }] }));
  };

  // ------------------------------------------------------------------ slots and send
  const pick = (id: string) => {
    const target = picker;
    setPicker(null);
    if (!model || target == null) return;
    if (target === "new") {
      setDraft((d) => ({ ...d, extra: d.extra.includes(id) || slots.includes(id) ? d.extra : [...d.extra, id], active: id }));
      return;
    }
    if (id === target) return;
    const k = model.palette.indexOf(target);
    if (k < 0) {
      setDraft((d) => ({ ...d, extra: d.extra.map((x) => (x === target ? id : x)), active: id }));
      return;
    }
    const op: EditorOp = { op: { op: "replace_slot", from_slot: k + 1, filament_id: id } };
    setDraft((d) => ({ ...d, ops: [...d.ops, op], active: d.active === target ? id : d.active }));
  };

  const send = async () => {
    if (!client || !draft.ops.length || busy || sending) return;
    setSending(true);
    setError(null);
    try {
      const { payload, uploads } = buildPaintEdit(draft.ops, { base: folder, filaments });
      for (const u of uploads) await client.uploadInput(job.id, u.path, new Blob([u.text], { type: "text/plain" }));
      await client.sendMessage({ jobId: job.id, kind: "paint_edit", payload, stage: "s09_paint" });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSending(false);
    }
  };

  const mapPaint = async () => {
    if (!client || busy || sending) return;
    setSending(true);
    setError(null);
    try {
      await client.sendMessage({ jobId: job.id, kind: "paint_edit", payload: { ops: [], source: "owned" }, stage: "s09_paint" });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSending(false);
    }
  };

  const phase = status?.phase ?? null;
  const showStatus = !!status && (busy || (phase !== "done" && !!status.note) || phase === "review" || phase === "paused");
  const overlay = glError
    ? "No WebGL"
    : !folder
      ? null
      : loadError
        ? loadError
        : loading || !files
          ? "Loading paint"
          : null;

  return (
    <div
      className="flex h-[min(64vh,620px)] min-h-[440px] overflow-hidden rounded-xl border border-line bg-panel"
      data-testid="paint-editor"
      data-busy={busy || undefined}
    >
      <div className="relative min-w-0 flex-1">
        <div
          ref={host}
          className={cx("absolute inset-0", editable && tool === "brush" && hover ? "cursor-none" : editable && hover ? "cursor-crosshair" : undefined)}
          data-testid="paint-canvas"
          onPointerDownCapture={onPointerDownCapture}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerLeave={() => !stroke.current && setHover(null)}
        />
        {hover && tool === "brush" && editable && (
          <span
            aria-hidden
            className="pointer-events-none absolute rounded-full border-2 border-white shadow-[0_0_0_1.5px_rgba(28,26,23,0.85)]"
            style={{ left: hover.x - hover.r, top: hover.y - hover.r, width: hover.r * 2, height: hover.r * 2 }}
          />
        )}
        <div className="absolute left-4 top-4 flex flex-wrap items-center gap-2">
          {controls}
          {folder && (
            <Segmented<Tool>
              label="Tool"
              value={tool}
              onChange={setTool}
              options={[
                { value: "brush", label: "Brush", disabled: !editable },
                { value: "fill", label: "Fill", disabled: !editable },
                { value: "pick", label: "Pick", disabled: !editable },
              ]}
            />
          )}
          {folder && tool === "brush" && (
            <label className="flex h-9 items-center gap-2 rounded-full border border-line bg-panel/90 px-3.5 text-sm backdrop-blur">
              <span className="text-muted">Size</span>
              <input
                type="range"
                min={0.5}
                max={6}
                step={0.5}
                value={size}
                disabled={!editable}
                onChange={(e) => setSize(Number(e.target.value))}
                aria-label="Brush size"
                className="w-24 accent-[var(--color-brick)]"
              />
              <span className="num w-12 text-right font-medium">{size} mm</span>
            </label>
          )}
        </div>
        {hover && (
          <div className="pointer-events-none absolute bottom-3 left-4 rounded-full bg-panel/90 px-3 py-1 text-xs text-muted backdrop-blur" data-testid="paint-hover">
            {hover.text}
          </div>
        )}
        {overlay && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-muted">{overlay}</div>
        )}
        {!folder && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3" data-testid="paint-empty">
            <span className="text-sm text-muted">No paint map</span>
            {busy && status ? (
              <span role="status" data-testid="paint-status" className="pulse h-10 text-sm font-semibold leading-10 text-accent">
                {PHASE_LABELS[status.phase]}
              </span>
            ) : (
              <button type="button" className="h-10 rounded-full bg-accent px-5 text-sm font-semibold text-accent-fg hover:brightness-110 disabled:opacity-50" disabled={sending} onClick={() => void mapPaint()}>
                Map paint
              </button>
            )}
            {status?.phase === "refused" && status.note && <span className="max-w-sm text-center text-xs text-bad">{status.note}</span>}
            <ErrorLine>{error}</ErrorLine>
          </div>
        )}
      </div>

      <aside className={cx("w-[248px] shrink-0 flex-col border-l border-line bg-panel", folder ? "flex" : "hidden")} aria-label="Slots">
        <div className="flex items-baseline justify-between px-4 pb-1 pt-4">
          <h3 className="text-sm font-semibold">Slots</h3>
          <span className="num text-xs text-muted">
            {slots.length} of {cap}
          </span>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto pb-2">
        <ul className="flex flex-col px-2" data-testid="paint-slots">
          {slots.map((id, i) => {
            const f = filamentLabel(id, byId);
            const on = id === active;
            const row = byId.get(id);
            return (
              <li key={id} className="flex flex-col">
                <div className={cx("group flex h-10 items-center gap-2.5 rounded-lg pl-2 pr-1", on ? "bg-hover" : "hover:bg-hover/60")}>
                  <button
                    type="button"
                    role="radio"
                    aria-checked={on}
                    aria-label={`Slot ${i + 1} ${f.name}`}
                    disabled={!editable}
                    onClick={() => setDraft((d) => ({ ...d, active: id }))}
                    className="flex min-w-0 flex-1 items-center gap-2.5 text-left disabled:cursor-default"
                  >
                    <span className="num w-3 shrink-0 text-xs text-muted">{i + 1}</span>
                    <span
                      className={cx("size-5 shrink-0 rounded-full border border-ink/25", on && "ring-2 ring-ink ring-offset-2 ring-offset-hover")}
                      style={{ background: f.hex ?? undefined }}
                    />
                    <span className={cx("min-w-0 flex-1 truncate text-[14px]", on && "font-semibold")} title={id}>
                      {f.name}
                    </span>
                    {row && !row.owned && <span className="shrink-0 rounded-full border border-line-strong px-1.5 text-[11px] text-muted">Not owned</span>}
                  </button>
                  <button
                    type="button"
                    disabled={!editable}
                    aria-label={`Change slot ${i + 1}`}
                    onClick={() => setPicker(picker === id ? null : id)}
                    className="hidden h-7 shrink-0 rounded-full px-2 text-xs font-semibold text-muted enabled:hover:bg-panel enabled:hover:text-fg group-hover:inline-flex group-hover:items-center disabled:!hidden aria-expanded:inline-flex aria-expanded:items-center"
                    aria-expanded={picker === id}
                  >
                    Change
                  </button>
                </div>
                {picker === id && <FilamentPicker filaments={filaments} current={id} onPick={pick} onClose={() => setPicker(null)} />}
              </li>
            );
          })}
        </ul>
        <div className="px-2">
          {picker === "new" ? (
            <FilamentPicker filaments={filaments} current={null} onPick={pick} onClose={() => setPicker(null)} />
          ) : (
            <button
              type="button"
              disabled={!editable || slots.length >= cap}
              onClick={() => setPicker("new")}
              className="mt-1 h-9 rounded-full px-3 text-sm font-semibold text-accent enabled:hover:bg-hover disabled:opacity-40"
            >
              Add color
            </button>
          )}
        </div>
        </div>

        <div className="flex shrink-0 flex-col gap-3 border-t border-line p-4">
          {showStatus && status && (
            <p role="status" data-testid="paint-status" data-phase={phase} className={cx("text-sm font-semibold", busy ? "pulse text-accent" : "text-bad")}>
              {PHASE_LABELS[status.phase]}
              {!busy && status.note ? <span className="mt-0.5 block text-xs font-normal text-muted">{status.note}</span> : null}
            </p>
          )}
          <ErrorLine>{error}</ErrorLine>
          <div className="flex flex-col gap-1">
            <span className="num text-sm text-muted" data-testid="paint-edits">
              {draft.ops.length ? `${draft.ops.length} ${draft.ops.length === 1 ? "edit" : "edits"} · ${changed.toLocaleString()} faces` : "No edits"}
            </span>
            <div className="-ml-3.5 flex items-center">
              <button type="button" className={BTN} disabled={!editable || !draft.ops.length} onClick={() => setDraft((d) => ({ ...d, ops: d.ops.slice(0, -1) }))}>
                Undo
              </button>
              <button type="button" className={cx(BTN, "text-muted")} disabled={!editable || !draft.ops.length} onClick={() => setDraft((d) => ({ ...d, ops: [], extra: [] }))}>
                Reset
              </button>
            </div>
          </div>
          <button
            type="button"
            data-testid="paint-send"
            disabled={!editable || !draft.ops.length}
            onClick={() => void send()}
            className="flex h-11 w-full items-center justify-center rounded-full bg-accent text-base font-semibold text-accent-fg hover:brightness-110 disabled:opacity-50"
          >
            {sending ? "Sending" : "Send"}
          </button>
        </div>
      </aside>
    </div>
  );
}
