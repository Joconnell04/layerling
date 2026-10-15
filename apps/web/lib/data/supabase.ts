// Supabase adapter: auth, jobs, stage_runs, messages, filaments, spend, workers, Storage, Realtime.
// RLS (see the migration) scopes every table to auth.uid(); the worker uses the service role.

import { createBrowserClient } from "@supabase/ssr";
import type { RealtimePostgresChangesPayload, SupabaseClient, User } from "@supabase/supabase-js";
import type { MessagePayloadMap } from "../contracts.gen";
import type { UserPayloadKind } from "../contracts";
import type {
  AnyMessage,
  FilamentRow,
  JobRow,
  MessageRow,
  SpendRow,
  StageRunRow,
  WorkerRow,
} from "../db.types";
import { assertPayload } from "../validate";
import { addonsFor } from "../products";
import { filamentRowFrom } from "./filaments";
import {
  PAINT_STROKE_PATH,
  artifactStorageKey,
  inputStorageKey,
  photoExt,
  sha256Hex,
  type DataClient,
  type FilamentInput,
  type NewJobInput,
  type RowEvent,
  type SendMessageInput,
  type SessionUser,
  type Unsubscribe,
} from "./types";

const SIGNED_URL_TTL_S = 60 * 60;

export { normalizeJob, normalizeRun, normalizeSpend } from "./normalize";
import { missingOptionalColumn, normalizeJob, normalizeRun, normalizeSpend, withoutOptionalColumns } from "./normalize";

function toSession(u: User | null | undefined): SessionUser | null {
  return u ? { id: u.id, email: u.email ?? null } : null;
}

// The client is untyped (no generated Database type); rows are cast to lib/db.types at the edge.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function check<T = any>(res: { data: unknown; error: { message: string } | null }): T {
  if (res.error) throw new Error(res.error.message);
  return res.data as T;
}

export class SupabaseDataClient implements DataClient {
  readonly mode = "supabase" as const;
  private urlCache = new Map<string, { url: string; exp: number }>();
  private channelSeq = 0;

  constructor(readonly sb: SupabaseClient) {}

  /** Browser client that keeps the session in cookies, so the proxy and API routes can see it. */
  static fromEnv(url: string, anonKey: string): SupabaseDataClient {
    return new SupabaseDataClient(createBrowserClient(url, anonKey) as unknown as SupabaseClient);
  }

  // ------------------------------------------------------------------ auth

  async getUser(): Promise<SessionUser | null> {
    const { data } = await this.sb.auth.getSession();
    return toSession(data.session?.user);
  }

  async signIn(email: string, password: string): Promise<SessionUser> {
    const { data, error } = await this.sb.auth.signInWithPassword({ email, password });
    if (error) throw new Error(error.message);
    return toSession(data.user)!;
  }

  async signOut(): Promise<void> {
    await this.sb.auth.signOut();
  }

  onAuthChange(cb: (user: SessionUser | null) => void): Unsubscribe {
    const { data } = this.sb.auth.onAuthStateChange((_e, session) => cb(toSession(session?.user)));
    return () => data.subscription.unsubscribe();
  }

  private async uid(): Promise<string> {
    const u = await this.getUser();
    if (!u) throw new Error("Not signed in");
    return u.id;
  }

  // ------------------------------------------------------------------ jobs

  async listJobs(): Promise<JobRow[]> {
    const rows = check(
      await this.sb.from("jobs").select("*").order("created_at", { ascending: false }).limit(200),
    );
    return (rows as Record<string, unknown>[]).map(normalizeJob);
  }

  async getJob(id: string): Promise<JobRow | null> {
    const row = check(await this.sb.from("jobs").select("*").eq("id", id).maybeSingle());
    return row ? normalizeJob(row as Record<string, unknown>) : null;
  }

