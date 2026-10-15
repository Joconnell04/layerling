import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MockDataClient } from "@/lib/data/mock/client";
import type { AnyMessage, JobRow } from "@/lib/db.types";
import { DataProvider } from "@/lib/hooks";

export function mockClient(opts: { seed?: boolean } = {}) {
  return new MockDataClient({ seed: opts.seed ?? true, heartbeatMs: null, stepMs: 50 });
}

export function renderWith(client: MockDataClient, ui: ReactElement) {
  return render(<DataProvider client={client}>{ui}</DataProvider>);
}

export async function seededJob(client: MockDataClient, match: RegExp): Promise<JobRow> {
  const job = (await client.listJobs()).find((j) => match.test(j.prompt));
  if (!job) throw new Error(`no seeded job matching ${match}`);
  return job;
}

export function userMessages(client: MockDataClient, jobId: string): AnyMessage[] {
  return client.store.messagesFor(jobId).filter((m) => m.role === "user" && m.kind !== "prompt");
}
