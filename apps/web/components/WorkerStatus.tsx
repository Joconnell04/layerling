"use client";

import { workerState } from "@/lib/derive";
import { ago } from "@/lib/format";
import { useWorkers } from "@/lib/hooks";
import { Dot, Tip, type Tone } from "./ui";

const TONE: Record<string, Tone> = { online: "ok", stale: "pause", offline: "bad" };
const LABEL: Record<string, string> = { online: "Worker", stale: "Worker stale", offline: "Worker offline" };

export function WorkerStatus() {
  const { workers, now } = useWorkers();
  if (workers === null) return null;
  const latest = [...workers].sort((a, b) => b.last_seen.localeCompare(a.last_seen))[0];
  const state = workerState(latest?.last_seen, now);
  return (
    <Tip
      side="bottom"
      label={
        latest ? (
          <span className="num grid grid-cols-[auto_auto] gap-x-3 gap-y-0.5">
            <span className="text-faint">Id</span>
            <span>{latest.id}</span>
            <span className="text-faint">Seen</span>
            <span>{ago(latest.last_seen, now)}</span>
            <span className="text-faint">Version</span>
            <span>{latest.version ?? "?"}</span>
            <span className="text-faint">Meshy</span>
            <span>{latest.meshy_mode ?? "?"}</span>
          </span>
        ) : (
          "No heartbeat"
        )
      }
    >
      <span
        tabIndex={0}
        data-testid="worker-status"
        data-state={state}
        className="inline-flex h-6 items-center gap-1.5 rounded-md px-2 text-xs text-muted hover:bg-hover"
      >
        <Dot tone={TONE[state]} pulse={state === "online"} />
        {LABEL[state]}
        {latest?.meshy_mode && (
          <span
            className={
              latest.meshy_mode === "live"
                ? "rounded-sm bg-bad/15 px-1 text-2xs font-semibold text-bad"
                : "rounded-sm bg-line px-1 text-2xs text-muted"
            }
          >
            {latest.meshy_mode}
          </span>
        )}
      </span>
    </Tip>
  );
}
