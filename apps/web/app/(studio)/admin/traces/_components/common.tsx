"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type ReactNode, type RefObject } from "react";
import { JOB_STATUS } from "@/components/status";
import { Pill, Tip, cx, type Tone } from "@/components/ui";
import type { JobStatus } from "@/lib/contracts";
import { ago, duration } from "@/lib/format";
import { PRODUCT_LABELS } from "@/lib/products";

export const MESHY_USD_TIP = "$0.02 per credit (assumed)";
export type FetchImpl = typeof fetch;

// ------------------------------------------------------------------ formatting

export const money = (n: number | null | undefined) => (n ?? 0).toFixed(2);

/** 41.5 s under a minute, else 1m 44s. */
export function secs(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "";
  return n < 60 ? `${n.toFixed(1)} s` : duration(n);
}

export function when(iso: string | null | undefined): string {
  const a = ago(iso);
  return !a || a === "now" ? a : `${a.replace(/(\d)([a-z])$/, "$1 $2")} ago`;
}

export const productLabel = (p: string) => PRODUCT_LABELS[p as keyof typeof PRODUCT_LABELS] ?? p;

export function fileName(p: string): string {
  return p.split("/").pop() ?? p;
}

/** Short caption for a thumbnail: front, r1c1, approved, photo 1. */
export function caption(p: string): string {
  const stem = fileName(p).replace(/\.[a-z0-9]+$/i, "");
  const thumb = /^thumb_m\d+_(\w+)$/.exec(stem);
  if (thumb) return thumb[1];
  const concept = /^concept_(r\d+c\d+)$/.exec(stem);
  if (concept) return concept[1];
  if (stem === "approved_concept") return "approved";
  return stem.replace(/_/g, " ");
}

export const isImage = (p: string) => /\.(png|jpe?g|webp|gif)$/i.test(p);

/** claude-haiku-4-5-20251001 -> claude-haiku-4-5 (the full id sits in a tooltip). */
export const modelLabel = (m: string) => m.replace(/-\d{8}$/, "");

export function Caret({ open }: { open: boolean }) {
  return (
    <svg
      aria-hidden
      viewBox="0 0 10 10"
      className={cx("size-2.5 shrink-0 text-faint transition-transform", open && "rotate-90")}
    >
      <path d="M3.5 1.8 7 5 3.5 8.2" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function ModelName({ model, className }: { model: string; className?: string }) {
  const short = modelLabel(model);
  const text = <span className={cx("truncate font-medium", className)}>{short}</span>;
  return short === model ? text : (
    <Tip label={model} className="min-w-0">
      {text}
    </Tip>
  );
}

export function fileUrl(jobId: string, p: string): string {
  const bucket = p.startsWith("input/") ? "inputs" : "artifacts";
  return `/api/admin/traces/${jobId}/file?bucket=${bucket}&path=${encodeURIComponent(p)}`;
}

// ------------------------------------------------------------------ data

export function useJson<T>(url: string | null, fetchImpl: FetchImpl = fetch) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const load = useCallback(async () => {
    if (!url) return;
    setLoading(true);
    try {
      const res = await fetchImpl(url, { cache: "no-store" });
      if (!res.ok) {
        const msg = res.status === 401 ? "Sign in required" : res.status === 404 ? "Not found" : `Load failed (${res.status})`;
        throw new Error(msg);
      }
      setData((await res.json()) as T);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [url, fetchImpl]);
  useEffect(() => {
    void load();
  }, [load]);
  return { data, error, loading, reload: load };
}

// ------------------------------------------------------------------ chrome

export function TracesTop({ tab, right }: { tab: "jobs" | "calls"; right?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-4">
      <h1 className="font-display text-[34px] font-bold leading-none tracking-[-0.02em]">Traces</h1>
      <nav aria-label="Traces" className="flex h-8 items-end gap-4 text-sm">
        {(
          [
            ["jobs", "Jobs", "/admin/traces"],
            ["calls", "Calls", "/admin/traces/calls"],
          ] as const
        ).map(([id, label, href]) => (
          <Link
            key={id}
            href={href}
            aria-current={tab === id ? "page" : undefined}
            className={cx(
              "border-b-2 pb-1 leading-5 transition-colors",
              tab === id ? "border-accent font-semibold text-fg hover:text-fg" : "border-transparent text-muted hover:text-fg",
            )}
          >
            {label}
          </Link>
        ))}
      </nav>
      <div className="ml-auto flex items-center gap-2">{right}</div>
    </div>
  );
}

export function SearchBox({
  value,
  onChange,
  placeholder,
  inputRef,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  inputRef: RefObject<HTMLInputElement | null>;
}) {
  return (
    <label className="relative flex h-8 w-64 items-center rounded-md border border-line bg-panel pl-2.5 pr-1.5 focus-within:border-ink">
      <input
        ref={inputRef}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            onChange("");
            e.currentTarget.blur();
          }
        }}
        placeholder={placeholder}
        aria-label={placeholder}
        className="min-w-0 flex-1 bg-transparent text-[13px] text-fg placeholder:text-faint focus:outline-none"
      />
    </label>
  );
}

