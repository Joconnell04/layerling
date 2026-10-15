// Generate the mock-mode sample assets in public/mock/: a textured GLB (Meshy-like), a preview GLB
// with per-face filament colors, a small 3MF, concept and photo SVGs, thumbnails, a render sheet.
// Everything is procedural so it can be regenerated: npm run gen:mock-assets

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { crc32, deflateRawSync, deflateSync } from "node:zlib";

const here = dirname(fileURLToPath(import.meta.url));
const out = join(here, "..", "public", "mock");
mkdirSync(out, { recursive: true });

// ------------------------------------------------------------------ palette
// Filament slots match the mock result: 1 brown, 2 white, 3 black, 4 red (Jackson's presets).
const FIL = { brown: "#7C4B00", white: "#FFFFFF", black: "#161616", red: "#F72323" };
// Texture tones: what a Meshy texture might look like before quantizing to filaments.
const TEX = { fur: [138, 92, 48], cream: [238, 228, 210], dark: [28, 26, 26], red: [196, 52, 44], base: [214, 210, 204] };

const hexRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
// glTF COLOR_0 is linear; the filament hex values are sRGB.
const srgbToLinear8 = (c8) => {
  const c = c8 / 255;
  return Math.round((c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4) * 255);
};

// ------------------------------------------------------------------ geometry (glTF frame: Y up, +Z front, mm)

function rotZ([x, y, z], deg) {
  const r = (deg * Math.PI) / 180;
  return [x * Math.cos(r) - y * Math.sin(r), x * Math.sin(r) + y * Math.cos(r), z];
}

/** Ellipsoid as a UV sphere. Returns {pos, nrm, uv, idx}. */
function ellipsoid(c, r, lat = 12, lon = 18, tilt = 0) {
  const pos = [], nrm = [], uv = [], idx = [];
  for (let i = 0; i <= lat; i++) {
    const th = (i / lat) * Math.PI;
    for (let j = 0; j <= lon; j++) {
      const ph = (j / lon) * Math.PI * 2;
      const n = [Math.sin(th) * Math.cos(ph), Math.cos(th), Math.sin(th) * Math.sin(ph)];
      let p = [n[0] * r[0], n[1] * r[1], n[2] * r[2]];
      let nn = [n[0] / r[0], n[1] / r[1], n[2] / r[2]];
      if (tilt) {
        p = rotZ(p, tilt);
        nn = rotZ(nn, tilt);
      }
      const l = Math.hypot(...nn);
      pos.push([p[0] + c[0], p[1] + c[1], p[2] + c[2]]);
      nrm.push(nn.map((v) => v / l));
      uv.push([j / lon, i / lat]);
    }
  }
  for (let i = 0; i < lat; i++)
    for (let j = 0; j < lon; j++) {
      const a = i * (lon + 1) + j, b = a + lon + 1;
      if (i !== 0) idx.push([a, a + 1, b]); // counter-clockwise seen from outside
      if (i !== lat - 1) idx.push([a + 1, b + 1, b]);
    }
  return { pos, nrm, uv, idx };
}

function cylinder(c, radius, h, seg = 32) {
  const pos = [], nrm = [], uv = [], idx = [];
  // side
  for (let j = 0; j <= seg; j++) {
    const ph = (j / seg) * Math.PI * 2;
    const n = [Math.cos(ph), 0, Math.sin(ph)];
    for (const y of [0, h]) {
      pos.push([c[0] + n[0] * radius, c[1] + y, c[2] + n[2] * radius]);
      nrm.push(n);
      uv.push([j / seg, y / h]);
    }
  }
  for (let j = 0; j < seg; j++) {
    const a = j * 2;
    idx.push([a, a + 1, a + 2], [a + 1, a + 3, a + 2]);
  }
  // caps
  for (const [y, ny] of [[h, 1], [0, -1]]) {
    const center = pos.length;
    pos.push([c[0], c[1] + y, c[2]]);
    nrm.push([0, ny, 0]);
    uv.push([0.5, 0.5]);
    for (let j = 0; j <= seg; j++) {
      const ph = (j / seg) * Math.PI * 2;
      pos.push([c[0] + Math.cos(ph) * radius, c[1] + y, c[2] + Math.sin(ph) * radius]);
      nrm.push([0, ny, 0]);
      uv.push([0.5 + Math.cos(ph) / 2, 0.5 + Math.sin(ph) / 2]);
    }
    for (let j = 0; j < seg; j++) {
      const a = center + 1 + j;
      idx.push(ny > 0 ? [center, a + 1, a] : [center, a, a + 1]);
    }
  }
  return { pos, nrm, uv, idx };
}

function torus(c, R, r, seg = 24, tube = 8) {
  const pos = [], nrm = [], uv = [], idx = [];
  for (let i = 0; i <= seg; i++) {
    const u = (i / seg) * Math.PI * 2;
    for (let j = 0; j <= tube; j++) {
      const v = (j / tube) * Math.PI * 2;
      const n = [Math.cos(v) * Math.cos(u), Math.sin(v), Math.cos(v) * Math.sin(u)];
      pos.push([c[0] + (R + r * Math.cos(v)) * Math.cos(u), c[1] + r * Math.sin(v), c[2] + (R + r * Math.cos(v)) * Math.sin(u)]);
      nrm.push(n);
      uv.push([i / seg, j / tube]);
    }
  }
  for (let i = 0; i < seg; i++)
    for (let j = 0; j < tube; j++) {
      const a = i * (tube + 1) + j, b = a + tube + 1;
      idx.push([a, a + 1, b], [a + 1, b + 1, b]);
    }
  return { pos, nrm, uv, idx };
}

// Parts: [name, mesh, texture tone, filament(face) function]
const parts = [
  ["base", cylinder([0, 0, 0], 22, 4), TEX.base, () => FIL.white],
  ["body", ellipsoid([0, 16, 0], [11, 12, 9]), TEX.fur, (c) => (c[2] > 4.5 && c[1] < 21 ? FIL.white : FIL.brown)],
  ["leg_l", ellipsoid([-5, 7, 6], [3.2, 5, 3.2], 8, 12), TEX.fur, () => FIL.brown],
  ["leg_r", ellipsoid([5, 7, 6], [3.2, 5, 3.2], 8, 12), TEX.fur, () => FIL.brown],
  ["tail", ellipsoid([0, 15, -9.5], [2, 2, 4.5], 8, 12), TEX.fur, () => FIL.brown],
  ["collar", torus([0, 25, 0.5], 7.2, 1.2), TEX.red, () => FIL.red],
  ["head", ellipsoid([0, 33, 2], [10, 9.5, 9.5]), TEX.fur, () => FIL.brown],
  ["muzzle", ellipsoid([0, 30, 9.5], [5.2, 4.2, 4.2], 10, 14), TEX.cream, () => FIL.white],
  ["nose", ellipsoid([0, 32, 13.4], [1.8, 1.3, 1.2], 6, 10), TEX.dark, () => FIL.black],
  ["eye_l", ellipsoid([-3.8, 36, 9.6], [1.4, 1.5, 1], 6, 10), TEX.dark, () => FIL.black],
  ["eye_r", ellipsoid([3.8, 36, 9.6], [1.4, 1.5, 1], 6, 10), TEX.dark, () => FIL.black],
  ["ear_l", ellipsoid([-9, 35, 0.5], [2.6, 7, 3.6], 8, 12, -25), TEX.fur, () => FIL.brown],
  ["ear_r", ellipsoid([9, 35, 0.5], [2.6, 7, 3.6], 8, 12, 25), TEX.fur, () => FIL.brown],
];

