// Readers for the paint files the editor loads as they are on Storage: face_regions.npy (numpy
// int32, one region id per figure face) and the figure PLY (binary little endian, as p2p_geometry
// and p2p_sim.save_ply write it). Face order is kept exactly: region ids and brush strokes
// address faces by index.

const NPY_MAGIC = [0x93, 0x4e, 0x55, 0x4d, 0x50, 0x59]; // \x93NUMPY

/** A 1-D int32 (or smaller int) .npy file as an Int32Array. */
export function readNpyInt32(buf: ArrayBuffer): Int32Array {
  const u8 = new Uint8Array(buf);
  if (!NPY_MAGIC.every((b, i) => u8[i] === b)) throw new Error("not a .npy file");
  const major = u8[6];
  const view = new DataView(buf);
  const hlen = major === 1 ? view.getUint16(8, true) : view.getUint32(8, true);
  const start = major === 1 ? 10 : 12;
  const header = new TextDecoder().decode(u8.subarray(start, start + hlen));
  const descr = /'descr':\s*'([^']+)'/.exec(header)?.[1];
  const shape = /'shape':\s*\((\d+),?\s*\)/.exec(header)?.[1];
  if (/'fortran_order':\s*True/.test(header)) throw new Error("fortran order .npy");
  if (!descr || shape == null) throw new Error(`unsupported .npy header: ${header.trim()}`);
  const n = Number(shape);
  const off = start + hlen;
  const typed: Record<string, (b: ArrayBuffer, o: number, n: number) => ArrayLike<number>> = {
    "<i4": (b, o, k) => new Int32Array(b.slice(o, o + 4 * k)),
    "<u4": (b, o, k) => new Uint32Array(b.slice(o, o + 4 * k)),
    "<i2": (b, o, k) => new Int16Array(b.slice(o, o + 2 * k)),
    "<u2": (b, o, k) => new Uint16Array(b.slice(o, o + 2 * k)),
    "|i1": (b, o, k) => new Int8Array(b.slice(o, o + k)),
    "|u1": (b, o, k) => new Uint8Array(b.slice(o, o + k)),
    "<i8": (b, o, k) => {
      const big = new BigInt64Array(b.slice(o, o + 8 * k));
      return Int32Array.from(big, (x) => Number(x));
    },
  };
  const read = typed[descr];
  if (!read) throw new Error(`unsupported .npy dtype ${descr}`);
  const arr = read(buf, off, n);
  return arr instanceof Int32Array ? arr : Int32Array.from(arr);
}

/** Write a 1-D int32 .npy (tests and mock fixtures). */
export function writeNpyInt32(values: ArrayLike<number>): ArrayBuffer {
  let header = `{'descr': '<i4', 'fortran_order': False, 'shape': (${values.length},), }`;
  const pad = 64 - ((10 + header.length + 1) % 64);
  header = header + " ".repeat(pad === 64 ? 0 : pad) + "\n";
  const out = new Uint8Array(10 + header.length + 4 * values.length);
  out.set(NPY_MAGIC, 0);
  out[6] = 1;
  out[7] = 0;
  new DataView(out.buffer).setUint16(8, header.length, true);
  out.set(new TextEncoder().encode(header), 10);
  new Int32Array(out.buffer, 10 + header.length, values.length).set(Array.from(values));
  return out.buffer;
}

export interface TriMesh {
  /** xyz per vertex, mm, contract frame (Z up). */
  positions: Float32Array;
  /** Three vertex indices per face, in file order. */
  indices: Uint32Array;
}

const SIZES: Record<string, number> = {
  char: 1, uchar: 1, int8: 1, uint8: 1, short: 2, ushort: 2, int16: 2, uint16: 2,
  int: 4, uint: 4, int32: 4, uint32: 4, float: 4, float32: 4, double: 8, float64: 8,
};

function readScalar(v: DataView, o: number, t: string): number {
  switch (t) {
    case "char": case "int8": return v.getInt8(o);
    case "uchar": case "uint8": return v.getUint8(o);
    case "short": case "int16": return v.getInt16(o, true);
    case "ushort": case "uint16": return v.getUint16(o, true);
    case "int": case "int32": return v.getInt32(o, true);
    case "uint": case "uint32": return v.getUint32(o, true);
    case "float": case "float32": return v.getFloat32(o, true);
    case "double": case "float64": return v.getFloat64(o, true);
  }
  throw new Error(`PLY type ${t}`);
}

interface Prop {
  name: string;
  type: string;
  list?: { count: string; item: string };
}

