import type { AnyMessage, FilamentRow, StageRunRow } from "./db.types";
import { filamentLabel } from "./filamentLabel";
import { paintInfo } from "./paintInfo";
import { parseSpoolSuggestion, spoolSuggestions } from "./spool";

describe("painted preview detection", () => {
  it("accepts COLOR_0 or one untextured material per filament, not a textured Meshy GLB", async () => {
    const THREE = await import("three");
    const { paintedModel } = await import("@/components/ModelViewer");
    const box = () => new THREE.BoxGeometry(1, 1, 1);
    const group = (...meshes: InstanceType<typeof THREE.Mesh>[]) => {
      const g = new THREE.Group();
      meshes.forEach((m) => g.add(m));
      return g;
    };
    const std = (hex: number, map?: InstanceType<typeof THREE.Texture>) => new THREE.MeshStandardMaterial({ color: hex, map: map ?? null });

    const colored = box();
    colored.setAttribute("color", new THREE.BufferAttribute(new Float32Array(colored.attributes.position.count * 3), 3));
    expect(paintedModel(group(new THREE.Mesh(colored, std(0xffffff))))).toBe(true);
    expect(paintedModel(group(new THREE.Mesh(box(), std(0x7c4b00)), new THREE.Mesh(box(), std(0x161616))))).toBe(true);
    expect(paintedModel(group(new THREE.Mesh(box(), std(0xffffff, new THREE.Texture()))))).toBe(false);
    expect(paintedModel(group(new THREE.Mesh(box(), std(0xcccccc))))).toBe(false);
  });
});

describe("filament labels", () => {
  const lib = [{ id: "generic-pla-black", name: "Black", hex: "#161616" }] as FilamentRow[];

  it("uses the library row, else a readable name and nominal color from the id", () => {
    expect(filamentLabel("generic-pla-black", lib)).toEqual({ id: "generic-pla-black", name: "Black", hex: "#161616", known: true });
    expect(filamentLabel("generic-pla-white", [])).toMatchObject({ name: "White", hex: "#FFFFFF", known: false });
    expect(filamentLabel("bambu-pla-basic-red", [])).toMatchObject({ name: "Red", hex: "#F72323" });
    expect(filamentLabel("bambu-pla-matte-ash-gray", [])).toMatchObject({ name: "Ash gray", hex: "#8E8E8C" });
    expect(filamentLabel(null, [], 3)).toMatchObject({ name: "Slot 3", hex: null });
  });
});

describe("spool suggestions", () => {
  it("reads the spool and dE00 from a note", () => {
    expect(parseSpoolSuggestion("Add gray PLA: mid gray maps to brown at dE00 26.9 -> 3.1 with gray")).toEqual({
      spool: "gray PLA",
      de00: 26.9,
      de00After: 3.1,
    });
    expect(parseSpoolSuggestion("Load a white PETG spool; nearest loaded is off by ΔE00 12.4")).toEqual({
      spool: "white PETG",
      de00: 12.4,
      de00After: null,
    });
    expect(parseSpoolSuggestion("Rerunning.")).toBeNull();
    expect(parseSpoolSuggestion("Add-ons not built: plate too small")).toBeNull();
  });

  it("keeps assistant notes only, newest first, one per spool, structured payload first", () => {
    const note = (id: string, payload: object, role = "assistant") =>
      ({ id, kind: "note", role, stage: "s08_palette", payload, body: null }) as unknown as AnyMessage;
    const out = spoolSuggestions([
      note("1", { text: "Add gray PLA: dE00 30.1" }),
      note("2", { text: "Add gray PLA: dE00 26.9 -> 3.1" }),
      note("3", { text: "Add tan PLA", suggest: { spool: "tan PLA", de00: 14.2 } }),
      note("4", { text: "Add pink PLA: dE00 9" }, "user"),
    ]);
    expect(out.map((s) => [s.spool, s.de00, s.de00After])).toEqual([
      ["tan PLA", 14.2, null],
      ["gray PLA", 26.9, 3.1],
    ]);
  });
});

describe("paint info", () => {
  const run = (attempt: number, data: object, extra: Partial<StageRunRow> = {}) =>
    ({
      id: `r${attempt}`,
      stage: "s09_paint",
      attempt,
      status: "succeeded",
      confidence: 0.58,
      manifest: { data, outputs: [{ path: `s09_paint/a${attempt}/checklist.json`, role: "checklist", sha256: "x" }] },
      ...extra,
    }) as unknown as StageRunRow;

  it("reads iterations, checkpoints and the checklist from the latest s09 run", () => {
    const info = paintInfo(
      [
        run(1, { iterations: 8 }),
        run(2, {
          iterations: 3,
          checkpoints: [
            { id: "ckpt_01", note: "First pass", before: "a.png", after: "b.png" },
            { checkpoint_id: "ckpt_02", iteration: 2, ideal: "c.png", sim: "d.png" },
          ],
        }),
      ],
      [],
    )!;
    expect(info.run.attempt).toBe(2);
    expect(info.iterations).toBe(3);
    expect(info.maxIterations).toBe(8);
    expect(info.checklistPath).toBe("s09_paint/a2/checklist.json");
    expect(info.checkpoints).toEqual([
      { id: "ckpt_01", iteration: 1, note: "First pass", before: "a.png", after: "b.png" },
      { id: "ckpt_02", iteration: 2, note: null, before: "c.png", after: "d.png" },
    ]);
  });

  it("falls back to the review request and skips an unrun stage", () => {
    const request = {
      id: "m1",
      kind: "review_request",
      stage: "s09_paint",
      payload: { reason: "Low confidence", confidence: 0.4, checklist: "s09_paint/a1/cl.json", images: [] },
    } as unknown as AnyMessage;
    const bare = { ...run(1, {}), confidence: null, manifest: { data: {} } } as unknown as StageRunRow;
    expect(paintInfo([bare], [request])).toMatchObject({ confidence: 0.4, checklistPath: "s09_paint/a1/cl.json", reason: "Low confidence", iterations: null });
    expect(paintInfo([{ ...bare, status: "skipped" }], [])).toBeNull();
  });
});
