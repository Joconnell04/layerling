"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { getDataClient } from "./data";
import type { DataClient, RowEvent, SessionUser } from "./data/types";
import type {
  AnyMessage,
  FilamentRow,
  JobRow,
  SpendRow,
  StageRunRow,
  WorkerRow,
} from "./db.types";

const DataContext = createContext<DataClient | null>(null);

/** Provides the data client. Tests pass `client`; the app loads the env-selected adapter. */
export function DataProvider({ client, children }: { client?: DataClient; children: ReactNode }) {
  const [loaded, setLoaded] = useState<DataClient | null>(client ?? null);
  useEffect(() => {
    if (client) return;
    let live = true;
    void getDataClient().then((c) => live && setLoaded(c));
    return () => {
      live = false;
    };
  }, [client]);
  return <DataContext.Provider value={loaded}>{children}</DataContext.Provider>;
}

export function useData(): DataClient | null {
  return useContext(DataContext);
}

export function upsertById<T extends { id: string | number }>(list: T[], row: T): T[] {
  const i = list.findIndex((x) => x.id === row.id);
  if (i === -1) return [...list, row];
  const next = list.slice();
  next[i] = row;
  return next;
}

// ------------------------------------------------------------------ session

export function useSession(): { user: SessionUser | null | undefined; client: DataClient | null } {
  const client = useData();
  const [user, setUser] = useState<SessionUser | null | undefined>(undefined);
  useEffect(() => {
    if (!client) return;
    let live = true;
    void client.getUser().then((u) => live && setUser(u));
    const off = client.onAuthChange((u) => setUser(u));
    return () => {
      live = false;
      off();
    };
  }, [client]);
  return { user, client };
}

// ------------------------------------------------------------------ jobs

export function useJobs() {
  const client = useData();
  const [jobs, setJobs] = useState<JobRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!client) return;
    let live = true;
    client
      .listJobs()
      .then((j) => live && setJobs(j))
      .catch((e: Error) => live && setError(e.message));
    const off = client.subscribeJobs((e) => {
      if (e.table !== "jobs") return;
      setJobs((cur) => {
        const next = upsertById(cur ?? [], e.row);
        return next.sort((a, b) => b.created_at.localeCompare(a.created_at));
      });
    });
    return () => {
      live = false;
      off();
    };
  }, [client]);
  return { jobs, error };
}

export interface JobState {
  job: JobRow | null;
  runs: StageRunRow[];
  messages: AnyMessage[];
  spend: SpendRow[];
  loading: boolean;
  notFound: boolean;
  error: string | null;
}

export function useJob(jobId: string | undefined): JobState & { refresh: () => void } {
  const client = useData();
  const [state, setState] = useState<JobState>({
    job: null,
    runs: [],
    messages: [],
    spend: [],
    loading: true,
    notFound: false,
    error: null,
  });
  const [nonce, setNonce] = useState(0);
  const refresh = useCallback(() => setNonce((n) => n + 1), []);
  const spendTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!client || !jobId) return;
    let live = true;
    Promise.all([
      client.getJob(jobId),
      client.listStageRuns(jobId),
      client.listMessages(jobId),
      client.listSpend(jobId),
    ])
      .then(([job, runs, messages, spend]) => {
        if (!live) return;
        setState({ job, runs, messages, spend, loading: false, notFound: !job, error: null });
      })
      .catch((e: Error) => live && setState((s) => ({ ...s, loading: false, error: e.message })));

    // spend is not in the Realtime publication: refetch it when the job row changes.
    const refetchSpend = () => {
      if (spendTimer.current) clearTimeout(spendTimer.current);
      spendTimer.current = setTimeout(() => {
        void client.listSpend(jobId).then((spend) => live && setState((s) => ({ ...s, spend })));
      }, 250);
    };

    const off = client.subscribeJob(jobId, (e: RowEvent) => {
      setState((s) => {
        switch (e.table) {
          case "jobs":
            return { ...s, job: e.row, notFound: false };
          case "stage_runs":
            return { ...s, runs: upsertById(s.runs, e.row) };
          case "messages":
            return {
              ...s,
              messages: upsertById(s.messages, e.row).sort((a, b) =>
                a.created_at.localeCompare(b.created_at),
              ),
            };
          default:
            return s;
        }
      });
      if (e.table === "jobs") refetchSpend();
    });
    return () => {
      live = false;
      off();
      if (spendTimer.current) clearTimeout(spendTimer.current);
    };
  }, [client, jobId, nonce]);

  return { ...state, refresh };
}

// ------------------------------------------------------------------ filaments

export function useFilaments() {
  const client = useData();
  const [filaments, setFilaments] = useState<FilamentRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    if (!client) return;
    client
      .listFilaments()
      .then(setFilaments)
      .catch((e: Error) => setError(e.message));
  }, [client]);
  useEffect(reload, [reload]);
  const put = useCallback((row: FilamentRow) => {
    setFilaments((cur) => upsertById(cur ?? [], row));
  }, []);
  return { filaments, error, reload, put };
}

// ------------------------------------------------------------------ workers

export function useWorkers(pollMs = 30_000) {
  const client = useData();
  const [workers, setWorkers] = useState<WorkerRow[] | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!client) return;
    let live = true;
    const load = () =>
      client
        .listWorkers()
        .then((w) => live && setWorkers(w))
        .catch(() => live && setWorkers([]));
    void load();
    const poll = setInterval(load, pollMs);
    const tick = setInterval(() => setNow(Date.now()), 5_000);
    const off = client.subscribeWorkers((e) => {
      if (e.table === "workers") setWorkers((cur) => upsertById(cur ?? [], e.row));
    });
    return () => {
      live = false;
      clearInterval(poll);
      clearInterval(tick);
      off();
    };
  }, [client, pollMs]);
  return { workers, now };
}

// ------------------------------------------------------------------ storage

/** Resolves a job-relative path to a signed (or mock) URL. */
export function useArtifactUrl(
  jobId: string | undefined,
  path: string | null | undefined,
  bucket: "artifacts" | "inputs" = "artifacts",
): string | null {
  const client = useData();
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!client || !jobId || !path) {
      setUrl(null);
      return;
    }
    let live = true;
    const p = bucket === "inputs" ? client.inputUrl(jobId, path) : client.artifactUrl(jobId, path);
    p.then((u) => live && setUrl(u)).catch(() => live && setUrl(null));
    return () => {
      live = false;
    };
  }, [client, jobId, path, bucket]);
  return url;
}

/** Fetch and parse a small JSON artifact (geometry.json, warnings.json). null until loaded or on error. */
export function useArtifactJson<T>(jobId: string | undefined, path: string | null | undefined): T | null {
  const url = useArtifactUrl(jobId, path);
  const [data, setData] = useState<T | null>(null);
  useEffect(() => {
    if (!url) {
      setData(null);
      return;
    }
    let live = true;
    fetch(url, { credentials: "same-origin" })
      .then((r) => (r.ok ? (r.json() as Promise<T>) : null))
      .then((d) => live && setData(d))
      .catch(() => live && setData(null));
    return () => {
      live = false;
    };
  }, [url]);
  return data;
}

export function useSwatchUrl(path: string | null | undefined): string | null {
  const client = useData();
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!client || !path) {
      setUrl(null);
      return;
    }
    let live = true;
    client
      .swatchUrl(path)
      .then((u) => live && setUrl(u))
      .catch(() => live && setUrl(null));
    return () => {
      live = false;
    };
  }, [client, path]);
  return url;
}
