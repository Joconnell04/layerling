// What the job page reads from stage manifests: the geometry bbox (s06 geometry.json), Meshy's
// printability report (s06 printability.json) and the warnings (warnings.json, manifest data,
// decisions, errors).

import { STAGE_BY_ID } from "./contracts";
import type { StageId } from "./contracts.gen";
import type { StageRunRow } from "./db.types";

export interface Warning {
  stage: StageId;
  text: string;
}

export type Orientation = "upright" | "flat_face_up" | "flat_back_down";

/** The parts of contracts GeometryResult the page uses. */
export interface GeometryInfo {
  bbox_min_mm: [number, number, number];
  bbox_max_mm: [number, number, number];
  orientation?: Orientation;
  warnings?: string[];
}

/** Meshy Analyze Printability, as s06 stores it in printability.json. */
export interface PrintabilityInfo {
  status?: string | null;
  issue_count?: number | null;
  error_count?: number | null;
  warning_count?: number | null;
  metrics?: {
    degenerate_faces?: number | null;
    non_manifold_edges?: number | null;
    holes?: number | null;
    is_watertight?: boolean | null;
  } | null;
}

const OK_STATUS = /^(ok|pass|passed|good|printable)$/i;
const count = (n: number) => n.toLocaleString("en-US");

/** "Printability warning: 164,801 degenerate faces", or null when Meshy found nothing. */
export function printabilityText(p: PrintabilityInfo | null | undefined): string | null {
  if (!p) return null;
  const m = p.metrics ?? {};
  const parts: string[] = [];
  const add = (n: number | null | undefined, one: string, many: string) => {
    if (n) parts.push(`${count(n)} ${n === 1 ? one : many}`);
  };
  add(m.degenerate_faces, "degenerate face", "degenerate faces");
  add(m.non_manifold_edges, "non-manifold edge", "non-manifold edges");
  add(m.holes, "hole", "holes");
  if (m.is_watertight === false) parts.push("not watertight");
  const status = p.status ?? null;
  if (!parts.length) {
    if (!status || OK_STATUS.test(status)) return null;
    const counts = [
      p.error_count ? `${count(p.error_count)} errors` : null,
      p.warning_count ? `${count(p.warning_count)} warnings` : null,
    ].filter(Boolean);
    return `Printability ${status}${counts.length ? `: ${counts.join(", ")}` : ""}`;
  }
  return `Printability ${status && !OK_STATUS.test(status) ? status : "issues"}: ${parts.join(", ")}`;
}

/** Group long bare integers in worker text: "164801 errors" -> "164,801 errors". */
export function groupDigits(text: string): string {
  return text.replace(/(?<![\d.,])\d{5,}(?![\d.])/g, (d) => count(Number(d)));
}

/** Latest attempt per stage. */
export function latestRuns(runs: StageRunRow[]): StageRunRow[] {
  const by = new Map<StageId, StageRunRow>();
  for (const r of runs) {
    const cur = by.get(r.stage);
    if (!cur || r.attempt > cur.attempt) by.set(r.stage, r);
  }
  return [...by.values()].sort((a, b) => (STAGE_BY_ID[a.stage]?.number ?? 0) - (STAGE_BY_ID[b.stage]?.number ?? 0));
}

/** Job-relative path of the latest output with this role. */
export function outputPath(runs: StageRunRow[], role: string): string | null {
  for (const r of latestRuns(runs).reverse()) {
    const hit = (r.manifest.outputs ?? []).find((o) => o.role === role);
    if (hit) return hit.path;
  }
  return null;
}

/** Warnings that live in the manifests themselves (no file fetch needed). */
export function manifestWarnings(runs: StageRunRow[]): Warning[] {
  const out: Warning[] = [];
  for (const r of latestRuns(runs)) {
    const m = r.manifest;
    const data = (m.data ?? {}) as Record<string, unknown>;
    if (Array.isArray(data.warnings)) {
      for (const w of data.warnings) if (typeof w === "string") out.push({ stage: r.stage, text: w });
    }
    if (typeof data.printability === "string" && !/^(ok|pass|passed|good|printable)$/i.test(data.printability)) {
      out.push({ stage: r.stage, text: `Printability: ${data.printability}` });
    }
    for (const d of m.decisions ?? []) {
      if (d.key === "geometry.build_product" && d.value === "skipped" && d.reason) {
        out.push({ stage: r.stage, text: d.reason });
      }
    }
    if (m.error?.message) out.push({ stage: r.stage, text: m.error.message });
    if (r.status === "failed" && !m.error) out.push({ stage: r.stage, text: "Stage failed" });
  }
  return out;
}

/** Merge file warnings with manifest warnings, dropping exact duplicates. */
export function mergeWarnings(fromFiles: Warning[], fromManifests: Warning[]): Warning[] {
  const seen = new Set<string>();
  const out: Warning[] = [];
  for (const w of [...fromFiles, ...fromManifests]) {
    const k = `${w.stage}:${w.text}`;
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(w);
  }
  return out;
}

/**
 * Everything the Warnings list shows: s06 warnings.json, GeometryResult warnings, Meshy's
 * printability counts (replacing the bare "Printability: warning" from the manifest), then the
 * manifest warnings. Long numbers get thousands separators.
 */
export function jobWarnings(input: {
  runs: StageRunRow[];
  files?: unknown;
  geometry?: GeometryInfo | null;
  printability?: PrintabilityInfo | null;
}): Warning[] {
  const s06: StageId = "s06_printability";
  const files: Warning[] = [];
  if (Array.isArray(input.files)) {
    for (const w of input.files) if (typeof w === "string") files.push({ stage: s06, text: w });
  }
  for (const w of input.geometry?.warnings ?? []) files.push({ stage: s06, text: w });
  const detail = printabilityText(input.printability);
  if (detail) files.push({ stage: s06, text: detail });
  const manifests = manifestWarnings(input.runs).filter((w) => !(detail && /^Printability: /.test(w.text)));
  return mergeWarnings(files, manifests).map((w) => ({ ...w, text: groupDigits(w.text) }));
}

/**
 * Print dimensions from the s06 bbox (print frame: mm, Z up, base on z = 0).
 * Upright pieces: width X, depth Y, height Z. Flat pieces (keychain, magnet) lie on the bed:
 * width X, height Y (the piece seen face on), relief thickness Z.
 */
export interface Dimensions {
  flat: boolean;
  widthMm: number | null;
  heightMm: number | null;
  /** Flat pieces only: Z extent, the relief thickness. */
  reliefMm: number | null;
  /** Upright pieces only: Y extent. */
  depthMm: number | null;
  /** Before s06: the target longest side from the job row. */
  targetMm: number | null;
}

export function dimensions(geometry: GeometryInfo | null, targetMm: number | null): Dimensions {
  const empty = { widthMm: null, heightMm: null, reliefMm: null, depthMm: null, targetMm };
  if (!geometry) return { flat: false, ...empty };
  const [x0, y0, z0] = geometry.bbox_min_mm;
  const [x1, y1, z1] = geometry.bbox_max_mm;
  const r = (v: number) => Math.round(v * 10) / 10;
  const flat = !!geometry.orientation && geometry.orientation !== "upright";
  return flat
    ? { ...empty, flat, widthMm: r(x1 - x0), heightMm: r(y1 - y0), reliefMm: r(z1 - z0) }
    : { ...empty, flat, widthMm: r(x1 - x0), heightMm: r(z1 - z0), depthMm: r(y1 - y0) };
}
