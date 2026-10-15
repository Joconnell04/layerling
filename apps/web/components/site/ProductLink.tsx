"use client";

import type { ReactNode } from "react";
import type { ProductType } from "@/lib/contracts.gen";
import type { SizeTier } from "@/lib/products";
import { orderHref, pickOnPage } from "./pick";

/** A link to the order form with this product picked (same page: no reload). */
export function ProductLink({
  product,
  tier,
  className,
  children,
  label,
}: {
  product: ProductType;
  tier?: SizeTier;
  className?: string;
  children: ReactNode;
  label?: string;
}) {
  return (
    <a
      href={orderHref(product, tier)}
      aria-label={label}
      className={className}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        if (pickOnPage({ product, tier })) e.preventDefault();
      }}
    >
      {children}
    </a>
  );
}