/** Rotate a mesh 90 degrees about X around its center c: a horizontal ring stands up, facing front. */
function standUp(m, c) {
  const rot = ([x, y, z]) => [x, c[1] - (z - c[2]), c[2] + (y - c[1])];
  const nrot = ([x, y, z]) => [x, -z, y];
  return { ...m, pos: m.pos.map(rot), nrm: m.nrm.map(nrot) };
}

// The 3D keychain (form figure): the same dog with no base and a keyring loop on top of the head,
// the ring standing up and facing front, in black (the structure color).
const kc3dParts = [
  ...parts.filter(([name]) => name !== "base"),
  ["loop", standUp(torus([0, 46.2, 1], 3.4, 1.15, 28, 10), [0, 46.2, 1]), TEX.dark, () => FIL.black],
];

// ------------------------------------------------------------------ texture atlas (one cell per part)

const CELL = 64;
const ATLAS_H = CELL;

function noise(x, y, seed) {
  const s = Math.sin(x * 12.9898 + y * 78.233 + seed * 37.719) * 43758.5453;
  return s - Math.floor(s);
}

function atlasPixels(list) {
  const ATLAS_W = CELL * list.length;
  const px = new Uint8Array(ATLAS_W * ATLAS_H * 3);
  list.forEach(([name, , tone], k) => {
    for (let y = 0; y < CELL; y++)
      for (let x = 0; x < CELL; x++) {
        const streak = name === "base" ? 0 : Math.sin((x + noise(0, y, k) * 6) * 0.9) * 0.06;
        const grain = (noise(x, y, k) - 0.5) * 0.12;
        const shade = 1 + streak + grain - (y / CELL) * 0.12;
        const i = (y * ATLAS_W + k * CELL + x) * 3;
        for (let ch = 0; ch < 3; ch++) px[i + ch] = Math.max(0, Math.min(255, Math.round(tone[ch] * shade)));
      }
  });
  return px;
}

