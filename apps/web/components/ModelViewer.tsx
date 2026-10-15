"use client";

// three.js GLB viewer. Texture view shows the Meshy GLB as delivered; filament view shows the
// worker's preview GLB (per-face COLOR_0 in filament colors, linear per glTF) with flat shading.

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { Segmented } from "./ui";

export type ViewMode = "texture" | "filament";

interface Props {
  textureUrl?: string | null;
  filamentUrl?: string | null;
  /** Pixel height, or "fill" to fill the parent (proof sheet). */
  height?: number | "fill";
  initial?: ViewMode;
  /** Controlled mode (the proof sheet puts the toggle in its header). */
  mode?: ViewMode;
  onModeChange?: (m: ViewMode) => void;
  /** No frame, background or toggle: the parent draws them. */
  bare?: boolean;
  /** Called after each load: whether the model carries per-vertex (filament) colors. */
  onInfo?: (info: ModelInfo) => void;
}

export interface ModelInfo {
  url: string;
  mode: ViewMode;
  hasColors: boolean;
  tris: number;
}

export function effectiveMode(mode: ViewMode, textureUrl?: string | null, filamentUrl?: string | null): ViewMode {
  if (mode === "filament" && filamentUrl) return "filament";
  if (mode === "texture" && textureUrl) return "texture";
  return filamentUrl ? "filament" : "texture";
}

export default function ModelViewer({
  textureUrl,
  filamentUrl,
  height = 380,
  initial,
  mode: controlled,
  onModeChange,
  bare,
  onInfo,
}: Props) {
  const [pick, setPick] = useState<ViewMode | null>(initial ?? null);
  const want: ViewMode = controlled ?? pick ?? (textureUrl ? "texture" : "filament");
  const effective = effectiveMode(want, textureUrl, filamentUrl);
  const url = effective === "filament" ? filamentUrl : textureUrl;
  const setMode = (m: ViewMode) => {
    setPick(m);
    onModeChange?.(m);
  };

  const info = (i: Omit<ModelInfo, "mode">) => onInfo?.({ ...i, mode: effective });
  if (bare) return <Canvas url={url ?? null} flat={effective === "filament"} height={height} grid={false} onInfo={info} />;

  return (
    <div className="relative overflow-hidden rounded-lg border border-line bg-panel">
      <Canvas url={url ?? null} flat={effective === "filament"} height={height} grid onInfo={info} />
      <div className="absolute left-2 top-2 flex items-center gap-2">
        <ViewToggle value={effective} onChange={setMode} textureUrl={textureUrl} filamentUrl={filamentUrl} size="sm" />
      </div>
    </div>
  );
}

export function ViewToggle({
  value,
  onChange,
  textureUrl,
  filamentUrl,
  size = "md",
}: {
  value: ViewMode;
  onChange: (m: ViewMode) => void;
  textureUrl?: string | null;
  filamentUrl?: string | null;
  size?: "sm" | "md";
}) {
  return (
    <Segmented<ViewMode>
      size={size}
      label="View"
      value={value}
      onChange={onChange}
      options={[
        { value: "filament", label: "Print", disabled: !filamentUrl },
        { value: "texture", label: "Model", disabled: !textureUrl },
      ]}
    />
  );
}