  /**
   * Insert as draft (RLS allows draft or queued), upload photos into inputs/<job_id>/, record them
   * on the row, add the prompt message, then flip to queued so the worker only sees complete jobs.
   */
  async createJob(input: NewJobInput): Promise<JobRow> {
    if (!input.prompt.trim()) throw new Error("Prompt required");
    if (input.photos.length === 0) throw new Error("Add at least one photo");
    const ownerId = await this.uid();
    const row: Record<string, unknown> = {
      owner_id: ownerId,
      title: input.title ?? null,
      prompt: input.prompt.trim(),
      product_type: input.productType,
      profile: input.profile,
      target_size_mm: input.targetSizeMm,
      addons: input.addons ?? addonsFor(input.productType),
      status: "draft",
    };
    if (input.form) row.form = input.form;
    let res = await this.sb.from("jobs").insert(row).select("*").single();
    if (res.error && missingOptionalColumn(res.error)) {
      res = await this.sb.from("jobs").insert(withoutOptionalColumns(row)).select("*").single();
    }
    const draft = check(res) as Record<string, unknown>;
    const jobId = draft.id as string;

    const photos = [];
    for (const [i, file] of input.photos.entries()) {
      const path = `input/photo_${i + 1}.${photoExt(file)}`;
      const up = await this.sb.storage
        .from("inputs")
        .upload(inputStorageKey(jobId, path), file, { contentType: file.type || undefined, upsert: true });
      if (up.error) throw new Error(`Upload ${file.name}: ${up.error.message}`);
      photos.push({ path, sha256: await sha256Hex(file) });
    }

    check(
      await this.sb.from("messages").insert({
        job_id: jobId,
        owner_id: ownerId,
        role: "user",
        kind: "prompt",
        body: input.prompt.trim(),
        payload: {},
      }),
    );

    const job = check(
      await this.sb
        .from("jobs")
        .update({ photos, status: "queued" })
        .eq("id", jobId)
        .select("*")
        .single(),
    );
    return normalizeJob(job as Record<string, unknown>);
  }

  async listStageRuns(jobId: string): Promise<StageRunRow[]> {
    const rows = check(
      await this.sb
        .from("stage_runs")
        .select("*")
        .eq("job_id", jobId)
        .order("stage")
        .order("attempt"),
    );
    return (rows as Record<string, unknown>[]).map(normalizeRun);
  }

  async listMessages(jobId: string): Promise<AnyMessage[]> {
    const rows = check(
      await this.sb
        .from("messages")
        .select("*")
        .eq("job_id", jobId)
        .order("created_at")
        .order("id"),
    );
    return rows as AnyMessage[];
  }

  async listMessagesByKind<K extends keyof MessagePayloadMap>(
    jobIds: string[],
    kind: K,
  ): Promise<MessageRow<K>[]> {
    if (jobIds.length === 0) return [];
    const rows = check(
      await this.sb
        .from("messages")
        .select("*")
        .in("job_id", jobIds)
        .eq("kind", kind)
        .order("created_at"),
    );
    return rows as MessageRow<K>[];
  }

  async listSpend(jobId?: string): Promise<SpendRow[]> {
    let q = this.sb.from("spend").select("*").order("at", { ascending: false }).limit(500);
    if (jobId) q = q.eq("job_id", jobId);
    const rows = check(await q);
    return (rows as Record<string, unknown>[]).map(normalizeSpend);
  }

  async sendMessage<K extends UserPayloadKind>(input: SendMessageInput<K>): Promise<MessageRow<K>> {
    assertPayload(input.kind, input.payload);
    const ownerId = await this.uid();
    const row = check(
      await this.sb
        .from("messages")
        .insert({
          job_id: input.jobId,
          owner_id: ownerId,
          role: "user",
          kind: input.kind,
          stage: input.stage ?? null,
          body: input.body ?? null,
          payload: input.payload,
          reply_to: input.replyTo ?? null,
        })
        .select("*")
        .single(),
    );
    return row as MessageRow<K>;
  }

  // ------------------------------------------------------------------ filaments

  async listFilaments(): Promise<FilamentRow[]> {
    const own = check(
      await this.sb
        .from("filaments")
        .select("*")
        .order("owned", { ascending: false })
        .order("name"),
    ) as FilamentRow[];
    // Jackson's AMS spools are seeded with owner_id null, which RLS (owner_id = auth.uid()) hides
    // from a signed-in session; the studio route reads them with the service role.
    const ids = new Set(own.map((f) => f.id));
    const shared = (await this.sharedFilaments()).filter((f) => !ids.has(f.id));
    if (!shared.length) return own;
    return [...own, ...shared].sort((a, b) => Number(b.owned) - Number(a.owned) || a.name.localeCompare(b.name));
  }

  private async sharedFilaments(): Promise<FilamentRow[]> {
    if (typeof window === "undefined" || typeof fetch !== "function") return [];
    try {
      const res = await fetch("/api/studio/filaments", { credentials: "same-origin", cache: "no-store" });
      if (!res.ok) return [];
      const body = (await res.json()) as { filaments?: FilamentRow[] };
      return (body.filaments ?? []).filter((f) => f.owner_id == null);
    } catch {
      return [];
    }
  }

  async saveFilament(input: FilamentInput): Promise<FilamentRow> {
    const ownerId = await this.uid();
    let existing: FilamentRow | undefined;
    if (input.id) {
      existing =
        (check(await this.sb.from("filaments").select("*").eq("id", input.id).maybeSingle()) as
          | FilamentRow
          | null) ?? undefined;
    }
    const row = filamentRowFrom(input, existing, new Date().toISOString(), ownerId);
    const { updated_at: _skip, ...values } = row;
    const q = input.id
      ? this.sb.from("filaments").update(values).eq("id", input.id)
      : this.sb.from("filaments").insert(values);
    return check(await q.select("*").single()) as FilamentRow;
  }