function png(w, h, rgb) {
  const raw = Buffer.alloc((w * 3 + 1) * h);
  for (let y = 0; y < h; y++) {
    raw[y * (w * 3 + 1)] = 0;
    Buffer.from(rgb.buffer, y * w * 3, w * 3).copy(raw, y * (w * 3 + 1) + 1);
  }
  const chunk = (type, data) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length);
    const td = Buffer.concat([Buffer.from(type), data]);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(td) >>> 0);
    return Buffer.concat([len, td, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0);
  ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 2; // RGB
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

// ------------------------------------------------------------------ GLB writer

function glb(json, bin) {
  const pad = (buf, byte) => Buffer.concat([buf, Buffer.alloc((4 - (buf.length % 4)) % 4, byte)]);
  const jsonBuf = pad(Buffer.from(JSON.stringify(json)), 0x20);
  const binBuf = pad(bin, 0);
  const header = Buffer.alloc(12);
  header.writeUInt32LE(0x46546c67, 0);
  header.writeUInt32LE(2, 4);
  header.writeUInt32LE(12 + 8 + jsonBuf.length + 8 + binBuf.length, 8);
  const ch = (buf, type) => {
    const h = Buffer.alloc(8);
    h.writeUInt32LE(buf.length, 0);
    h.writeUInt32LE(type, 4);
    return Buffer.concat([h, buf]);
  };
  return Buffer.concat([header, ch(jsonBuf, 0x4e4f534a), ch(binBuf, 0x004e4942)]);
}

class Bin {
  constructor() {
    this.parts = [];
    this.length = 0;
    this.views = [];
  }
  add(buf, target) {
    const padLen = (4 - (this.length % 4)) % 4;
    if (padLen) {
      this.parts.push(Buffer.alloc(padLen));
      this.length += padLen;
    }
    const view = { buffer: 0, byteOffset: this.length, byteLength: buf.length };
    if (target) view.target = target;
    this.views.push(view);
    this.parts.push(buf);
    this.length += buf.length;
    return this.views.length - 1;
  }
  buffer() {
    return Buffer.concat(this.parts);
  }
}

function minmax(vs) {
  const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
  for (const v of vs) for (let i = 0; i < 3; i++) {
    min[i] = Math.min(min[i], v[i]);
    max[i] = Math.max(max[i], v[i]);
  }
  return { min, max };
}

const f32 = (arr) => Buffer.from(new Float32Array(arr.flat()).buffer);

function texturedGlb(list = parts) {
  const pos = [], nrm = [], uv = [], idx = [];
  list.forEach(([, m], k) => {
    const base = pos.length;
    pos.push(...m.pos);
    nrm.push(...m.nrm);
    uv.push(...m.uv.map(([u, v]) => [(k + 0.08 + u * 0.84) / list.length, 0.08 + v * 0.84]));
    idx.push(...m.idx.map((t) => t.map((i) => i + base)));
  });
  const bin = new Bin();
  const vPos = bin.add(f32(pos), 34962);
  const vNrm = bin.add(f32(nrm), 34962);
  const vUv = bin.add(f32(uv), 34962);
  const vIdx = bin.add(Buffer.from(new Uint16Array(idx.flat()).buffer), 34963);
  const vImg = bin.add(png(CELL * list.length, ATLAS_H, atlasPixels(list)));
  const json = {
    asset: { version: "2.0", generator: "p2p-web gen-mock-assets" },
    scene: 0,
    scenes: [{ nodes: [0] }],
    nodes: [{ mesh: 0, name: "figurine" }],
    meshes: [{ primitives: [{ attributes: { POSITION: 0, NORMAL: 1, TEXCOORD_0: 2 }, indices: 3, material: 0 }] }],
    materials: [{ name: "texture", pbrMetallicRoughness: { baseColorTexture: { index: 0 }, metallicFactor: 0, roughnessFactor: 0.85 } }],
    textures: [{ source: 0, sampler: 0 }],
    samplers: [{ magFilter: 9729, minFilter: 9987, wrapS: 33071, wrapT: 33071 }],
    images: [{ bufferView: vImg, mimeType: "image/png" }],
    accessors: [
      { bufferView: vPos, componentType: 5126, count: pos.length, type: "VEC3", ...minmax(pos) },
      { bufferView: vNrm, componentType: 5126, count: nrm.length, type: "VEC3" },
      { bufferView: vUv, componentType: 5126, count: uv.length, type: "VEC2" },
      { bufferView: vIdx, componentType: 5123, count: idx.length * 3, type: "SCALAR" },
    ],
    bufferViews: bin.views,
    buffers: [{ byteLength: 0 }],
  };
  const b = bin.buffer();
  json.buffers[0].byteLength = b.length + ((4 - (b.length % 4)) % 4);
  return { buf: glb(json, b), tris: idx.length };
}

/** Flat triangles with one filament color per face (the worker's preview GLB shape). */
function faceTriangles(list = parts) {
  const tris = [];
  for (const [, m, , fil] of list) {
    for (const t of m.idx) {
      const v = t.map((i) => m.pos[i]);
      const c = [0, 1, 2].map((a) => (v[0][a] + v[1][a] + v[2][a]) / 3);
      tris.push({ v, color: fil(c) });
    }
  }
  return tris;
}

function previewGlb() {
  return colorGlb(faceTriangles(), "figurine_filaments");
}

/** Non-indexed triangles {v, color} -> GLB with POSITION + linear COLOR_0 and one white material. */
function colorGlb(tris, nodeName) {
  const pos = [], col = [];
  for (const t of tris) {
    const [r, g, b] = hexRgb(t.color).map(srgbToLinear8);
    for (const v of t.v) {
      pos.push(v);
      col.push([r, g, b, 255]);
    }
  }
  const bin = new Bin();
  const vPos = bin.add(f32(pos), 34962);
  const vCol = bin.add(Buffer.from(new Uint8Array(col.flat()).buffer), 34962);
  const json = {
    asset: { version: "2.0", generator: "p2p-web gen-mock-assets" },
    scene: 0,
    scenes: [{ nodes: [0] }],
    nodes: [{ mesh: 0, name: nodeName }],
    meshes: [{ primitives: [{ attributes: { POSITION: 0, COLOR_0: 1 }, material: 0 }] }],
    materials: [{ name: "filament", pbrMetallicRoughness: { baseColorFactor: [1, 1, 1, 1], metallicFactor: 0, roughnessFactor: 0.9 } }],
    accessors: [
      { bufferView: vPos, componentType: 5126, count: pos.length, type: "VEC3", ...minmax(pos) },
      { bufferView: vCol, componentType: 5121, normalized: true, count: col.length, type: "VEC4" },
    ],
    bufferViews: bin.views,
    buffers: [{ byteLength: 0 }],
  };
  const b = bin.buffer();
  json.buffers[0].byteLength = b.length + ((4 - (b.length % 4)) % 4);
  return { buf: glb(json, b), tris: tris.length };
}

// ------------------------------------------------------------------ flat keychain (preview-flat.glb)
// A painted relief keychain as it lies on the bed: a 1.6 mm black back, colored domes on top and a
// black loop above the head. Drawn in its own plane first (u = right, v = toward the head, mm), then
// placed in glTF: X = u, Y = thickness up from the bed at 0, Z = -v (head toward -Z, face up).

const KC = {
  cell: 0.5, // height field grid, mm
  slab: 1.6, // black back
  length: 45, // paws to top of loop
  // Silhouette domes: [u, v, radius u, radius v, degrees CCW, dome height, profile exponent]
  // Height = dome height * sqrt(1 - d^exponent); the head's flatter profile keeps it above the body.
  domes: {
    earL: [-7.5, 27.2, 2.41, 7.27, -18, 2.8, 2],
    earR: [7.5, 27.2, 2.41, 7.27, 18, 2.8, 2],
    body: [0, 12.79, 9.23, 12.54, 0, 4, 2],
    pawL: [-4.41, 2.76, 2.81, 2.76, 0, 3.4, 2],
    pawR: [4.41, 2.76, 2.81, 2.76, 0, 3.4, 2],
    head: [0, 28.47, 8.63, 10.03, 0, 5.3, 4],
  },
  // Raised details added on top of the domes.
  bumps: {
    muzzle: [0, 24.2, 4.21, 4.01, 0, 0.8, 2],
    nose: [0, 26.2, 1.3, 1, 0, 0.4, 2],
    eyeL: [-3.3, 30.3, 1.3, 1.3, 0, 0.3, 2],
    eyeR: [3.3, 30.3, 1.3, 1.3, 0, 0.3, 2],
  },
  belly: [0, 10.5, 4.8, 5.6, 0],
  collar: { from: [-6.62, 20.57], ctrl: [0, 16.55], to: [6.62, 20.57], half: 0.88 },
  loop: { R: 2.9, r: 1.1 }, // 8 mm outer diameter, resting on the bed
};
const KC_DOMES = Object.values(KC.domes);
const KC_BUMPS = Object.values(KC.bumps);

/** Squared normalized radius of (u, v) in ellipse e, and its gradient. */
function ellipseField([cu, cv, ru, rv, deg], u, v) {
  const t = (deg * Math.PI) / 180, c = Math.cos(t), s = Math.sin(t);
  const du = u - cu, dv = v - cv;
  const x = du * c + dv * s, y = dv * c - du * s;
  const fx = (2 * x) / (ru * ru), fy = (2 * y) / (rv * rv);
  return [(x * x) / (ru * ru) + (y * y) / (rv * rv), fx * c - fy * s, fx * s + fy * c];
}
const inEllipse = (e, u, v) => ellipseField(e, u, v)[0] < 1;
const dome = (e, u, v) => {
  const d2 = ellipseField(e, u, v)[0];
  return d2 < 1 ? e[5] * Math.sqrt(1 - d2 ** (e[6] / 2)) : 0;
};
const kcInside = (u, v) => KC_DOMES.some((e) => inEllipse(e, u, v));

/** Top surface height: back slab + the tallest dome + raised details. */
function kcHeight(u, v) {
  let h = 0;
  for (const e of KC_DOMES) h = Math.max(h, dome(e, u, v));
  for (const e of KC_BUMPS) h += dome(e, u, v);
  return KC.slab + h;
}

const collarLine = Array.from({ length: 65 }, (_, i) => {
  const t = i / 64, { from, ctrl, to } = KC.collar;
  const w = [(1 - t) ** 2, 2 * (1 - t) * t, t * t];
  return [0, 1].map((k) => w[0] * from[k] + w[1] * ctrl[k] + w[2] * to[k]);
});
function collarDist(u, v) {
  let best = Infinity;
  for (let i = 1; i < collarLine.length; i++) {
    const [ax, ay] = collarLine[i - 1], [bx, by] = collarLine[i];
    const dx = bx - ax, dy = by - ay;
    const t = Math.max(0, Math.min(1, ((u - ax) * dx + (v - ay) * dy) / (dx * dx + dy * dy)));
    best = Math.min(best, Math.hypot(u - ax - t * dx, v - ay - t * dy));
  }
  return best;
}

/** Top color by priority: eyes and nose, then muzzle and belly, then collar, then fur. */
function kcColor(u, v) {
  const { eyeL, eyeR, nose, muzzle } = KC.bumps;
  if ([eyeL, eyeR, nose].some((e) => inEllipse(e, u, v))) return FIL.black;
  if (inEllipse(muzzle, u, v) || inEllipse(KC.belly, u, v)) return FIL.white;
  if (collarDist(u, v) < KC.collar.half) return FIL.red;
  return FIL.brown;
}

/** Newton steps onto the edge of ellipse e. */
function toEllipseEdge(e, [u, v]) {
  for (let it = 0; it < 6; it++) {
    const [d2, gu, gv] = ellipseField(e, u, v), g2 = gu * gu + gv * gv;
    u -= ((d2 - 1) * gu) / g2;
    v -= ((d2 - 1) * gv) / g2;
  }
  return [u, v];
}

/** Newton steps onto the nearest silhouette ellipse edge. */
function toSilhouetteEdge([u, v]) {
  for (let it = 0; it < 6; it++) {
    let best = null;
    for (const e of KC_DOMES) {
      const [d2, gu, gv] = ellipseField(e, u, v);
      const g = Math.hypot(gu, gv);
      if (g < 1e-9) continue;
      const sd = (d2 - 1) / g;
      if (!best || sd < best[0]) best = [sd, gu / g, gv / g];
    }
    u -= best[0] * best[1];
    v -= best[0] * best[2];
  }
  return [u, v];
}

function flatKeychain() {
  const { cell: c, slab, length } = KC;
  const U0 = -12, V0 = -1, NU = 48, NV = 82; // grid covers u -12..12, v -1..40
  const W = NU + 1;
  const mask = new Uint8Array(NU * NV);
  for (let j = 0; j < NV; j++)
    for (let i = 0; i < NU; i++) mask[j * NU + i] = kcInside(U0 + (i + 0.5) * c, V0 + (j + 0.5) * c) ? 1 : 0;
  const inside = (i, j) => i >= 0 && j >= 0 && i < NU && j < NV && mask[j * NU + i] === 1;
  const grid = [];
  for (let j = 0; j <= NV; j++) for (let i = 0; i <= NU; i++) grid.push([U0 + i * c, V0 + j * c]);

  // Inside cells: corners a b cc d counter-clockwise, triangles (a b cc) and (a cc d).
  // Boundary edges keep the cell on their left: [from, to, cell, triangle].
  const cells = [], edges = [];
  for (let j = 0; j < NV; j++)
    for (let i = 0; i < NU; i++) {
      if (!inside(i, j)) continue;
      const a = j * W + i, b = a + 1, cc = a + W + 1, d = a + W, k = cells.length;
      cells.push([a, b, cc, d]);
      if (!inside(i, j - 1)) edges.push([a, b, k, 0]);
      if (!inside(i + 1, j)) edges.push([b, cc, k, 0]);
      if (!inside(i, j + 1)) edges.push([cc, d, k, 1]);
      if (!inside(i - 1, j)) edges.push([d, a, k, 1]);
    }
  const tri = ([a, b, cc, d], t) => (t ? [a, cc, d] : [a, b, cc]);

  // Smooth the stair-stepped outline: snap outline vertices onto the silhouette, then put back any
  // vertex of a triangle that the snap folded over.
  const P = grid.map((p) => [...p]);
  for (const k of new Set(edges.flatMap(([p, q]) => [p, q]))) {
    const [u, v] = toSilhouetteEdge(grid[k]);
    const du = u - grid[k][0], dv = v - grid[k][1], m = Math.hypot(du, dv), lim = 0.9 * c;
    P[k] = m > lim ? [grid[k][0] + (du * lim) / m, grid[k][1] + (dv * lim) / m] : [u, v];
  }
  const area2 = ([a, b, cc]) =>
    (P[b][0] - P[a][0]) * (P[cc][1] - P[a][1]) - (P[cc][0] - P[a][0]) * (P[b][1] - P[a][1]);
  for (let folded = true; folded; ) {
    folded = false;
    for (const q of cells)
      for (const t of [0, 1]) {
        const T = tri(q, t);
        if (area2(T) >= 0) continue;
        for (const k of T) P[k] = [...grid[k]];
        folded = true;
      }
  }
  const H = P.map(([u, v]) => kcHeight(u, v));

  // Frame: paws at z = +length/2, top of the loop at z = -length/2, X centered.
  let umin = Infinity, umax = -Infinity, vmin = Infinity, vmax = -Infinity;
  for (const k of new Set(cells.flat())) {
    umin = Math.min(umin, P[k][0]);
    umax = Math.max(umax, P[k][0]);
    vmin = Math.min(vmin, P[k][1]);
    vmax = Math.max(vmax, P[k][1]);
  }
  const { R, r } = KC.loop;
  const ux = (umin + umax) / 2;
  const pt = (k, h) => [P[k][0] - ux, h, length / 2 - (P[k][1] - vmin)].map(Math.fround);

  const tris = [], topColor = [];
  cells.forEach((q, n) => {
    for (const t of [0, 1]) {
      const T = tri(q, t);
      const color = kcColor((P[T[0]][0] + P[T[1]][0] + P[T[2]][0]) / 3, (P[T[0]][1] + P[T[1]][1] + P[T[2]][1]) / 3);
      topColor[n * 2 + t] = color;
      if (area2(T) > 1e-9) tris.push({ v: T.map((k) => pt(k, H[k])), color });
    }
  });
  // Side walls, outward: black up to the slab top, then the color of the top triangle above.
  const wall = (p, q, lo, hp, hq, color) => {
    if (hq - lo > 1e-6) tris.push({ v: [pt(p, lo), pt(q, lo), pt(q, hq)], color });
    if (hp - lo > 1e-6) tris.push({ v: [pt(p, lo), pt(q, hq), pt(p, hp)], color });
  };
  for (const [p, q, n, t] of edges) {
    if (Math.hypot(P[q][0] - P[p][0], P[q][1] - P[p][1]) < 1e-6) continue;
    wall(p, q, 0, slab, slab, FIL.black);
    wall(p, q, slab, H[p], H[q], topColor[n * 2 + t]);
  }
  for (const q of cells)
    for (const t of [0, 1]) {
      const T = tri(q, t);
      if (area2(T) > 1e-9) tris.push({ v: [T[0], T[2], T[1]].map((k) => pt(k, 0)), color: FIL.black });
    }
  // Keyring loop lying flat on the bed; its outer edge overlaps the top of the head.
  const ring = torus([-ux, r, length / 2 - (length - R - r)], R, r, 32, 12);
  for (const t of ring.idx) tris.push({ v: t.map((i) => ring.pos[i].map(Math.fround)), color: FIL.black });

  const box = minmax(tris.flatMap((t) => t.v));
  return {
    tris,
    size: [0, 1, 2].map((a) => box.max[a] - box.min[a]),
    loopV: vmin + length - R - r, // loop center in the keychain plane
    vmin,
    headTop: vmax,
  };
}

// ------------------------------------------------------------------ 3MF (core spec, basematerials per triangle)

function zip(files) {
  const locals = [], centrals = [];
  let offset = 0;
  for (const [name, data] of files) {
    const nameBuf = Buffer.from(name);
    const comp = deflateRawSync(data, { level: 9 });
    const crc = crc32(data) >>> 0;
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0, 6);
    local.writeUInt16LE(8, 8);
    local.writeUInt16LE(0, 10);
    local.writeUInt16LE(0x21, 12);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(comp.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBuf.length, 26);
    local.writeUInt16LE(0, 28);
    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(0, 8);
    central.writeUInt16LE(8, 10);
    central.writeUInt16LE(0, 12);
    central.writeUInt16LE(0x21, 14);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(comp.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(nameBuf.length, 28);
    central.writeUInt32LE(offset, 42);
    locals.push(local, nameBuf, comp);
    centrals.push(central, nameBuf);
    offset += 30 + nameBuf.length + comp.length;
  }
  const cd = Buffer.concat(centrals);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(files.length, 8);
  end.writeUInt16LE(files.length, 10);
  end.writeUInt32LE(cd.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, cd, end]);
}

