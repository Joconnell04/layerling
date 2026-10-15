import type { JobStatus, StageStatus } from "@/lib/contracts";
import { Pill, type Tone } from "./ui";

export const JOB_STATUS: Record<JobStatus, { tone: Tone; label: string }> = {
  draft: { tone: "faint", label: "Draft" },
  queued: { tone: "faint", label: "Queued" },
  running: { tone: "run", label: "Running" },
  waiting_user: { tone: "accent", label: "Your turn" },
  paused: { tone: "pause", label: "Paused" },
  review: { tone: "review", label: "Review" },
  done: { tone: "ok", label: "Done" },
  failed: { tone: "bad", label: "Failed" },
  cancelled: { tone: "faint", label: "Cancelled" },
};

export const STAGE_STATUS: Record<StageStatus, { tone: Tone; label: string }> = {
  pending: { tone: "faint", label: "Pending" },
  running: { tone: "run", label: "Running" },
  waiting_user: { tone: "accent", label: "Your turn" },
  waiting_vendor: { tone: "run", label: "Meshy" },
  succeeded: { tone: "ok", label: "Done" },
  failed: { tone: "bad", label: "Failed" },
  paused: { tone: "pause", label: "Paused" },
  skipped: { tone: "faint", label: "Skipped" },
  stale: { tone: "faint", label: "Stale" },
};

export function JobStatusPill({ status }: { status: JobStatus }) {
  const s = JOB_STATUS[status] ?? JOB_STATUS.queued;
  return (
    <Pill tone={s.tone} pulse={status === "running"}>
      {s.label}
    </Pill>
  );
}
