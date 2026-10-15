"use client";

// The /sizes page body (V1SizesPage): product tabs, the big SizeVisualizer next to a reference
// object, a size table with per-size prices, and Actual size. Actual size stores a calibrated
// px per mm in localStorage (wrapped in try/catch); until then the drawing is relative scale.

import Link from "next/link";
import { useEffect, useState } from "react";
import { priceFor } from "@/lib/pricing";
import {
  PUBLIC_LABELS,
  SIZE_TIERS,
  SIZES_PRODUCTS,
  axisWord,
  tierMm,
  type SizeTier,
  type StandardProduct,
} from "@/lib/products";
import { DEFAULT_REF, REFS, REF_ORDER, type RefKind } from "./references";
import { SegmentedControl } from "./SegmentedControl";
import { SizeVisualizer } from "./SizeVisualizer";
import { SHEET, btn, cx } from "./ui";

const PPM_KEY = "layerling.ppm";
/** 96 dpi: 3.78 px per mm. Shown as relative scale until calibrated. */
export const DEFAULT_PPM = 96 / 25.4;
const CARD_MM = REFS.card.w;

function readPpm(): number | null {
  try {
    const v = Number(window.localStorage.getItem(PPM_KEY));
    return Number.isFinite(v) && v > 1 && v < 20 ? v : null;
  } catch {
    return null;
  }
}

function writePpm(v: number | null) {
  try {
    if (v == null) window.localStorage.removeItem(PPM_KEY);
    else window.localStorage.setItem(PPM_KEY, v.toFixed(3));
  } catch {
    // storage blocked: calibration lasts for this visit only
  }
}

export function SizesExplorer({ initialProduct = "keychain" }: { initialProduct?: StandardProduct }) {
  const [product, setProduct] = useState<StandardProduct>(initialProduct);
  const [reference, setReference] = useState<RefKind>(DEFAULT_REF[initialProduct]);
  const [tier, setTier] = useState<SizeTier>("m");
  const [phone, setPhone] = useState(false);
  const [ppm, setPpm] = useState<number | null>(null);
  const [draft, setDraft] = useState(DEFAULT_PPM);

  useEffect(() => {
    const saved = readPpm();
    if (saved) {
      setPpm(saved);
      setDraft(saved);
    }
    const mq = window.matchMedia?.("(max-width: 639px)");
    setPhone(!!mq?.matches);
    const on = () => setPhone(!!mq?.matches);
    mq?.addEventListener?.("change", on);
    return () => mq?.removeEventListener?.("change", on);
  }, []);

  const pick = (p: StandardProduct) => {
    setProduct(p);
    setReference(DEFAULT_REF[p]);
  };

  return (
    <div className="flex flex-col gap-10">
      <SegmentedControl
        label="Product"
        options={SIZES_PRODUCTS.map((p) => ({ value: p, label: PUBLIC_LABELS[p] }))}
        value={product}
        onChange={pick}
        className="self-start max-w-full overflow-x-auto"
      />

      <section aria-labelledby="sizes-drawing" className={cx(SHEET, "flex flex-col gap-5 p-5 sm:p-8")}>
        <div className="flex flex-wrap items-center justify-between gap-4">
          <h2 id="sizes-drawing" className="font-display m-0 text-[24px] leading-[1.1] sm:text-[28px]">
            {PUBLIC_LABELS[product]}
            <span className="measure ml-3 align-middle text-[13px] text-ink-3" data-testid="scale-mode">
              {ppm ? "Actual size" : "Relative scale"}
            </span>
          </h2>
          <SegmentedControl
            label="Compare with"
            options={REF_ORDER.map((k) => ({ value: k, label: REFS[k].label }))}
            value={reference}
            onChange={setReference}
          />
        </div>
        <SizeVisualizer
          product={product}
          reference={reference}
          heightPx={phone ? 220 : 360}
          expectedWidthPx={phone ? 326 : 1180}
          highlight={tier}
          onPick={setTier}
          fixedPpm={ppm}
        />
        <div className="flex flex-wrap items-center justify-end gap-3">
          <Link href={`/order?product=${product}&size=${tier}`} className={btn("primary", "md")}>
            {`Order ${tier.toUpperCase()}, ${tierMm(product, tier)} mm`}
          </Link>
        </div>
      </section>

      <div className="grid grid-cols-1 gap-8 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <section aria-labelledby="sizes-table">
          <h2 id="sizes-table" className="font-display m-0 mb-4 text-[24px] leading-[1.1]">
            {PUBLIC_LABELS[product]} sizes
          </h2>
          <table className="w-full border-collapse text-left">
            <thead>
              <tr className="border-b border-line-strong text-label text-ink-2">
                <th className="py-2 pr-4 font-medium">Size</th>
                <th className="py-2 pr-4 font-medium">{axisWord(product) === "wide" ? "Width" : "Height"}</th>
                <th className="py-2 text-right font-medium">Price</th>
              </tr>
            </thead>
            <tbody>
              {SIZE_TIERS.map((t) => (
                <tr
                  key={t}
                  className={cx("border-b border-line", t === tier && "bg-tint")}
                  aria-current={t === tier ? "true" : undefined}
                >
                  <td className="py-3 pr-4 text-body-strong">{t.toUpperCase()}</td>
                  <td className="measure py-3 pr-4">{`${tierMm(product, t)} mm`}</td>
                  <td className="py-3 text-right tabular-nums" data-testid={`size-price-${t}`}>
                    {priceFor(product, t).label}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section aria-labelledby="sizes-actual" className={cx(SHEET, "flex flex-col gap-4 p-5 shadow-none sm:p-6")}>
          <h2 id="sizes-actual" className="font-display m-0 text-[24px] leading-[1.1]">
            Actual size
          </h2>
          <div className="overflow-hidden" style={{ height: `${Math.ceil(REFS.card.h * draft) + 4}px` }}>
            <div
              aria-hidden
              data-testid="card-outline"
              className="rounded-[10px] border-2 border-dashed border-ink"
              style={{ width: `${CARD_MM * draft}px`, height: `${REFS.card.h * draft}px`, maxWidth: "100%" }}
            />
          </div>
          <label className="flex flex-col gap-2 text-label text-ink">
            Card width
            <input
              type="range"
              min={2}
              max={8}
              step={0.01}
              value={draft}
              onChange={(e) => setDraft(Number(e.target.value))}
              className="w-full accent-[var(--color-brick)]"
            />
          </label>
          <div className="flex flex-wrap gap-3">
            <button
              type="button"
              className={btn("secondary", "md")}
              onClick={() => {
                setPpm(draft);
                writePpm(draft);
              }}
            >
              Use this size
            </button>
            {ppm && (
              <button
                type="button"
                className={btn("quiet", "md")}
                onClick={() => {
                  setPpm(null);
                  setDraft(DEFAULT_PPM);
                  writePpm(null);
                }}
              >
                Reset
              </button>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
