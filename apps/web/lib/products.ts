// Product helpers shared by the studio, the public site, the order form and the server routes.
// Size tiers mirror contracts/src/p2p_contracts/job.py SIZE_TIERS_MM (lib/products.test.ts parses
// job.py and fails if they drift).

import { NAME_TEXT_MAX, NAME_TEXT_PRODUCTS, PRODUCT_DEFAULTS } from "./contracts";
import type { AddOn, ProductType } from "./contracts.gen";

/** Studio labels. */
export const PRODUCT_LABELS: Record<ProductType, string> = {
  figurine: "Figurine",
  keychain: "Keychain",
  nameplate: "Nameplate",
  ornament: "Ornament",
  magnet: "Magnet",
  other: "Other",
};

export const ADDON_LABELS: Record<AddOn["kind"], string> = {
  round_base: "Round base",
  oval_base: "Oval base",
  keyring_loop: "Keyring loop",
  hanging_loop: "Hanging loop",
  nameplate: "Name plate",
  flat_back: "Flat back",
  magnet_pocket: "Magnet pocket",
};

/** Public site labels: a figurine is a "Figure". */
export const PUBLIC_LABELS: Record<ProductType, string> = {
  ...PRODUCT_LABELS,
  figurine: "Figure",
};

export type StandardProduct = Exclude<ProductType, "other">;
export type SizeTier = "xs" | "s" | "m" | "l";

export const SIZE_TIERS: readonly SizeTier[] = ["xs", "s", "m", "l"];
export const DEFAULT_SIZE_TIER: SizeTier = "m";

// job.py SIZE_TIERS_MM: the product's longest visible dimension per tier. Nameplate = plate width.
export const SIZE_TIERS_MM: Record<StandardProduct, Record<SizeTier, number>> = {
  keychain: { xs: 40, s: 45, m: 52, l: 60 },
  nameplate: { xs: 80, s: 100, m: 120, l: 150 },
  figurine: { xs: 50, s: 70, m: 90, l: 120 },
  ornament: { xs: 50, s: 60, m: 70, l: 85 },
  magnet: { xs: 40, s: 50, m: 60, l: 75 },
};

/** Which way the tier measures: nameplate by width, the rest by height. */
export const TIER_AXIS: Record<StandardProduct, "width" | "height"> = {
  keychain: "height",
  nameplate: "width",
  figurine: "height",
  ornament: "height",
  magnet: "height",
};

/** Picker order: the five standard products, then Other. */
export const ORDER_PRODUCTS: readonly ProductType[] = ["keychain", "nameplate", "figurine", "ornament", "magnet", "other"];
/** Landing lineup order, left to right on the shelf. */
export const LINEUP_PRODUCTS: readonly StandardProduct[] = ["figurine", "nameplate", "ornament", "magnet", "keychain"];
/** Sizes page tabs. */
export const SIZES_PRODUCTS: readonly StandardProduct[] = ["keychain", "nameplate", "figurine", "ornament", "magnet"];

/** One concrete line per product (landing lineup). */
export const PRODUCT_LINES: Record<StandardProduct, string> = {
  figurine: "Stands on a round base. Two pets together works too.",
  nameplate: "Their name on a plate, with them sitting on one end.",
  ornament: "A loop on top for a ribbon or hook.",
  magnet: "Flat back with a pocket for the magnet.",
  keychain: "The loop is printed in, not glued on.",
};

/** Ideas the typing hint cycles through in the empty What you want field. */
export const TYPING_HINTS: Record<ProductType, readonly string[]> = {
  keychain: ["keychain of my frenchie with her pink collar", "my cat's face, loop on top", "our dog Pepper sitting, red bandana"],
  nameplate: ["our beagle Biscuit on a nameplate", "two dogs side by side, names Milo and June"],
  figurine: ["me and my dad fishing, 4 inch figure", "my two cats curled up asleep", "my horse standing, brown with a white blaze"],
  ornament: ["my puppy in a Santa hat", "our cat as a tree ornament"],
  magnet: ["fridge magnet of my parrot", "my dog's head, big ears"],
  other: ["a small planter shaped like my dog", "a cake topper of us and our cat"],
};

export function isStandard(product: ProductType): product is StandardProduct {
  return product !== "other";
}

export function isSizeTier(v: unknown): v is SizeTier {
  return typeof v === "string" && (SIZE_TIERS as readonly string[]).includes(v);
}

export function tierMm(product: StandardProduct, tier: SizeTier): number {
  return SIZE_TIERS_MM[product][tier];
}

/** "tall" or "wide". */
export function axisWord(product: StandardProduct): "tall" | "wide" {
  return TIER_AXIS[product] === "width" ? "wide" : "tall";
}

/** "45 mm tall", "120 mm wide". */
export function sizeLabel(product: StandardProduct, tier: SizeTier): string {
  return `${tierMm(product, tier)} mm ${axisWord(product)}`;
}

/** "40 to 60 mm tall". */
export function sizeRange(product: StandardProduct): string {
  const t = SIZE_TIERS_MM[product];
  return `${t.xs} to ${t.l} mm ${axisWord(product)}`;
}

/** "M, 52 millimeters tall" for screen readers. */
export function sizeSpoken(product: StandardProduct, tier: SizeTier): string {
  return `${tier.toUpperCase()}, ${tierMm(product, tier)} millimeters ${axisWord(product)}`;
}

export function acceptsNameText(product: ProductType): boolean {
  return NAME_TEXT_PRODUCTS.includes(product);
}

export function cleanNameText(text: string | null | undefined): string | null {
  const t = (text ?? "").replace(/\s+/g, " ").trim();
  return t ? t.slice(0, NAME_TEXT_MAX) : null;
}

/**
 * Default add-ons for a product (contracts PRODUCT_DEFAULTS), with name text on the nameplate.
 * A keychain with name text gets a small name plate as well as its loop.
 */
export function addonsFor(product: ProductType, nameText?: string | null): AddOn[] {
  const text = acceptsNameText(product) ? cleanNameText(nameText) : null;
  const kinds = PRODUCT_DEFAULTS[product].addons;
  const addons: AddOn[] = kinds.map((kind) =>
    kind === "nameplate" && text ? { kind, text: { text } } : { kind },
  );
  if (text && !kinds.includes("nameplate")) addons.push({ kind: "nameplate", text: { text } });
  return addons;
}

/** The first name text found in a job's add-ons. */
export function nameTextOf(addons: AddOn[] | null | undefined): string | null {
  return addons?.find((a) => a.text?.text)?.text?.text ?? null;
}
