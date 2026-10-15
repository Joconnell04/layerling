import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { PaintState, RegionMap, StageId } from "./contracts";
import type { AnyMessage, FilamentRow, StageRunRow } from "./db.types";
import { readNpyInt32, readPly, writeNpyInt32, writePly } from "./meshFiles";
import {
  FACES_INLINE_MAX,
  applyOp,
  buildPaintEdit,
  changedFaces,
  currentPaint,
  decodeFaces,
  editBusy,
  editStatus,
  encodeFaces,
  faceFilament,
  faceSlots,
  finalPalette,
  latestPreview,
  modelFrom,
  previewNumbers,
  replay,
  type EditorOp,
} from "./paintEdit";

const BLACK = "generic-pla-black";
const WHITE = "generic-pla-white";
const BROWN = "generic-pla-brown";
const GRAY = "bambu-pla-basic-gray";

const ref = (path: string) => ({ path, sha256: "x" });

function fixture() {
  // 10 faces: region 1 = faces 0-4 (brown), region 2 = 5-7 (white), region 3 = 8-9 (black)
  const faceRegions = Int32Array.from([1, 1, 1, 1, 1, 2, 2, 2, 3, 3]);
  const state: PaintState = {
    region_map: ref("s09_paint/a1/region_map.json"),
    palette: [BLACK, BROWN, WHITE],
    assignment: { "1": BROWN, "2": WHITE, "3": BLACK },
    default_filament: BROWN,
    part_filaments: { loop: BLACK },
  };
  const map = {
    mesh: { path: "s06_printability/a1/figure.ply", sha256: "x", n_faces: 10, n_vertices: 12 },
    face_regions: ref("s09_paint/a1/face_regions.npy"),
    regions: [1, 2, 3].map((id) => ({ id, label: `s1.r${id}`, part: "other", source: "manual", n_faces: 1, area_mm2: 1, centroid_mm: [0, 0, 0], bbox_min_mm: [0, 0, 0], bbox_max_mm: [0, 0, 0] })),
    next_region_id: 4,
  } as RegionMap;
  return modelFrom(state, map, faceRegions);
}

const fill = (ids: number[], filament_id: string): EditorOp => ({ op: { op: "fill_region", region_ids: ids, filament_id } });
const brush = (faces: number[], filament_id: string): EditorOp => ({ op: { op: "brush_faces", filament_id }, faces: Uint32Array.from(faces) });

describe("face ranges", () => {
  it("round trips like the worker's encoder", () => {
    expect(encodeFaces([0, 1, 2, 3, 7, 9, 10, 11, 40])).toBe("0-3,7,9-11,40");
    expect(encodeFaces([5, 5, 4])).toBe("4-5");
    expect(encodeFaces([])).toBe("");
    expect(decodeFaces("0-3,7,9-11,40")).toEqual([0, 1, 2, 3, 7, 9, 10, 11, 40]);
    expect(() => decodeFaces("1-")).toThrow();
  });

  it("uses the contract's inline limit", () => {
    const py = readFileSync(join(__dirname, "..", "..", "..", "contracts/src/p2p_contracts/payloads.py"), "utf8");
    expect(Number(/^FACES_INLINE_MAX = ([\d_]+)/m.exec(py)?.[1].replace(/_/g, ""))).toBe(FACES_INLINE_MAX);
  });
});