function threeMf() {
  const tris = faceTriangles();
  const palette = [FIL.brown, FIL.white, FIL.black, FIL.red];
  const verts = [], tri = [];
  const key = new Map();
  const vid = ([x, y, z]) => {
    const p = [x, -z, y]; // glTF (Y up, +Z front) -> contract frame (Z up, -Y front)
    const k = p.map((n) => n.toFixed(3)).join(",");
    if (!key.has(k)) {
      key.set(k, verts.length);
      verts.push(p);
    }
    return key.get(k);
  };
  for (const t of tris) tri.push([...t.v.map(vid), palette.indexOf(t.color)]);
  const xml = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">',
    ' <metadata name="Title">photo2print mock sample</metadata>',
    " <resources>",
    '  <basematerials id="1">',
    ...palette.map((c, i) => `   <base name="slot${i + 1}" displaycolor="${c}FF"/>`),
    "  </basematerials>",
    '  <object id="2" type="model" pid="1" pindex="0">',
    "   <mesh>",
    "    <vertices>",
    ...verts.map((v) => `     <vertex x="${v[0].toFixed(3)}" y="${v[1].toFixed(3)}" z="${v[2].toFixed(3)}"/>`),
    "    </vertices>",
    "    <triangles>",
    ...tri.map(([a, b, c, p]) => `     <triangle v1="${a}" v2="${b}" v3="${c}" pid="1" p1="${p}"/>`),
    "    </triangles>",
    "   </mesh>",
    "  </object>",
    " </resources>",
    ' <build><item objectid="2"/></build>',
    "</model>",
  ].join("\n");
  return zip([
    [
      "[Content_Types].xml",
      Buffer.from(
        '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>',
      ),
    ],
    [
      "_rels/.rels",
      Buffer.from(
        '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>',
      ),
    ],
    ["3D/3dmodel.model", Buffer.from(xml)],
  ]);
}

