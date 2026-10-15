// The first look prompt: the worker's concept rules, copied verbatim from
// worker/src/p2p_worker/stages/prompts.py (firstLook.test.ts parses that file and fails on drift).
// for_model(f"{PRODUCT_LINES[product]} {subject} {PROMPT_RULES}", "nano-banana").

import type { ProductType } from "../contracts.gen";

export const PROMPT_RULES =
  "One connected sculpted figure: every subject touching the others so it prints as one solid " +
  "piece, with a flat underside. Chunky proportions for a small print. No loose whiskers, no " +
  "thin tails, no gaps between subjects. Neutral plain background. Flat even lighting. " +
  "3/4 hero view.";

export const FORBIDDEN = [
  "base",
  "plinth",
  "pedestal",
  "plate",
  "stand",
  "podium",
  "ground or floor",
  "text or letters",
  "keyring",
  "loop or hole",
  "frame, ring or border",
  "extra objects the request does not name",
] as const;

export const NANO_OPENING =
  "The figure stands on its own feet with nothing under it, cut out and isolated " +
  "on a plain light background like a product photo.";
export const NANO_CLOSING = "Avoid: " + FORBIDDEN.join(", ") + ".";

export const PRODUCT_LINES: Record<ProductType, string> = {
  figurine: "A small collectible figurine.",
  keychain:
    "A keychain charm figure: chunky and compact, head and body close together, no thin " +
    "limbs, ears or tails sticking out.",
  nameplate:
    "A figure that will sit on a flat name plate: a flat, wide underside so it stands " + "steadily, compact pose.",
  ornament: "A hanging ornament figure: compact, rounded silhouette, no thin parts.",
  magnet: "A fridge magnet figure: facing forward, shallow depth, flat back, bold simple shapes.",
  other: "A small 3D-printed object.",
};

export const FIRST_LOOK_MODEL = "nano-banana";
const REQUEST_MAX = 300;

export function firstLookPrompt(product: ProductType, request: string | null | undefined): string {
  const req = (request ?? "").trim().slice(0, REQUEST_MAX);
  const subject = req ? `Based on the reference photo. ${req}` : "Based on the reference photo.";
  return `${NANO_OPENING} ${PRODUCT_LINES[product]} ${subject} ${PROMPT_RULES} ${NANO_CLOSING}`;
}