/** Binary little-endian PLY with triangle faces (vertex x, y, z; face vertex_indices). */
export function readPly(buf: ArrayBuffer): TriMesh {
  const u8 = new Uint8Array(buf);
  const endAt = findHeaderEnd(u8);
  const header = new TextDecoder().decode(u8.subarray(0, endAt)).split(/\r?\n/);
  if (header[0] !== "ply") throw new Error("not a PLY file");
  if (!header.some((l) => l.startsWith("format binary_little_endian"))) throw new Error("PLY must be binary little endian");
  const elements: { name: string; count: number; props: Prop[] }[] = [];
  for (const line of header) {
    const w = line.trim().split(/\s+/);
    if (w[0] === "element") elements.push({ name: w[1], count: Number(w[2]), props: [] });
    else if (w[0] === "property" && elements.length) {
      const el = elements[elements.length - 1];
      if (w[1] === "list") el.props.push({ name: w[4], type: "list", list: { count: w[2], item: w[3] } });
      else el.props.push({ name: w[2], type: w[1] });
    }
  }
  const view = new DataView(buf);
  let o = endAt;
  let positions = new Float32Array(0);
  let indices = new Uint32Array(0);
  for (const el of elements) {
    if (el.name === "vertex") {
      positions = new Float32Array(el.count * 3);
      const fixed = el.props.every((p) => !p.list);
      if (!fixed) throw new Error("PLY vertex lists are not supported");
      const offs = new Map<string, { at: number; type: string }>();
      let stride = 0;
      for (const p of el.props) {
        offs.set(p.name, { at: stride, type: p.type });
        stride += SIZES[p.type] ?? 0;
      }
      const xs = ["x", "y", "z"].map((k) => offs.get(k));
      if (xs.some((x) => !x)) throw new Error("PLY vertices need x, y, z");
      for (let i = 0; i < el.count; i++) {
        for (let k = 0; k < 3; k++) positions[i * 3 + k] = readScalar(view, o + xs[k]!.at, xs[k]!.type);
        o += stride;
      }
    } else if (el.name === "face") {
      indices = new Uint32Array(el.count * 3);
      for (let i = 0; i < el.count; i++) {
        for (const p of el.props) {
          if (p.list) {
            const n = readScalar(view, o, p.list.count);
            o += SIZES[p.list.count];
            const size = SIZES[p.list.item];
            if (p.name === "vertex_indices" || p.name === "vertex_index") {
              if (n !== 3) throw new Error(`PLY face ${i} has ${n} corners`);
              for (let k = 0; k < 3; k++) indices[i * 3 + k] = readScalar(view, o + k * size, p.list.item);
            }
            o += n * size;
          } else {
            o += SIZES[p.type];
          }
        }
      }
    } else {
      for (let i = 0; i < el.count; i++) {
        for (const p of el.props) {
          if (p.list) {
            const n = readScalar(view, o, p.list.count);
            o += SIZES[p.list.count] + n * SIZES[p.list.item];
          } else o += SIZES[p.type];
        }
      }
    }
  }
  return { positions, indices };
}

function findHeaderEnd(u8: Uint8Array): number {
  const tag = new TextEncoder().encode("end_header");
  const limit = Math.min(u8.length, 65536);
  for (let i = 0; i < limit - tag.length; i++) {
    let ok = true;
    for (let k = 0; k < tag.length; k++) if (u8[i + k] !== tag[k]) { ok = false; break; }
    if (ok) {
      let j = i + tag.length;
      if (u8[j] === 0x0d) j++;
      if (u8[j] === 0x0a) j++;
      return j;
    }
  }
  throw new Error("PLY header has no end_header");
}

/** Binary PLY from positions and triangles (tests and mock fixtures). */
export function writePly(mesh: TriMesh): ArrayBuffer {
  const nv = mesh.positions.length / 3;
  const nf = mesh.indices.length / 3;
  const head = new TextEncoder().encode(
    `ply\nformat binary_little_endian 1.0\nelement vertex ${nv}\nproperty float x\nproperty float y\nproperty float z\nelement face ${nf}\nproperty list uchar int vertex_indices\nend_header\n`,
  );
  const out = new ArrayBuffer(head.length + nv * 12 + nf * 13);
  new Uint8Array(out).set(head, 0);
  const v = new DataView(out);
  let o = head.length;
  for (let i = 0; i < nv * 3; i++, o += 4) v.setFloat32(o, mesh.positions[i], true);
  for (let f = 0; f < nf; f++) {
    v.setUint8(o, 3);
    o += 1;
    for (let k = 0; k < 3; k++, o += 4) v.setInt32(o, mesh.indices[f * 3 + k], true);
  }
  return out;
}
