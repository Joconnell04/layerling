"use client";

// The job page's right column once a result exists: verdict, print numbers, one row per filament
// slot with its grams, and the actions at the bottom. Each row opens a filament picker (owned
// spools first, then catalog colors by line). Colors can be added and removed; Apply sends the
// contract palette_edit message and the rows wait until the worker posts a new result.

import { useEffect, useMemo, useRef, useState } from "react";
import type { JobResult } from "@/lib/contracts.gen";
import type { AnyMessage, FilamentRow, JobRow } from "@/lib/db.types";
import { downloadHref, downloadName } from "@/lib/download";
import { filamentLabel } from "@/lib/filamentLabel";
import { duration, grams } from "@/lib/format";
import { useData } from "@/lib/hooks";
import {
  draftChanged,
  draftFrom,
  lineLabel,
  nearestSlot,
  paletteEdit,
  pendingPaletteEdit,
  pickerGroups,
  type DraftSlot,
} from "@/lib/palette";
import type { SpoolSuggestion } from "@/lib/spool";
import { ErrorLine, cx } from "../ui";

const H2 = "font-display text-[30px] font-bold leading-[1.05] tracking-[-0.01em]";
const PRIMARY =
  "flex h-12 w-full items-center justify-center rounded-full bg-accent text-base font-semibold text-accent-fg hover:brightness-110 hover:text-accent-fg disabled:opacity-50";
const SECONDARY =
  "flex h-12 w-full items-center justify-center rounded-full border border-ink text-base font-semibold hover:bg-hover disabled:opacity-50";

/** Why a result is not ready: the review reason, else what the slicer gate said. */
export function resultVerdict(job: JobRow, r: JobResult | null = job.result): { ready: boolean; reason: string | null } {
  if (!r) return { ready: false, reason: "No result yet" };
  if (r.slice_ok === true) return { ready: true, reason: null };
  const reason =
    job.review_reason ?? (r.slice_ok === false ? "Failed the slicer gate" : "Not checked by the slicer gate");
  return { ready: false, reason };
}

