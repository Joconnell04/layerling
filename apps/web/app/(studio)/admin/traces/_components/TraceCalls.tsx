"use client";

import Link from "next/link";
import { useMemo, useRef, useState } from "react";
import { Empty, ErrorLine, Segmented, Tip, cx } from "@/components/ui";
import { STAGE_LABEL, filterCalls } from "@/lib/traces/derive";
import type { CallRow, CallsResponse, SpendSummary } from "@/lib/traces/types";
import {
  CallStatus,
  MESHY_USD_TIP,
  ModelName,
  SearchBox,
  TipHead,
  TracesTop,
  money,
  secs,
  useJson,
  when,
  type FetchImpl,
} from "./common";

type Range = "7" | "30" | "all";
type Vendor = "all" | "claude" | "meshy" | "runpod";
const COLS = "grid-cols-[76px_minmax(0,150px)_104px_minmax(0,170px)_minmax(0,1fr)_68px_60px_60px_60px_76px]";

const callHref = (c: CallRow) => `/admin/traces/${c.job_id}#${c.stage}-a${c.attempt}`;

export function TraceCalls({ fetchImpl = fetch }: { fetchImpl?: FetchImpl }) {
  const [range, setRange] = useState<Range>("all");
  const [vendor, setVendor] = useState<Vendor>("all");
  const [q, setQ] = useState("");
  const search = useRef<HTMLInputElement>(null);
  const url = `/api/admin/traces/calls${range === "all" ? "" : `?days=${range}`}`;
  const { data, error } = useJson<CallsResponse>(url, fetchImpl);
  const shown = useMemo(() => filterCalls(data?.calls ?? [], { vendor, q }), [data, vendor, q]);

  return (
    <div className="space-y-4">
      <TracesTop
        tab="calls"
        right={
          <Segmented
            label="Range"
            size="sm"
            value={range}
            onChange={setRange}
            options={[
              { value: "7", label: "7 d" },
              { value: "30", label: "30 d" },
              { value: "all", label: "All" },
            ]}
          />
        }
      />
      <ErrorLine>{error}</ErrorLine>
      <SpendStrip summary={data?.summary ?? null} calls={data?.calls ?? []} />

      <div className="flex flex-wrap items-center gap-2">
        <Segmented
          label="Vendor"
          size="sm"
          value={vendor}
          onChange={setVendor}
          options={[
            { value: "all", label: "All" },
            { value: "claude", label: "Claude" },
            { value: "meshy", label: "Meshy" },
            { value: "runpod", label: "Runpod" },
          ]}
        />
        <SearchBox value={q} onChange={setQ} placeholder="Model or purpose" inputRef={search} />
        <span className="num ml-auto text-xs text-faint">{data ? `${shown.length} of ${data.calls.length}` : ""}</span>
      </div>

      <div className="overflow-x-auto rounded-lg border border-line bg-panel">
        <div className="min-w-[1040px]">
          <div
            className={cx(
              "grid h-8 items-center gap-3 border-b border-line bg-bg/60 px-3 text-xs font-medium text-muted",
              COLS,
            )}
          >
            <span>When</span>
            <span>Job</span>
            <span>Stage</span>
            <span>Model</span>
            <span>Purpose</span>
            <span className="text-right">Time</span>
            <span className="text-right">Credits</span>
            <span className="text-right">
              <TipHead tip={MESHY_USD_TIP}>$</TipHead>
            </span>
            <span className="text-right">List $</span>
            <span>Status</span>
          </div>
          {data === null && !error && <Empty>Loading</Empty>}
          {data && shown.length === 0 && <Empty>No calls</Empty>}
          <ul aria-label="Model calls">
            {shown.map((c, i) => (
              <li key={`${c.job_id}-${c.stage}-${c.attempt}-${i}`}>
                <Link
                  href={callHref(c)}
                  data-testid="call-row"
                  className={cx(
                    "grid h-9 items-center gap-3 border-b border-line/70 px-3 text-[13.5px] text-fg hover:text-fg",
                    COLS,
                    "hover:bg-hover",
                  )}
                >
                  <span className="num truncate text-faint">
                    <Tip label={c.at ? new Date(c.at).toLocaleString() : "unknown"} side="bottom">
                      <span>{when(c.at)}</span>
                    </Tip>
                  </span>
                  <span className="truncate">{c.job_name}</span>
                  <span className="truncate text-muted">
                    {STAGE_LABEL[c.stage] ?? c.stage} <span className="font-mono text-xs text-faint">a{c.attempt}</span>
                  </span>
                  <ModelName model={c.model} />
                  <span className="flex min-w-0 items-center gap-2">
                    <span className="truncate text-muted">{c.purpose}</span>
                    {c.derived && (
                      <Tip label="From the manifest; no trace body">
                        <span className="text-2xs text-faint">derived</span>
                      </Tip>
                    )}
                  </span>
                  <span className="num text-right text-muted">{secs(c.duration_s)}</span>
                  <span className="num text-right">{c.kind === "meshy" ? c.credits : ""}</span>
                  <span className="num text-right">{money(c.usd)}</span>
                  <span className="num text-right text-faint">{c.usd_list > 0 ? money(c.usd_list) : ""}</span>
                  <span>
                    <CallStatus status={c.status} />
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}

function Stat({ k, v, tip, sub }: { k: string; v: string; tip?: string; sub?: string }) {
  return (
    <div className="flex min-h-[112px] flex-col justify-between rounded-lg border border-line bg-panel px-4 py-3" data-testid={`stat-${k}`}>
      <div className="text-xs text-faint">{tip ? <TipHead tip={tip}>{k}</TipHead> : k}</div>
      <div>
        <div className="num font-display text-[28px] font-bold leading-8">{v}</div>
        <div className="num mt-0.5 h-4 truncate text-2xs text-faint">{sub}</div>
      </div>
    </div>
  );
}

function SpendStrip({ summary, calls }: { summary: SpendSummary | null; calls: CallRow[] }) {
  const s = summary ?? { usd: 0, usd_list: 0, credits: 0, calls: 0, by_model: [] };
  const top = s.by_model.slice(0, 4);
  const more = s.by_model.length - top.length;
  const of = (k: string) => calls.filter((c) => c.kind === k);
  const usdOf = (k: string) => money(of(k).reduce((a, c) => a + c.usd, 0));
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-[repeat(4,minmax(0,1fr))_minmax(0,1.7fr)]">
      <Stat k="Spent $" v={money(s.usd)} tip={MESHY_USD_TIP} sub={`Meshy ${usdOf("meshy")} · Claude ${usdOf("claude")}`} />
      <Stat k="List $" v={money(s.usd_list)} sub="Claude at list" />
      <Stat k="Credits" v={String(s.credits)} sub={`${of("meshy").length} Meshy tasks`} />
      <Stat k="Calls" v={String(s.calls)} sub={`Claude ${of("claude").length} · Meshy ${of("meshy").length}`} />
      <div className="col-span-2 rounded-lg border border-line bg-panel px-3 py-2 lg:col-span-1" data-testid="by-model">
        <div className="grid grid-cols-[minmax(0,1fr)_44px_56px_56px] gap-2 border-b border-line pb-1 text-xs font-medium text-muted">
          <span>By model</span>
          <span className="text-right">Calls</span>
          <span className="text-right">Credits</span>
          <span className="text-right">$</span>
        </div>
        {top.length === 0 && <div className="py-2 text-xs text-faint">No calls</div>}
        {top.map((m) => (
          <div key={m.model} className="num grid h-[22px] grid-cols-[minmax(0,1fr)_44px_56px_56px] items-center gap-2 text-xs">
            <ModelName model={m.model} className="font-normal text-fg" />
            <span className="text-right text-muted">{m.calls}</span>
            <span className="text-right text-muted">{m.credits}</span>
            <span className="text-right">{money(m.usd)}</span>
          </div>
        ))}
        {more > 0 && (
          <Tip label={s.by_model.slice(4).map((m) => `${m.model} ${m.calls}`).join(", ")}>
            <span className="text-2xs text-faint">{more} more</span>
          </Tip>
        )}
      </div>
    </div>
  );
}
