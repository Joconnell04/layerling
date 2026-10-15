// Links that open the order form with a product (and size) picked. On the landing the form is on
// the same page, so the link updates the URL, tells the form, and scrolls to it without a reload.

import type { ProductType } from "@/lib/contracts.gen";
import type { SizeTier } from "@/lib/products";

export const PICK_EVENT = "ll:pick";

export interface PickDetail {
  product: ProductType;
  tier?: SizeTier;
}

export function orderHref(product: ProductType, tier?: SizeTier): string {
  const q = new URLSearchParams({ product });
  if (tier) q.set("size", tier);
  return `/?${q}#order`;
}

/** Same-page pick: returns true when it handled the click. */
export function pickOnPage(detail: PickDetail): boolean {
  if (typeof window === "undefined" || window.location.pathname !== "/") return false;
  const target = document.getElementById("order");
  if (!target) return false;
  window.history.replaceState(window.history.state, "", orderHref(detail.product, detail.tier));
  window.dispatchEvent(new CustomEvent<PickDetail>(PICK_EVENT, { detail }));
  const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  target.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  return true;
}
