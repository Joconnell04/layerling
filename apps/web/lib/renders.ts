// Product renders (public/products, from scripts/render_products.py at the repo root).
// `-front` renders are orthographic at front_px_per_mm (10) with a 60 px margin; front_object_px
// is the object's pixel box [x0, y0, x1, y1] inside that margin. Size drawings scale these so one
// px-per-mm holds for the whole drawing.

import renders from "../public/products/renders.json";
import { TIER_AXIS, tierMm, type SizeTier, type StandardProduct } from "./products";

export interface FrontRender {
  src: string;
  /** Source image size in px. */
  width: number;
  height: number;
  /** Object box in source px: [x0, y0, x1, y1]. */
  box: [number, number, number, number];
  /** Source px per mm (10). */
  pxPerMm: number;
  /** The object's size in mm at render time: [x, y, z]. */
  sizeMm: [number, number, number];
}

type Raw = {
  hero: string;
  front: string;
  front_px_per_mm: number;
  front_size_px: [number, number];
  front_object_px: [number, number, number, number];
  size_mm: [number, number, number];
};

const R = renders as unknown as Record<StandardProduct, Raw>;

export function heroSrc(product: StandardProduct): string {
  return `/products/${R[product].hero}`;
}

export function frontRender(product: StandardProduct): FrontRender {
  const r = R[product];
  return {
    src: `/products/${r.front}`,
    width: r.front_size_px[0],
    height: r.front_size_px[1],
    box: r.front_object_px,
    pxPerMm: r.front_px_per_mm,
    sizeMm: r.size_mm,
  };
}

/** The rendered object's size on its tier axis in mm (nameplate width is x, the rest height is z). */
export function renderAxisMm(product: StandardProduct): number {
  const s = R[product].size_mm;
  return TIER_AXIS[product] === "width" ? s[0] : s[2];
}

export interface Placed {
  src: string;
  /** Whole image box in CSS px (margins included). */
  width: number;
  height: number;
  /** Where the image's left and bottom edges sit relative to the object's left and bottom. */
  offsetLeft: number;
  offsetBottom: number;
  /** The object's own drawn size in CSS px. */
  objectWidth: number;
  objectHeight: number;
}

/** The -front render at `tier` in mm (placeFront at 1 px per mm): multiply by a px-per-mm length. */
export function frontMm(product: StandardProduct, tier: SizeTier) {
  const d = placeFront(product, tier, 1);
  const r = (n: number) => Math.round(n * 1000) / 1000;
  return {
    src: d.src,
    imgW: r(d.width),
    imgH: r(d.height),
    /** The transparent margin around the object (the same on every side). */
    margin: r(-d.offsetLeft),
    objW: r(d.objectWidth),
    objH: r(d.objectHeight),
  };
}

/**
 * The -front render of `product` at `tier`, drawn at `pxPerMm` CSS px per mm: scale =
 * pxPerMm * (tierMm / renderAxisMm) / sourcePxPerMm. The margins scale with it, so the image box
 * is placed with negative offsets to put the object's bottom-left on the drawing's anchor.
 */
export function placeFront(product: StandardProduct, tier: SizeTier, pxPerMm: number): Placed {
  const f = frontRender(product);
  const scale = (pxPerMm * (tierMm(product, tier) / renderAxisMm(product))) / f.pxPerMm;
  const [x0, , x1, y1] = f.box;
  return {
    src: f.src,
    width: f.width * scale,
    height: f.height * scale,
    offsetLeft: -x0 * scale,
    offsetBottom: -(f.height - y1) * scale,
    objectWidth: (x1 - x0) * scale,
    objectHeight: (y1 - f.box[1]) * scale,
  };
}
