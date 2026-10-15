"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import type { JobRow, MessageRow, StageRunRow } from "@/lib/db.types";
import { ago, pct, usd } from "@/lib/format";
import { useData, useJobs } from "@/lib/hooks";
import { paintInfo } from "@/lib/paintInfo";
import { LOW_CONFIDENCE, PaintPanel } from "./job/PaintPanel";
import { ReviewPanel } from "./ReviewPanel";
import { Empty, Tag } from "./ui";

/** In the queue: jobs waiting on review, and any other live job whose confidence is low. */
export function inQueue(job: JobRow): boolean {
  if (job.status === "review") return true;
  return job.confidence != null && job.confidence < LOW_CONFIDENCE && job.status !== "failed" && job.status !== "done";
}

export function ReviewQueue() {
  const client = useData();
  const { jobs, error } = useJobs();
  const queue = useMemo(
    () => (jobs ?? []).filter(inQueue).sort((a, b) => (a.confidence ?? 1) - (b.confidence ?? 1)),
    [jobs],
  );
  const ids = queue.map((j) => j.id).join(",");
  const [requests, setRequests] = useState<Record<string, MessageRow<"review_request">>>({});
  const [decisions, setDecisions] = useState<Record<string, MessageRow<"review_decision">>>({});

  useEffect(() => {
    if (!client || !ids) return;
    let live = true;
    const jobIds = ids.split(",");
    void Promise.all([
      client.listMessagesByKind(jobIds, "review_request"),
      client.listMessagesByKind(jobIds, "review_decision"),
    ]).then(([reqs, decs]) => {
      if (!live) return;
      const latestReq: Record<string, MessageRow<"review_request">> = {};
      for (const r of reqs) latestReq[r.job_id] = r; // ordered by created_at
      const dec: Record<string, MessageRow<"review_decision">> = {};
      for (const d of decs) if (latestReq[d.job_id] && d.created_at >= latestReq[d.job_id].created_at) dec[d.job_id] = d;
      setRequests(latestReq);
      setDecisions(dec);
    });
    return () => {
      live = false;
    };
  }, [client, ids]);

  // Pick up decisions made here without waiting for the job row to change.
  useEffect(() => {
    if (!client || !ids) return;
    const offs = ids.split(",").map((id) =>
      client.subscribeJob(id, (e) => {
        if (e.table === "messages" && e.row.kind === "review_decision") {
          setDecisions((d) => ({ ...d, [id]: e.row as MessageRow<"review_decision"> }));
        }
      }),
    );
    return () => offs.forEach((off) => off());
  }, [client, ids]);

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <h1 className="font-display text-[34px] font-bold leading-none tracking-[-0.02em]">Review</h1>
        <span className="num text-sm text-faint">{queue.length}</span>
      </div>
      {error && <Empty>{error}</Empty>}
      {jobs && queue.length === 0 && <Empty>Queue empty</Empty>}
      {queue.map((job) => {
        const req = requests[job.id];
        return (
          <div key={job.id} data-testid={`review-${job.id}`}>
            {req ? (
              <ReviewPanel
                jobId={job.id}
                request={req}
                decision={decisions[job.id] ?? null}
                compact
                title={
                  <span className="flex min-w-0 items-center gap-2">
                    <Link href={`/jobs/${job.id}`} className="truncate hover:text-accent">
                      {job.title ?? job.prompt}
                    </Link>
                    <span className="num shrink-0 text-xs font-normal text-faint">{ago(job.updated_at)}</span>
                    <span className="num shrink-0 text-xs font-normal text-faint">{usd(job.spent_usd)}</span>
                  </span>
                }
              />
            ) : (
              <div className="flex h-10 items-center gap-2 rounded-lg border border-line bg-panel px-3 text-sm">
                <Link href={`/jobs/${job.id}`} className="truncate hover:text-accent">
                  {job.title ?? job.prompt}
                </Link>
                {job.confidence != null && <Tag className="num">{pct(job.confidence)}</Tag>}
                <span className="text-xs text-faint">{job.review_reason ?? "No request"}</span>
              </div>
            )}
            <QueuePaint job={job} request={req ?? null} />
          </div>
        );
      })}
    </div>
  );
}

/** The job's Paint panel under its review row, when stage 9 has run. */
function QueuePaint({ job, request }: { job: JobRow; request: MessageRow<"review_request"> | null }) {
  const client = useData();
  const [runs, setRuns] = useState<StageRunRow[] | null>(null);
  useEffect(() => {
    if (!client) return;
    let live = true;
    void client.listStageRuns(job.id).then((r) => live && setRuns(r));
    return () => {
      live = false;
    };
  }, [client, job.id, job.updated_at]);
  const info = useMemo(() => (runs ? paintInfo(runs, request ? [request] : []) : null), [runs, request]);
  if (!info) return null;
  return (
    <div className="mt-2">
      {/* The review row above already states the reason. */}
      <PaintPanel jobId={job.id} info={info} showReason={!request} />
    </div>
  );
}
