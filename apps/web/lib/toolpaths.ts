// Per-layer toolpaths from a paint edit's preview (contracts PaintPreview, toolpaths version 1:
// a JSON index plus a binary file of uint16 points and uint32 chains), drawn top down on a 2D
// canvas one layer at a time.

export interface ToolpathLayer {
  z: number;
  height: number;
  /** [first chain, chain count] */
  chains: [number, number];
  time_s: number | null;
  /** Filament slots with model material on the layer. */
  slots: number[];
  /** Filament changes on the layer. */
  changes: number;
}

export interface ToolpathIndex {
  version: number;
  bin: string;
  scale_mm: number;
  origin_mm: [number, number];
  bbox_mm: [number, number, number, number];
  slots: { slot: number; filament_id: string | null; hex: string | null }[];
  roles: string[];
  points: { offset: number; count: number };
  chains: { offset: number; count: number };
  layers: ToolpathLayer[];
}

export interface Toolpaths {
  index: ToolpathIndex;
  /** x, y pairs in grid units (mm = origin + value * scale). */
  points: Uint16Array;
  /** first point, point count, slot << 8 | role, per chain. */
  chains: Uint32Array;
  /** The print without the prime tower, mm [x0, y0, x1, y1]: what the layer view frames. */
  modelBox: [number, number, number, number] | null;
}

export const PRIME_TOWER_ROLE = "Prime tower";

export function parseToolpaths(index: ToolpathIndex, bin: ArrayBuffer): Toolpaths {
  if (index.version !== 1) throw new Error(`toolpaths version ${index.version}`);
  const points = new Uint16Array(bin, index.points.offset, index.points.count * 2);
  const chains = new Uint32Array(bin, index.chains.offset, index.chains.count * 3);
  const tower = index.roles.indexOf(PRIME_TOWER_ROLE);
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (let c = 0; c < chains.length / 3; c++) {
    if ((chains[c * 3 + 2] & 0xff) === tower) continue;
    const p0 = chains[c * 3];
    for (let k = 0; k < chains[c * 3 + 1]; k++) {
      const u = points[(p0 + k) * 2];
      const v = points[(p0 + k) * 2 + 1];
      if (u < x0) x0 = u;
      if (u > x1) x1 = u;
      if (v < y0) y0 = v;
      if (v > y1) y1 = v;
    }
  }
  const s = index.scale_mm;
  const [gx, gy] = index.origin_mm;
  const modelBox: Toolpaths["modelBox"] = Number.isFinite(x0) ? [gx + x0 * s, gy + y0 * s, gx + x1 * s, gy + y1 * s] : null;
  return { index, points, chains, modelBox };
}

export interface DrawOptions {
  /** Canvas size in CSS pixels (the backing store is scaled by devicePixelRatio). */
  width: number;
  height: number;
  /** Fill for slots without a hex. */
  fallback?: string;
  /** Color of the layer below, drawn first. */
  under?: string;
  background?: string;
  dpr?: number;
}

/** mm -> canvas px transform that fits the print (every layer, the tower left out) with a margin. */
export function fitTransform(t: Toolpaths, width: number, height: number, margin = 16) {
  const [x0, y0, x1, y1] = t.modelBox ?? t.index.bbox_mm;
  const w = Math.max(x1 - x0, 1);
  const h = Math.max(y1 - y0, 1);
  const s = Math.min((width - 2 * margin) / w, (height - 2 * margin) / h);
  const ox = (width - w * s) / 2;
  const oy = (height - h * s) / 2;
  const scale = t.index.scale_mm;
  const [gx, gy] = t.index.origin_mm;
  // Bed Y grows away from the viewer: flip so the front of the plate is at the bottom.
  return {
    s,
    x: (u: number) => ox + (gx + u * scale - x0) * s,
    y: (v: number) => height - (oy + (gy + v * scale - y0) * s),
  };
}

function strokeLayer(ctx: CanvasRenderingContext2D, t: Toolpaths, layer: ToolpathLayer, color: (slot: number) => string,
  tf: ReturnType<typeof fitTransform>, width: number) {
  const [first, count] = layer.chains;
  ctx.lineWidth = width;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  let current = "";
  let open = false;
  for (let c = first; c < first + count; c++) {
    const p0 = t.chains[c * 3];
    const n = t.chains[c * 3 + 1];
    const col = color(t.chains[c * 3 + 2] >> 8);
    if (col !== current) {
      if (open) ctx.stroke();
      ctx.strokeStyle = col;
      ctx.beginPath();
      current = col;
      open = true;
    }
    ctx.moveTo(tf.x(t.points[p0 * 2]), tf.y(t.points[p0 * 2 + 1]));
    for (let k = 1; k < n; k++) ctx.lineTo(tf.x(t.points[(p0 + k) * 2]), tf.y(t.points[(p0 + k) * 2 + 1]));
  }
  if (open) ctx.stroke();
}

/** Layer `i` in filament colors over a faint copy of the layer below. */
export function drawLayer(ctx: CanvasRenderingContext2D, t: Toolpaths, i: number, opts: DrawOptions): void {
  const dpr = opts.dpr ?? 1;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, opts.width, opts.height);
  if (opts.background) {
    ctx.fillStyle = opts.background;
    ctx.fillRect(0, 0, opts.width, opts.height);
  }
  const layers = t.index.layers;
  const layer = layers[i];
  if (!layer) return;
  const tf = fitTransform(t, opts.width, opts.height);
  const line = Math.max(1, 0.42 * tf.s);
  const hex = new Map(t.index.slots.map((s) => [s.slot, s.hex ?? opts.fallback ?? "#888888"]));
  if (i > 0 && opts.under) strokeLayer(ctx, t, layers[i - 1], () => opts.under!, tf, line);
  strokeLayer(ctx, t, layer, (slot) => hex.get(slot) ?? opts.fallback ?? "#888888", tf, line);
}

/** Total extruded length of a layer in mm (tests and the layer readout). */
export function layerLength(t: Toolpaths, i: number): number {
  const layer = t.index.layers[i];
  if (!layer) return 0;
  const s = t.index.scale_mm;
  let mm = 0;
  for (let c = layer.chains[0]; c < layer.chains[0] + layer.chains[1]; c++) {
    const p0 = t.chains[c * 3];
    const n = t.chains[c * 3 + 1];
    for (let k = 1; k < n; k++) {
      const a = (p0 + k - 1) * 2;
      const b = (p0 + k) * 2;
      mm += Math.hypot(t.points[b] - t.points[a], t.points[b + 1] - t.points[a + 1]) * s;
    }
  }
  return mm;
}
