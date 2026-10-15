import { readFileSync } from "node:fs";
import { join } from "node:path";
import { FaceGrid, faceGeometry } from "./brush";
import { readPly } from "./meshFiles";
import { drawLayer, fitTransform, layerLength, parseToolpaths, type ToolpathIndex } from "./toolpaths";

const PAINT = join(__dirname, "..", "public", "mock", "paint");

describe("brush", () => {
  const mesh = readPly(readFileSync(join(PAINT, "figure.ply")).buffer as ArrayBuffer);
  const geo = faceGeometry(mesh.positions, mesh.indices);
  const grid = new FaceGrid(geo, 2);

  function brute(p: [number, number, number], r: number, view?: [number, number, number]): number[] {
    const out: number[] = [];
    for (let f = 0; f < geo.centroids.length / 3; f++) {
      const d = Math.hypot(geo.centroids[f * 3] - p[0], geo.centroids[f * 3 + 1] - p[1], geo.centroids[f * 3 + 2] - p[2]);
      const facing = !view || geo.normals[f * 3] * view[0] + geo.normals[f * 3 + 1] * view[1] + geo.normals[f * 3 + 2] * view[2] <= 0.1;
      if (d <= r && facing) out.push(f);
    }
    return out;
  }

  it("finds exactly the faces within the radius, through the grid", () => {
    expect(geo.centroids.length / 3).toBe(3128);
    for (const f of [0, 900, 1284, 2500]) {
      const p: [number, number, number] = [geo.centroids[f * 3], geo.centroids[f * 3 + 1], geo.centroids[f * 3 + 2]];
      for (const r of [0.5, 2, 5]) {
        expect(grid.query(p, r).sort((a, b) => a - b)).toEqual(brute(p, r));
      }
    }
  });

  it("keeps faces turned toward the camera only", () => {
    const f = 1284;
    const p: [number, number, number] = [geo.centroids[f * 3], geo.centroids[f * 3 + 1], geo.centroids[f * 3 + 2]];
    const view: [number, number, number] = [0, 1, 0]; // looking from the front (-Y) toward +Y
    const hits = grid.query(p, 6, view);
    expect(hits.sort((a, b) => a - b)).toEqual(brute(p, 6, view));
    expect(hits.every((h) => geo.normals[h * 3 + 1] <= 0.1)).toBe(true);
    expect(hits.length).toBeLessThan(grid.query(p, 6).length);
  });
});

describe("toolpaths", () => {
  const index = JSON.parse(readFileSync(join(PAINT, "edit", "toolpaths.json"), "utf8")) as ToolpathIndex;
  const bin = readFileSync(join(PAINT, "edit", "toolpaths.bin"));
  const t = parseToolpaths(index, bin.buffer.slice(bin.byteOffset, bin.byteOffset + bin.byteLength) as ArrayBuffer);

  it("reads the worker's files: layers in order, chains back to back, the tower framed out", () => {
    expect(t.chains.length / 3).toBe(index.chains.count);
    let next = 0;
    for (const l of index.layers) {
      expect(l.chains[0]).toBe(next);
      next += l.chains[1];
    }
    expect(next).toBe(index.chains.count);
    const [bx0, , bx1] = index.bbox_mm;
    const [mx0, , mx1] = t.modelBox!;
    expect(mx1 - mx0).toBeLessThan(bx1 - bx0); // the prime tower sits beside the model
    expect(layerLength(t, 10)).toBeGreaterThan(10);
    const tf = fitTransform(t, 300, 300);
    expect(tf.x((mx0 - index.origin_mm[0]) / index.scale_mm)).toBeGreaterThanOrEqual(15);
  });

  it("draws a layer in its filaments' colors over the layer below", () => {
    const styles = new Set<string>();
    let strokes = 0;
    const ctx = {
      setTransform() {},
      clearRect() {},
      fillRect() {},
      beginPath() {},
      moveTo() {},
      lineTo() {},
      stroke() {
        strokes++;
        styles.add(this.strokeStyle);
      },
      strokeStyle: "",
      fillStyle: "",
      lineWidth: 0,
      lineCap: "",
      lineJoin: "",
    };
    const i = index.layers.findIndex((l) => l.slots.length > 1);
    drawLayer(ctx as unknown as CanvasRenderingContext2D, t, i, { width: 300, height: 300, under: "under" });
    expect(strokes).toBeGreaterThan(1);
    const hex = new Map(index.slots.map((s) => [s.slot, s.hex]));
    for (const s of index.layers[i].slots) expect(styles.has(hex.get(s)!)).toBe(true);
    expect(styles.has("under")).toBe(true);
  });
});