// ------------------------------------------------------------------ SVGs

/** A front view of the figurine drawn with simple shapes. */
function figure({ fur, belly, dark, accent, headScale = 1, tilt = 0, shade = true, noLeftEye = false, base = true }) {
  const hs = headScale;
  const s = shade
    ? `<radialGradient id="g" cx="40%" cy="35%" r="70%"><stop offset="0" stop-color="#fff" stop-opacity=".22"/><stop offset="1" stop-color="#000" stop-opacity=".18"/></radialGradient>`
    : "";
  const over = (shape) => (shade ? shape.replace("/>", ' fill="url(#g)"/>').replace(/fill="[^"]+" fill=/, "fill=") : "");
  const body = `<ellipse cx="256" cy="330" rx="92" ry="100" fill="${fur}"/>`;
  const head = `<ellipse cx="256" cy="${205 - (hs - 1) * 30}" rx="${86 * hs}" ry="${80 * hs}" fill="${fur}"/>`;
  const hy = 205 - (hs - 1) * 30;
  return `<defs>${s}</defs>
  ${base ? `<ellipse cx="256" cy="440" rx="170" ry="34" fill="${belly}"/>
  <ellipse cx="256" cy="430" rx="170" ry="30" fill="${belly}" stroke="#0002"/>` : ""}
  <g transform="rotate(${tilt} 256 330)">
  ${body}${over(body)}
  <ellipse cx="256" cy="360" rx="52" ry="62" fill="${belly}"/>
  <ellipse cx="212" cy="410" rx="28" ry="22" fill="${fur}"/>
  <ellipse cx="300" cy="410" rx="28" ry="22" fill="${fur}"/>
  <path d="M190 268 Q256 300 322 268" stroke="${accent}" stroke-width="14" fill="none" stroke-linecap="round"/>
  <ellipse cx="${256 - 78 * hs}" cy="${hy + 10}" rx="${24 * hs}" ry="${58 * hs}" fill="${fur}" transform="rotate(18 ${256 - 78 * hs} ${hy})"/>
  <ellipse cx="${256 + 78 * hs}" cy="${hy + 10}" rx="${24 * hs}" ry="${58 * hs}" fill="${fur}" transform="rotate(-18 ${256 + 78 * hs} ${hy})"/>
  ${head}${over(head)}
  <ellipse cx="256" cy="${hy + 34 * hs}" rx="${42 * hs}" ry="${32 * hs}" fill="${belly}"/>
  <ellipse cx="256" cy="${hy + 18 * hs}" rx="${12 * hs}" ry="${9 * hs}" fill="${dark}"/>
  <circle cx="${256 - 32 * hs}" cy="${hy - 14 * hs}" r="${10 * hs}" fill="${noLeftEye ? fur : dark}"/>
  <circle cx="${256 + 32 * hs}" cy="${hy - 14 * hs}" r="${10 * hs}" fill="${dark}"/>
  ${noLeftEye ? "" : `<circle cx="${256 - 29 * hs}" cy="${hy - 17 * hs}" r="${3 * hs}" fill="#fff"/>`}
  <circle cx="${256 + 35 * hs}" cy="${hy - 17 * hs}" r="${3 * hs}" fill="#fff"/>
  </g>`;
}

const svg = (w, h, body, bg) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}">${bg ? `<rect width="${w}" height="${h}" fill="${bg}"/>` : ""}${body}</svg>\n`;

const concepts = [
  { fur: "#8A5A30", belly: "#EFE6D6", dark: "#1C1A1A", accent: "#C4342C", headScale: 1 },
  { fur: "#7A4C22", belly: "#F3EBDD", dark: "#161414", accent: "#C4342C", headScale: 1.2 },
  { fur: "#94623A", belly: "#EDE3D1", dark: "#1C1A1A", accent: "#B8322A", headScale: 1.05, tilt: -4 },
  { fur: "#A57246", belly: "#F5EEE2", dark: "#221E1E", accent: "#C63B32", headScale: 1.25, tilt: 3 },
];
concepts.forEach((c, i) => writeFileSync(join(out, `concept-${i + 1}.svg`), svg(512, 512, figure(c), "#E7E5E1")));

// Product concepts (s03 `image`, what the customer approves) and the figure alone (`figure_image`,
// the 3D input), per product and form. Loops, plates and names are drawn the way s03 draws them in
// code; the flat charm is the figure on a black backing cut to its outline.
const headTop = (hs) => 205 - (hs - 1) * 30 - 80 * hs;
const ring = (cx, cy, r, w = 10) =>
  `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="#1C1A1A" stroke-width="${w}"/><circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="#fff" stroke-opacity=".18" stroke-width="2"/>`;
