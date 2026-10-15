// Fidelity views for the job page: the product form, the approved concept, the concept check in a
// few words, and the proof sets (the product from the eight contract PROOF_VIEWS) for the 3D
// model, the paint and the simulated print.

import type { CheckItem, ConceptCandidate, ConceptCheck, CustomAsk, ProductForm } from "./contracts.gen";
import type { JobRow, StageRunRow } from "./db.types";
import type { Gates } from "./derive";
import { askKeyWord, formFromText, formIsChoice } from "./intent";
import { latestRuns } from "./manifestInfo";

// ------------------------------------------------------------------ form and asks

/**
 * The job's form once anything decided it: the jobs row (s01 writes it), the concept round, the
 * answer to the form question, what intake says the order form set, the request's own words.
 * Only keychains and magnets have a choice; every other product is a 3D figure.
 */
export function jobForm(job: JobRow, gates: Gates): ProductForm | null {
  if (!formIsChoice(job.product_type)) return "figure";
  if (job.form) return job.form;
  const round = gates.concepts?.latest.payload;
  const fromRound = round?.form ?? round?.candidates.find((c) => c.form)?.form;
  if (fromRound) return fromRound;
  const answer = gates.questions?.answer?.payload.answers.find((a) => a.question_id === "form");
  const picked = answer?.option_ids?.find((o) => o === "flat" || o === "figure");
  if (picked) return picked as ProductForm;
  return gates.questions?.message.payload.form ?? formFromText(job.prompt);
}

/** Custom asks: the jobs row (s01), else what intake listed. */
export function jobAsks(job: JobRow, gates: Gates): CustomAsk[] {
  if (job.custom_asks?.length) return job.custom_asks;
  return gates.questions?.message.payload.custom_asks ?? [];
}

// ------------------------------------------------------------------ concepts

export function candidateById(gates: Gates, id: string | null | undefined): ConceptCandidate | null {
  if (!id) return null;
  for (const r of [...(gates.concepts?.rounds ?? [])].reverse()) {
    const c = r.payload.candidates.find((x) => x.id === id);
    if (c) return c;
  }
  return null;
}

/** The approved concept: the product the customer said yes to. */
export function approvedCandidate(gates: Gates): ConceptCandidate | null {
  return candidateById(gates, gates.concepts?.approved?.payload.candidate_id);
}

export interface CheckChip {
  id: string;
  /** Two or three words: "Name checked", "Loop missing". */
  text: string;
  passed: boolean;
  /** Tooltip: what was checked and the model's note. */
  detail: string;
}

const PART_WORDS: Record<string, string> = {
  relief: "Charm",
  keyring_loop: "Loop",
  hanging_loop: "Loop",
  nameplate: "Plate",
  round_base: "Base",
  oval_base: "Base",
  flat_back: "Back",
  magnet_pocket: "Pocket",
};

/** One check item in a few words. The subject comes from the item, the verdict from `passed`. */
export function checkChip(item: CheckItem): CheckChip {
  const ok = item.passed;
  let text: string;
  switch (item.kind) {
    case "text":
      text = ok ? "Name checked" : "Name wrong";
      break;
    case "part": {
      const part = PART_WORDS[item.id.replace(/^part\./, "")] ?? "Part";
      text = ok ? `${part} checked` : `${part} missing`;
      break;
    }
    case "ask": {
      // The label is the ask in the customer's words (product_plan.check_items).
      const word = askKeyWord(item.label);
      text = ok ? `${word} checked` : `${word} missing`;
      break;
    }
    case "markings":
      text = ok ? "Markings checked" : "Markings off";
      break;
    case "no_extra_base":
      text = ok ? "No extra base" : "Extra base";
      break;
    case "figure_identical":
      text = ok ? "Figure kept" : "Figure changed";
      break;
    case "view":
      text = ok ? "View checked" : "View off";
      break;
    default:
      text = ok ? "Checked" : "Failed";
  }
  return { id: item.id, text, passed: ok, detail: [item.label, item.note].filter(Boolean).join(". ") };
}

/**
 * A concept check as chips, failures first, one chip per distinct text (a check can list the
 * same part on the figure and on the product). "Name checked" carries what the model read.
 */
export function checkChips(check: ConceptCheck | null | undefined): CheckChip[] {
  if (!check) return [];
  const chips = (check.items ?? []).map(checkChip);
  if (check.text_read) {
    for (const c of chips) if (c.text.startsWith("Name")) c.detail = `${c.detail}. Read: ${check.text_read}`.replace(/^\. /, "");
  }
  const seen = new Map<string, CheckChip>();
  for (const c of chips) {
    const cur = seen.get(c.text);
    if (!cur) seen.set(c.text, c);
  }
  return [...seen.values()].sort((a, b) => Number(a.passed) - Number(b.passed));
}

// ------------------------------------------------------------------ proof views

