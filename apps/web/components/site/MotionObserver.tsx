"use client";

// One IntersectionObserver for the public site's once-only motion (app/motion.css):
//   [data-reveal]                    gets .is-revealed (h2s and ledes rise and fade in)
//   .print-in[data-print="scroll"]   gets .is-printing, only where scroll timelines are missing
// No scroll listeners. Claims html.js (see app/layout.tsx) so the page never stays hidden.

import { usePathname } from "next/navigation";
import { useEffect } from "react";

declare global {
  interface Window {
    __llMotion?: boolean;
  }
}

export function MotionObserver() {
  const pathname = usePathname();

  useEffect(() => {
    window.__llMotion = true;
    document.documentElement.classList.add("js");
    const scrollTimelines = typeof CSS !== "undefined" && CSS.supports?.("animation-timeline: view()");
    if (typeof IntersectionObserver === "undefined") {
      document.querySelectorAll("[data-reveal]").forEach((el) => el.classList.add("is-revealed"));
      document.querySelectorAll(".print-in").forEach((el) => el.classList.add("is-printing"));
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (!e.isIntersecting) continue;
          const el = e.target as HTMLElement;
          el.classList.add(el.hasAttribute("data-reveal") ? "is-revealed" : "is-printing");
          io.unobserve(el);
        }
      },
      { rootMargin: "0px 0px -8% 0px", threshold: 0.12 },
    );
    const seen = new WeakSet<Element>();
    const scan = () => {
      const sel = scrollTimelines ? "[data-reveal]" : '[data-reveal], .print-in[data-print="scroll"]';
      document.querySelectorAll(sel).forEach((el) => {
        if (seen.has(el)) return;
        seen.add(el);
        io.observe(el);
      });
    };
    scan();
    const mo = new MutationObserver(scan);
    mo.observe(document.body, { childList: true, subtree: true });
    return () => {
      io.disconnect();
      mo.disconnect();
    };
  }, [pathname]);

  return null;
}
