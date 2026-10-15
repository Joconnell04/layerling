// "Start an order": heading, lede and what happens next on the left, the order form on the right.
// The landing's #order section and the /order page.

import type { ProductType } from "@/lib/contracts.gen";
import type { SizeTier } from "@/lib/products";
import { NextSteps } from "./NextSteps";
import { OrderForm } from "./OrderForm";
import { cx } from "./ui";

export function OrderSection({
  heading = "h2",
  initialProduct,
  initialTier,
  className,
}: {
  heading?: "h1" | "h2";
  initialProduct?: ProductType;
  initialTier?: SizeTier;
  className?: string;
}) {
  const H = heading;
  return (
    <section
      id="order"
      aria-labelledby="order-title"
      className={cx(
        "mx-auto grid max-w-[1440px] scroll-mt-4 gap-8 px-4 md:px-[72px] lg:grid-cols-[minmax(0,420px)_minmax(0,800px)] lg:justify-between lg:gap-16",
        className,
      )}
    >
      <div className="flex flex-col gap-4 lg:gap-[18px]">
        <H
          id="order-title"
          data-reveal={heading === "h2" ? "" : undefined}
          className={cx(
            "font-display m-0",
            heading === "h1" ? "text-display-m md:text-display-l" :"text-display-l-phone md:text-display-l",
          )}
        >
          Start an order
        </H>
        <p data-reveal="" className="m-0 text-lede-phone text-ink-2 md:text-lede">
          We&rsquo;ll reply by email or text with a preview and a price. Nothing is charged until you approve it.
        </p>
        <div className="mt-5 hidden flex-col gap-3.5 border-t border-line pt-6 lg:flex">
          <span className="text-body-strong">What happens next</span>
          <NextSteps />
        </div>
      </div>
      <OrderForm initialProduct={initialProduct} initialTier={initialTier} />
    </section>
  );
}
