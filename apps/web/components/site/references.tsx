// Reference objects for the size visualizer, drawn in mm (1 user unit = 1 mm) from the design
// package's reference-objects.svg: single-weight ink outlines at their published sizes.
//   US quarter 24.26 mm across (US Mint); credit card 85.60 x 53.98 mm (ISO/IEC 7810 ID-1);
//   AirPods Pro case (2nd gen) 45.2 x 60.6 mm (Apple tech specs); adult hand about 190 mm.

import type { StandardProduct } from "@/lib/products";

export type RefKind = "quarter" | "card" | "airpods" | "hand";

export interface RefObject {
  kind: RefKind;
  label: string;
  /** Under the drawing, in measure style. */
  dims: string;
  /** Accessible name. */
  alt: string;
  /** viewBox: [x, y, w, h] in mm; the object's bottom sits `bottom` mm above the box bottom. */
  viewBox: [number, number, number, number];
  bottom: number;
  /** Object size in mm (for fitting the drawing). */
  w: number;
  h: number;
}

export const REFS: Record<RefKind, RefObject> = {
  quarter: { kind: "quarter", label: "Quarter", dims: "24.26 mm", alt: "US quarter, 24.26 mm", viewBox: [-1, -1, 26.26, 26.26], bottom: 1, w: 24.26, h: 24.26 },
  card: { kind: "card", label: "Card", dims: "85.6 x 54 mm", alt: "Credit card, 85.6 by 54 mm", viewBox: [-1, -1, 87.6, 55.98], bottom: 1, w: 85.6, h: 53.98 },
  airpods: { kind: "airpods", label: "AirPods", dims: "45.2 x 60.6 mm", alt: "AirPods Pro case, 45.2 by 60.6 mm", viewBox: [-1, -1, 47.2, 62.6], bottom: 1, w: 45.2, h: 60.6 },
  hand: { kind: "hand", label: "Hand", dims: "about 190 mm", alt: "Adult hand, about 19 cm", viewBox: [-34, -2, 132, 194], bottom: 2, w: 132, h: 190 },
};

export const REF_ORDER: readonly RefKind[] = ["quarter", "card", "airpods", "hand"];

/** Quarter for keychain and magnet, card for nameplate and ornament, hand for figure. */
export const DEFAULT_REF: Record<StandardProduct, RefKind> = {
  keychain: "quarter",
  magnet: "quarter",
  nameplate: "card",
  ornament: "card",
  figurine: "hand",
};

const S = { stroke: "currentColor", vectorEffect: "non-scaling-stroke" as const };
const PAPER = "var(--color-paper)";

export function RefShape({ kind }: { kind: RefKind }) {
  switch (kind) {
    case "quarter":
      return (
        <>
          <circle cx="12.13" cy="12.13" r="12.13" fill={PAPER} strokeWidth={1.5} {...S} />
          <circle cx="12.13" cy="12.13" r="10.9" fill="none" strokeWidth={1} strokeDasharray="1 2.2" opacity={0.55} {...S} />
        </>
      );
    case "card":
      return (
        <>
          <rect x="0" y="0" width="85.6" height="53.98" rx="3.18" fill={PAPER} strokeWidth={1.5} {...S} />
          <rect x="9" y="19" width="11.5" height="9" rx="1.6" fill="none" strokeWidth={1} {...S} />
          <path d="M9 23.5h11.5M14.75 19v9" strokeWidth={1} opacity={0.6} {...S} />
          <path d="M9 37h8M20 37h8M31 37h8M42 37h8" strokeWidth={1.5} strokeLinecap="round" opacity={0.55} {...S} />
        </>
      );
    case "airpods":
      return (
        <>
          <rect x="0" y="0" width="45.2" height="60.6" rx="11" fill={PAPER} strokeWidth={1.5} {...S} />
          <path d="M0.4 19.5h44.4" strokeWidth={1} opacity={0.6} {...S} />
          <circle cx="22.6" cy="27" r="0.9" fill="currentColor" opacity={0.6} />
        </>
      );
    case "hand": {
      const parts = (
        <>
          <rect x="10" y="84" width="85" height="92" rx="26" />
          <rect x="12" y="33" width="19" height="72" rx="9.5" />
          <rect x="33" y="0" width="20" height="100" rx="10" />
          <rect x="55" y="8" width="19.5" height="92" rx="9.75" />
          <rect x="76.5" y="26" width="17.5" height="76" rx="8.75" />
          <rect x="6" y="96" width="24" height="64" rx="12" transform="rotate(-34 18 160)" />
          <rect x="24" y="158" width="58" height="32" rx="10" />
        </>
      );
      // Union outline: thick ink strokes under paper fills.
      return (
        <>
          <g fill="none" stroke="currentColor" strokeWidth={3} strokeLinejoin="round" style={{ vectorEffect: "non-scaling-stroke" }}>
            {parts}
          </g>
          <g fill={PAPER}>{parts}</g>
        </>
      );
    }
  }
}

/** A reference drawn at a CSS px-per-mm length, its bottom on the baseline. */
export function RefDrawing({ kind, ppm }: { kind: RefKind; ppm: string }) {
  const r = REFS[kind];
  const [x, y, w, h] = r.viewBox;
  return (
    <svg
      role="img"
      aria-label={r.alt}
      viewBox={`${x} ${y} ${w} ${h}`}
      className="block shrink-0 text-ink [&_rect]:[vector-effect:non-scaling-stroke]"
      style={{ width: `calc(${ppm} * ${w})`, height: `calc(${ppm} * ${h})`, marginBottom: `calc(${ppm} * -${r.bottom})` }}
    >
      <RefShape kind={kind} />
    </svg>
  );
}