/** The figure's outline grown by `grow` px, as one filled shape (the flat charm's backing). */
function backing(c, grow, fill) {
  const hs = c.headScale ?? 1;
  const hy = 205 - (hs - 1) * 30;
  const e = (cx, cy, rx, ry, rot = 0) =>
    `<ellipse cx="${cx}" cy="${cy}" rx="${rx + grow}" ry="${ry + grow}"${rot ? ` transform="rotate(${rot} ${cx} ${hy})"` : ""}/>`;
  return `<g fill="${fill}" transform="rotate(${c.tilt ?? 0} 256 330)">${[
    e(256, 330, 92, 100),
    e(212, 410, 28, 22),
    e(300, 410, 28, 22),
    e(256, hy, 86 * hs, 80 * hs),
    e(256 - 78 * hs, hy + 10, 24 * hs, 58 * hs, 18),
    e(256 + 78 * hs, hy + 10, 24 * hs, 58 * hs, -18),
  ].join("")}</g>`;
}
const BG = "#E7E5E1";
concepts.forEach((c, i) => {
  const n = i + 1;
  const hs = c.headScale ?? 1;
  const fig = figure({ ...c, base: false });
  const top = headTop(hs);
  writeFileSync(join(out, `figure-${n}.svg`), svg(512, 512, fig, BG));
  // 3D keychain: the figure with a keyring loop standing on top of the head.
  writeFileSync(
    join(out, `kc3d-${n}.svg`),
    svg(512, 512, `<g transform="translate(26 40) scale(0.9)">${fig}${ring(256, top - 20, 22)}</g>`, BG),
  );
  // Flat charm: a relief of the front view on a black backing, loop at the top edge.
  const charm = `${backing(c, 14, "#161616")}${figure({ ...c, base: false, shade: false })}`;
  writeFileSync(
    join(out, `charm-${n}.svg`),
    svg(512, 512, `<g transform="translate(36 52) scale(0.86)">${ring(256, top - 30, 24, 12)}${charm}</g>`, BG),
  );
  writeFileSync(join(out, `relief-${n}.svg`), svg(512, 512, `<g transform="translate(36 30) scale(0.86)">${charm}</g>`, BG));
  // Ornament: a small hanging loop.
  writeFileSync(
    join(out, `ornament-${n}.svg`),
    svg(512, 512, `<g transform="translate(26 40) scale(0.9)">${fig}${ring(256, top - 14, 14, 7)}</g>`, BG),
  );
  // Nameplate: the figure sitting on one end of a rounded plate with the name raised on it.
  writeFileSync(
    join(out, `plate-${n}.svg`),
    svg(
      512,
      512,
      `<rect x="28" y="352" width="456" height="92" rx="22" fill="#2B2724"/><rect x="28" y="344" width="456" height="92" rx="22" fill="#3A3430"/>` +
        `<text x="186" y="408" text-anchor="middle" font-family="Georgia, serif" font-weight="700" font-size="50" letter-spacing="3" fill="#F3EBDD">PEPPER</text>` +
        `<g transform="translate(250 112) scale(0.55)">${fig}</g>`,
      BG,
    ),
  );
});

const photos = [
  // dog on grass
  svg(640, 480, `<defs><linearGradient id="s" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#9EC2DE"/><stop offset=".55" stop-color="#CFE0EA"/><stop offset=".56" stop-color="#6E8F3E"/><stop offset="1" stop-color="#4E6B2A"/></linearGradient></defs><rect width="640" height="480" fill="url(#s)"/><g transform="translate(70 20) scale(0.95)">${figure({ ...concepts[0], shade: false })}</g>`),
  // person portrait
  svg(640, 480, `<rect width="640" height="480" fill="#C9B8A6"/><rect y="330" width="640" height="150" fill="#8E7A66"/><circle cx="320" cy="190" r="86" fill="#E2B794"/><path d="M234 175 Q240 90 320 92 Q402 92 406 175 Q380 128 320 130 Q262 130 234 175Z" fill="#3B2A1E"/><circle cx="290" cy="195" r="9" fill="#2A211C"/><circle cx="350" cy="195" r="9" fill="#2A211C"/><path d="M296 236 Q320 252 344 236" stroke="#8C4A3C" stroke-width="6" fill="none" stroke-linecap="round"/><path d="M180 480 Q190 300 320 290 Q450 300 460 480Z" fill="#35577A"/>`),
  // pet on a sofa
  svg(640, 480, `<rect width="640" height="480" fill="#50443C"/><rect x="40" y="250" width="560" height="200" rx="40" fill="#7B6A5A"/><g transform="translate(90 0) scale(0.9)">${figure({ ...concepts[3], shade: false })}</g>`),
];
photos.forEach((p, i) => writeFileSync(join(out, `photo-${i + 1}.svg`), p));

const grey = { fur: "#9A9A9A", belly: "#B8B8B8", dark: "#6A6A6A", accent: "#8A8A8A" };
const views = { front: 0, right: 12, back: 0, left: -12 };
for (const [name, tilt] of Object.entries(views)) {
  const body =
    name === "back"
      ? `<ellipse cx="256" cy="430" rx="170" ry="30" fill="${grey.belly}"/><ellipse cx="256" cy="330" rx="92" ry="100" fill="${grey.fur}"/><ellipse cx="256" cy="205" rx="86" ry="80" fill="${grey.fur}"/><ellipse cx="256" cy="360" rx="16" ry="44" fill="#8A8A8A"/>`
      : figure({ ...grey, tilt, headScale: 1 });
  writeFileSync(join(out, `thumb-${name}.svg`), svg(512, 512, body, null));
}

// Render sheet: 3 x 2 views in filament colors, flat.
const flat = { fur: FIL.brown, belly: FIL.white, dark: FIL.black, accent: FIL.red, shade: false };
const cells = [
  ["front", 0, 1],
  ["front_3q", -8, 1.05],
  ["left", -14, 1],
  ["right", 14, 1],
  ["back", 0, 1],
  ["top", 0, 1.2],
];
const sheet = cells
  .map(([, tilt, hs], i) => {
    const x = (i % 3) * 400, y = Math.floor(i / 3) * 400;
    return `<g transform="translate(${x} ${y})"><rect width="398" height="398" fill="#FFFDF8" stroke="#D8CFBF"/><g transform="translate(8 0) scale(0.75)">${figure({ ...flat, tilt, headScale: hs })}</g></g>`;
  })
  .join("");
writeFileSync(join(out, "render-sheet.svg"), svg(1198, 798, sheet, "#F3EEE4"));

// Flat keychain render sheet, same layout. Views are drawn in the keychain plane (u right, v toward
// the head, mm) and projected per height with an SVG matrix. Solid views stack horizontal slices
// bottom to top (the correct paint order for any view from above), then draw the colored details
// as outlines lifted onto the surface.
const kc = flatKeychain();
const n3 = (x) => +x.toFixed(3);
const n2 = (x) => +x.toFixed(2);
const kcEllipse = ([cu, cv, ru, rv, deg]) =>
  `<ellipse cx="${n3(cu)}" cy="${n3(cv)}" rx="${n3(ru)}" ry="${n3(rv)}"${deg ? ` transform="rotate(${deg} ${n3(cu)} ${n3(cv)})"` : ""}/>`;
