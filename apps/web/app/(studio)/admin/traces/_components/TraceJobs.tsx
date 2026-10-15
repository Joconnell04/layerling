"use client";

import Link from "next/link";
import { useMemo, useRef, useState } from "react";
import { Button, Empty, ErrorLine, Segmented, Tip, cx, inputCls } from "@/components/ui";
import { PRODUCT_TYPES } from "@/lib/db.types";
import { duration } from "@/lib/format";
import { STAGE_LABEL } from "@/lib/traces/derive";
import type { TraceJobRow } from "@/lib/traces/types";
import {
  MESHY_USD_TIP,
  SearchBox,
  StatusPill,
  TipHead,
  TracesTop,
  money,
  productLabel,
  useJson,
  when,
  type FetchImpl,
} from "./common";

type StatusFilter = "all" | "active" | "done" | "review" | "failed";
const GROUP: Record<string, StatusFilter> = {
  draft: "active",
  queued: "active",
  running: "active",
  waiting_user: "active",
  paused: "active",
  done: "done",
  review: "review",
  failed: "failed",
  cancelled: "failed",
};
const COLS = "grid-cols-[minmax(0,1fr)_104px_112px_100px_68px_68px_76px_76px]";

export function TraceJobs({ fetchImpl = fetch }: { fetchImpl?: FetchImpl }) {
  const { data, error, loading, reload } = useJson<{ jobs: TraceJobRow[] }>("/api/admin/traces", fetchImpl);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [product, setProduct] = useState("all");
  const search = useRef<HTMLInputElement>(null);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (data?.jobs ?? []).filter(
      (j) =>
        (status === "all" || GROUP[j.status] === status) &&
        (product === "all" || j.product_type === product) &&
        (!needle || `${j.name} ${j.prompt} ${j.id}`.toLowerCase().includes(needle)),
    );
  }, [data, q, status, product]);

  return (
    <div className="space-y-4">
      <TracesTop
        tab="jobs"
        right={
          <Button size="sm" variant="ghost" onClick={() => void reload()} disabled={loading}>
            Refresh
          </Button>
        }
      />
      <div className="flex flex-wrap items-center gap-2">
        <SearchBox value={q} onChange={setQ} placeholder="Search jobs" inputRef={search} />
        <Segmented
          label="Status"
          size="sm"
          value={status}
          onChange={setStatus}
          options={[
            { value: "all", label: "All" },
            { value: "active", label: "Active" },
            { value: "done", label: "Done" },
            { value: "review", label: "Review" },
            { value: "failed", label: "Failed" },
          ]}
        />
        <select
          aria-label="Product"
          value={product}
          onChange={(e) => setProduct(e.target.value)}
          className={inputCls("h-8 w-36 rounded-full px-3 text-xs")}
        >
          <option value="all">All products</option>
          {PRODUCT_TYPES.map((p) => (
            <option key={p} value={p}>
              {productLabel(p)}
            </option>
          ))}
        </select>
        <span className="num ml-auto text-xs text-faint">{data ? `${shown.length} of ${data.jobs.length}` : ""}</span>
      </div>
      <ErrorLine>{error}</ErrorLine>

      <div className="overflow-x-auto rounded-lg border border-line bg-panel">
        <div className="min-w-[860px]">
          <div
            className={cx(
              "grid h-8 items-center gap-3 border-b border-line bg-bg/60 px-3 text-xs font-medium text-muted",
              COLS,
            )}
          >
            <span>Job</span>
            <span>Product</span>
            <span>Status</span>
            <span>Stage</span>
            <span className="text-right">Credits</span>
            <span className="text-right">
              <TipHead tip={MESHY_USD_TIP}>$</TipHead>
            </span>
            <span className="text-right">Time</span>
            <span className="text-right">Created</span>
          </div>
          {data === null && !error && <Empty>Loading</Empty>}
          {data && shown.length === 0 && <Empty>No jobs</Empty>}
          <ul aria-label="Trace jobs">
            {shown.map((j, i) => (
              <li key={j.id}>
                <Link
                  href={`/admin/traces/${j.id}`}
                  data-testid={`trace-job-${j.id}`}
                  className={cx(
                    "grid h-9 items-center gap-3 border-b border-line/70 px-3 text-[13.5px] text-fg hover:text-fg",
                    COLS,
                    "hover:bg-hover",
                  )}
                >
                  <span className="flex min-w-0 items-baseline gap-2">
                    <span className={cx("truncate font-medium", j.name !== j.prompt && "max-w-[55%] flex-none")}>
                      {j.name}
                    </span>
                    {j.name !== j.prompt && <span className="min-w-0 truncate text-xs text-faint">{j.prompt}</span>}
                  </span>
                  <span className="truncate text-muted">{productLabel(j.product_type)}</span>
                  <span>
                    <StatusPill status={j.status} />
                  </span>
                  <span className="truncate text-muted">{j.stage ? STAGE_LABEL[j.stage] : "Queued"}</span>
                  <span className="num text-right">{j.credits}</span>
                  <span className="num text-right">{money(j.usd)}</span>
                  <span className="num text-right text-muted">{duration(j.wall_s)}</span>
                  <span className="num text-right text-faint">
                    <Tip label={new Date(j.created_at).toLocaleString()} side="bottom">
                      <span>{when(j.created_at)}</span>
                    </Tip>
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
