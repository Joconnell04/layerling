// Map job-relative artifact paths from mock payloads to files in public/mock/ (made by
// scripts/gen-mock-assets.mjs and scripts/gen_mock_proofs.py). Real paths follow
// <stage>/a<N>/<file>, same as the worker. With the job, concept images and models follow its
// product and form (a flat charm, a 3D keychain, a nameplate, ...).

import type { JobRow } from "../../db.types";

const BASE = "/mock";

type MockJob = Pick<JobRow, "product_type" | "form"> | null | undefined;

/** The mock set a job's art comes from. */
export function mockSet(job: MockJob): "flat" | "kc3d" | "plate" | "ornament" | "relief" | "figurine" {
  if (!job) return "figurine";
  const flat = job.form === "flat" || (job.product_type === "magnet" && job.form !== "figure");
  if (job.product_type === "keychain") return flat ? "flat" : "kc3d";
  if (job.product_type === "magnet") return flat ? "relief" : "figurine";
  if (job.product_type === "nameplate") return "plate";
  if (job.product_type === "ornament") return "ornament";
  return "figurine";
}

const CONCEPT_FILE: Record<ReturnType<typeof mockSet>, string> = {
  flat: "charm",
  kc3d: "kc3d",
  plate: "plate",
  ornament: "ornament",
  relief: "relief",
  figurine: "concept",
};

/**
 * The paint editor's files (scripts/gen_mock_paint_edit.py, the real pipeline on the 3D
 * keychain): the shipped paint (base) and a paint edit's answer (edit), which also holds the
 * preview (paint_preview.json, toolpaths, proof views). Null for any other file.
 */
export function mockPaintUrl(path: string, edit: boolean): string | null {
  const file = path.split("/").pop() ?? path;
  const set = edit ? "edit" : "base";
  if (file === "figure.ply") return `${BASE}/paint/figure.ply`;
  if (["paint_state.json", "region_map.json", "face_regions.npy"].includes(file)) return `${BASE}/paint/${set}/${file}`;
  if (!edit) return null;
  if (["paint_preview.json", "toolpaths.json", "toolpaths.bin"].includes(file)) return `${BASE}/paint/edit/${file}`;
  const proof = /\/preview\/proof\/((?:paint|print)_[a-z0-9_]+\.jpg)$/.exec(path);
  if (proof) return `${BASE}/paint/edit/proof/${proof[1]}`;
  return null;
}

export function mockAssetUrl(path: string, job?: MockJob): string {
  const file = path.split("/").pop() ?? path;
  const set = mockSet(job);
  // s03: concept_r<R>c<N> is the product (what the customer approves), figure_r<R>c<N> the figure
  // alone; r<R>c<N> is a candidate from before the split.
  const concept = /^(figure_|concept_)?r(\d+)c(\d+)\./.exec(file);
  if (concept) {
    const round = Number(concept[2]);
    const n = Number(concept[3]);
    const variant = ((round - 1) % 2) * 2 + n; // 1..4
    if (concept[1] === "figure_") return `${BASE}/figure-${variant}.svg`;
    if (concept[1] === "concept_") return `${BASE}/${CONCEPT_FILE[set]}-${variant}.svg`;
    return `${BASE}/concept-${variant}.svg`;
  }
  // Proof renders (s09 proof/final/, s10 proof/): <kind>_<view>.png.
  const proof = /(?:^|\/)proof\/(?:final\/)?(model|paint|print)_([a-z0-9_]+)\.\w+$/.exec(path);
  if (proof) {
    const dir = set === "flat" || set === "relief" ? "flat" : set === "kc3d" ? "kc3d" : "figurine";
    return `${BASE}/proof/${dir}/${proof[1]}_${proof[2]}.jpg`;
  }
  if (file.endsWith(".glb")) {
    if (file.startsWith("preview_flat")) return `${BASE}/preview-flat.glb`;
    if (file.startsWith("preview_kc3d")) return `${BASE}/kc3d-preview.glb`;
    if (file.includes("preview")) return `${BASE}/preview.glb`;
    // Meshy's model for this product: the 3D keychain, the flat charm, or the figurine.
    if (set === "kc3d") return `${BASE}/kc3d.glb`;
    if (set === "flat" || set === "relief") return `${BASE}/preview-flat.glb`;
    return `${BASE}/mesh.glb`;
  }
  if (file.endsWith(".3mf")) return `${BASE}/sample.3mf`;
  const thumb = /^thumb_(?:m\d+_)?([a-z]+)\./.exec(file);
  if (thumb) return `${BASE}/thumb-${thumb[1]}.svg`;
  if (file.startsWith("render_sheet_flat")) return `${BASE}/render-sheet-flat.svg`;
  if (file.startsWith("render_sheet")) return `${BASE}/render-sheet.svg`;
  // Paint checkpoints: ideal paint and the simulated print of the same view.
  if (/^ckpt_\d+_ideal\./.test(file)) return `${BASE}/paint-ideal.svg`;
  if (/^ckpt_\d+_sim\./.test(file) || file.startsWith("sim_")) return `${BASE}/paint-sim.svg`;
  if (file.endsWith(".json")) return `${BASE}/checklist.json`;
  const photo = /^photo_(\d+)\./.exec(file);
  if (photo) return `${BASE}/photo-${((Number(photo[1]) - 1) % 3) + 1}.svg`;
  return `${BASE}/render-sheet.svg`;
}
