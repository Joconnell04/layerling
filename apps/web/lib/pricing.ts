// Prices from the cost model (costs/pricing.json, copied at predev/prebuild/pretest).
// Shape: {updated_at, currency, products: {<product>: {price, from, cost_floor, basis, channels,
// sizes?}}, other?: {min, max}}.
// `from` is a flag in the cost model ("show as from $X"); older files had it as a number.
// Per-size prices: products.<p>.sizes.<xs|s|m|l> as a number or {price}. The Other range:
// products.other.range {min, max}, products.other.price_min/price_max, or top-level other {min, max}.

// "@pricing" resolves to lib/pricing.generated.json (copied from costs/pricing.json before dev,
// build and test), falling back to the checked-in lib/pricing.placeholder.json when it is missing.
import generated from "@pricing";
import type { ProductType } from "./contracts.gen";
import type { SizeTier, StandardProduct } from "./products";

type SizePrice = number | { price?: number; mm?: number };

export interface ProductPrice {
  price?: number;
  /** true: show "from $price"; a number: the from price itself (older files). */
  from?: boolean | number;
  cost_floor?: number;
  basis?: string;
  channels?: Record<string, number>;
  sizes?: Partial<Record<SizeTier, SizePrice>>;
  range?: { min?: number; max?: number };
  price_min?: number;
  price_max?: number;
}

export interface Pricing {
  missing?: boolean;
  updated_at?: string;
  currency?: string;
  products?: Partial<Record<ProductType, ProductPrice>>;
  other?: { min?: number; max?: number; basis?: string };
}

export const PRICE_PLACEHOLDER = "[PRICE]";
/** What Other shows until the cost model has a range. */
export const OTHER_UNPRICED = "Priced after we see it";

const DEFAULT = generated as Pricing;

const valid = (n: unknown): n is number => typeof n === "number" && Number.isFinite(n) && n > 0;

export function formatMoney(amount: number, currency = "USD"): string {
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency,
      maximumFractionDigits: Number.isInteger(amount) ? 0 : 2,
    }).format(amount);
  } catch {
    return `$${amount}`;
  }
}

function productPrice(product: ProductType, pricing: Pricing): ProductPrice | undefined {
  return pricing.missing ? undefined : pricing.products?.[product];
}

/** The starting price for a product (`price`, or an older numeric `from`), or null. */
export function baseAmount(product: ProductType, pricing: Pricing = DEFAULT): number | null {
  const p = productPrice(product, pricing);
  const amount = typeof p?.from === "number" ? p.from : p?.price;
  return valid(amount) ? amount : null;
}

/** The lowest per-size price when the cost model has sizes, else the starting price, or null. */
export function fromAmount(product: ProductType, pricing: Pricing = DEFAULT): number | null {
  const sizes = Object.values(productPrice(product, pricing)?.sizes ?? {})
    .map((s) => (typeof s === "number" ? s : s?.price))
    .filter(valid);
  return sizes.length ? Math.min(...sizes) : baseAmount(product, pricing);
}

/** "from $X" for a product, or "[PRICE]" when the cost model has no number for it yet. */
export function priceFrom(product: ProductType, pricing: Pricing = DEFAULT): string {
  const amount = fromAmount(product, pricing);
  if (amount == null) return PRICE_PLACEHOLDER;
  const money = formatMoney(amount, pricing.currency ?? "USD");
  return productPrice(product, pricing)?.from === false ? money : `from ${money}`;
}

/** "from $X" for public pages, or null when there is no number (never "[PRICE]"). */
export function priceFromLabel(product: ProductType, pricing: Pricing = DEFAULT): string | null {
  const s = priceFrom(product, pricing);
  return s === PRICE_PLACEHOLDER ? null : s;
}

/** The per-size price when the cost model has one, else null. */
export function sizeAmount(product: StandardProduct, tier: SizeTier, pricing: Pricing = DEFAULT): number | null {
  const s = productPrice(product, pricing)?.sizes?.[tier];
  const amount = typeof s === "number" ? s : s?.price;
  return valid(amount) ? amount : null;
}

export interface PriceQuote {
  /** The number to show, when there is one. */
  amount: number | null;
  /** "$31.99", "from $31.99", "$31.99 to $100.99" or "Priced after we see it". */
  label: string;
  kind: "size" | "from" | "range" | "unpriced";
}

/** What a standard product at a size costs: the per-size price, else "from $X", else unpriced. */
export function priceFor(product: StandardProduct, tier: SizeTier, pricing: Pricing = DEFAULT): PriceQuote {
  const currency = pricing.currency ?? "USD";
  const exact = sizeAmount(product, tier, pricing);
  if (exact != null) return { amount: exact, label: formatMoney(exact, currency), kind: "size" };
  const from = priceFromLabel(product, pricing);
  if (from) return { amount: fromAmount(product, pricing), label: from, kind: "from" };
  return { amount: null, label: OTHER_UNPRICED, kind: "unpriced" };
}

/** The Other range from the cost model, or null. */
export function otherRange(pricing: Pricing = DEFAULT): { min: number; max: number } | null {
  if (pricing.missing) return null;
  const p = pricing.products?.other;
  const candidates = [
    { min: p?.range?.min, max: p?.range?.max },
    { min: p?.price_min, max: p?.price_max },
    { min: pricing.other?.min, max: pricing.other?.max },
  ];
  for (const c of candidates) if (valid(c.min) && valid(c.max) && c.max >= c.min) return { min: c.min, max: c.max };
  return null;
}

/** "$31.99 to $100.99" for Other, or "Priced after we see it" until the range exists. */
export function priceRange(product: "other", pricing: Pricing = DEFAULT): PriceQuote {
  void product;
  const r = otherRange(pricing);
  if (!r) return { amount: null, label: OTHER_UNPRICED, kind: "unpriced" };
  const currency = pricing.currency ?? "USD";
  return { amount: r.min, label: `${formatMoney(r.min, currency)} to ${formatMoney(r.max, currency)}`, kind: "range" };
}

/** The quote for any product: Other gets its range, the rest their size price. */
export function quote(product: ProductType, tier: SizeTier, pricing: Pricing = DEFAULT): PriceQuote {
  return product === "other" ? priceRange("other", pricing) : priceFor(product, tier, pricing);
}
