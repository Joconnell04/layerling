// The studio paint editor's model. It loads the paint behind the current result (paint state,
// region map, face regions), applies the contract paint_edit ops locally for the live view and
// undo (the worker's rules, p2p_mcp_paint.edits), builds the paint_edit payload (face ranges
// inline, or a stroke file for large strokes), and reads the worker's answer from the rows: the
// s09 paint edit attempt, its PaintPreview, then s10 through the gate.

import type { PaintEdit, PaintOp, PaintPreview, PaintState, PrintNumbers, RegionMap } from "./contracts.gen";
import type { AnyMessage, FilamentRow, MessageRow, StageRunRow } from "./db.types";

/** contracts payloads.FACES_INLINE_MAX: a longer face list goes to a file (faces_path). */
export const FACES_INLINE_MAX = 200_000;

// ------------------------------------------------------------------ face ranges

/** Face ids as "0-9,15,20-31" (sorted, duplicates dropped), the contract's face encoding. */
export function encodeFaces(ids: Iterable<number>): string {
  const a = Uint32Array.from(new Set(ids)).sort();
  const out: string[] = [];
  let i = 0;
  while (i < a.length) {
    let j = i;
    while (j + 1 < a.length && a[j + 1] === a[j] + 1) j++;
    out.push(i === j ? `${a[i]}` : `${a[i]}-${a[j]}`);
    i = j + 1;
  }
  return out.join(",");
}

export function decodeFaces(text: string): number[] {
  const out: number[] = [];
  if (!text.trim()) return out;
  for (const part of text.split(",")) {
    const m = /^(\d+)(?:-(\d+))?$/.exec(part);
    if (!m) throw new Error(`bad face range ${part}`);
    const lo = Number(m[1]);
    const hi = m[2] != null ? Number(m[2]) : lo;
    for (let f = lo; f <= hi; f++) out.push(f);
  }
  return out;
}

// ------------------------------------------------------------------ where the paint is

const byAttempt = (a: StageRunRow, b: StageRunRow) => a.attempt - b.attempt;
const dirOf = (path: string) => path.slice(0, path.lastIndexOf("/"));

/**
 * Attempt folder of the paint behind the current result (e.g. "s09_paint/a2"): the paint_state
 * input of the newest s10 attempt, else the newest succeeded s09, then s08, paint_state output.
 * Null when the job has no paint map yet (a thin-slice job before any palette or paint edit).
 */
export function currentPaint(runs: StageRunRow[]): string | null {
  const s10 = runs.filter((r) => r.stage === "s10_write" && r.status !== "skipped").sort(byAttempt).at(-1);
  const input = s10?.manifest.inputs?.find((i) => i.role === "paint_state");
  if (input) return dirOf(input.path);
  for (const stage of ["s09_paint", "s08_palette"] as const) {
    const run = runs.filter((r) => r.stage === stage && r.status === "succeeded").sort(byAttempt).at(-1);
    const out = run?.manifest.outputs?.find((o) => o.role === "paint_state");
    if (out) return dirOf(out.path);
  }
  return null;
}

// ------------------------------------------------------------------ the model

export interface PaintModel {
  /** Region id per figure face, 0 = unlabeled (paints the default filament). */
  faceRegions: Int32Array;
  assignment: Map<number, string>;
  palette: string[];
  defaultFilament: string;
  partFilaments: Record<string, string>;
  nextRegionId: number;
  labels: Map<number, string>;
}

export function modelFrom(state: PaintState, map: RegionMap, faceRegions: Int32Array): PaintModel {
  return {
    faceRegions,
    assignment: new Map(Object.entries(state.assignment).map(([k, v]) => [Number(k), v])),
    palette: [...state.palette],
    defaultFilament: state.default_filament,
    partFilaments: { ...(state.part_filaments ?? {}) },
    nextRegionId: map.next_region_id,
    labels: new Map(map.regions.map((r) => [r.id, r.label])),
  };
}

/** One op as the editor keeps it: brush strokes keep their faces decoded. */
export interface EditorOp {
  op: PaintOp;
  faces?: Uint32Array;
}

function target(m: PaintModel, op: PaintOp): string {
  if (op.slot != null) {
    const f = m.palette[op.slot - 1];
    if (!f) throw new Error(`slot ${op.slot}: the palette has ${m.palette.length} slots`);
    return f;
  }
  if (!op.filament_id) throw new Error("op without a filament");
  return op.filament_id;
}

