"use client";

// A number that rolls to its new value over dur-base (price and mm in the order summary and size
// cells). Old and new share one grid cell with tabular numbers, so nothing reflows. Under reduced
// motion the new value simply replaces the old (app/motion.css).

import { useState } from "react";
import { cx } from "./ui";

export function Tick({ value, className, testId }: { value: string; className?: string; testId?: string }) {
  const [s, setS] = useState({ cur: value, prev: null as string | null, n: 0 });
  if (s.cur !== value) setS({ cur: value, prev: s.cur, n: s.n + 1 });
  return (
    <span className={cx("tick", className)} data-testid={testId}>
      {s.prev != null && (
        <span key={`o${s.n}`} className="tick-out" aria-hidden onAnimationEnd={() => setS((x) => ({ ...x, prev: null }))}>
          {s.prev}
        </span>
      )}
      <span key={`i${s.n}`} className={s.n > 0 ? "tick-in" : undefined}>
        {s.cur}
      </span>
    </span>
  );
}
