// The signature motion: a render builds from the bottom up in 18 layers, like a print.
// CSS only (app/motion.css): scroll-linked where `animation-timeline: view()` is supported, else
// MotionObserver adds .is-printing once it scrolls into view and it runs over dur-print.
// mode "load" runs once on load (the hero, the first look). The wrapper is the image box, so
// size it (width and height, or a sized parent) and nothing shifts. Never on text or photos.

import type { CSSProperties, ReactNode } from "react";
import { cx } from "./ui";

export function PrintIn({
  children,
  mode = "scroll",
  step = 0,
  delayMs,
  className,
  style,
}: {
  children: ReactNode;
  mode?: "scroll" | "load";
  /** Position in a row (0, 1, 2, ...): later items print a little later, left to right. */
  step?: number;
  /** Time-based delay; defaults to 80 ms per step. */
  delayMs?: number;
  className?: string;
  style?: CSSProperties;
}) {
  const vars = {
    "--print-delay": `${delayMs ?? step * 80}ms`,
    "--print-from": `${10 + step * 3}%`,
    "--print-to": `${42 + step * 3}%`,
  } as CSSProperties;
  return (
    <span className={cx("print-in", className)} data-print={mode} style={{ ...vars, ...style }}>
      <span className="print-in-body">{children}</span>
      <span className="print-in-line" aria-hidden />
    </span>
  );
}
