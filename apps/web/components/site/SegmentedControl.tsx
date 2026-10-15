"use client";

// Two to four mutually exclusive views in an ink outlined pill. The ink fill slides between
// segments over dur-base (a clip-path on one ink layer, so only clip-path animates).

import { useLayoutEffect, useRef, useState } from "react";
import { cx } from "./ui";

export function SegmentedControl<T extends string>({
  label,
  options,
  value,
  onChange,
  className,
}: {
  label: string;
  options: readonly { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
  className?: string;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const btns = useRef<(HTMLButtonElement | null)[]>([]);
  const [clip, setClip] = useState<string | null>(null);

  useLayoutEffect(() => {
    const measure = () => {
      const w = wrap.current;
      const b = btns.current[options.findIndex((o) => o.value === value)];
      if (!w || !b) return;
      const left = b.offsetLeft;
      const right = w.clientWidth - (b.offsetLeft + b.offsetWidth);
      const top = b.offsetTop;
      const bottom = w.clientHeight - (b.offsetTop + b.offsetHeight);
      setClip(`inset(${top}px ${right}px ${bottom}px ${left}px round 999px)`);
    };
    measure();
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    if (wrap.current) ro?.observe(wrap.current);
    return () => ro?.disconnect();
  }, [value, options]);

  return (
    <div
      ref={wrap}
      role="group"
      aria-label={label}
      className={cx("relative inline-flex shrink-0 rounded-pill border border-ink p-[3px]", className)}
    >
      {clip && (
        <span
          aria-hidden
          className="pointer-events-none absolute inset-0 rounded-pill bg-ink transition-[clip-path] duration-[var(--dur-base)] ease-[var(--ease-out)]"
          style={{ clipPath: clip }}
        />
      )}
      {options.map((o, i) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            ref={(el) => {
              btns.current[i] = el;
            }}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(o.value)}
            className={cx(
              "relative z-[1] h-9 whitespace-nowrap rounded-pill px-3 text-[15px] font-medium leading-none transition-colors duration-[var(--dur-base)] sm:px-4",
              on ? "text-paper" : "text-ink hover:bg-hover",
              on && !clip && "bg-ink",
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
