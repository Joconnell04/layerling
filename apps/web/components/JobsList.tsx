"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { STAGE_BY_ID } from "@/lib/contracts";
import type { JobRow } from "@/lib/db.types";
import { stageProgress } from "@/lib/derive";
import { ago, credits, titleCase, usd } from "@/lib/format";
import { PRODUCT_LABELS, nameTextOf } from "@/lib/products";
import { useJobs } from "@/lib/hooks";
import { JobStatusPill } from "./status";
import { Button, Empty, Segmented, Tip } from "./ui";
import { WorkerStatus } from "./WorkerStatus";

type Filter = "all" | "active" | "done";
const ACTIVE = new Set(["draft", "queued", "running", "waiting_user", "paused", "review"]);

export function JobsList() {
  const { jobs, error } = useJobs();
  const [filter, setFilter] = useState<Filter>("all");

  const shown = useMemo(
    () =>
      (jobs ?? []).filter((j) =>
        filter === "all" ? true : filter === "active" ? ACTIVE.has(j.status) : !ACTIVE.has(j.status),
      ),
    [jobs, filter],
  );

  const totals = useMemo(() => {
    const all = jobs ?? [];
    return { usd: all.reduce((s, j) => s + j.spent_usd, 0), n: all.length };
  }, [jobs]);

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <h1 className="font-display text-[34px] font-bold leading-none tracking-[-0.02em]">Jobs</h1>
        <span className="num text-sm text-muted">{totals.n}</span>
        <Tip label="All jobs">
          <span className="num text-sm text-muted">{usd(totals.usd)}</span>
        </Tip>
        <WorkerStatus />
        <div className="ml-auto flex items-center gap-2">
          <Segmented
            label="Filter"
            size="sm"
            value={filter}
            onChange={setFilter}
            options={[
              { value: "all", label: "All" },
              { value: "active", label: "Active" },
              { value: "done", label: "Closed" },
            ]}
          />
          <Link href="/jobs/new">
            <Button variant="primary" tabIndex={-1}>
              New job
            </Button>
          </Link>
        </div>
      </div>

      <div className="overflow-x-auto rounded-xl border border-line bg-panel">
        <div className="min-w-[760px]">
        <div className="grid grid-cols-[112px_minmax(0,1fr)_96px_150px_88px_64px_44px] items-center gap-3 border-b border-line px-4 py-2.5 text-xs text-muted">
          <span>Status</span>
          <span>Request</span>
          <span>Product</span>
          <span>Stage</span>
          <span className="text-right">Cost</span>
          <span className="text-right">Credits</span>
          <span className="text-right">Age</span>
        </div>
        {error && <Empty>{error}</Empty>}
        {jobs === null && !error && <Empty>Loading</Empty>}
        {jobs && shown.length === 0 && (
          <Empty>
            <Link href="/jobs/new" className="text-accent hover:underline">
              New job
            </Link>
          </Empty>
        )}
        <ul aria-label="Jobs">
          {shown.map((j) => (
            <JobRowItem key={j.id} job={j} />
          ))}
        </ul>
        </div>
      </div>
    </div>
  );
}

function JobRowItem({ job }: { job: JobRow }) {
  const [n, total] = stageProgress(job);
  const stage = job.stage ? STAGE_BY_ID[job.stage] : null;
  return (
    <li>
      <Link
        href={`/jobs/${job.id}`}
        className="grid h-12 grid-cols-[112px_minmax(0,1fr)_96px_150px_88px_64px_44px] items-center gap-3 border-b border-line/60 px-4 text-sm last:border-b-0 hover:bg-hover"
      >
        <span>
          <JobStatusPill status={job.status} />
        </span>
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate text-fg">{job.title ?? job.prompt}</span>
          {nameTextOf(job.addons) && (
            <span className="shrink-0 rounded-sm border border-line px-1 text-2xs font-semibold text-muted">
              {nameTextOf(job.addons)}
            </span>
          )}
        </span>
        <span className="text-muted">{PRODUCT_LABELS[job.product_type] ?? titleCase(job.product_type)}</span>
        <span className="flex min-w-0 items-center gap-2 text-muted">
          <span className="num shrink-0 text-muted">
            {n}/{total}
          </span>
          <span className="truncate">{stage?.name ?? "Queued"}</span>
        </span>
        <span className="num text-right text-fg">{usd(job.spent_usd)}</span>
        <span className="num text-right text-muted">{credits(job.spent_credits).replace(" cr", "")}</span>
        <span className="num text-right text-faint">{ago(job.created_at)}</span>
      </Link>
    </li>
  );
}
