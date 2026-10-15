"use client";

// Decorative ideas typed into the empty What you want field: types at 42 ms a character, holds
// 1800 ms, erases at 20 ms a character, waits 400 ms, then the next phrase. The parent turns it off
// the moment the field has focus or text (active=false) and on again once it is empty and blurred.
// It pauses while off screen or while the tab is hidden. aria-hidden: the real label stays above
// the field and one static example sits in the helper text. Reduced motion: one static example.

import { useEffect, useRef, useState } from "react";

const TYPE_MS = 42;
const HOLD_MS = 1800;
const ERASE_MS = 20;
const GAP_MS = 400;

function prefersReduced(): boolean {
  return typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

export function TypingHint({ phrases, active }: { phrases: readonly string[]; active: boolean }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [text, setText] = useState("");
  const [reduced, setReduced] = useState(false);
  const [onScreen, setOnScreen] = useState(true);
  const [tabVisible, setTabVisible] = useState(true);
  const key = phrases.join("|");

  useEffect(() => {
    setReduced(prefersReduced());
    const onVis = () => setTabVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", onVis);
    let io: IntersectionObserver | null = null;
    if (ref.current && typeof IntersectionObserver !== "undefined") {
      io = new IntersectionObserver(([e]) => setOnScreen(e.isIntersecting));
      io.observe(ref.current);
    }
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      io?.disconnect();
    };
  }, []);

  const running = active && !reduced && onScreen && tabVisible && phrases.length > 0;

  useEffect(() => {
    if (!running) return;
    const list = key.split("|");
    let i = 0;
    let n = 0;
    let dir: 1 | -1 = 1;
    let t: ReturnType<typeof setTimeout>;
    setText("");
    const tick = () => {
      const p = list[i];
      if (dir === 1) {
        n++;
        setText(p.slice(0, n));
        if (n >= p.length) {
          dir = -1;
          t = setTimeout(tick, HOLD_MS);
          return;
        }
        t = setTimeout(tick, TYPE_MS);
      } else {
        n--;
        setText(p.slice(0, n));
        if (n <= 0) {
          dir = 1;
          i = (i + 1) % list.length;
          t = setTimeout(tick, GAP_MS);
          return;
        }
        t = setTimeout(tick, ERASE_MS);
      }
    };
    t = setTimeout(tick, GAP_MS);
    return () => clearTimeout(t);
  }, [running, key]);

  const shown = !active ? "" : reduced ? phrases[0] ?? "" : text;
  return (
    <span
      ref={ref}
      aria-hidden
      data-testid="typing-hint"
      className="pointer-events-none absolute left-[15px] right-[15px] top-[13px] whitespace-pre-wrap text-base leading-6 text-ink-3"
    >
      {shown}
      {active && !reduced && <span className="typing-caret" />}
    </span>
  );
}