/** Points around ellipse e scaled by f, in the keychain plane. */
const ellipsePoints = ([cu, cv, ru, rv, deg], f = 1, n = 72) => {
  const t = (deg * Math.PI) / 180;
  return Array.from({ length: n }, (_, i) => {
    const w = (i / n) * 2 * Math.PI, x = ru * f * Math.cos(w), y = rv * f * Math.sin(w);
    return [cu + x * Math.cos(t) - y * Math.sin(t), cv + x * Math.sin(t) + y * Math.cos(t)];
  });
};
/** Outline of the collar band (round ends), kept inside the body outline. */
function collarOutline() {
  const L = collarLine, n = L.length, half = KC.collar.half;
  const frame = (i) => {
    const a = L[Math.max(0, i - 1)], b = L[Math.min(n - 1, i + 1)], l = Math.hypot(b[0] - a[0], b[1] - a[1]);
    const t = [(b[0] - a[0]) / l, (b[1] - a[1]) / l];
    return [t, [-t[1], t[0]]];
  };
  const at = (p, [t, nn], ct, cn) => [p[0] + half * (ct * t[0] + cn * nn[0]), p[1] + half * (ct * t[1] + cn * nn[1])];
  const cap = (i, dir) =>
    Array.from({ length: 13 }, (_, k) => {
      const w = (k / 12) * Math.PI;
      return at(L[i], frame(i), dir * Math.sin(w), dir * Math.cos(w));
    });
  const pts = [
    ...L.map((p, i) => at(p, frame(i), 0, 1)),
    ...cap(n - 1, 1),
    ...L.map((p, i) => at(p, frame(i), 0, -1)).reverse(),
    ...cap(0, -1),
  ];
  return pts.map((p) => (kcInside(p[0], p[1]) ? p : toEllipseEdge(KC.domes.body, p)));
}

function flatSheet() {
  const S = 7.2; // px per mm
  const { slab } = KC;
  const { R, r } = KC.loop;
  const { from, ctrl, to, half } = KC.collar;
  const { muzzle, nose, eyeL, eyeR } = KC.bumps;
  const sil = KC_DOMES.map(kcEllipse).join("");
  // Fur cross-sections of the relief by height.
  const tallest = Math.max(...[...KC_DOMES, ...KC_BUMPS].map((e) => kcHeight(e[0], e[1]) - slab));
  const levels = [];
  for (let s = 0; s < Math.max(...KC_DOMES.map((e) => e[5])); s += 0.2) levels.push(s);
  const level = (s) =>
    KC_DOMES.filter((e) => e[5] > s)
      .map((e) => {
        const f = (1 - (s / e[5]) ** 2) ** (1 / e[6]);
        return kcEllipse([e[0], e[1], e[2] * f, e[3] * f, e[4]]);
      })
      .join("");
  const defs =
    `<defs><g id="ks">${sil}</g><clipPath id="kc">${sil}</clipPath>` +
    `<g id="kp"><g fill="${FIL.brown}">${sil}</g>` +
    `<path d="M${from} Q${ctrl} ${to}" fill="none" stroke="${FIL.red}" stroke-width="${2 * half}" stroke-linecap="round" clip-path="url(#kc)"/>` +
    `<g fill="${FIL.white}">${kcEllipse(muzzle)}${kcEllipse(KC.belly)}</g>` +
    `<g fill="${FIL.black}">${[nose, eyeL, eyeR].map(kcEllipse).join("")}</g></g>` +
    levels.map((s, i) => `<g id="kl${i}" fill="${FIL.brown}">${level(s)}</g>`).join("") +
    `<clipPath id="cell"><rect width="398" height="398"/></clipPath></defs>`;
  // Colored details in paint order; a raised detail also gets its upper rings so its top shows.
  const collar = collarOutline();
  const details = [
    [FIL.red, [collar]],
    [FIL.white, [ellipsePoints(KC.belly)]],
    ...[
      [FIL.white, muzzle],
      [FIL.black, nose],
      [FIL.black, eyeL],
      [FIL.black, eyeR],
    ].map(([color, e]) => [color, [1, 0.85, 0.6, 0.3].map((f) => ellipsePoints(e, f, e[2] > 2 ? 72 : 32))]),
  ];

  // Parallel view: azimuth a turns the piece on the bed, elevation e is the camera angle above it.
  const rad = (d) => (d * Math.PI) / 180;
  const view = (a, e) => {
    const [ca, sa, ce, se] = [Math.cos(rad(a)), Math.sin(rad(a)), Math.cos(rad(e)), Math.sin(rad(e))];
    const xy = (u, v, h) => [S * (ca * u - sa * v), -S * (se * (sa * u + ca * v) + h * ce)];
    // Center the piece in the cell.
    const pts = [];
    for (const [cu, cv, ru, rv, deg] of KC_DOMES) {
      pts.push(xy(cu, cv, kcHeight(cu, cv)));
      for (let i = 0; i < 48; i++) {
        const w = (i / 24) * Math.PI, x = ru * Math.cos(w), y = rv * Math.sin(w), t = rad(deg);
        const u = cu + x * Math.cos(t) - y * Math.sin(t), v = cv + x * Math.sin(t) + y * Math.cos(t);
        pts.push(xy(u, v, 0), xy(u, v, slab));
      }
    }
    for (let i = 0; i < 48; i++) {
      const w = (i / 24) * Math.PI, u = (R + r) * Math.cos(w), v = kc.loopV + (R + r) * Math.sin(w);
      pts.push(xy(u, v, 0), xy(u, v, 2 * r));
    }
    const mid = (k) => (Math.min(...pts.map((p) => p[k])) + Math.max(...pts.map((p) => p[k]))) / 2;
    const ox = 199 - mid(0), oy = 199 - mid(1);
    return {
      ox,
      oy,
      xy: (u, v, h) => xy(u, v, h).map((n, k) => n + [ox, oy][k]),
      m: (h) => `matrix(${[S * ca, -S * se * sa, -S * sa, -S * se * ca, ox, oy - S * h * ce].map(n3).join(" ")})`,
    };
  };
  const ring = (w) =>
    `<circle cx="0" cy="${n3(kc.loopV)}" r="${R}" fill="none" stroke="${FIL.black}" stroke-width="${n3(2 * w)}"/>`;

  // Face up with the loop at the top; the back is the same outline seen from below (mirrored).
  const top = view(0, 90);
  const faceUp = `<g transform="matrix(${S} 0 0 ${-S} ${n3(top.ox)} ${n3(top.oy)})">${ring(r)}<use href="#kp"/></g>`;
  const back = `<g transform="matrix(${-S} 0 0 ${-S} ${n3(398 - top.ox)} ${n3(top.oy)})">${ring(r)}<use href="#ks" fill="${FIL.black}"/></g>`;

  const solid = (a, e, bed = false) => {
    const p = view(a, e);
    const items = [];
    for (let h = 0; h < slab - 1e-9; h += 0.2) items.push([h, `<use href="#ks" fill="${FIL.black}"/>`]);
    for (let h = 0.1; h < 2 * r; h += 0.2) items.push([h, ring(Math.sqrt(r * r - (h - r) ** 2))]);
    levels.forEach((s, i) => items.push([slab + s, `<use href="#kl${i}"/>`]));
    items.sort((x, y) => x[0] - y[0]);
    const path = (pts, h) => `M${pts.map(([u, v]) => p.xy(u, v, h(u, v)).map(n2).join(" ")).join("L")}Z`;
    const plate = bed
      ? `<path d="${path([[-18, -9], [18, -9], [18, 54], [-18, 54]], () => 0)}" fill="#ECE5D8" stroke="#D8CFBF" stroke-width="1.5"/>`
      : "";
    return (
      plate +
      items.map(([h, body]) => `<g transform="${p.m(h)}">${body}</g>`).join("") +
      details.map(([color, rings]) => `<path d="${rings.map((pts) => path(pts, kcHeight)).join("")}" fill="${color}"/>`).join("")
    );
  };

  // Side profile, seen from -u: for each slice along v, the first surface each height meets.
  const profile = [], runs = new Map();
  const dv = 0.1;
  for (let v0 = Math.floor(kc.vmin / dv) * dv; v0 < kc.headTop; v0 += dv) {
    const v = v0 + dv / 2;
    let best = slab;
    const seen = [];
    for (let u = -11.5; u <= 11.5; u += 0.05) {
      if (!kcInside(u, v)) continue;
      const h = kcHeight(u, v);
      if (h <= best) continue;
      const color = kcColor(u, v);
      if (seen.length && seen[seen.length - 1][2] === color) seen[seen.length - 1][1] = h;
      else seen.push([best, h, color]);
      best = h;
    }
    if (!seen.length) continue;
    profile.push([v, best]);
    for (const [lo, hi, color] of seen)
      if (color !== FIL.brown)
        runs.set(color, (runs.get(color) ?? "") + `M${n2(v0)} ${n2(-hi)}h${n2(dv + 0.02)}v${n2(hi - lo)}h${n2(-dv - 0.02)}z`);
  }
  const v1 = kc.headTop, v0 = kc.vmin, loopEnd = kc.loopV + R + r;
  const sideBody =
    `<path d="M${n3(v0 - 6)} 0H${n3(loopEnd + 6)}" stroke="#D8CFBF" stroke-width="0.2"/>` +
    `<rect x="${n3(kc.loopV - R - r)}" y="${-2 * r}" width="${2 * (R + r)}" height="${2 * r}" rx="${r}" fill="${FIL.black}"/>` +
    `<path d="M${n2(v0)} 0${profile.map(([v, h]) => `L${n2(v)} ${n2(-h)}`).join("")}L${n2(v1)} 0Z" fill="${FIL.brown}"/>` +
    [...runs].map(([color, d]) => `<path d="${d}" fill="${color}"/>`).join("") +
    `<rect x="${n3(v0)}" y="${-slab}" width="${n3(v1 - v0)}" height="${slab}" fill="${FIL.black}"/>`;
  const vc = (v0 + loopEnd) / 2, hc = (slab + tallest) / 2;
  // Left view looks toward +u: the paws are on the right. The right view mirrors it.
  const sideView = (left) =>
    `<g transform="matrix(${left ? -S : S} 0 0 ${S} ${n3(199 + (left ? S : -S) * vc)} ${n3(199 + S * hc)})"><use href="#kside"/></g>`;

  const bodies = [faceUp, solid(-30, 50), sideView(true), sideView(false), back, solid(-40, 34, true)];
  return (
    defs.replace("</defs>", `<g id="kside">${sideBody}</g></defs>`) +
    bodies
      .map((body, i) => {
        const x = (i % 3) * 400, y = Math.floor(i / 3) * 400;
        return `<g transform="translate(${x} ${y})"><rect width="398" height="398" fill="#FFFDF8" stroke="#D8CFBF"/><g clip-path="url(#cell)">${body}</g></g>`;
      })
      .join("")
  );
}
writeFileSync(join(out, "render-sheet-flat.svg"), svg(1198, 798, flatSheet(), "#F3EEE4"));

