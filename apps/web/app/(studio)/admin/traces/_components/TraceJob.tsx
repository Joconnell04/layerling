"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import { Button, Empty, ErrorLine, Tip, cx, type Tone } from "@/components/ui";
import { duration } from "@/lib/format";
import { STAGE_LABEL } from "@/lib/traces/derive";
import type { AttemptView, JobTrace, SlicerSummary, TraceBody, TraceEntry } from "@/lib/traces/types";
import {
  AttemptPill,
  Caret,
  Chip,
  FileChip,
  MESHY_USD_TIP,
  ModelName,
  StatusPill,
  Thumb,
  TipHead,
  attemptTone,
  fileName,
  isImage,
  money,
  productLabel,
  secs,
  useJson,
  type FetchImpl,
} from "./common";

export function TraceJob({ jobId, fetchImpl = fetch }: { jobId: string; fetchImpl?: FetchImpl }) {
  const { data, error, loading, reload } = useJson<JobTrace>(`/api/admin/traces/${jobId}`, fetchImpl);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!data) return;
    const hash = decodeURIComponent(window.location.hash.slice(1));
    if (hash) document.getElementById(hash)?.scrollIntoView({ block: "start" });
  }, [data]);

  const back = (
    <Link href="/admin/traces" className="inline-flex items-center gap-1.5 text-xs text-faint hover:text-fg">
      <span aria-hidden>&larr;</span> Traces
    </Link>
  );
  if (!data) {
    return (
      <div className="space-y-3">
        {back}
        <ErrorLine>{error}</ErrorLine>
        {!error && <Empty>Loading</Empty>}
      </div>
    );
  }
  const { job, request, attempts, skipped, totals } = data;
  const copy = () => {
    void navigator.clipboard?.writeText(job.id).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        {back}
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="font-display text-[30px] font-bold leading-none tracking-[-0.02em]">{job.name}</h1>
          <span className="text-sm text-muted">{productLabel(job.product_type)}</span>
          <StatusPill status={job.status} />
          <Tip label={job.id} side="bottom">
            <span className="font-mono text-xs text-muted">{job.id.slice(0, 8)}</span>
          </Tip>
          <Button size="sm" variant="ghost" onClick={copy}>
            {copied ? "Copied" : "Copy id"}
          </Button>
          <div className="ml-auto flex items-center gap-2">
            <Link href={`/jobs/${job.id}`} tabIndex={-1}>
              <Button size="sm">Job page</Button>
            </Link>
            {job.threemf && (
              <a href={`/api/studio/jobs/${job.id}/download`} tabIndex={-1}>
                <Button size="sm">3MF</Button>
              </a>
            )}
            <Button size="sm" variant="ghost" onClick={() => void reload()} disabled={loading}>
              Refresh
            </Button>
          </div>
        </div>
        <ErrorLine>{error}</ErrorLine>
        <div className="num flex flex-wrap gap-5 text-[13px] text-muted" data-testid="trace-totals">
          <span>
            Credits <b className="font-semibold text-fg">{totals.credits}</b>
          </span>
          <span>
            <TipHead tip={MESHY_USD_TIP}>$</TipHead> <b className="font-semibold text-fg">{money(totals.usd)}</b>
          </span>
          <span>
            List $ <b className="font-semibold text-fg">{money(totals.usd_list)}</b>
          </span>
          <span>
            <TipHead tip="Stage time of the current attempts">Time</TipHead>{" "}
            <b className="font-semibold text-fg">{duration(totals.wall_s) || "none"}</b>
          </span>
          <span>
            Calls <b className="font-semibold text-fg">{totals.calls}</b>
          </span>
        </div>
      </div>

      <ol className="relative space-y-3.5 pl-7 before:absolute before:bottom-4 before:left-[9px] before:top-4 before:w-px before:bg-line-strong">
        <RequestCard jobId={job.id} request={request} />
        {attempts.map((a) => (
          <AttemptCard key={a.key} jobId={job.id} a={a} fetchImpl={fetchImpl} />
        ))}
        {skipped.length > 0 && (
          <li className="relative text-[13px] text-faint">
            <Dot tone="faint" hollow className="top-[5px]" />
            Skipped: {skipped.map((s) => STAGE_LABEL[s]).join(", ")}
          </li>
        )}
      </ol>
    </div>
  );
}

// ------------------------------------------------------------------ pieces

const DOT: Record<Tone, string> = {
  ok: "border-ok bg-ok",
  run: "border-run bg-run",
  bad: "border-bad bg-bad",
  pause: "border-pause bg-pause",
  review: "border-review bg-review",
  accent: "border-accent bg-accent",
  faint: "border-faint bg-panel",
};