describe("paint model", () => {
  it("fills, brushes and replaces the way the worker does", () => {
    const base = fixture();
    const a = applyOp(base, fill([2], BLACK));
    expect(faceFilament(a, 5)).toBe(BLACK);
    expect(faceFilament(base, 5)).toBe(WHITE); // the base is untouched
    const b = applyOp(a, brush([0, 1], GRAY));
    expect(b.nextRegionId).toBe(5);
    expect(b.faceRegions[0]).toBe(4);
    expect(base.faceRegions[0]).toBe(1);
    expect(b.palette).toEqual([BLACK, BROWN, WHITE, GRAY]);
    expect(faceFilament(b, 0)).toBe(GRAY);
    // the new region can be filled by id in a later op, as the worker numbers it
    const c = applyOp(b, fill([4], WHITE));
    expect(faceFilament(c, 1)).toBe(WHITE);
    // replace slot 2 (brown): regions, the default and parts follow; brown's slot takes gray...
    const d = applyOp(fixture(), { op: { op: "replace_slot", from_slot: 2, filament_id: GRAY } });
    expect(d.palette).toEqual([BLACK, GRAY, WHITE]);
    expect(d.defaultFilament).toBe(GRAY);
    expect(faceFilament(d, 0)).toBe(GRAY);
    // ...and drops out when the new filament is already in the palette
    const e = applyOp(fixture(), { op: { op: "replace_slot", from_slot: 3, filament_id: BLACK } });
    expect(e.palette).toEqual([BLACK, BROWN]);
    expect(faceFilament(e, 6)).toBe(BLACK);
    const f = applyOp(fixture(), { op: { op: "replace_slot", from_slot: 1, filament_id: WHITE } });
    expect(f.partFilaments.loop).toBe(WHITE);
    // a slot target
    expect(faceFilament(applyOp(base, { op: { op: "fill_region", region_ids: [3], slot: 3 } }), 8)).toBe(WHITE);
    expect(() => applyOp(base, { op: { op: "fill_region", region_ids: [3], slot: 9 } })).toThrow(/slot 9/);
  });

  it("replays for undo and prunes like the worker", () => {
    const base = fixture();
    const ops = [fill([3], WHITE), brush([8], GRAY)];
    const m = replay(base, ops);
    expect(replay(base, ops.slice(0, 1)).nextRegionId).toBe(4);
    expect(changedFaces(base, m)).toBe(2); // face 8 gray, face 9 white
    // black is still the loop's filament, so it stays; nothing else went unused
    expect(finalPalette(base, m)).toEqual([BLACK, BROWN, WHITE, GRAY]);
    const all = replay(base, [fill([1], WHITE), { op: { op: "replace_slot", from_slot: 1, filament_id: WHITE } }]);
    expect(finalPalette(base, all)).toEqual([BROWN, WHITE]);
    const slots = faceSlots(m);
    expect(Array.from(slots)).toEqual([1, 1, 1, 1, 1, 2, 2, 2, 3, 2]);
  });
});

describe("paint_edit payload", () => {
  const fils = [
    { id: BLACK, owned: true },
    { id: GRAY, owned: false },
  ] as FilamentRow[];

  it("sends faces inline, a big stroke as a file, and source any for catalog colors", () => {
    const small = buildPaintEdit([brush([3, 4, 5], BLACK), fill([2], BLACK)], { base: "s09_paint/a1", filaments: fils, note: "eyes" });
    expect(small.uploads).toEqual([]);
    expect(small.payload).toEqual({
      ops: [
        { op: "brush_faces", filament_id: BLACK, faces: "3-5", faces_path: undefined },
        { op: "fill_region", region_ids: [2], filament_id: BLACK },
      ],
      source: "owned",
      base_attempt: "s09_paint/a1",
      note: "eyes",
    });
    const every = Array.from({ length: 80_000 }, (_, i) => i * 2); // no runs: long text
    const big = buildPaintEdit([brush(every, GRAY)], { base: null, filaments: fils, name: () => "s1" });
    expect(big.payload.source).toBe("any");
    expect(big.payload.base_attempt).toBeUndefined();
    expect(big.uploads).toHaveLength(1);
    expect(big.uploads[0].path).toBe("input/paint/s1.txt");
    expect(big.uploads[0].text.length).toBeGreaterThan(FACES_INLINE_MAX);
    expect(big.payload.ops![0]).toEqual({ op: "brush_faces", filament_id: GRAY, faces_path: "input/paint/s1.txt" });
  });
});

// ------------------------------------------------------------------ rows

let seq = 0;
function run(stage: StageId, attempt: number, status: StageRunRow["status"], manifest: Partial<StageRunRow["manifest"]> = {}): StageRunRow {
  return {
    id: `r${++seq}`, job_id: "j", owner_id: null, stage, attempt, status, started_at: null, finished_at: null,
    usd: 0, credits: 0, confidence: null, manifest, updated_at: "",
  };
}

function msg(kind: string, id: string, extra: Record<string, unknown> = {}): AnyMessage {
  return { id, job_id: "j", owner_id: null, created_at: "", role: kind === "note" ? "assistant" : "user", kind, stage: null, body: null, payload: {}, reply_to: null, consumed_at: null, ...extra } as AnyMessage;
}

