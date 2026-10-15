"use client";

// The last block of the order form and the only place a price shows in the order flow: the
// render thumbnail, "Keychain, M", "52 mm tall · Quantity at checkout", the price (per size, else
// "from $X"; Other shows its range or "Priced after we see it"), then Send order.
// Price and mm tick to the new value; nothing reflows.

import Image from "next/image";
import type { ReactNode } from "react";
import type { ProductType } from "@/lib/contracts.gen";
import type { PriceQuote } from "@/lib/pricing";
import { PUBLIC_LABELS, axisWord, isStandard, tierMm, type SizeTier } from "@/lib/products";
import { heroSrc } from "@/lib/renders";
import { Tick } from "./Tick";
import { SpoolIcon, cx } from "./ui";

export function SummaryLine({ product, tier, quote }: { product: ProductType; tier: SizeTier; quote: PriceQuote }) {
  const standard = isStandard(product);
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
      <span className="flex size-[72px] shrink-0 items-center justify-center overflow-hidden rounded-md bg-paper">
        {standard ? (
          <Image src={heroSrc(product)} alt="" width={1200} height={1200} sizes="72px" className="block size-full object-contain" />
        ) : (
          <span className="text-ink [&>svg]:size-11">
            <SpoolIcon />
          </span>
        )}
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-body-strong" data-testid="summary-item">
          {standard ? `${PUBLIC_LABELS[product]}, ${tier.toUpperCase()}` : PUBLIC_LABELS[product]}
        </div>
        <div className="text-[14px] leading-5 text-ink-2">
          {standard ? (
            <>
              <Tick value={`${tierMm(product, tier)} mm ${axisWord(product)}`} className="measure align-top text-measure" testId="summary-mm" />
              {" · "}Quantity at checkout
            </>
          ) : (
            "Priced after we see it"
          )}
        </div>
      </div>
      {quote.kind !== "unpriced" && (
        <Tick
          value={quote.label}
          testId="order-price"
          className={cx(
            "font-display ml-auto shrink-0 leading-none",
            quote.kind === "range" ? "text-[22px] sm:text-[24px]" : "text-[28px]",
          )}
        />
      )}
    </div>
  );
}

export function OrderSummary({
  product,
  tier,
  quote,
  children,
}: {
  product: ProductType;
  tier: SizeTier;
  quote: PriceQuote;
  /** The Send order button and any form error. */
  children: ReactNode;
}) {
  return (
    <div className="-mt-2 flex flex-col gap-4 border-t border-line pt-5">
      <SummaryLine product={product} tier={tier} quote={quote} />
      <p className="m-0 text-[14px] leading-5 text-ink-2">Nothing is charged until you approve the preview.</p>
      {children}
    </div>
  );
}
