// 3MF download: "<name>-<product>.3mf". Cross-origin signed URLs ignore the <a download> name,
// so real modes go through /api/studio/jobs/<id>/download, which answers with Content-Disposition.

import type { JobRow } from "./db.types";
import { PRODUCT_LABELS, nameTextOf } from "./products";

function slug(s: string): string {
  return s
    .normalize("NFKD")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}

export function downloadName(job: Pick<JobRow, "id" | "addons" | "title" | "prompt" | "product_type">, name?: string | null): string {
  const who = slug(name ?? nameTextOf(job.addons) ?? job.title ?? "") || job.id.slice(0, 8);
  const product = slug(PRODUCT_LABELS[job.product_type] ?? job.product_type) || "print";
  return `${who}-${product}.3mf`;
}

export function downloadHref(mode: "mock" | "supabase" | "server", job: JobRow, name?: string | null): string {
  if (mode === "mock") return "/mock/sample.3mf";
  const q = name ? `?name=${encodeURIComponent(name)}` : "";
  return `/api/studio/jobs/${encodeURIComponent(job.id)}/download${q}`;
}
