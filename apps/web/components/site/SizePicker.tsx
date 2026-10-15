"use client";

// Four cells in a radio group, XS S M L. Each draws the product's -front render at its tier, all
// four at one px-per-mm (the L object fills its cell), with the letter and the real mm. M is the
// default. "Compare sizes" opens the size dialog. Hidden for Other.

import type { CSSProperties } from "react";
import { SIZE_TIERS, sizeSpoken, tierMm, type SizeTier, type StandardProduct } from "@/lib/products";
import { frontMm } from "@/lib/renders";
import { FrontImage } from "./FrontImage";
import { useRovingRadio } from "./ProductPicker";
import { Tick } from "./Tick";
import { LABEL, btn, cx } from "./ui";

export function SizePicker({
  product,
  value,
  onChange,
  onCompare,
}: {
  product: StandardProduct;
  value: SizeTier;
  onChange: (t: SizeTier) => void;
  onCompare: () => void;
}) {
  const { refs, onKeyDown } = useRovingRadio(SIZE_TIERS, value, onChange);
  const l = frontMm(product, "l");
  const vars = { "--hl": l.objH, "--wl": l.objW } as CSSProperties;
  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <span id="lbl-size" className={LABEL}>
          Size
        </span>
        <button type="button" onClick={onCompare} className={btn("quiet", "md", "min-h-0 text-[14px]")} aria-haspopup="dialog">
          Compare sizes
        </button>
      </div>
      <div role="radiogroup" aria-labelledby="lbl-size" className="grid grid-cols-4 gap-2 sm:gap-3" onKeyDown={onKeyDown}>
        {SIZE_TIERS.map((t, i) => {
          const on = t === value;
          return (
            <button
              key={t}
              ref={(el) => {
                refs.current[i] = el;
              }}
              type="button"
              role="radio"
              aria-checked={on}
              aria-label={sizeSpoken(product, t)}
              tabIndex={on ? 0 : -1}
              onClick={() => onChange(t)}
              className={cx(
                "size-cell flex min-w-0 flex-col items-center gap-0.5 rounded-md text-ink transition-colors duration-[var(--dur-fast)]",
                on
                  ? "border-[1.5px] border-ink bg-sheet px-2 py-3"
                  : "border border-line-strong px-[8.5px] py-[12.5px] hover:bg-hover",
              )}
            >
              <span className="size-cell-render pointer-events-none mb-2 flex h-[88px] w-full items-end justify-center" style={vars} aria-hidden>
                <FrontImage product={product} tier={t} ppm="var(--cppm)" maxPpm={2} />
              </span>
              <span className="font-display text-[20px] leading-[1.1]">{t.toUpperCase()}</span>
              <Tick value={`${tierMm(product, t)} mm`} className="measure text-measure text-ink-2" />
            </button>
          );
        })}
      </div>
    </div>
  );
}