/** contracts regions.PROOF_VIEWS, in sheet order. */
export const PROOF_ORDER = ["front", "front_3q", "left_3q", "left", "right", "back", "back_3q", "top"] as const;
export type ProofView = (typeof PROOF_ORDER)[number];
export const PROOF_LABELS: Record<ProofView, string> = {
  front: "Front",
  front_3q: "Front 3/4",
  left_3q: "Left 3/4",
  left: "Left",
  right: "Right",
  back: "Back",
  back_3q: "Back 3/4",
  top: "Top",
};
/** Camera per view (regions.PROOF_VIEWS): azimuth around +Z from +X, elevation, in degrees. */
export const PROOF_CAMERAS: Record<ProofView, { azimuth: number; elevation: number }> = {
  front: { azimuth: -90, elevation: 10 },
  front_3q: { azimuth: -55, elevation: 20 },
  left_3q: { azimuth: -125, elevation: 20 },
  left: { azimuth: 180, elevation: 10 },
  right: { azimuth: 0, elevation: 10 },
  back: { azimuth: 90, elevation: 10 },
  back_3q: { azimuth: 125, elevation: 20 },
  top: { azimuth: -90, elevation: 89.9 },
};

export type ProofKind = "model" | "paint" | "print";
export const PROOF_KIND_LABELS: Record<ProofKind, string> = { model: "Model", paint: "Paint", print: "Print" };

/** Where a set came from: the worker's proof renders, Meshy's thumbnails, or a render in the page. */
export type ProofSource = "proof" | "thumbnails" | "glb";

export interface ProofSet {
  kind: ProofKind;
  source: ProofSource;
  /** Job-relative image per view (source proof or thumbnails); missing views are absent. */
  images: Partial<Record<ProofView, string>>;
  /** Source glb: the GLB the page renders from every view. */
  glb?: string;
}

const VIEW_RE = new RegExp(`(?:^|[_/-])(${[...PROOF_ORDER].sort((a, b) => b.length - a.length).join("|")})\\.(?:png|jpe?g|webp)$`, "i");

/** The proof view a file name ends with: print_front_3q.png, thumb_m1_left.png -> front_3q, left. */
export function viewOf(path: string): ProofView | null {
  const m = VIEW_RE.exec(path.split("/").pop() ?? path);
  return m ? (m[1].toLowerCase() as ProofView) : null;
}

const KIND_ROLES: Record<ProofKind, string> = { model: "proof_model", paint: "proof_paint", print: "proof_print" };

function fromOutputs(runs: StageRunRow[], kind: ProofKind): Partial<Record<ProofView, string>> {
  // Newest stage first: s10's proof set beats s09's proof/final set.
  for (const r of latestRuns(runs).reverse()) {
    const images: Partial<Record<ProofView, string>> = {};
    for (const o of r.manifest.outputs ?? []) {
      if (o.role !== KIND_ROLES[kind]) continue;
      const v = viewOf(o.path);
      if (v && !images[v]) images[v] = o.path;
    }
    if (Object.keys(images).length) return images;
  }
  return {};
}

/**
 * The proof sets the page can show, in toggle order (print, paint, model). Sources per kind:
 *
 * 1. `result.proof_images` (contracts JobResult, s10);
 * 2. stage outputs with role proof_model, proof_paint or proof_print (s10 proof/, s09 proof/final/);
 * 3. model only: while a 3D model waits for approval, the page renders that mesh GLB from every
 *    view, else Meshy's four thumbnails; paint only: the preview GLB in filament colors (thin
 *    slice results have no proof set).
 *
 * TODO(worker): no stage renders the mesh before the mesh gate yet (s09 and s10 write the proof
 * set after it). When one writes proof_model outputs for the mesh under review (DEEP-DIVE, "Where
 * the customer should have seen the real form", item 3), prefer them over the page render here.
 */
export function proofSets(job: JobRow, runs: StageRunRow[], gates: Gates): ProofSet[] {
  const result = job.result ?? gates.result?.payload ?? null;
  const meshGate = gates.active === "mesh_review" ? gates.mesh?.latest.payload : null;
  const sets: ProofSet[] = [];
  for (const kind of ["print", "paint", "model"] as ProofKind[]) {
    if (kind === "model" && meshGate) {
      // The model waiting on you: never a proof set from an earlier mesh.
      sets.push({ kind, source: "glb", images: {}, glb: meshGate.glb });
      continue;
    }
    const images: Partial<Record<ProofView, string>> = {};
    for (const p of result?.proof_images ?? []) {
      const v = p.kind === kind && (PROOF_ORDER as readonly string[]).includes(p.view) ? (p.view as ProofView) : null;
      if (v && !images[v]) images[v] = p.path;
    }
    if (Object.keys(images).length) {
      sets.push({ kind, source: "proof", images });
      continue;
    }
    const outputs = fromOutputs(runs, kind);
    if (Object.keys(outputs).length) {
      sets.push({ kind, source: "proof", images: outputs });
      continue;
    }
    if (kind === "paint" && result?.preview_glb) sets.push({ kind, source: "glb", images: {}, glb: result.preview_glb });
    if (kind === "model") {
      const mesh = gates.mesh?.latest.payload;
      if (mesh?.glb) sets.push({ kind, source: "glb", images: {}, glb: mesh.glb });
      else if (mesh?.thumbnails?.length) {
        const thumbs: Partial<Record<ProofView, string>> = {};
        for (const t of mesh.thumbnails) {
          const v = viewOf(t);
          if (v && !thumbs[v]) thumbs[v] = t;
        }
        if (Object.keys(thumbs).length) sets.push({ kind, source: "thumbnails", images: thumbs });
      }
    }
  }
  return sets;
}
