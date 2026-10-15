// A 1 mm ruler for drawings at a real scale: 1 mm ticks, longer at 5 and 10 mm, a label every
// `labelEvery` mm. Drawn in mm units and stretched to the drawing's px-per-mm, so it stays exact at
// any scale; the parent clips it to the drawing's width.

import type { CSSProperties } from "react";
import { cx } from "./ui";

export function Ruler({
  ppm,
  lengthMm = 480,
  labelEvery = 20,
  className,
}: {
  /** A CSS length per mm: "var(--ppm)", "3.4px", ... */
  ppm: string;
  lengthMm?: number;
  labelEvery?: number;
  className?: string;
}) {
  let d = "";
  for (let i = 0; i <= lengthMm; i++) d += `M${i} 0v${i % 10 === 0 ? 12 : i % 5 === 0 ? 8 : 5}`;
  const labels: number[] = [];
  for (let n = 0; n <= lengthMm; n += labelEvery) labels.push(n);
  return (
    <div aria-hidden className={cx("relative h-7 overflow-hidden", className)} style={{ "--rppm": ppm } as CSSProperties}>
      <svg
        viewBox={`0 0 ${lengthMm} 12`}
        preserveAspectRatio="none"
        shapeRendering="crispEdges"
        className="absolute left-px top-0 block h-3"
        style={{ width: `calc(var(--rppm) * ${lengthMm})` }}
      >
        <path d={d} stroke="var(--color-line-strong)" strokeWidth={1} vectorEffect="non-scaling-stroke" fill="none" />
      </svg>
      {labels.map((n) => (
        <span
          key={n}
          className="measure absolute top-[13px] whitespace-nowrap pl-[3px] text-measure-s text-ink-2"
          style={{ left: `calc(var(--rppm) * ${n})` }}
        >
          {n === 0 ? "0 mm" : n}
        </span>
      ))}
    </div>
  );
}