function Canvas({
  url,
  flat,
  height,
  grid: showGrid,
  onInfo,
}: {
  url: string | null;
  flat: boolean;
  height: number | "fill";
  grid: boolean;
  onInfo?: (i: { url: string; hasColors: boolean; tris: number }) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const ctx = useRef<{
    renderer: THREE.WebGLRenderer;
    scene: THREE.Scene;
    camera: THREE.PerspectiveCamera;
    controls: OrbitControls;
    model: THREE.Object3D | null;
    grid: THREE.GridHelper | null;
  } | null>(null);
  const [status, setStatus] = useState<"idle" | "loading" | "error" | "ready">("idle");
  const onInfoRef = useRef(onInfo);
  onInfoRef.current = onInfo;

  // Scene setup, once.
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    } catch {
      setStatus("error");
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.setClearColor(0x000000, 0);
    el.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    scene.add(new THREE.HemisphereLight(0xffffff, 0x2a2b31, 1.6));
    const key = new THREE.DirectionalLight(0xffffff, 1.8);
    key.position.set(0.6, 1, 0.9);
    scene.add(key);
    const rim = new THREE.DirectionalLight(0xffffff, 0.6);
    rim.position.set(-0.8, 0.4, -1);
    scene.add(rim);

    const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 10000);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.12;

    const resize = () => {
      const w = el.clientWidth || 1;
      const h = el.clientHeight || 1;
      renderer.setSize(w, h, false);
      renderer.domElement.style.width = "100%";
      renderer.domElement.style.height = "100%";
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(el);

    let raf = 0;
    const loop = () => {
      controls.update();
      renderer.render(scene, camera);
      raf = requestAnimationFrame(loop);
    };
    loop();

    ctx.current = { renderer, scene, camera, controls, model: null, grid: null };
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      controls.dispose();
      disposeTree(scene);
      renderer.dispose();
      renderer.domElement.remove();
      ctx.current = null;
    };
  }, []);

  // Model load on URL or mode change.
  useEffect(() => {
    const c = ctx.current;
    if (!c || !url) return;
    let cancelled = false;
    setStatus("loading");
    new GLTFLoader().load(
      url,
      (gltf) => {
        if (cancelled) {
          disposeTree(gltf.scene);
          return;
        }
        if (c.model) {
          c.scene.remove(c.model);
          disposeTree(c.model);
        }
        if (c.grid) {
          c.scene.remove(c.grid);
          c.grid.geometry.dispose();
        }
        const model = gltf.scene;
        let tris = 0;
        const hasColors = paintedModel(model);
        model.traverse((o) => {
          const mesh = o as THREE.Mesh;
          if (!mesh.isMesh) return;
          const g = mesh.geometry;
          tris += (g.index ? g.index.count : g.attributes.position.count) / 3;
          // Only repaint when the file really has filament colors; otherwise keep its own
          // material so an unpainted preview never shows as a white model.
          if (flat && hasColors) {
            // Filament view: per-face color as assigned (COLOR_0, else the material color),
            // faceted, matte, no texture.
            const olds = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
            const next = olds.map(
              (old) =>
                new THREE.MeshLambertMaterial({
                  vertexColors: !!g.attributes.color,
                  color: g.attributes.color ? 0xffffff : ((old as THREE.MeshStandardMaterial).color ?? new THREE.Color(0xffffff)),
                  flatShading: true,
                }),
            );
            mesh.material = Array.isArray(mesh.material) ? next : next[0];
            olds.forEach((m) => m.dispose());
          }
        });
        const box = new THREE.Box3().setFromObject(model);
        const size = box.getSize(new THREE.Vector3());
        const center = box.getCenter(new THREE.Vector3());
        model.position.sub(new THREE.Vector3(center.x, box.min.y, center.z));
        c.scene.add(model);
        c.model = model;

        const r = Math.max(size.x, size.y, size.z);
        if (showGrid) {
          const grid = new THREE.GridHelper(r * 3, 24, 0xc9bfae, 0xe4dbcb);
          c.scene.add(grid);
          c.grid = grid;
        }

        // A piece lying flat on the bed (keychain, magnet) is seen from above at an angle, the
        // way it sits on the plate; an upright figure from the front, slightly above.
        const lying = size.y < 0.45 * Math.max(size.x, size.z);
        const dist = (r / 2 / Math.tan((c.camera.fov * Math.PI) / 360)) * (lying ? 1.2 : 1.6);
        const az = ((lying ? -12 : -35) * Math.PI) / 180;
        const el = ((lying ? 62 : 18) * Math.PI) / 180;
        const target = new THREE.Vector3(0, size.y / 2, 0);
        c.camera.position.set(
          target.x + Math.sin(-az) * dist * Math.cos(el),
          target.y + Math.sin(el) * dist,
          target.z + Math.cos(az) * dist * Math.cos(el),
        );
        c.camera.near = dist / 100;
        c.camera.far = dist * 20;
        c.camera.updateProjectionMatrix();
        c.controls.target.copy(target);
        c.controls.update();
        onInfoRef.current?.({ url, hasColors, tris: Math.round(tris) });
        setStatus("ready");
      },
      undefined,
      () => !cancelled && setStatus("error"),
    );
    return () => {
      cancelled = true;
    };
  }, [url, flat, showGrid]);

  return (
    <div className="relative" style={{ height: height === "fill" ? "100%" : height }}>
      <div ref={host} className="absolute inset-0" data-testid="viewer-canvas" />
      {!url && <Overlay>No model</Overlay>}
      {url && status === "loading" && <Overlay>Loading</Overlay>}
      {status === "error" && <Overlay>Viewer error</Overlay>}
    </div>
  );
}

/**
 * True when a GLB carries filament colors: per-vertex COLOR_0, or untextured materials in at
 * least two colors (one primitive per filament). A Meshy GLB has a base color texture.
 */
export function paintedModel(root: THREE.Object3D): boolean {
  let vertexColors = false;
  let textured = false;
  const colors = new Set<string>();
  root.traverse((o) => {
    const mesh = o as THREE.Mesh;
    if (!mesh.isMesh) return;
    if (mesh.geometry.attributes.color) vertexColors = true;
    for (const m of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
      const std = m as THREE.MeshStandardMaterial;
      if (std?.map) textured = true;
      if (std?.color) colors.add(std.color.getHexString());
    }
  });
  return vertexColors || (!textured && colors.size >= 2);
}

function Overlay({ children }: { children: React.ReactNode }) {
  return (
    <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-xs text-faint">
      {children}
    </div>
  );
}

function disposeTree(root: THREE.Object3D) {
  root.traverse((o) => {
    const mesh = o as THREE.Mesh;
    if (!mesh.isMesh) return;
    mesh.geometry?.dispose();
    const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
    for (const m of mats) {
      if (!m) continue;
      for (const v of Object.values(m)) if (v instanceof THREE.Texture) v.dispose();
      m.dispose();
    }
  });
}