/** The worker's rules for one op (p2p_mcp_paint.edits.apply_ops), on a copy of the model. */
export function applyOp(m: PaintModel, e: EditorOp): PaintModel {
  const op = e.op;
  const fid = target(m, op);
  const next: PaintModel = { ...m, assignment: new Map(m.assignment), palette: [...m.palette] };
  const add = () => {
    if (!next.palette.includes(fid)) next.palette.push(fid);
  };
  if (op.op === "fill_region") {
    add();
    for (const r of op.region_ids ?? []) next.assignment.set(r, fid);
  } else if (op.op === "brush_faces") {
    const faces = e.faces ?? Uint32Array.from(decodeFaces(op.faces ?? ""));
    if (!faces.length) return m;
    add();
    const rid = m.nextRegionId;
    next.faceRegions = m.faceRegions.slice();
    for (let i = 0; i < faces.length; i++) next.faceRegions[faces[i]] = rid;
    next.assignment.set(rid, fid);
    next.nextRegionId = rid + 1;
    next.labels = new Map(m.labels).set(rid, op.label ?? `s1.paint_${rid}`);
  } else {
    const k = op.from_slot ?? 0;
    const old = m.palette[k - 1];
    if (!old) throw new Error(`from_slot ${k}: the palette has ${m.palette.length} slots`);
    if (old === fid) return m;
    for (const [r, f] of next.assignment) if (f === old) next.assignment.set(r, fid);
    if (next.defaultFilament === old) next.defaultFilament = fid;
    next.partFilaments = Object.fromEntries(Object.entries(m.partFilaments).map(([p, f]) => [p, f === old ? fid : f]));
    if (next.palette.includes(fid)) next.palette.splice(k - 1, 1);
    else next.palette[k - 1] = fid;
  }
  return next;
}

export function replay(base: PaintModel, ops: EditorOp[]): PaintModel {
  return ops.reduce(applyOp, base);
}

/** Filaments the paint uses: regions that still have faces, add-on parts, the default. */
export function usedFilaments(m: PaintModel): Set<string> {
  const present = new Uint8Array(m.nextRegionId + 1);
  for (let i = 0; i < m.faceRegions.length; i++) present[m.faceRegions[i]] = 1;
  const used = new Set<string>([m.defaultFilament, ...Object.values(m.partFilaments)]);
  for (const [r, f] of m.assignment) if (present[r]) used.add(f);
  return used;
}

/**
 * The palette the worker ends with: a filament the ops left unused drops out; an unused slot the
 * base already had stays.
 */
export function finalPalette(base: PaintModel, m: PaintModel): string[] {
  const used = usedFilaments(m);
  const baseUsed = usedFilaments(base);
  return m.palette.filter((f) => used.has(f) || (base.palette.includes(f) && !baseUsed.has(f)));
}

/** Filament id per face (the default for unlabeled or unassigned faces). */
export function faceFilament(m: PaintModel, face: number): string {
  const r = m.faceRegions[face];
  return (r && m.assignment.get(r)) || m.defaultFilament;
}

/** Palette index per face for the viewer (indexes into `palette`; 255 when missing). */
export function faceSlots(m: PaintModel, palette: string[] = m.palette): Uint8Array {
  const idx = new Map(palette.map((f, i) => [f, i]));
  const lut = new Uint8Array(m.nextRegionId + 1).fill(idx.get(m.defaultFilament) ?? 255);
  for (const [r, f] of m.assignment) if (r <= m.nextRegionId) lut[r] = idx.get(f) ?? 255;
  const out = new Uint8Array(m.faceRegions.length);
  for (let i = 0; i < out.length; i++) out[i] = lut[m.faceRegions[i]] ?? lut[0];
  return out;
}

/** Faces whose filament differs between two models. */
export function changedFaces(a: PaintModel, b: PaintModel): number {
  const pal = [...new Set([...a.palette, ...b.palette])];
  const sa = faceSlots(a, pal);
  const sb = faceSlots(b, pal);
  let n = 0;
  for (let i = 0; i < sa.length; i++) if (sa[i] !== sb[i]) n++;
  return n;
}

// ------------------------------------------------------------------ the payload

export interface StrokeUpload {
  /** Job-relative, bucket inputs: input/paint/<name>.txt */
  path: string;
  text: string;
}

/**
 * The contract payload for the ops. Strokes whose face ranges run past FACES_INLINE_MAX go to
 * a file the caller uploads first (faces_path). source is any as soon as an op paints a
 * filament that is not owned.
 */