/** Job status pill. Brick is kept for failures and the active tab, so "your turn" reads blue. */
export function StatusPill({ status }: { status: string }) {
  const s = JOB_STATUS[status as JobStatus] ?? { tone: "faint" as Tone, label: status };
  return (
    <Pill tone={s.tone === "accent" ? "run" : s.tone} pulse={status === "running"}>
      {s.label}
    </Pill>
  );
}

const ATTEMPT_TONE: Record<string, [Tone, string]> = {
  succeeded: ["ok", "Done"],
  running: ["run", "Running"],
  waiting_user: ["run", "Waiting"],
  waiting_vendor: ["run", "Meshy"],
  pending: ["faint", "Pending"],
  failed: ["bad", "Failed"],
  paused: ["pause", "Paused"],
  stale: ["faint", "Stale"],
  skipped: ["faint", "Skipped"],
};

export function attemptTone(status: string): [Tone, string] {
  return ATTEMPT_TONE[status] ?? ["faint", status];
}

export function AttemptPill({ status }: { status: string }) {
  const [tone, label] = attemptTone(status);
  return <Pill tone={tone}>{label}</Pill>;
}

export function CallStatus({ status }: { status: string }) {
  const tone: Tone = status === "ok" ? "ok" : status === "pending" ? "run" : "bad";
  return <Pill tone={tone}>{status}</Pill>;
}

export function Thumb({ jobId, path, size = 72, label = true }: { jobId: string; path: string; size?: number; label?: boolean }) {
  const [failed, setFailed] = useState(false);
  const url = fileUrl(jobId, path);
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className="group/thumb flex shrink-0 flex-col items-center gap-1 text-faint hover:text-fg"
      style={{ width: size }}
    >
      {failed ? (
        <span
          className="flex items-center justify-center rounded-md border border-dashed border-line bg-canvas text-2xs"
          style={{ width: size, height: size }}
        >
          {fileName(path).split(".").pop()}
        </span>
      ) : (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={url}
          alt={caption(path)}
          loading="lazy"
          onError={() => setFailed(true)}
          className="rounded-md border border-line bg-canvas object-contain transition-colors group-hover/thumb:border-line-strong"
          style={{ width: size, height: size }}
        />
      )}
      {label && <span className="w-full truncate text-center text-2xs">{caption(path)}</span>}
    </a>
  );
}

export function FileChip({ jobId, path, note }: { jobId: string; path: string; note?: string }) {
  return (
    <a
      href={fileUrl(jobId, path)}
      target="_blank"
      rel="noreferrer"
      className="inline-flex h-6 max-w-full items-center gap-1.5 rounded-full border border-line bg-panel px-2.5 text-xs text-muted hover:border-line-strong hover:text-fg"
    >
      <span className="truncate">{fileName(path)}</span>
      {note && <span className="text-faint">{note}</span>}
    </a>
  );
}

export function Chip({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span className={cx("num inline-flex h-6 items-center rounded-md bg-raised px-2 text-xs text-fg", className)}>{children}</span>
  );
}

export function TipHead({ children, tip, className }: { children: ReactNode; tip: string; className?: string }) {
  return (
    <Tip label={tip} side="bottom" className={className}>
      <span className="cursor-help border-b border-dotted border-faint">{children}</span>
    </Tip>
  );
}
