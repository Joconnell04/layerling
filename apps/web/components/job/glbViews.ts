"use client";

// Renders a GLB from the eight contract PROOF_VIEWS in the browser, for proof sets no worker stage
// renders yet: the Meshy model waiting at the mesh gate, and the thin slice preview in filament
// colors. Same cameras as p2p_sim.proof (azimuth around +Z from +X, elevation, contract frame Z up,
// front toward -Y), same display pose (a piece lying flat is stood back up to face the front).
// Results are cached per URL for the page's lifetime.

import { PROOF_CAMERAS, PROOF_ORDER, type ProofView } from "@/lib/fidelity";

export type GlbViews = Record<ProofView, string>;

const cache = new Map<string, Promise<GlbViews | null>>();

export function renderGlbViews(url: string, sizePx = 512): Promise<GlbViews | null> {
  const key = `${sizePx}|${url}`;
  let p = cache.get(key);
  if (!p) {
    p = render(url, sizePx).catch(() => null);
    cache.set(key, p);
  }
  return p;
}

async function render(url: string, sizePx: number): Promise<GlbViews | null> {
  const THREE = await import("three");
  const { GLTFLoader } = await import("three/examples/jsm/loaders/GLTFLoader.js");
  const { paintedModel } = await import("../ModelViewer");

  let renderer: InstanceType<typeof THREE.WebGLRenderer>;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
  } catch {
    return null; // no WebGL (tests, locked-down browsers): the gallery shows empty tiles
  }
  try {
    renderer.setPixelRatio(1);
    renderer.setSize(sizePx, sizePx, false);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.setClearColor(0x000000, 0);

    const gltf = await new GLTFLoader().loadAsync(url);
    const model = gltf.scene;
    // Filament colors (COLOR_0 or one material per filament): matte and faceted, like the viewer.
    if (paintedModel(model)) {
      model.traverse((o) => {
        const mesh = o as InstanceType<typeof THREE.Mesh>;
        if (!mesh.isMesh) return;
        const g = mesh.geometry;
        const olds = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
        const next = olds.map(
          (old) =>
            new THREE.MeshLambertMaterial({
              vertexColors: !!g.attributes.color,
              color: g.attributes.color ? 0xffffff : ((old as InstanceType<typeof THREE.MeshStandardMaterial>).color ?? new THREE.Color(0xffffff)),
              flatShading: true,
            }),
        );
        mesh.material = Array.isArray(mesh.material) ? next : next[0];
      });
    }
    // Display pose. glTF is Y up with the front toward +Z. A piece lying on the bed (face up,
    // head toward -Z) stands back up: +90 degrees about X turns +Y to +Z and -Z to +Y.
    const pose = new THREE.Group();
    pose.add(model);
    const raw = new THREE.Box3().setFromObject(model).getSize(new THREE.Vector3());
    if (raw.y < 0.45 * Math.max(raw.x, raw.z)) pose.rotation.x = Math.PI / 2;
    pose.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(pose);
    const center = box.getCenter(new THREE.Vector3());
    const radius = box.getBoundingSphere(new THREE.Sphere()).radius || 1;

    const scene = new THREE.Scene();
    scene.add(pose);
    scene.add(new THREE.HemisphereLight(0xffffff, 0x3a3b41, 1.5));
    const key = new THREE.DirectionalLight(0xffffff, 1.7);
    scene.add(key);
    scene.add(key.target);
    const fov = 30;
    const camera = new THREE.PerspectiveCamera(fov, 1, radius / 50, radius * 50);
    const aim = (view: ProofView, dist: number) => {
      const { azimuth, elevation } = PROOF_CAMERAS[view];
      const az = (azimuth * Math.PI) / 180;
      const el = (elevation * Math.PI) / 180;
      // Contract direction (x, y, z), Z up -> glTF (x, z, -y).
      const dc = [Math.cos(el) * Math.cos(az), Math.cos(el) * Math.sin(az), Math.sin(el)];
      const d = new THREE.Vector3(dc[0], dc[2], -dc[1]).normalize();
      camera.position.copy(center).addScaledVector(d, dist);
      // Straight down: the model's back toward the top of the image (contract +Y is glTF -Z).
      camera.up.set(0, 1, 0);
      if (Math.abs(dc[2]) > 0.999) camera.up.set(0, 0, -1);
      camera.lookAt(center);
      camera.updateMatrixWorld(true);
    };
    // Each view framed on the box corners it sees, so every tile is as large as it can be.
    const corners = [0, 1, 2, 3, 4, 5, 6, 7].map(
      (i) => new THREE.Vector3(i & 1 ? box.max.x : box.min.x, i & 2 ? box.max.y : box.min.y, i & 4 ? box.max.z : box.min.z),
    );
    const fit = (view: ProofView) => {
      aim(view, radius * 4);
      const inv = camera.matrixWorldInverse;
      const c = center.clone().applyMatrix4(inv);
      let half = 0;
      for (const p of corners) {
        const q = p.clone().applyMatrix4(inv);
        half = Math.max(half, Math.abs(q.x - c.x), Math.abs(q.y - c.y));
      }
      return Math.max(radius * 1.05, (half * 1.1) / Math.tan(((fov / 2) * Math.PI) / 180));
    };

    const out = {} as GlbViews;
    for (const view of PROOF_ORDER) {
      aim(view, fit(view));
      // Light from over the camera's shoulder, so every side reads the same.
      key.position.copy(camera.position).add(new THREE.Vector3(0, radius * 1.5, 0));
      key.target.position.copy(center);
      renderer.render(scene, camera);
      out[view] = renderer.domElement.toDataURL("image/png");
    }
    disposeTree(scene);
    return out;
  } finally {
    renderer.dispose();
  }
}

function disposeTree(root: { traverse: (fn: (o: unknown) => void) => void }) {
  root.traverse((o) => {
    const mesh = o as { isMesh?: boolean; geometry?: { dispose: () => void }; material?: unknown };
    if (!mesh.isMesh) return;
    mesh.geometry?.dispose();
    const mats = (Array.isArray(mesh.material) ? mesh.material : [mesh.material]) as { dispose?: () => void }[];
    for (const m of mats) {
      if (!m) continue;
      for (const v of Object.values(m)) if (v && typeof v === "object" && "isTexture" in v) (v as { dispose: () => void }).dispose();
      m.dispose?.();
    }
  });
}