  async uploadSwatch(filamentId: string, file: File): Promise<FilamentRow> {
    const ownerId = await this.uid();
    const key = `${ownerId}/${filamentId}.${photoExt(file)}`;
    const up = await this.sb.storage
      .from("swatches")
      .upload(key, file, { contentType: file.type || undefined, upsert: true });
    if (up.error) throw new Error(up.error.message);
    this.urlCache.delete(`swatches/${key}`);
    return check(
      await this.sb
        .from("filaments")
        .update({ swatch_path: key })
        .eq("id", filamentId)
        .select("*")
        .single(),
    ) as FilamentRow;
  }

  // ------------------------------------------------------------------ workers

  async listWorkers(): Promise<WorkerRow[]> {
    return check(
      await this.sb.from("workers").select("*").order("last_seen", { ascending: false }),
    ) as WorkerRow[];
  }

  // ------------------------------------------------------------------ storage

  private async signed(bucket: string, key: string): Promise<string> {
    const cacheKey = `${bucket}/${key}`;
    const hit = this.urlCache.get(cacheKey);
    if (hit && hit.exp > Date.now()) return hit.url;
    const { data, error } = await this.sb.storage
      .from(bucket)
      .createSignedUrl(key, SIGNED_URL_TTL_S);
    if (error || !data) throw new Error(error?.message ?? "No signed URL");
    this.urlCache.set(cacheKey, { url: data.signedUrl, exp: Date.now() + (SIGNED_URL_TTL_S - 60) * 1000 });
    return data.signedUrl;
  }

  async uploadInput(jobId: string, path: string, body: Blob): Promise<void> {
    if (!PAINT_STROKE_PATH.test(path)) throw new Error(`Not an editor upload path: ${path}`);
    const up = await this.sb.storage
      .from("inputs")
      .upload(inputStorageKey(jobId, path), body, { contentType: "text/plain", upsert: true });
    if (up.error) throw new Error(up.error.message);
  }

  artifactUrl(jobId: string, path: string): Promise<string> {
    return this.signed("artifacts", artifactStorageKey(jobId, path));
  }

  inputUrl(jobId: string, path: string): Promise<string> {
    return this.signed("inputs", inputStorageKey(jobId, path));
  }

  swatchUrl(path: string): Promise<string> {
    return this.signed("swatches", path);
  }

  // ------------------------------------------------------------------ realtime

  private channel(name: string) {
    return this.sb.channel(`${name}:${++this.channelSeq}`);
  }

  subscribeJob(jobId: string, cb: (e: RowEvent) => void): Unsubscribe {
    const ch = this.channel(`job:${jobId}`)
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "jobs", filter: `id=eq.${jobId}` },
        (p: RealtimePostgresChangesPayload<Record<string, unknown>>) => {
          if (p.eventType !== "DELETE") cb({ table: "jobs", row: normalizeJob(p.new) });
        },
      )
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "stage_runs", filter: `job_id=eq.${jobId}` },
        (p: RealtimePostgresChangesPayload<Record<string, unknown>>) => {
          if (p.eventType !== "DELETE") cb({ table: "stage_runs", row: normalizeRun(p.new) });
        },
      )
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "messages", filter: `job_id=eq.${jobId}` },
        (p: RealtimePostgresChangesPayload<Record<string, unknown>>) => {
          if (p.eventType !== "DELETE") cb({ table: "messages", row: p.new as unknown as AnyMessage });
        },
      )
      .subscribe();
    return () => {
      void this.sb.removeChannel(ch);
    };
  }

  subscribeJobs(cb: (e: RowEvent) => void): Unsubscribe {
    const ch = this.channel("jobs")
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "jobs" },
        (p: RealtimePostgresChangesPayload<Record<string, unknown>>) => {
          if (p.eventType !== "DELETE") cb({ table: "jobs", row: normalizeJob(p.new) });
        },
      )
      .subscribe();
    return () => {
      void this.sb.removeChannel(ch);
    };
  }

  subscribeWorkers(cb: (e: RowEvent) => void): Unsubscribe {
    const ch = this.channel("workers")
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "workers" },
        (p: RealtimePostgresChangesPayload<Record<string, unknown>>) => {
          if (p.eventType !== "DELETE") cb({ table: "workers", row: p.new as unknown as WorkerRow });
        },
      )
      .subscribe();
    return () => {
      void this.sb.removeChannel(ch);
    };
  }
}
