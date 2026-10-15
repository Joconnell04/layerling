"use client";

import { useState } from "react";
import { useArtifactUrl } from "@/lib/hooks";
import { cx } from "./ui";

/** A job image by job-relative path. Space is reserved by the caller; a skeleton shows until it loads. */
export function ArtifactImage({
  jobId,
  path,
  alt,
  bucket = "artifacts",
  className,
  fit = "cover",
}: {
  jobId: string;
  path: string | null | undefined;
  alt: string;
  bucket?: "artifacts" | "inputs";
  className?: string;
  fit?: "cover" | "contain";
}) {
  const url = useArtifactUrl(jobId, path, bucket);
  const [loaded, setLoaded] = useState<string | null>(null);
  const ready = !!url && loaded === url;
  return (
    <div className={cx("relative overflow-hidden bg-bg", className)}>
      {!ready && <div className="absolute inset-0 animate-pulse bg-raised" aria-hidden data-testid="image-skeleton" />}
      {url && (
        // Signed Storage URLs; next/image would need remotePatterns per project.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={url}
          alt={alt}
          onLoad={() => setLoaded(url)}
          className={cx("relative size-full transition-opacity", fit === "cover" ? "object-cover" : "object-contain", ready ? "opacity-100" : "opacity-0")}
        />
      )}
    </div>
  );
}