function Dot({ tone, hollow, className }: { tone: Tone; hollow?: boolean; className?: string }) {
  return (
    <span
      aria-hidden
      className={cx(
        "absolute -left-[25px] top-[15px] size-[11px] rounded-full border-2",
        hollow ? "border-line-strong bg-bg" : DOT[tone],
        className,
      )}
    />
  );
}

function Card({ id, tone, dim, children }: { id?: string; tone: Tone; dim?: boolean; children: ReactNode }) {
  return (
    <li
      id={id}
      className={cx(
        "relative scroll-mt-20 rounded-lg border border-line bg-panel shadow-[3px_3px_0_var(--color-shadow)]",
        dim && "opacity-60 transition-opacity hover:opacity-100",
      )}
    >
      <Dot tone={tone} hollow={tone === "faint"} />
      {children}
    </li>
  );
}

function CardHead({ title, children, meta }: { title: string; children?: ReactNode; meta?: ReactNode }) {
  return (
    <header className="flex min-h-11 flex-wrap items-center gap-3 border-b border-line px-4 py-2">
      <h2 className="font-display text-[17px] font-semibold leading-none">{title}</h2>
      {children}
      <div className="num ml-auto flex items-center gap-4 text-xs text-muted">{meta}</div>
    </header>
  );
}

