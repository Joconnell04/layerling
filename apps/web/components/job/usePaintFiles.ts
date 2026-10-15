"use client";

// Loads the paint behind a result for the editor: paint_state.json, the region map it names,
// face_regions.npy, the figure PLY the map indexes, and the add-on parts from s06's geometry.

import { useEffect, useState } from "react";
import type { PaintState, RegionMap } from "@/lib/contracts.gen";
import { useData } from "@/lib/hooks";
import { readNpyInt32, readPly, type TriMesh } from "@/lib/meshFiles";

export interface PaintFiles {
  folder: string;
  state: PaintState;
  map: RegionMap;
  faceRegions: Int32Array;
  figure: TriMesh;
  parts: { name: string; mesh: TriMesh; filament: string | null }[];
}

interface GeometryParts {
  parts?: { name: string; role?: string; filament_id?: string | null; mesh: { path: string } }[];
}

export function usePaintFiles(jobId: string, folder: string | null, geometryPath: string | null) {
  const client = useData();
  const [state, setState] = useState<{ key: string; files: PaintFiles | null; error: string | null }>({ key: "", files: null, error: null });
  const key = `${jobId}|${folder ?? ""}|${geometryPath ?? ""}`;
  useEffect(() => {
    if (!client || !folder) return;
    let live = true;
    const url = (p: string) => client.artifactUrl(jobId, p);
    const get = async (p: string) => {
      const res = await fetch(await url(p), { credentials: "same-origin" });
      if (!res.ok) throw new Error(`${p}: ${res.status}`);
      return res;
    };
    (async () => {
      const ps = (await (await get(`${folder}/paint_state.json`)).json()) as PaintState;
      const map = (await (await get(ps.region_map.path)).json()) as RegionMap;
      const [fr, ply] = await Promise.all([
        get(map.face_regions.path).then((r) => r.arrayBuffer()),
        get(map.mesh.path).then((r) => r.arrayBuffer()),
      ]);
      const faceRegions = readNpyInt32(fr);
      const figure = readPly(ply);
      if (faceRegions.length !== figure.indices.length / 3) {
        throw new Error(`face_regions has ${faceRegions.length} ids for ${figure.indices.length / 3} faces`);
      }
      const parts: PaintFiles["parts"] = [];
      if (geometryPath) {
        try {
          const geo = (await (await get(geometryPath)).json()) as GeometryParts;
          for (const p of geo.parts ?? []) {
            if (p.role === "cutter") continue;
            const mesh = readPly(await (await get(p.mesh.path)).arrayBuffer());
            parts.push({ name: p.name, mesh, filament: ps.part_filaments?.[p.name] ?? p.filament_id ?? null });
          }
        } catch {
          // parts are context only; the figure is what the editor paints
        }
      }
      return { folder, state: ps, map, faceRegions, figure, parts };
    })()
      .then((files) => live && setState({ key, files, error: null }))
      .catch((e: Error) => live && setState({ key, files: null, error: e.message }));
    return () => {
      live = false;
    };
  }, [client, jobId, folder, geometryPath, key]);
  const current = state.key === key;
  return { files: current ? state.files : null, error: current ? state.error : null, loading: !!folder && !current };
}
