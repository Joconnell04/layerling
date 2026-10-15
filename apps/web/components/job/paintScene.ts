// The paint editor's three.js scene: the figure as unindexed triangles (one color per face, in
// the region map's face order), add-on parts in their filament, orbit controls, and GPU face
// picking (each face drawn in its own id color into a 1 x 1 target under the cursor).
// Contract frame (mm, Z up, front -Y) maps to three.js (Y up, front +Z): (x, y, z) -> (x, z, -y).

import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import type { TriMesh } from "@/lib/meshFiles";

export interface ScenePart {
  mesh: TriMesh;
  hex: string;
}

const PICK_VERT = /* glsl */ `
flat out int vFace;
void main() {
  vFace = gl_VertexID / 3;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}`;
const PICK_FRAG = /* glsl */ `
flat in int vFace;
layout(location = 0) out highp vec4 faceColor;
void main() {
  int id = vFace + 1;
  faceColor = vec4(float(id & 255) / 255.0, float((id >> 8) & 255) / 255.0, float((id >> 16) & 255) / 255.0, 1.0);
}`;

/** sRGB hex -> linear 0..255 (three.js treats vertex colors as linear). */
export function linearRgb(hex: string): [number, number, number] {
  const c = new THREE.Color(hex);
  return [Math.round(c.r * 255), Math.round(c.g * 255), Math.round(c.b * 255)];
}

function unindexed(mesh: TriMesh): Float32Array {
  const n = mesh.indices.length;
  const out = new Float32Array(n * 3);
  const p = mesh.positions;
  for (let i = 0; i < n; i++) {
    const v = mesh.indices[i] * 3;
    out[i * 3] = p[v];
    out[i * 3 + 1] = p[v + 2];
    out[i * 3 + 2] = -p[v + 1];
  }
  return out;
}

export class PaintScene {
  readonly renderer: THREE.WebGLRenderer;
  readonly controls: OrbitControls;
  private scene = new THREE.Scene();
  private pickScene = new THREE.Scene();
  private camera = new THREE.PerspectiveCamera(30, 1, 0.1, 10000);
  private target = new THREE.WebGLRenderTarget(1, 1, { type: THREE.UnsignedByteType });
  private figure: THREE.Mesh | null = null;
  private colors: THREE.BufferAttribute | null = null;
  private extras: THREE.Object3D[] = [];
  private raf = 0;
  private ro: ResizeObserver;
  private dirty = true;

