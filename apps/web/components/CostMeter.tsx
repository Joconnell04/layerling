"use client";

import type { CostView } from "@/lib/derive";
import type { SpendRow } from "@/lib/db.types";
import { ago, usd } from "@/lib/format";
import { STAGE_BY_ID } from "@/lib/contracts";
import { Meter, Popover, cx, type Tone } from "./ui";

const LEVEL_TONE: Record<CostView["level"], Tone> = { ok: "accent", warn: "pause", over: "bad" };

export function CostMeter({ cost, spend }: { cost: CostView; spend: SpendRow[] }) {
  const tone = (p: number): Tone => (p >= 1 ? "bad" : p >= 0.75 ? "pause" : "ok");
  return (
    <Popover
      side="top"
      trigger={({ toggle, open }) => (
        <button
          type="button"
          onClick={toggle}
          aria-expanded={open}
          aria-label="Cost"
          data-level={cost.level}
          className="num flex w-full flex-col gap-2 rounded-md text-left text-[15px] hover:text-fg"
        >
          <span className="flex w-full justify-between">
            <span className="text-muted">Spent</span>
            <span className={cx("font-semibold", cost.usdPct >= 1 && "text-bad")}>
              {usd(cost.usd)} of {usd(cost.capUsd, 0)}
            </span>
          </span>
          <Meter value={cost.usd} max={cost.capUsd} tone={tone(cost.usdPct)} label="USD" />
          <span className="flex w-full justify-between">
            <span className="text-muted">Credits</span>
            <span className={cx("font-semibold", cost.creditsPct >= 1 && "text-bad")}>
              {cost.credits} of {cost.capCredits}
            </span>
          </span>
          <Meter value={cost.credits} max={cost.capCredits} tone={tone(cost.creditsPct)} label="Credits" />
        </button>
      )}
    >
      {() => (
        <div className="w-72 space-y-2 text-xs">
          <div className="grid grid-cols-[1fr_auto_auto] gap-x-3 gap-y-1">
            <span className="text-faint">Vendor</span>
            <span className="text-right text-faint">USD</span>
            <span className="text-right text-faint">Credits</span>
            {cost.byVendor.length === 0 && <span className="col-span-3 text-faint">No spend</span>}
            {cost.byVendor.map((v) => (
              <VendorRow key={v.vendor} name={v.vendor} usdV={v.usd} cr={v.credits} />
            ))}
          </div>
          {spend.length > 0 && (
            <ul className="max-h-48 space-y-0.5 overflow-auto border-t border-line pt-2">
              {spend.slice(0, 30).map((s) => (
                <li key={s.id} className="num grid grid-cols-[auto_1fr_auto] gap-2 text-muted">
                  <span className="text-faint">{ago(s.at)}</span>
                  <span className="truncate">
                    {s.stage ? STAGE_BY_ID[s.stage]?.name : s.vendor} {s.kind}
                  </span>
                  <span>{s.credits ? `${s.credits} cr` : usd(s.usd, 3)}</span>
                </li>
              ))}
            </ul>
          )}
          <div className={cx("text-2xs", LEVEL_TONE[cost.level] === "bad" ? "text-bad" : "text-faint")}>
            {cost.level === "over" ? "Cap hit" : `${Math.round(Math.max(cost.usdPct, cost.creditsPct) * 100)}% of cap`}
          </div>
        </div>
      )}
    </Popover>
  );
}

function VendorRow({ name, usdV, cr }: { name: string; usdV: number; cr: number }) {
  return (
    <>
      <span className="capitalize text-fg">{name}</span>
      <span className="num text-right">{usd(usdV)}</span>
      <span className="num text-right">{cr}</span>
    </>
  );
}