export function buildPaintEdit(
  ops: EditorOp[],
  opts: { base: string | null; filaments: FilamentRow[]; note?: string | null; name?: () => string },
): { payload: PaintEdit; uploads: StrokeUpload[] } {
  const owned = new Map(opts.filaments.map((f) => [f.id, f.owned]));
  const uploads: StrokeUpload[] = [];
  const name = opts.name ?? (() => (typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}`));
  const out: PaintOp[] = ops.map((e) => {
    if (e.op.op !== "brush_faces") return e.op;
    const text = e.faces ? encodeFaces(e.faces) : (e.op.faces ?? "");
    if (text.length <= FACES_INLINE_MAX) return { ...e.op, faces: text, faces_path: undefined };
    const path = `input/paint/${name()}.txt`;
    uploads.push({ path, text });
    const { faces: _drop, ...rest } = e.op;
    return { ...rest, faces_path: path };
  });
  const unowned = out.some((o) => o.filament_id && owned.get(o.filament_id) === false);
  const payload: PaintEdit = { ops: out, source: unowned ? "any" : "owned" };
  if (opts.base) payload.base_attempt = opts.base;
  if (opts.note) payload.note = opts.note;
  return { payload, uploads };
}

// ------------------------------------------------------------------ the worker's answer

export type EditPhase = "queued" | "painting" | "slicing" | "gate" | "done" | "review" | "refused" | "paused";

export const PHASE_LABELS: Record<EditPhase, string> = {
  queued: "Queued",
  painting: "Painting",
  slicing: "Slicing",
  gate: "Gate check",
  done: "Done",
  review: "Needs review",
  refused: "Not applied",
  paused: "Paused",
};

export interface EditStatus {
  messageId: string;
  phase: EditPhase;
  /** The worker's note on this edit (why it was refused), if any. */
  note: string | null;
  edit: StageRunRow | null;
  gate: StageRunRow | null;
}

const data = (r: StageRunRow | undefined | null) => (r?.manifest.data ?? {}) as Record<string, unknown>;

/** Where the newest paint_edit is: queued, painting, slicing, at the gate, done or refused. */
export function editStatus(messages: AnyMessage[], runs: StageRunRow[]): EditStatus | null {
  const msg = messages.filter((m): m is MessageRow<"paint_edit"> => m.kind === "paint_edit").at(-1);
  if (!msg) return null;
  const edit = runs.find((r) => r.stage === "s09_paint" && (data(r).paint_edit as { message_id?: string } | undefined)?.message_id === msg.id) ?? null;
  const gate = runs.find((r) => r.stage === "s10_write" && data(r).paint_edit_message === msg.id) ?? null;
  const note =
    messages.filter((m): m is MessageRow<"note"> => m.kind === "note" && m.reply_to === msg.id).at(-1)?.payload.text ?? null;
  let phase: EditPhase;
  if (!edit) phase = note ? "refused" : "queued";
  else if (edit.status === "failed") phase = "refused";
  else if (edit.status !== "succeeded") phase = data(edit).progress === "preview" ? "slicing" : "painting";
  else if (!gate || gate.status === "running" || gate.status === "pending") phase = "gate";
  else if (gate.status === "succeeded") phase = "done";
  else if (gate.status === "paused") phase = /Bambu gate pending|slicer gate failed/.test(gate.manifest.pause_reason ?? "") ? "review" : "paused";
  else phase = "paused";
  const error = edit?.manifest.error?.message ?? null;
  return { messageId: msg.id, phase, note: note ?? (phase === "refused" ? error : null), edit, gate };
}

export const editBusy = (s: EditStatus | null) => !!s && ["queued", "painting", "slicing", "gate"].includes(s.phase);

/** The newest finished paint edit attempt and its paint_preview.json. */
export function latestPreview(runs: StageRunRow[]): { run: StageRunRow; path: string } | null {
  const edits = runs
    .filter((r) => r.stage === "s09_paint" && r.status === "succeeded" && data(r).paint_edit)
    .sort(byAttempt);
  for (const run of edits.reverse()) {
    const path = run.manifest.outputs?.find((o) => o.role === "paint_preview")?.path;
    if (path) return { run, path };
  }
  return null;
}

export interface NumberRow {
  key: "time" | "grams" | "purge";
  label: string;
  before: number | null;
  after: number | null;
}

/** Time, grams and purge before and after (both p2p_slicer). */
export function previewNumbers(p: PaintPreview): NumberRow[] {
  const pick = (n: PrintNumbers | null | undefined, k: "print_time_s" | "total_grams" | "purge_grams") => n?.[k] ?? null;
  return [
    { key: "time", label: "Print time", before: pick(p.before, "print_time_s"), after: pick(p.after, "print_time_s") },
    { key: "grams", label: "Filament", before: pick(p.before, "total_grams"), after: pick(p.after, "total_grams") },
    { key: "purge", label: "Purge", before: pick(p.before, "purge_grams"), after: pick(p.after, "purge_grams") },
  ];
}
