"use client";

// Six tiles in a radio group: the product's real render and its name, no prices. Other shows an
// ink spool outline. Arrow keys move the selection (roving tabindex).

import Image from "next/image";
import { useRef, type KeyboardEvent } from "react";
import type { ProductType } from "@/lib/contracts.gen";
import { ORDER_PRODUCTS, PUBLIC_LABELS, isStandard } from "@/lib/products";
import { heroSrc } from "@/lib/renders";
import { LABEL, SpoolIcon, cx } from "./ui";

/** Arrow-key handling for a radio group of buttons. */
export function useRovingRadio<T>(values: readonly T[], value: T, onChange: (v: T) => void) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKeyDown = (e: KeyboardEvent) => {
    const i = values.indexOf(value);
    let next = -1;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (i + 1) % values.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = (i - 1 + values.length) % values.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = values.length - 1;
    if (next < 0) return;
    e.preventDefault();
    onChange(values[next]);
    refs.current[next]?.focus();
  };
  return { refs, onKeyDown };
}

export function ProductPicker({ value, onChange }: { value: ProductType; onChange: (p: ProductType) => void }) {
  const { refs, onKeyDown } = useRovingRadio(ORDER_PRODUCTS, value, onChange);
  return (
    <div className="flex flex-col gap-2.5">
      <span id="lbl-product" className={LABEL}>
        Product
      </span>
      <div role="radiogroup" aria-labelledby="lbl-product" className="grid grid-cols-3 gap-3 lg:grid-cols-6" onKeyDown={onKeyDown}>
        {ORDER_PRODUCTS.map((p, i) => {
          const on = p === value;
          return (
            <button
              key={p}
              ref={(el) => {
                refs.current[i] = el;
              }}
              type="button"
              role="radio"
              aria-checked={on}
              tabIndex={on ? 0 : -1}
              onClick={() => onChange(p)}
              className={cx(
                "flex min-w-0 flex-col items-center gap-1.5 rounded-md text-[14px] leading-5 transition-colors duration-[var(--dur-fast)]",
                // The selected border is 0.5 px thicker; the padding gives it back so nothing moves.
                on
                  ? "border-[1.5px] border-ink bg-sheet px-2 pb-3 pt-2.5 font-semibold text-ink"
                  : "border border-line px-[8.5px] pb-[12.5px] pt-[10.5px] font-medium text-ink-2 hover:bg-hover",
              )}
            >
              <span className="flex aspect-square w-full max-w-[89px] items-center justify-center">
                {isStandard(p) ? (
                  <Image src={heroSrc(p)} alt="" width={1200} height={1200} sizes="90px" className="block size-full object-contain" />
                ) : (
                  <span className="text-ink [&>svg]:size-[46px] sm:[&>svg]:size-[52px]">
                    <SpoolIcon />
                  </span>
                )}
              </span>
              {PUBLIC_LABELS[p]}
            </button>
          );
        })}
      </div>
    </div>
  );
}
