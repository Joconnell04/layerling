import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import type { Mesh } from "three";
import { mockAssetUrl } from "./assets";

const pub = join(__dirname, "..", "..", "..", "public");

function glbJson(file: string) {
  const b = readFileSync(join(pub, "mock", file));
  expect(b.readUInt32LE(0)).toBe(0x46546c67); // "glTF"
  expect(b.readUInt32LE(4)).toBe(2);
  expect(b.readUInt32LE(8)).toBe(b.length);
  const len = b.readUInt32LE(12);
  return { json: JSON.parse(b.subarray(20, 20 + len).toString()), buf: b };
}

describe("mock assets", () => {
  it("maps every payload path the mock worker writes to a committed file", () => {
    const paths = [
      "s03_concept/a1/r1c1.png",
      "s03_concept/a1/r1c2.png",
      "s03_concept/a2/r2c1.png",
      "s03_concept/a2/r2c2.png",
      "s05_mesh/a1/model.glb",
      "s05_mesh/a1/thumb_front.png",
      "s05_mesh/a1/thumb_back.png",
      "s05_mesh/a1/thumb_left.png",
      "s05_mesh/a1/thumb_right.png",
      "s07_baseline/a1/preview.glb",
      "s07_baseline/a1/baseline.3mf",
      "s07_baseline/a1/render_sheet.png",
      "s09_paint/a1/checklist.json",
      "s09_paint/a1/sim_front.png",
      "s09_paint/a1/ckpt_01_ideal.png",
      "s09_paint/a1/ckpt_06_sim.png",
      "s07_baseline/a1/preview_flat.glb",
      "s07_baseline/a1/render_sheet_flat.png",
      "input/photo_1.svg",
      "input/photo_3.svg",
    ];
    for (const p of paths) {
      const url = mockAssetUrl(p);
      expect(existsSync(join(pub, url)), `${p} -> ${url}`).toBe(true);
    }
  });

  it("ships a textured GLB and a per-face color preview GLB", async () => {
    const tex = glbJson("mesh.glb").json;
    expect(tex.materials[0].pbrMetallicRoughness.baseColorTexture).toBeDefined();
    expect(tex.images[0].mimeType).toBe("image/png");

    const { buf, json } = glbJson("preview.glb");
    expect(json.meshes[0].primitives[0].attributes.COLOR_0).toBeDefined();
    const ab = new ArrayBuffer(buf.length); // realm-local ArrayBuffer for the loader instanceof check
    new Uint8Array(ab).set(buf);
    const gltf = await new Promise<{ scene: import("three").Group }>((resolve, reject) =>
      new GLTFLoader().parse(ab, "", resolve as never, reject),
    );
    let colored = 0;
    gltf.scene.traverse((o) => {
      const m = o as Mesh;
      if (m.isMesh && m.geometry.attributes.color) colored += m.geometry.attributes.color.count;
    });
    expect(colored).toBeGreaterThan(1000);
  });

  it("ships a painted flat keychain that the viewer treats as painted and lying flat", async () => {
    const { buf, json } = glbJson("preview-flat.glb");
    expect(json.meshes[0].primitives[0].attributes.COLOR_0).toBeDefined();
    const ab = new ArrayBuffer(buf.length);
    new Uint8Array(ab).set(buf);
    const gltf = await new Promise<{ scene: import("three").Group }>((resolve, reject) =>
      new GLTFLoader().parse(ab, "", resolve as never, reject),
    );
    const { Box3, Vector3 } = await import("three");
    const { paintedModel } = await import("@/components/ModelViewer");
    expect(paintedModel(gltf.scene)).toBe(true);
    const size = new Box3().setFromObject(gltf.scene).getSize(new Vector3());
    // Thickness along glTF Y, 45 mm long: the viewer's "lying flat" rule (Y under 45% of longest).
    expect(size.z).toBeCloseTo(45, 1);
    expect(size.y).toBeLessThan(0.45 * Math.max(size.x, size.z));
    // mockGeometry's keychain bbox matches the file.
    const { mockGeometry } = await import("./client");
    const g = mockGeometry({ product_type: "keychain", form: "flat", target_size_mm: 45 } as never);
    expect(g.bbox_max_mm[0] - g.bbox_min_mm[0]).toBeCloseTo(size.x, 0);
    expect(g.bbox_max_mm[2]).toBeCloseTo(size.y, 0);
  });

  it("ships a 3MF zip with a model part", () => {
    const b = readFileSync(join(pub, "mock", "sample.3mf"));
    expect(b.readUInt32LE(0)).toBe(0x04034b50);
    expect(b.includes(Buffer.from("3D/3dmodel.model"))).toBe(true);
  });
});