// Paint checkpoint pair: the ideal paint, and its simulated print with the left eye lost and layer lines.
writeFileSync(join(out, "paint-ideal.svg"), svg(512, 512, figure(flat), "#FFFDF8"));
const figureClip = [
  [256, 440, 170, 34],
  [256, 430, 170, 30],
  [256, 330, 92, 100],
  [212, 410, 28, 22],
  [300, 410, 28, 22],
  [256, 205, 86, 80],
]
  .map(([cx, cy, rx, ry]) => `<ellipse cx="${cx}" cy="${cy}" rx="${rx}" ry="${ry}"/>`)
  .join("");
const earClip = [178, 334]
  .map((cx, i) => `<ellipse cx="${cx}" cy="215" rx="24" ry="58" transform="rotate(${i ? -18 : 18} ${cx} 205)"/>`)
  .join("");
const layerLines =
  `<defs><pattern id="ll" width="6" height="6" patternUnits="userSpaceOnUse"><rect width="6" height="1" fill="#000" fill-opacity=".12"/></pattern>` +
  `<clipPath id="fc">${figureClip}${earClip}</clipPath></defs><rect width="512" height="512" fill="url(#ll)" clip-path="url(#fc)"/>`;
writeFileSync(join(out, "paint-sim.svg"), svg(512, 512, figure({ ...flat, noLeftEye: true }) + layerLines, "#FFFDF8"));

writeFileSync(
  join(out, "swatch.svg"),
  svg(96, 96, `<rect x="8" y="8" width="80" height="80" rx="12" fill="#3A3B41"/><path d="M8 60 L88 28 L88 88 L8 88Z" fill="#2E2F34"/>`),
);

writeFileSync(
  join(out, "checklist.json"),
  JSON.stringify(
    {
      items: [
        { id: "spec_coverage", passed: true, by: "code", evidence: "11 of 11 spec parts painted" },
        { id: "face_parts", passed: false, by: "model", evidence: "Left eye merged into head in simulation" },
        { id: "markings", passed: true, by: "model", evidence: "Belly patch present" },
        { id: "islands", passed: true, by: "code", evidence: "No island under 0.8 mm2" },
        { id: "creases", passed: null, by: "code", evidence: "62% of boundary length on creases" },
        { id: "slot_count", passed: true, by: "code", evidence: "4 of 4 AMS slots" },
        { id: "sim_matches_intent", passed: false, by: "model", evidence: "Eye missing in sim_front" },
        { id: "purge_under_40pct", passed: true, by: "code", evidence: "Purge 21% of model mass" },
      ],
      passed_all: false,
      confidence: 0.58,
    },
    null,
    2,
  ) + "\n",
);

const tex = texturedGlb();
const prev = previewGlb();
const kc3d = texturedGlb(kc3dParts);
const kc3dPrev = colorGlb(faceTriangles(kc3dParts), "keychain_3d_filaments");
writeFileSync(join(out, "kc3d.glb"), kc3d.buf);
writeFileSync(join(out, "kc3d-preview.glb"), kc3dPrev.buf);
console.log(`kc3d.glb ${kc3d.buf.length} B (${kc3d.tris} tris), kc3d-preview.glb ${kc3dPrev.buf.length} B`);
const mf = threeMf();
writeFileSync(join(out, "mesh.glb"), tex.buf);
writeFileSync(join(out, "preview.glb"), prev.buf);
writeFileSync(join(out, "sample.3mf"), mf);
const flatPrev = colorGlb(kc.tris, "keychain_filaments");
writeFileSync(join(out, "preview-flat.glb"), flatPrev.buf);
console.log(
  `mesh.glb ${tex.buf.length} B (${tex.tris} tris), preview.glb ${prev.buf.length} B (${prev.tris} tris), sample.3mf ${mf.length} B`,
);
const [kw, kt, kl] = kc.size.map((n) => n.toFixed(3));
console.log(`preview-flat.glb ${flatPrev.buf.length} B (${flatPrev.tris} tris), bbox x ${kw} y ${kt} z ${kl} mm`);