describe("rows", () => {
  it("finds the paint behind the result", () => {
    expect(currentPaint([run("s10_write", 1, "skipped")])).toBeNull();
    const s09 = run("s09_paint", 1, "succeeded", { outputs: [{ ...ref("s09_paint/a1/paint_state.json"), role: "paint_state" }] });
    expect(currentPaint([s09])).toBe("s09_paint/a1");
    const s10 = run("s10_write", 2, "succeeded", { inputs: [{ ...ref("s09_paint/a2/paint_state.json"), role: "paint_state" }] });
    const failed = run("s09_paint", 3, "failed", { outputs: [] });
    expect(currentPaint([s09, s10, failed])).toBe("s09_paint/a2");
  });

  it("follows an edit from the message to the gate", () => {
    const m = msg("paint_edit", "m1");
    expect(editStatus([m], [])?.phase).toBe("queued");
    expect(editBusy(editStatus([m], []))).toBe(true);
    const stale = msg("note", "n1", { reply_to: "m1", payload: { text: "Paint changed since s09_paint/a1; reload" } });
    expect(editStatus([m, stale], [])).toMatchObject({ phase: "refused", note: "Paint changed since s09_paint/a1; reload" });
    const edit = run("s09_paint", 2, "running", { data: { paint_edit: { message_id: "m1" }, progress: "edit" } });
    expect(editStatus([m], [edit])?.phase).toBe("painting");
    edit.manifest.data = { paint_edit: { message_id: "m1" }, progress: "preview" };
    expect(editStatus([m], [edit])?.phase).toBe("slicing");
    edit.status = "succeeded";
    expect(editStatus([m], [edit])?.phase).toBe("gate");
    const gate = run("s10_write", 2, "running", { data: { paint_edit_message: "m1" } });
    expect(editStatus([m], [edit, gate])?.phase).toBe("gate");
    gate.status = "succeeded";
    expect(editStatus([m], [edit, gate])?.phase).toBe("done");
    expect(editBusy(editStatus([m], [edit, gate]))).toBe(false);
    gate.status = "paused";
    gate.manifest.pause_reason = "slicer gate failed: filament conflict";
    expect(editStatus([m], [edit, gate])?.phase).toBe("review");
    const bad = run("s09_paint", 3, "failed", { data: { paint_edit: { message_id: "m2" } }, error: { code: "validation", message: "op 1 (fill_region): no region 9", retryable: false } });
    expect(editStatus([m, msg("paint_edit", "m2")], [bad])).toMatchObject({ phase: "refused", note: "op 1 (fill_region): no region 9" });
  });

  it("reads the newest preview and its numbers", () => {
    const out = (a: number) => [{ ...ref(`s09_paint/a${a}/paint_preview.json`), role: "paint_preview" }];
    const runs = [
      run("s09_paint", 2, "succeeded", { data: { paint_edit: {} }, outputs: out(2) }),
      run("s09_paint", 3, "failed", { data: { paint_edit: {} }, outputs: out(3) }),
      run("s09_paint", 1, "succeeded", { outputs: [] }),
    ];
    expect(latestPreview(runs)?.path).toBe("s09_paint/a2/paint_preview.json");
    const rows = previewNumbers({
      palette: [], threemf: "x",
      before: { slicer: "p2p_slicer", print_time_s: 100, total_grams: 5, purge_grams: 1 },
      after: { slicer: "p2p_slicer", print_time_s: 90, total_grams: 4.5, purge_grams: null },
    });
    expect(rows.map((r) => [r.key, r.before, r.after])).toEqual([["time", 100, 90], ["grams", 5, 4.5], ["purge", 1, null]]);
  });
});

describe("mesh files", () => {
  it("reads npy int32 and binary PLY in face order", () => {
    const npy = writeNpyInt32([3, 0, 7, -1]);
    expect(Array.from(readNpyInt32(npy))).toEqual([3, 0, 7, -1]);
    const ply = writePly({ positions: Float32Array.from([0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]), indices: Uint32Array.from([0, 1, 2, 0, 2, 3]) });
    const m = readPly(ply);
    expect(Array.from(m.indices)).toEqual([0, 1, 2, 0, 2, 3]);
    expect(Array.from(m.positions.slice(3, 6))).toEqual([1, 0, 0]);
  });

  it("reads the PLY p2p_sim writes (face colors after the indices)", () => {
    const head = "ply\nformat binary_little_endian 1.0\nelement vertex 3\nproperty float x\nproperty float y\nproperty float z\nelement face 1\nproperty list uchar int vertex_indices\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nproperty uchar alpha\nend_header\n";
    const h = new TextEncoder().encode(head);
    const buf = new ArrayBuffer(h.length + 36 + 13 + 4);
    new Uint8Array(buf).set(h);
    const v = new DataView(buf);
    [0, 0, 0, 2, 0, 0, 0, 2, 0].forEach((x, i) => v.setFloat32(h.length + i * 4, x, true));
    let o = h.length + 36;
    v.setUint8(o++, 3);
    [2, 1, 0].forEach((x) => { v.setInt32(o, x, true); o += 4; });
    const m = readPly(buf);
    expect(Array.from(m.indices)).toEqual([2, 1, 0]);
    expect(m.positions[3]).toBe(2);
  });
});
