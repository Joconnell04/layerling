import type { Manifest } from "./contracts.gen";
import type { JobRow, StageRunRow } from "./db.types";
import { downloadHref, downloadName } from "./download";
import {
  dimensions,
  groupDigits,
  jobWarnings,
  manifestWarnings,
  mergeWarnings,
  outputPath,
  printabilityText,
} from "./manifestInfo";

function run(stage: StageRunRow["stage"], attempt: number, manifest: Partial<Manifest>, status: StageRunRow["status"] = "succeeded"): StageRunRow {
  return {
    id: `${stage}-${attempt}`,
    job_id: "j1",
    owner_id: null,
    stage,
    attempt,
    status,
    started_at: null,
    finished_at: null,
    usd: 0,
    credits: 0,
    confidence: null,
    manifest,
    updated_at: "",
  };
}

describe("manifest info", () => {
  const runs = [
    run("s06_printability", 1, { outputs: [{ path: "s06_printability/a1/geometry.json", role: "geometry", sha256: "x" }] }),
    run("s06_printability", 2, {
      outputs: [
        { path: "s06_printability/a2/geometry.json", role: "geometry", sha256: "x" },
        { path: "s06_printability/a2/warnings.json", role: "geometry_warnings", sha256: "x" },
      ],
      data: { warnings: ["Wall under 0.8 mm at the ear"], printability: "thin_walls" },
      decisions: [{ key: "geometry.build_product", value: "skipped", reason: "No base for this product" } as never],
    }),
    run("s10_write", 1, { error: { message: "Slicer: floating region" } as never }, "failed"),
  ];

  it("finds the latest output by role", () => {
    expect(outputPath(runs, "geometry")).toBe("s06_printability/a2/geometry.json");
    expect(outputPath(runs, "geometry_warnings")).toBe("s06_printability/a2/warnings.json");
    expect(outputPath(runs, "nothing")).toBeNull();
  });

  it("collects warnings from data, printability, skipped decisions and errors", () => {
    expect(manifestWarnings(runs).map((w) => `${w.stage}: ${w.text}`)).toEqual([
      "s06_printability: Wall under 0.8 mm at the ear",
      "s06_printability: Printability: thin_walls",
      "s06_printability: No base for this product",
      "s10_write: Slicer: floating region",
    ]);
    expect(manifestWarnings([run("s06_printability", 1, { data: { printability: "ok", warnings: 2 } })])).toEqual([]);
  });

  it("merges file and manifest warnings without duplicates", () => {
    const a = { stage: "s06_printability" as const, text: "Repaired 12 faces" };
    expect(mergeWarnings([a, a], [a, { ...a, text: "Other" }])).toHaveLength(2);
  });

  it("measures an upright piece: width X, depth Y, height Z", () => {
    const d = dimensions({ bbox_min_mm: [-60, -9, 0], bbox_max_mm: [60.04, 9, 66.25], orientation: "upright" }, 120);
    expect(d).toEqual({ flat: false, widthMm: 120, depthMm: 18, heightMm: 66.3, reliefMm: null, targetMm: 120 });
    expect(dimensions(null, 45)).toEqual({ flat: false, widthMm: null, heightMm: null, reliefMm: null, depthMm: null, targetMm: 45 });
  });

  it("measures a flat piece face on: width X, height Y, relief thickness Z (proof run 1 keychain)", () => {
    const g = {
      orientation: "flat_face_up" as const,
      bbox_min_mm: [-10.240772380000001, -18.47301507, 0.0] as [number, number, number],
      bbox_max_mm: [10.24043058, 26.526984930038452, 10.285674887612547] as [number, number, number],
    };
    expect(dimensions(g, 45)).toEqual({ flat: true, widthMm: 20.5, heightMm: 45, reliefMm: 10.3, depthMm: null, targetMm: 45 });
  });
});

describe("printability and warnings", () => {
  const proof1 = {
    status: "warning",
    issue_count: 1,
    error_count: 0,
    warning_count: 1,
    metrics: { degenerate_faces: 164801, holes: 0, is_watertight: true, non_manifold_edges: 0 },
  };

  it("spells out Meshy's counts", () => {
    expect(printabilityText(proof1)).toBe("Printability warning: 164,801 degenerate faces");
    expect(printabilityText({ status: "error", metrics: { holes: 1, is_watertight: false } })).toBe("Printability error: 1 hole, not watertight");
    expect(printabilityText({ status: "error", error_count: 3 })).toBe("Printability error: 3 errors");
    expect(printabilityText({ status: "ok", metrics: { degenerate_faces: 0 } })).toBeNull();
    expect(groupDigits("Meshy printability: 164801 errors; 0.20 x; 2026")).toBe("Meshy printability: 164,801 errors; 0.20 x; 2026");
  });

  it("replaces the bare Printability: warning with the counts and keeps the rest", () => {
    const runs = [run("s06_printability", 1, { data: { printability: "warning", warnings: 1 } })];
    const list = jobWarnings({ runs, files: ["10.3 mm thick; depth already squeezed to x0.20"], printability: proof1 });
    expect(list.map((w) => w.text)).toEqual([
      "10.3 mm thick; depth already squeezed to x0.20",
      "Printability warning: 164,801 degenerate faces",
    ]);
    expect(jobWarnings({ runs }).map((w) => w.text)).toEqual(["Printability: warning"]);
  });
});

describe("download name", () => {
  const job = {
    id: "0f3c2a91-aaaa-bbbb-cccc-000000000000",
    product_type: "nameplate",
    title: null,
    prompt: "x",
    addons: [{ kind: "nameplate", text: { text: "PEPPER" } }],
  } as unknown as JobRow;

  it("is <name>-<product>.3mf", () => {
    expect(downloadName(job)).toBe("pepper-nameplate.3mf");
    expect(downloadName(job, "Zoë & Miso")).toBe("zoe-miso-nameplate.3mf");
    expect(downloadName({ ...job, addons: [], product_type: "keychain" } as JobRow)).toBe("0f3c2a91-keychain.3mf");
  });

  it("goes through the server route outside mock mode", () => {
    expect(downloadHref("mock", job)).toBe("/mock/sample.3mf");
    expect(downloadHref("server", job, "Pepper")).toBe(`/api/studio/jobs/${job.id}/download?name=Pepper`);
    expect(downloadHref("supabase", job)).toBe(`/api/studio/jobs/${job.id}/download`);
  });
});