export function ResultRail({
  job,
  result,
  name,
  filaments,
  messages,
  suggestions,
  cost,
  paintBusy = false,
}: {
  job: JobRow;
  result: JobResult;
  name: string | null;
  filaments: FilamentRow[];
  messages: AnyMessage[];
  suggestions: SpoolSuggestion[];
  /** The cost meter, shown above the actions. */
  cost: React.ReactNode;
  /** A paint edit is on its way to a new result: colors and download wait for it. */
  paintBusy?: boolean;
}) {
  const client = useData();
  const { ready, reason } = resultVerdict(job, result);
  const byId = useMemo(() => new Map(filaments.map((f) => [f.id, f])), [filaments]);
  const usage = result.filaments;
  const usageKey = JSON.stringify(usage ?? []);
  const [draft, setDraft] = useState<DraftSlot[]>(() => draftFrom(usage));
  const [picker, setPicker] = useState<number | "new" | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setDraft(draftFrom(usage));
    setPicker(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [usageKey]);

  const pending = !!pendingPaletteEdit(messages) || paintBusy;
  const changed = draftChanged(draft, usage);
  const gramsBySlot = new Map((usage ?? []).map((u) => [u.slot, u.grams]));
  const total = (usage ?? []).reduce((s, f) => s + f.grams, 0);
  const cap = job.settings.ams_slots ?? 5;
  const href = client ? downloadHref(client.mode, job, name) : null;
  const locked = pending || busy;

  const send = async (next: DraftSlot[]) => {
    if (!client || locked) return;
    setBusy(true);
    setError(null);
    try {
      await client.sendMessage({
        jobId: job.id,
        kind: "palette_edit",
        payload: paletteEdit(next, usage, filaments),
        stage: "s08_palette",
      });
      setDraft(next);
      setPicker(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const pick = (id: string) => {
    if (picker === "new") setDraft((d) => [...d, { from: null, id }]);
    else if (picker != null) setDraft((d) => d.map((s, i) => (i === picker ? { ...s, id } : s)));
    setPicker(null);
  };

  // One click: add the suggested color when a slot is free, else swap it into the nearest slot.
  const applySuggestion = (s: SpoolSuggestion) => {
    if (!s.filamentId) return;
    const id = s.filamentId;
    if (draft.length < cap) return void send([...draft, { from: null, id }]);
    const hexOf = (fid: string) => filamentLabel(fid, byId).hex;
    const i = nearestSlot(draft, hexOf, s.targetHex ?? hexOf(id) ?? "#808080");
    void send(draft.map((d, j) => (j === Math.max(0, i) ? { ...d, id } : d)));
  };

  return (
    <div className="flex h-full flex-col gap-5" data-testid="result-rail">
      <div className="flex flex-col gap-2">
        <h2 className={H2}>{ready ? "Ready to print" : "Needs review"}</h2>
        {!ready && (
          <p role="status" data-testid="result-reason" className="text-base text-bad">
            {reason}
          </p>
        )}
      </div>

      <dl className="num grid grid-cols-3 gap-2 text-center" aria-label="Print">
        <Stat label="Print time" value={result.print_time_s != null ? duration(result.print_time_s) : "?"} />
        <Stat label="Filament" value={total > 0 ? grams(total) : "?"} />
        <Stat
          label="Slicer gate"
          value={result.slice_ok == null ? "Not run" : result.slice_ok ? "Pass" : "Fail"}
          tone={result.slice_ok === false ? "text-bad" : result.slice_ok ? "text-ok" : undefined}
        />
      </dl>

      <section aria-label="Colors" className="flex flex-col gap-1">
        <div className="flex items-baseline justify-between">
          <h3 className="text-sm font-semibold">Colors</h3>
          {pending && (
            <span role="status" data-testid="palette-pending" className="pulse text-sm font-semibold text-accent">
              {paintBusy ? "Updating paint" : "Updating colors"}
            </span>
          )}
        </div>
        {draft.length === 0 && <p className="text-sm text-muted">No filament usage</p>}
        <ul aria-label="Filaments" className="flex flex-col">
          {draft.map((d, i) => {
            const f = filamentLabel(d.id, byId, d.from ?? i + 1);
            const row = byId.get(d.id);
            const swapped = d.from == null || draftFrom(usage)[i]?.id !== d.id || draftFrom(usage)[i]?.from !== d.from;
            const g = d.from != null ? gramsBySlot.get(d.from) : null;
            return (
              <li key={`${d.from ?? "new"}-${i}`} data-testid={`filament-row-${i + 1}`} className="flex flex-col">
                <div className="group flex items-center gap-1">
                  <button
                    type="button"
                    disabled={locked}
                    aria-expanded={picker === i}
                    aria-label={`Change slot ${i + 1}`}
                    onClick={() => setPicker(picker === i ? null : i)}
                    className={cx(
                      "flex h-11 min-w-0 flex-1 items-center gap-3 rounded-lg px-2 text-left text-[15px] enabled:hover:bg-hover disabled:cursor-default",
                      picker === i && "bg-hover",
                    )}
                  >
                    <span className="num w-4 shrink-0 text-sm text-muted">{i + 1}</span>
                    <Swatch hex={f.hex} />
                    <span className={cx("min-w-0 flex-1 truncate", swapped && changed && "font-semibold text-accent")} title={f.id ?? undefined}>
                      {f.name}
                    </span>
                    {row && !row.owned && <NotOwned />}
                    <span className="num shrink-0 font-semibold">{g != null ? grams(g) : "New"}</span>
                  </button>
                  <button
                    type="button"
                    disabled={locked || draft.length <= 1}
                    aria-label={`Remove slot ${i + 1}`}
                    onClick={() => setDraft((cur) => cur.filter((_, j) => j !== i))}
                    className="flex size-8 shrink-0 items-center justify-center rounded-full text-lg leading-none text-faint enabled:hover:bg-hover enabled:hover:text-fg disabled:opacity-30"
                  >
                    ×
                  </button>
                </div>
                {picker === i && <FilamentPicker filaments={filaments} current={d.id} onPick={pick} onClose={() => setPicker(null)} />}
              </li>
            );
          })}
        </ul>
        {picker === "new" ? (
          <FilamentPicker filaments={filaments} current={null} onPick={pick} onClose={() => setPicker(null)} />
        ) : (
          <button
            type="button"
            disabled={locked || draft.length >= cap}
            onClick={() => setPicker("new")}
            className="h-9 self-start rounded-full px-3 text-sm font-semibold text-accent enabled:hover:bg-hover disabled:opacity-40"
          >
            Add color
          </button>
        )}
      </section>

      {suggestions.length > 0 && (
        <section aria-label="Spool suggestions" className="flex flex-col gap-2">
          {suggestions.map((s) => {
            const hex = (s.filamentId && filamentLabel(s.filamentId, byId).hex) || s.targetHex || filamentLabel(s.spool.replace(/\s+/g, "-"), []).hex;
            const cat = s.filamentId ? byId.get(s.filamentId) : undefined;
            return (
              <div key={s.messageId} data-testid="spool-suggestion" title={s.text} className="flex items-center gap-3 rounded-lg border border-line bg-bg px-3 py-2">
                <Swatch hex={hex} />
                <span className="min-w-0 flex-1 truncate text-[15px] font-semibold">Add {cat ? `${cat.name} ${cat.material}` : s.spool}</span>
                {s.filamentId && (
                  <button
                    type="button"
                    disabled={locked || draft.some((d) => d.id === s.filamentId)}
                    aria-label={`Apply ${s.spool}`}
                    onClick={() => applySuggestion(s)}
                    className="h-8 shrink-0 rounded-full border border-ink px-3 text-sm font-semibold enabled:hover:bg-hover disabled:opacity-40"
                  >
                    Apply
                  </button>
                )}
              </div>
            );
          })}
        </section>
      )}

      <div className="mt-auto flex flex-col gap-4 pt-2">
        <div className="border-t border-line pt-4">{cost}</div>
        <ErrorLine>{error}</ErrorLine>
        {changed && !pending ? (
          <div className="grid grid-cols-[1fr_auto] gap-2">
            <button type="button" className={PRIMARY} disabled={locked} onClick={() => void send(draft)}>
              Apply
            </button>
            <button
              type="button"
              className="h-12 rounded-full px-5 text-base font-semibold text-muted hover:bg-hover disabled:opacity-50"
              disabled={locked}
              onClick={() => {
                setDraft(draftFrom(usage));
                setPicker(null);
              }}
            >
              Reset
            </button>
          </div>
        ) : null}
        <a
          href={href ?? undefined}
          download={downloadName(job, name)}
          aria-disabled={!href || pending}
          className={cx(ready && !changed ? PRIMARY : SECONDARY, (!href || pending) && "pointer-events-none opacity-50")}
        >
          Download 3MF
        </a>
      </div>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex flex-col gap-0.5 rounded-lg bg-bg px-2 py-2.5">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className={cx("text-base font-semibold", tone)}>{value}</dd>
    </div>
  );
}

function Swatch({ hex }: { hex: string | null }) {
  return (
    <span
      className={cx("size-6 shrink-0 rounded-full border border-ink/25", !hex && "bg-raised")}
      style={hex ? { background: hex } : undefined}
      aria-hidden
    />
  );
}

function NotOwned() {
  return <span className="shrink-0 rounded-full border border-line-strong px-2 text-xs text-muted">Not owned</span>;
}

/** Owned spools first, then catalog colors grouped by line, with search. */
export function FilamentPicker({
  filaments,
  current,
  onPick,
  onClose,
}: {
  filaments: FilamentRow[];
  current: string | null;
  onPick: (id: string) => void;
  onClose: () => void;
}) {
  const [q, setQ] = useState("");
  const groups = useMemo(() => pickerGroups(filaments, q), [filaments, q]);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => input.current?.focus(), []);
  return (
    <div
      data-testid="filament-picker"
      className="my-1 flex flex-col rounded-xl border border-line-strong/60 bg-panel shadow-[0_8px_24px_-12px_rgba(28,26,23,0.35)]"
      onKeyDown={(e) => {
        if (e.key === "Escape") onClose();
      }}
    >
      <div className="border-b border-line p-2">
        <input
          ref={input}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search"
          aria-label="Search colors"
          className="h-9 w-full rounded-md border border-line bg-bg px-3 text-sm placeholder:text-faint focus:border-ink focus:outline-none"
        />
      </div>
      <div role="listbox" aria-label="Colors" className="max-h-72 overflow-y-auto py-1">
        {groups.length === 0 && <p className="px-3 py-4 text-sm text-muted">No match</p>}
        {groups.map((g) => (
          <div key={g.key} role="group" aria-label={g.label}>
            <div className="px-3 pb-1 pt-2 text-xs font-semibold text-muted">{g.label}</div>
            {g.items.map((f) => (
              <button
                key={f.id}
                type="button"
                role="option"
                aria-selected={f.id === current}
                data-testid={`pick-${f.id}`}
                onClick={() => onPick(f.id)}
                className={cx("flex h-10 w-full items-center gap-3 px-3 text-left text-sm hover:bg-hover", f.id === current && "bg-hover")}
              >
                <Swatch hex={f.hex} />
                <span className="min-w-0 flex-1 truncate">
                  <span className="font-medium">{f.name}</span>
                  <span className="ml-2 text-xs text-muted">{f.owned ? lineLabel(f) : f.material}</span>
                </span>
                {!f.owned && <NotOwned />}
                {f.owned && f.ams_slot != null && <span className="num text-xs text-muted">AMS {f.ams_slot}</span>}
              </button>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