  constructor(private host: HTMLElement) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.setClearColor(0x000000, 0);
    host.appendChild(this.renderer.domElement);
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x3a3b40, 1.7));
    const key = new THREE.DirectionalLight(0xffffff, 1.6);
    key.position.set(0.6, 1, 0.9);
    this.camera.add(key);
    this.scene.add(this.camera);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.12;
    this.controls.addEventListener("change", () => (this.dirty = true));
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(host);
    this.resize();
    const loop = () => {
      this.controls.update();
      if (this.dirty) {
        this.renderer.render(this.scene, this.camera);
        this.dirty = false;
      }
      this.raf = requestAnimationFrame(loop);
    };
    loop();
  }

  private resize(): void {
    const w = this.host.clientWidth || 1;
    const h = this.host.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.renderer.domElement.style.width = "100%";
    this.renderer.domElement.style.height = "100%";
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.dirty = true;
  }

  setMesh(figure: TriMesh, parts: ScenePart[]): void {
    this.clear();
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(unindexed(figure), 3));
    this.colors = new THREE.BufferAttribute(new Uint8Array((figure.indices.length) * 3), 3, true);
    this.colors.setUsage(THREE.DynamicDrawUsage);
    geo.setAttribute("color", this.colors);
    geo.computeVertexNormals();
    const mat = new THREE.MeshLambertMaterial({ vertexColors: true, flatShading: true });
    this.figure = new THREE.Mesh(geo, mat);
    this.scene.add(this.figure);
    const pick = new THREE.ShaderMaterial({ glslVersion: THREE.GLSL3, vertexShader: PICK_VERT, fragmentShader: PICK_FRAG });
    const pickMesh = new THREE.Mesh(geo, pick);
    this.pickScene.add(pickMesh);
    this.extras.push(pickMesh);
    const occluder = new THREE.MeshBasicMaterial({ color: 0x000000 });
    for (const part of parts) {
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(unindexed(part.mesh), 3));
      g.computeVertexNormals();
      const m = new THREE.Mesh(g, new THREE.MeshLambertMaterial({ color: new THREE.Color(part.hex), flatShading: true }));
      this.scene.add(m);
      const o = new THREE.Mesh(g, occluder);
      this.pickScene.add(o);
      this.extras.push(m, o);
    }
    this.frame();
  }

  /** Aim the camera like the job viewer: a piece lying flat is seen from above at an angle. */
  frame(): void {
    const box = new THREE.Box3();
    this.scene.traverse((o) => {
      if ((o as THREE.Mesh).isMesh) box.expandByObject(o);
    });
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const r = Math.max(size.x, size.y, size.z);
    const lying = size.y < 0.45 * Math.max(size.x, size.z);
    const dist = (r / 2 / Math.tan((this.camera.fov * Math.PI) / 360)) * (lying ? 1.1 : 1.3);
    const az = ((lying ? -12 : -28) * Math.PI) / 180;
    const el = ((lying ? 62 : 14) * Math.PI) / 180;
    this.camera.position.set(
      center.x + Math.sin(-az) * dist * Math.cos(el),
      center.y + Math.sin(el) * dist,
      center.z + Math.cos(az) * dist * Math.cos(el),
    );
    this.camera.near = dist / 100;
    this.camera.far = dist * 20;
    this.camera.updateProjectionMatrix();
    this.controls.target.copy(center);
    this.controls.update();
    this.dirty = true;
  }

  /** Every face's color: slots[f] indexes rgb (linear 0..255 triplets). */
  setFaceColors(slots: Uint8Array, rgb: [number, number, number][]): void {
    const c = this.colors;
    if (!c) return;
    const arr = c.array as Uint8Array;
    const miss: [number, number, number] = [200, 200, 200];
    for (let f = 0; f < slots.length; f++) {
      const col = rgb[slots[f]] ?? miss;
      for (let k = 0; k < 3; k++) {
        const o = f * 9 + k * 3;
        arr[o] = col[0];
        arr[o + 1] = col[1];
        arr[o + 2] = col[2];
      }
    }
    c.clearUpdateRanges();
    c.needsUpdate = true;
    this.dirty = true;
  }

  /** Recolor some faces (a live brush stroke). */
  paintFaces(faces: ArrayLike<number>, col: [number, number, number]): void {
    const c = this.colors;
    if (!c || !faces.length) return;
    const arr = c.array as Uint8Array;
    let lo = Infinity;
    let hi = -1;
    for (let i = 0; i < faces.length; i++) {
      const f = faces[i];
      if (f < lo) lo = f;
      if (f > hi) hi = f;
      for (let k = 0; k < 3; k++) {
        const o = f * 9 + k * 3;
        arr[o] = col[0];
        arr[o + 1] = col[1];
        arr[o + 2] = col[2];
      }
    }
    c.addUpdateRange(lo * 9, (hi - lo + 1) * 9);
    c.needsUpdate = true;
    this.dirty = true;
  }

  /** The figure face under a client point, or null (background, or an add-on part in front). */
  pick(clientX: number, clientY: number): number | null {
    if (!this.figure) return null;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const x = clientX - rect.left;
    const y = clientY - rect.top;
    if (x < 0 || y < 0 || x >= rect.width || y >= rect.height) return null;
    this.camera.setViewOffset(rect.width, rect.height, Math.floor(x), Math.floor(y), 1, 1);
    const clear = this.renderer.getClearAlpha();
    this.renderer.setRenderTarget(this.target);
    this.renderer.setClearColor(0x000000, 1);
    this.renderer.clear();
    this.renderer.render(this.pickScene, this.camera);
    const px = new Uint8Array(4);
    this.renderer.readRenderTargetPixels(this.target, 0, 0, 1, 1, px);
    this.renderer.setRenderTarget(null);
    this.renderer.setClearColor(0x000000, clear);
    this.camera.clearViewOffset();
    const id = px[0] | (px[1] << 8) | (px[2] << 16);
    return id > 0 ? id - 1 : null;
  }

  /** Direction the camera looks, in the contract frame. */
  viewDir(): [number, number, number] {
    const d = new THREE.Vector3();
    this.camera.getWorldDirection(d);
    return [d.x, -d.z, d.y];
  }

  /** Screen pixels per mm at a contract-frame point (brush cursor size). */
  pxPerMm(p: [number, number, number]): number {
    const v = new THREE.Vector3(p[0], p[2], -p[1]);
    const dist = v.distanceTo(this.camera.position);
    const h = this.renderer.domElement.getBoundingClientRect().height || 1;
    return h / (2 * dist * Math.tan((this.camera.fov * Math.PI) / 360));
  }

  private clear(): void {
    for (const o of [this.figure, ...this.extras]) {
      if (!o) continue;
      o.removeFromParent();
      const m = o as THREE.Mesh;
      m.geometry?.dispose();
      (Array.isArray(m.material) ? m.material : [m.material]).forEach((x) => x?.dispose());
    }
    this.figure = null;
    this.extras = [];
    this.colors = null;
  }

  dispose(): void {
    cancelAnimationFrame(this.raf);
    this.ro.disconnect();
    this.controls.dispose();
    this.clear();
    this.target.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}
