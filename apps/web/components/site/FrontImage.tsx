// A product's orthographic -front render at a CSS px-per-mm length, so every drawing that shares
// the length is at one honest scale. The image box includes the render's transparent margin; the
// negative margins put the object's bottom edge on the parent's baseline (the shelf line).

import Image from "next/image";
import type { CSSProperties } from "react";
import type { SizeTier, StandardProduct } from "@/lib/products";
import { frontMm } from "@/lib/renders";
import { PrintIn } from "./PrintIn";
import { cx } from "./ui";

export function FrontImage({
  product,
  tier,
  ppm,
  maxPpm = 4,
  align = "center",
  print,
  alt = "",
  className,
}: {
  product: StandardProduct;
  tier: SizeTier;
  /** A CSS length per mm: "var(--ppm)", "3.4px", ... */
  ppm: string;
  /** The largest px per mm this drawing reaches, for the srcset `sizes` hint. */
  maxPpm?: number;
  /** "start": the object's left edge on the column's left edge; "center": centered. */
  align?: "start" | "center";
  /** Print in on scroll at this step in a row. */
  print?: { step: number };
  alt?: string;
  className?: string;
}) {
  const d = frontMm(product, tier);
  const mm = (n: number) => `calc(${ppm} * ${n})`;
  const box: CSSProperties = {
    width: mm(d.imgW),
    height: mm(d.imgH),
    marginBottom: `calc(${ppm} * -${d.margin})`,
    marginLeft: `calc(${ppm} * -${d.margin})`,
    marginRight: align === "center" ? `calc(${ppm} * -${d.margin})` : undefined,
    flexShrink: 0,
  };
  const img = (
    <Image
      src={d.src}
      alt={alt}
      width={Math.round(d.imgW * 10)}
      height={Math.round(d.imgH * 10)}
      sizes={`${Math.ceil(d.imgW * maxPpm)}px`}
      draggable={false}
      className="block h-full w-full select-none"
    />
  );
  if (print) {
    return (
      <PrintIn step={print.step} className={className} style={box}>
        {img}
      </PrintIn>
    );
  }
  return (
    <span className={cx("block", className)} style={box}>
      {img}
    </span>
  );
}
