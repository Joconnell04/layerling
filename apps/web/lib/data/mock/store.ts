// In-memory tables for mock mode. Every write emits a RowEvent, like Supabase Realtime.

import type { AnyMessage, FilamentRow, JobRow, SpendRow, StageRunRow, WorkerRow } from "../../db.types";
import type { RowEvent } from "../types";

export const MOCK_USER_ID = "00000000-0000-4000-8000-ffffffffffff";

export class MockStore {
  jobs = new Map<string, JobRow>();
  runs = new Map<string, StageRunRow>();
  messages = new Map<string, AnyMessage>();
  spend: SpendRow[] = [];
  filaments = new Map<string, FilamentRow>();
  workers = new Map<string, WorkerRow>();
  /** Object URLs for uploaded files, keyed by `<bucket>/<key>`. */
  blobs = new Map<string, string>();

  private listeners = new Set<(e: RowEvent) => void>();
  private spendSeq = 1;
  private idSeq = 1;
  /** Deterministic ids while seeding, random afterwards. */
  deterministicIds = false;
  /** Clock for row timestamps; seeding swaps in a virtual clock. */
  now: () => Date = () => new Date();

  newId(): string {
    if (this.deterministicIds) {
      // Stable across loads (seed rows keep their URLs) but not all zeros.
      const n = this.idSeq++;
      const h = (k: number) => (Math.imul(n * 2654435761 + k, 2246822519) >>> 0).toString(16).padStart(8, "0");
      return `${h(1)}-${h(2).slice(0, 4)}-4${h(3).slice(0, 3)}-8${h(4).slice(0, 3)}-${h(5)}${h(6).slice(0, 4)}`;
    }
    if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-8xxx-xxxxxxxxxxxx".replace(/x/g, () =>
      Math.floor(Math.random() * 16).toString(16),
    );
  }

  iso(): string {
    return this.now().toISOString();
  }

  on(cb: (e: RowEvent) => void): () => void {
    this.listeners.add(cb);
    return () => this.listeners.delete(cb);
  }

  private emit(e: RowEvent): void {
    const copy = { ...e, row: structuredClone(e.row) } as RowEvent;
    for (const cb of [...this.listeners]) cb(copy);
  }

  putJob(job: JobRow): JobRow {
    this.jobs.set(job.id, job);
    this.emit({ table: "jobs", row: job });
    return job;
  }

  patchJob(id: string, patch: Partial<JobRow>): JobRow {
    const cur = this.jobs.get(id);
    if (!cur) throw new Error(`job ${id} not found`);
    return this.putJob({ ...cur, ...patch, updated_at: this.iso() });
  }

  putRun(run: StageRunRow): StageRunRow {
    this.runs.set(run.id, run);
    this.emit({ table: "stage_runs", row: run });
    return run;
  }

  patchRun(id: string, patch: Partial<StageRunRow>): StageRunRow {
    const cur = this.runs.get(id);
    if (!cur) throw new Error(`stage run ${id} not found`);
    return this.putRun({ ...cur, ...patch, updated_at: this.iso() });
  }

  putMessage(msg: AnyMessage): AnyMessage {
    this.messages.set(msg.id, msg);
    this.emit({ table: "messages", row: msg });
    return msg;
  }

  addSpend(row: Omit<SpendRow, "id">): SpendRow {
    const full = { ...row, id: this.spendSeq++ };
    this.spend.push(full);
    return full;
  }

  putFilament(f: FilamentRow): FilamentRow {
    this.filaments.set(f.id, f);
    return f;
  }

  putWorker(w: WorkerRow): WorkerRow {
    this.workers.set(w.id, w);
    this.emit({ table: "workers", row: w });
    return w;
  }

  runsFor(jobId: string): StageRunRow[] {
    return [...this.runs.values()].filter((r) => r.job_id === jobId);
  }

  messagesFor(jobId: string): AnyMessage[] {
    return [...this.messages.values()]
      .filter((m) => m.job_id === jobId)
      .sort((a, b) => a.created_at.localeCompare(b.created_at));
  }
}