function Sec({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[84px_minmax(0,1fr)] gap-3 border-b border-line px-4 py-2.5 last:border-b-0">
      <div className="pt-[3px] text-xs text-faint">{k}</div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function Pre({ children, tall }: { children: ReactNode; tall?: boolean }) {
  return (
    <pre
      className={cx(
        "overflow-auto whitespace-pre-wrap break-words font-mono text-xs leading-[17px] text-fg",
        tall ? "max-h-[28rem]" : "max-h-72",
      )}
    >
      {children}
    </pre>
  );
}

function Fold({ label, children, initial = false }: { label: ReactNode; children: ReactNode; initial?: boolean }) {
  const [open, setOpen] = useState(initial);
  return (
    <div className="space-y-1.5">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="inline-flex items-center gap-1.5 text-xs text-muted hover:text-fg"
      >
        <Caret open={open} />
        {label}
      </button>
      {open && children}
    </div>
  );
}

function pretty(v: unknown): string {
  if (typeof v === "string") {
    const t = v.trim();
    if (t.startsWith("{") || t.startsWith("[")) {
      try {
        return JSON.stringify(JSON.parse(t), null, 2);
      } catch {
        return v;
      }
    }
    return v;
  }
  return JSON.stringify(v, null, 2);
}

function bytes(n: number | null | undefined): string | undefined {
  if (!n || n < 100_000) return undefined;
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1e3)} KB`;
}

// ------------------------------------------------------------------ request

function RequestCard({ jobId, request }: { jobId: string; request: JobTrace["request"] }) {
  return (
    <Card tone="ok">
      <CardHead
        title="Request"
        meta={
          <>
            <span>{productLabel(request.product_type)}</span>
            {request.size_mm != null && <span>{request.size_mm} mm</span>}
            {request.name_text && <span>Name &quot;{request.name_text}&quot;</span>}
          </>
        }
      />
      <Sec k="Prompt">
        <p className="text-sm text-fg">{request.prompt}</p>
      </Sec>
      {request.photos.length > 0 && (
        <Sec k="Photos">
          <div className="flex flex-wrap gap-2.5">
            {request.photos.map((p) => (
              <Thumb key={p} jobId={jobId} path={p} size={96} />
            ))}
          </div>
        </Sec>
      )}
      {request.addons.length > 0 && (
        <Sec k="Add-ons">
          <div className="flex flex-wrap gap-1.5">
            {request.addons.map((a, i) => (
              <Chip key={i}>{a}</Chip>
            ))}
          </div>
        </Sec>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------ attempts

function AttemptCard({ jobId, a, fetchImpl }: { jobId: string; a: AttemptView; fetchImpl: FetchImpl }) {
  const [open, setOpen] = useState<number | null>(null);
  const [tone] = attemptTone(a.status);
  const images = a.outputs.filter((o) => isImage(o.path));
  const others = a.outputs.filter((o) => !isImage(o.path));
  return (
    <Card id={a.key} tone={tone} dim={a.stale}>
      <CardHead
        title={STAGE_LABEL[a.stage] ?? a.stage}
        meta={
          <>
            <span>{secs(a.duration_s)}</span>
            <span>{a.credits} cr</span>
            <span>${money(a.usd)}</span>
            {a.usd_list > 0 && <span className="text-faint">list {money(a.usd_list)}</span>}
          </>
        }
      >
        <span className="font-mono text-xs text-muted">a{a.attempt}</span>
        {a.stale_reason ? (
          <Tip label={a.stale_reason}>
            <AttemptPill status={a.status} />
          </Tip>
        ) : (
          <AttemptPill status={a.status} />
        )}
      </CardHead>
      {a.calls.length > 0 && (
        <Sec k="Calls">
          <div>
            {a.calls.map((c, i) => (
              <CallItem
                key={`${c.kind}-${c.ref ?? i}-${i}`}
                jobId={jobId}
                call={c}
                open={open === i}
                onToggle={() => setOpen(open === i ? null : i)}
                fetchImpl={fetchImpl}
              />
            ))}
          </div>
        </Sec>
      )}
      {a.slicer && (
        <Sec k="Slicer">
          <SlicerChips s={a.slicer} />
        </Sec>
      )}
      {a.outputs.length > 0 && (
        <Sec k="Outputs">
          <div className="flex flex-wrap items-start gap-2.5">
            {images.map((o) => (
              <Thumb key={o.path} jobId={jobId} path={o.path} />
            ))}
            {others.length > 0 && (
              <div className={cx("flex min-w-0 flex-wrap gap-1.5", images.length > 0 && "self-center")}>
                {others.map((o) => (
                  <FileChip key={o.path} jobId={jobId} path={o.path} note={bytes(o.bytes)} />
                ))}
              </div>
            )}
          </div>
        </Sec>
      )}
      {a.decisions.length > 0 && (
        <Sec k="Decisions">
          <Decisions items={a.decisions} />
        </Sec>
      )}
      {(a.error || a.pause_reason) && (
        <Sec k="Error">
          <p className="text-[13px] text-bad">{a.error ?? a.pause_reason}</p>
        </Sec>
      )}
      <Sec k="Raw">
        <RawManifest jobId={jobId} stage={a.stage} attempt={a.attempt} fetchImpl={fetchImpl} />
      </Sec>
    </Card>
  );
}

const CALL_COLS = "grid-cols-[12px_minmax(0,200px)_minmax(0,1fr)_64px_52px_60px_76px]";

function CallItem({
  jobId,
  call,
  open,
  onToggle,
  fetchImpl,
}: {
  jobId: string;
  call: TraceEntry;
  open: boolean;
  onToggle: () => void;
  fetchImpl: FetchImpl;
}) {
  const bad = call.status === "error";
  return (
    <div className="border-b border-dashed border-line last:border-b-0">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        data-testid="trace-call"
        className={cx("grid h-8 w-full items-center gap-2.5 text-left text-[13px] hover:bg-hover/70", CALL_COLS)}
      >
        <Caret open={open} />
        <ModelName model={call.model} className={bad ? "text-bad" : "text-fg"} />
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate text-muted">{call.purpose}</span>
          {call.status === "pending" && <span className="shrink-0 text-2xs text-run">pending</span>}
          {call.derived && (
            <Tip label="From the manifest; no trace body">
              <span className="shrink-0 text-2xs text-faint">derived</span>
            </Tip>
          )}
          {call.reconstructed && (
            <Tip label="Rebuilt from disk after the run">
              <span className="shrink-0 text-2xs text-faint">backfilled</span>
            </Tip>
          )}
        </span>
        <span className="num text-right text-muted">{secs(call.duration_s)}</span>
        <span className="num text-right text-muted">{call.kind === "meshy" ? `${call.credits} cr` : ""}</span>
        <span className="num text-right">{call.kind === "slicer" ? "" : `$${money(call.usd)}`}</span>
        <span className="num text-right text-faint">{call.usd_list > 0 ? `list ${money(call.usd_list)}` : ""}</span>
      </button>
      {open && <CallDetail jobId={jobId} call={call} fetchImpl={fetchImpl} />}
    </div>
  );
}

function Field({ k, children }: { k: string; children: ReactNode }) {
  return (
    <>
      <div className="pt-[3px] text-xs text-faint">{k}</div>
      <div className="min-w-0">{children}</div>
    </>
  );
}

function Params({ params }: { params: Record<string, unknown> }) {
  const entries = Object.entries(params).filter(([, v]) => v != null && v !== "");
  if (!entries.length) return <span className="text-faint">none</span>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {entries.map(([k, v]) => (
        <Chip key={k} className="max-w-full">
          <span className="text-muted">{k}</span>&nbsp;
          <span className="truncate">{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
        </Chip>
      ))}
    </div>
  );
}

function Refs({ jobId, paths }: { jobId: string; paths: string[] }) {
  if (!paths.length) return <span className="text-faint">none</span>;
  return (
    <div className="flex flex-wrap items-start gap-2">
      {paths.map((p) =>
        isImage(p) ? (
          <Thumb key={p} jobId={jobId} path={p} />
        ) : p.startsWith("unresolved:") ? (
          <Chip key={p} className="text-faint">
            {p.replace("unresolved:", "")} not matched
          </Chip>
        ) : (
          <FileChip key={p} jobId={jobId} path={p} />
        ),
      )}
    </div>
  );
}

function CallDetail({ jobId, call, fetchImpl }: { jobId: string; call: TraceEntry; fetchImpl: FetchImpl }) {
  const url = call.path ? `/api/admin/traces/${jobId}/body?path=${encodeURIComponent(call.path)}` : null;
  const { data, error } = useJson<{ body: TraceBody }>(url, fetchImpl);
  const body = data?.body ?? null;
  const loading = !!url && !body && !error;
  const text = body ? body.input.text : call.detail?.text ?? (call.summary || null);
  const images = body?.input.images ?? call.images;
  const params = body?.input.params ?? call.detail?.params ?? {};
  const files = body?.output.files?.length ? body.output.files : call.files;
  const outText = body?.output.text ?? null;
  const outJson = body?.output.json;
  const err = body?.error ?? call.error;

  return (
    <div
      data-testid="trace-call-detail"
      className="my-1.5 grid grid-cols-[64px_minmax(0,1fr)] gap-x-3 gap-y-2.5 rounded-md border border-line bg-bg px-3 py-2.5 text-[13px]"
    >
      {loading && <Field k="Body">Loading</Field>}
      {error && (
        <Field k="Body">
          <span className="text-faint">{error === "Not found" ? "Not in Storage yet" : error}</span>
        </Field>
      )}
      {call.kind === "claude" && body && (
        <Field k="System">
          {body.input.system ? (
            <Fold label={`${body.input.system.length.toLocaleString()} chars`}>
              <Pre>{body.input.system}</Pre>
            </Fold>
          ) : (
            <span className="text-faint">Not captured</span>
          )}
        </Field>
      )}
      {call.kind !== "slicer" && (
        <Field k="Prompt">
          {text ? <Pre>{text}</Pre> : <span className="text-faint">{call.kind === "claude" ? "Not captured" : "none"}</span>}
        </Field>
      )}
      {(call.kind !== "claude" || Object.keys(params).length > 0) && (
        <Field k="Params">
          <Params params={params} />
        </Field>
      )}
      {call.kind !== "slicer" && (
        <Field k="Refs">
          <Refs jobId={jobId} paths={images} />
        </Field>
      )}
      {call.kind === "claude" ? (
        <Field k="Reply">
          {outText || outJson != null ? (
            <Pre tall>{pretty(outText ?? outJson)}</Pre>
          ) : (
            <span className="text-faint">Not captured</span>
          )}
        </Field>
      ) : (
        <Field k="Result">
          <div className="space-y-2">
            {call.kind === "slicer" && outJson != null && <SlicerJson v={outJson as Record<string, unknown>} />}
            {files.length > 0 ? <Refs jobId={jobId} paths={files} /> : call.kind !== "slicer" && <span className="text-faint">No files</span>}
            {call.kind === "slicer" && outText && (
              <Fold label={`Log, ${outText.split("\n").length} lines`}>
                <Pre tall>{outText}</Pre>
              </Fold>
            )}
            {call.kind !== "slicer" && outJson != null && (
              <Fold label="Task JSON">
                <Pre tall>{pretty(outJson)}</Pre>
              </Fold>
            )}
          </div>
        </Field>
      )}
      {err && (
        <Field k="Error">
          <span className="text-bad">{err}</span>
        </Field>
      )}
      {body && call.kind === "claude" && (
        <Field k="Tokens">
          <div className="flex flex-wrap gap-1.5">
            <Chip>in {body.tokens.in.toLocaleString()}</Chip>
            <Chip>out {body.tokens.out.toLocaleString()}</Chip>
            {body.tokens.cache_write > 0 && <Chip>cache write {body.tokens.cache_write.toLocaleString()}</Chip>}
            {body.tokens.cache_read > 0 && <Chip>cache read {body.tokens.cache_read.toLocaleString()}</Chip>}
            {body.billing && <Chip className="text-muted">{body.billing}</Chip>}
          </div>
        </Field>
      )}
      {body && body.rounds.length > 0 && (
        <Field k="Rounds">
          <Pre>{pretty(body.rounds)}</Pre>
        </Field>
      )}
      <Field k="Raw">
        {call.path ? (
          body ? (
            <Fold label={fileName(call.path)}>
              <Pre tall>{JSON.stringify(body, null, 2)}</Pre>
            </Fold>
          ) : (
            <span className="font-mono text-xs text-faint">{fileName(call.path)}</span>
          )
        ) : (
          <span className="text-faint">Manifest only</span>
        )}
      </Field>
    </div>
  );
}

function SlicerJson({ v }: { v: Record<string, unknown> }) {
  const n = (x: unknown) => (typeof x === "number" ? x : null);
  const errs = Array.isArray(v.errors) ? v.errors.length : 0;
  const warns = Array.isArray(v.warnings) ? v.warnings.length : 0;
  return (
    <SlicerChips
      s={{
        ok: v.ok === true,
        print_time_s: n(v.print_time_s),
        total_grams: n(v.total_grams),
        purge_grams: n(v.purge_grams),
        slots_used: n(v.slots_used),
        errors: errs,
        warnings: warns,
        version: typeof v.slicer_version === "string" ? v.slicer_version : null,
      }}
    />
  );
}

function SlicerChips({ s }: { s: SlicerSummary }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5" data-testid="slicer-chips">
      <Chip className={s.ok ? "text-ok" : "text-bad"}>{s.ok ? "Gate pass" : "Gate fail"}</Chip>
      {s.print_time_s != null && <Chip>{duration(s.print_time_s)}</Chip>}
      {s.total_grams != null && <Chip>{s.total_grams.toFixed(1)} g</Chip>}
      {s.purge_grams != null && <Chip>Purge {s.purge_grams.toFixed(1)} g</Chip>}
      {s.slots_used != null && <Chip>{s.slots_used} slots</Chip>}
      <Chip className={s.errors ? "text-bad" : undefined}>{s.errors} errors</Chip>
      {s.warnings > 0 && <Chip>{s.warnings} warnings</Chip>}
      {s.version && <span className="num text-2xs text-faint">{s.version}</span>}
    </div>
  );
}

function decisionText(v: unknown): string {
  if (v == null) return "none";
  if (typeof v !== "object") return String(v);
  if (Array.isArray(v)) {
    return v.every((x) => x == null || typeof x !== "object")
      ? v.join(", ")
      : v.map((x) => (x && typeof x === "object" && "kind" in x ? String((x as { kind: unknown }).kind) : JSON.stringify(x))).join(", ");
  }
  const o = v as Record<string, unknown>;
  if (Array.isArray(o.answers)) {
    return (o.answers as { question_id: string; option_ids?: string[]; text?: string | null }[])
      .map((a) => `${a.question_id} ${[...(a.option_ids ?? []), a.text].filter(Boolean).join("/")}`)
      .join(", ");
  }
  return JSON.stringify(v);
}

function Decisions({ items }: { items: AttemptView["decisions"] }) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs leading-5 text-muted">
      {items.map((d) => {
        const text = decisionText(d.value);
        const short = text.length > 72 ? `${text.slice(0, 72)}...` : text;
        const line = (
          <span>
            <b className="font-semibold text-fg">{d.key}</b> {short} <span className="text-faint">({d.by})</span>
          </span>
        );
        return (
          <span key={d.key}>
            {short !== text || d.reason ? <Tip label={d.reason ? `${text}. ${d.reason}` : text}>{line}</Tip> : line}
          </span>
        );
      })}
    </div>
  );
}

function RawManifest({ jobId, stage, attempt, fetchImpl }: { jobId: string; stage: string; attempt: number; fetchImpl: FetchImpl }) {
  const [open, setOpen] = useState(false);
  const url = open ? `/api/admin/traces/${jobId}/raw?stage=${stage}&attempt=${attempt}` : null;
  const { data, error } = useJson<{ manifest: unknown }>(url, fetchImpl);
  return (
    <div className="space-y-1.5">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="inline-flex items-center gap-1.5 text-xs text-muted hover:text-fg"
      >
        <Caret open={open} />
        manifest.json
      </button>
      {open && (error ? <ErrorLine>{error}</ErrorLine> : data ? <Pre tall>{JSON.stringify(data.manifest, null, 2)}</Pre> : <p className="text-xs text-faint">Loading</p>)}
    </div>
  );
}

