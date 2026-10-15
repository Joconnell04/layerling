"use client";

// A product at XS, S, M and L at true relative scale next to a thing people carry. One px-per-mm
// (--vppm) for the whole drawing: products use their orthographic -front renders, references are
// drawn at their published sizes. The scale is CSS (app/site.css .visualizer-track): the tallest
// item fits the height and the reference plus four columns fit the width, unless a calibrated
// px-per-mm is given (Actual size on the sizes page).

import type { CSSProperties } from "react";
import { SIZE_TIERS, sizeLabel, type SizeTier, type StandardProduct } from "@/lib/products";
import { frontMm } from "@/lib/renders";
import { FrontImage } from "./FrontImage";
import { REFS, RefDrawing, type RefKind } from "./references";
import { Ruler } from "./Ruler";
import { cx } from "./ui";

const COL_GAP = 20; // px between columns
const REF_PAD = 14; // px each side of the reference column

/** Label spacing on the ruler so labels sit at least ~60 px apart at the expected scale. */
function labelStep(ppm: number): number {
  for (const n of [10, 20, 50, 100]) if (ppm * n >= 60) return n;
  return 200;
}

export function visualizerScale(product: StandardProduct, ref: RefKind, heightPx: number, widthPx: number) {
  const l = frontMm(product, "l");
  const r = REFS[ref];
  const hMm = Math.max(l.objH, r.h) * 1.02;
  const wMm = r.w + 4 * l.objW;
  const gaps = 4 * COL_GAP + 2 * REF_PAD + 8;
  return { hMm, wMm, gaps, ppm: Math.min(heightPx / hMm, (widthPx - gaps) / wMm) };
}

export function SizeVisualizer({
  product,
  reference,
  heightPx,
  expectedWidthPx,
  highlight,
  onPick,
  fixedPpm,
  className,
}: {
  product: StandardProduct;
  reference: RefKind;
  /** Drawing height budget in px. */
  heightPx: number;
  /** Typical drawing width, for ruler label spacing and image sizes. */
  expectedWidthPx: number;
  highlight?: SizeTier;
  onPick?: (t: SizeTier) => void;
  /** Calibrated px per mm (Actual size); the drawing may then scroll sideways. */
  fixedPpm?: number | null;
  className?: string;
}) {
  const s = visualizerScale(product, reference, heightPx, expectedWidthPx);
  const expected = fixedPpm ?? s.ppm;
  const vars = {
    "--vh": `${heightPx}px`,
    "--vh-mm": s.hMm.toFixed(2),
    "--vw-mm": s.wMm.toFixed(2),
    "--vgaps": `${s.gaps}px`,
    ...(fixedPpm ? { "--vppm-fixed": `${fixedPpm}px` } : {}),
  } as CSSProperties;
  const cols = "grid-cols-[auto_repeat(4,minmax(0,1fr))]";
  const r = REFS[reference];
  return (
    <div className={cx("visualizer", !!fixedPpm && "overflow-x-auto", className)}>
      <div className={cx("visualizer-track flex flex-col", !!fixedPpm && "min-w-max")} style={vars}>
        <div className={cx("grid items-end gap-x-5", cols)}>
          <div className="flex items-end justify-center px-3.5" style={{ height: fixedPpm ? undefined : `var(--vh)` }}>
            <RefDrawing kind={reference} ppm="var(--vppm)" />
          </div>
          {SIZE_TIERS.map((t) => {
            const on = t === highlight;
            const inner = <FrontImage product={product} tier={t} ppm="var(--vppm)" maxPpm={Math.max(expected, 2)} />;
            return (
              <div
                key={t}
                className={cx("flex items-end justify-center rounded-t-md", on && "bg-tint")}
                style={{ height: fixedPpm ? undefined : `var(--vh)` }}
              >
                {onPick ? (
                  <button
                    type="button"
                    tabIndex={-1}
                    aria-hidden
                    onClick={() => onPick(t)}
                    className="flex h-full w-full items-end justify-center"
                  >
                    {inner}
                  </button>
                ) : (
                  inner
                )}
              </div>
            );
          })}
        </div>
        <div className="border-t-[1.5px] border-ink" />
        <Ruler ppm="var(--vppm)" lengthMm={1000} labelEvery={labelStep(expected)} />
        <div className={cx("mt-3 grid gap-x-5", cols)}>
          <div className="px-3.5 text-center" style={{ minWidth: `calc(var(--vppm) * ${r.viewBox[2]} + ${REF_PAD * 2}px)` }}>
            <div className="text-[14px] font-semibold leading-5">{r.label}</div>
            <div className="measure text-[12px] text-ink-2">{r.dims}</div>
          </div>
          {SIZE_TIERS.map((t) => {
            const body = (
              <>
                <span className="font-display block text-[22px] leading-[1.1]">{t.toUpperCase()}</span>
                <span className="measure block text-measure text-ink-2">{sizeLabel(product, t)}</span>
              </>
            );
            return onPick ? (
              <button
                key={t}
                type="button"
                aria-pressed={t === highlight}
                onClick={() => onPick(t)}
                className={cx("min-w-0 rounded-md py-1 text-center hover:bg-hover", t === highlight && "bg-tint hover:bg-tint")}
              >
                {body}
              </button>
            ) : (
              <div key={t} className="min-w-0 text-center">
                {body}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
