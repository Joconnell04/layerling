// Brush selection for the paint editor: the figure faces within a radius (mm, straight line) of
// the point under the cursor that face the camera. A uniform grid over face centroids keeps a
// dab on a 1M-face figure to the few cells it touches.

export interface FaceGeometry {
  /** Face centroid xyz, mm (the mesh's frame). */
  centroids: Float32Array;
  /** Unit face normal xyz. */
  normals: Float32Array;
}

export function faceGeometry(positions: Float32Array, indices: Uint32Array): FaceGeometry {
  const n = indices.length / 3;
  const centroids = new Float32Array(n * 3);
  const normals = new Float32Array(n * 3);
  for (let f = 0; f < n; f++) {
    const a = indices[f * 3] * 3;
    const b = indices[f * 3 + 1] * 3;
    const c = indices[f * 3 + 2] * 3;
    const ax = positions[a], ay = positions[a + 1], az = positions[a + 2];
    const bx = positions[b], by = positions[b + 1], bz = positions[b + 2];
    const cx = positions[c], cy = positions[c + 1], cz = positions[c + 2];
    centroids[f * 3] = (ax + bx + cx) / 3;
    centroids[f * 3 + 1] = (ay + by + cy) / 3;
    centroids[f * 3 + 2] = (az + bz + cz) / 3;
    const ux = bx - ax, uy = by - ay, uz = bz - az;
    const vx = cx - ax, vy = cy - ay, vz = cz - az;
    let nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
    const len = Math.hypot(nx, ny, nz) || 1;
    nx /= len; ny /= len; nz /= len;
    normals[f * 3] = nx;
    normals[f * 3 + 1] = ny;
    normals[f * 3 + 2] = nz;
  }
  return { centroids, normals };
}

export class FaceGrid {
  private min: [number, number, number];
  private dims: [number, number, number];
  private start: Uint32Array;
  private faces: Uint32Array;

  constructor(
    private geo: FaceGeometry,
    private cell = 2,
  ) {
    const c = geo.centroids;
    const n = c.length / 3;
    const lo: [number, number, number] = [Infinity, Infinity, Infinity];
    const hi: [number, number, number] = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < n; i++) {
      for (let k = 0; k < 3; k++) {
        const v = c[i * 3 + k];
        if (v < lo[k]) lo[k] = v;
        if (v > hi[k]) hi[k] = v;
      }
    }
    if (!n) {
      lo.fill(0);
      hi.fill(0);
    }
    this.min = lo;
    this.dims = [0, 1, 2].map((k) => Math.max(1, Math.floor((hi[k] - lo[k]) / cell) + 1)) as [number, number, number];
    const total = this.dims[0] * this.dims[1] * this.dims[2];
    const cellOf = new Uint32Array(n);
    const count = new Uint32Array(total + 1);
    for (let i = 0; i < n; i++) {
      const id = this.cellId(c[i * 3], c[i * 3 + 1], c[i * 3 + 2]);
      cellOf[i] = id;
      count[id + 1]++;
    }
    for (let i = 0; i < total; i++) count[i + 1] += count[i];
    this.start = count.slice();
    const fill = count.slice(0, total);
    this.faces = new Uint32Array(n);
    for (let i = 0; i < n; i++) this.faces[fill[cellOf[i]]++] = i;
  }

  private axis(v: number, k: number): number {
    const i = Math.floor((v - this.min[k]) / this.cell);
    return i < 0 ? 0 : i >= this.dims[k] ? this.dims[k] - 1 : i;
  }

  private cellId(x: number, y: number, z: number): number {
    return this.axis(x, 0) + this.dims[0] * (this.axis(y, 1) + this.dims[1] * this.axis(z, 2));
  }

  /**
   * Faces with a centroid within `radius` of `p`; with `view` (the direction the camera looks),
   * only faces turned toward the camera.
   */
  query(p: [number, number, number], radius: number, view?: [number, number, number]): number[] {
    const { centroids: c, normals: nrm } = this.geo;
    const r2 = radius * radius;
    const lo = [0, 1, 2].map((k) => this.axis(p[k] - radius, k));
    const hi = [0, 1, 2].map((k) => this.axis(p[k] + radius, k));
    const out: number[] = [];
    for (let z = lo[2]; z <= hi[2]; z++) {
      for (let y = lo[1]; y <= hi[1]; y++) {
        for (let x = lo[0]; x <= hi[0]; x++) {
          const id = x + this.dims[0] * (y + this.dims[1] * z);
          for (let j = this.start[id]; j < this.start[id + 1]; j++) {
            const f = this.faces[j];
            const dx = c[f * 3] - p[0], dy = c[f * 3 + 1] - p[1], dz = c[f * 3 + 2] - p[2];
            if (dx * dx + dy * dy + dz * dz > r2) continue;
            if (view && nrm[f * 3] * view[0] + nrm[f * 3 + 1] * view[1] + nrm[f * 3 + 2] * view[2] > 0.1) continue;
            out.push(f);
          }
        }
      }
    }
    return out;
  }
}
