"use client";

import dynamic from "next/dynamic";
import { useArtifactUrl } from "@/lib/hooks";
import type { ModelInfo, ViewMode } from "./ModelViewer";

const ModelViewer = dynamic(() => import("./ModelViewer"), {
  ssr: false,
  loading: () => <div className="h-full min-h-[200px] animate-pulse bg-raised/50" />,
});

/** Resolves job-relative GLB paths to URLs and renders the three.js viewer client-side. */
export function Viewer({
  jobId,
  texturePath,
  filamentPath,
  height,
  initial,
  mode,
  onModeChange,
  bare,
  onInfo,
}: {
  jobId: string;
  texturePath?: string | null;
  filamentPath?: string | null;
  height?: number | "fill";
  initial?: ViewMode;
  mode?: ViewMode;
  onModeChange?: (m: ViewMode) => void;
  bare?: boolean;
  onInfo?: (i: ModelInfo) => void;
}) {
  const textureUrl = useArtifactUrl(jobId, texturePath);
  const filamentUrl = useArtifactUrl(jobId, filamentPath);
  return (
    <ModelViewer
      textureUrl={textureUrl}
      filamentUrl={filamentUrl}
      height={height}
      initial={initial}
      mode={mode}
      onModeChange={onModeChange}
      bare={bare}
      onInfo={onInfo}
    />
  );
}
