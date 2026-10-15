"use client";

// Optional first look on the confirmation page (V1Confirmation). States: idle, printing (the
// product's front render as a mask filled with filament layers building bottom-up, a brick line
// on top, a changing status), done (the image prints in), capped or off, failed. One per order.
// The server decides everything (app/api/orders/first-look); this only shows states and polls.

import Image from "next/image";
import { useEffect, useRef, useState } from "react";
import type { StandardProduct } from "@/lib/products";
import { frontRender } from "@/lib/renders";
import { PrintIn } from "./PrintIn";
import { SHEET, btn, cx } from "./ui";

export type FirstLookView = "idle" | "printing" | "done" | "capped" | "failed";

export const FIRST_LOOK_COPY = {
  button: "See a first look",
  statuses: ["Reading your photo", "Sketching", "Almost there"],
  tag: "Rough first look",
  paused: "First looks are paused",
  failed: "No first look this time",
} as const;

const POLL_MS = 3000;
const GIVE_UP_MS = 4 * 60 * 1000;
const tokenKey = (receipt: string) => `layerling.firstlook.${receipt.slice(-16)}`;

function readToken(receipt: string): string | null {
  try {
    return window.sessionStorage.getItem(tokenKey(receipt));
  } catch {
    return null;
  }
}
function saveToken(receipt: string, t: string) {
  try {
    window.sessionStorage.setItem(tokenKey(receipt), t);
  } catch {
    // no storage: polling still works for this page view
  }
}

type Reply = { state: "off" | "capped" | "failed" | "printing" | "done"; task?: string | null; url?: string; progress?: number };

export function FirstLook({
  receipt,
  product,
  initialUrl = null,
  initialView,
  fetchImpl = fetch,
}: {
  receipt: string;
  product: StandardProduct | null;
  initialUrl?: string | null;
  /** Screenshots and tests only. */
  initialView?: FirstLookView;
  fetchImpl?: typeof fetch;
}) {
  const [view, setView] = useState<FirstLookView>(initialUrl ? "done" : (initialView ?? "idle"));
  const [url, setUrl] = useState<string | null>(initialUrl);
  const [status, setStatus] = useState(0);
  const startedAt = useRef(0);

  const apply = (r: Reply) => {
    if (r.state === "done" && r.url) {
      setUrl(r.url);
      setView("done");
    } else if (r.state === "off" || r.state === "capped") setView("capped");
    else if (r.state === "failed") setView("failed");
    else setView("printing");
  };

  // Poll while printing.
  useEffect(() => {
    if (view !== "printing") return;
    const statusTimer = window.setInterval(() => setStatus((s) => Math.min(s + 1, FIRST_LOOK_COPY.statuses.length - 1)), 9000);
    const token = readToken(receipt);
    if (!token) return () => window.clearInterval(statusTimer);
    startedAt.current ||= Date.now();
    let live = true;
    const tick = async () => {
      if (!live) return;
      if (Date.now() - startedAt.current > GIVE_UP_MS) return setView("failed");
      try {
        const res = await fetchImpl(`/api/orders/first-look?t=${encodeURIComponent(token)}`);
        const r = (await res.json()) as Reply;
        if (!live) return;
        if (r.state !== "printing") return apply(r);
      } catch {
        // keep polling until the give-up time
      }
      if (live) window.setTimeout(tick, POLL_MS);
    };
    const first = window.setTimeout(tick, POLL_MS);
    return () => {
      live = false;
      window.clearTimeout(first);
      window.clearInterval(statusTimer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, receipt]);

  const start = async () => {
    setView("printing");
    setStatus(0);
    startedAt.current = Date.now();
    try {
      const res = await fetchImpl("/api/orders/first-look", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ receipt }),
      });
      const r = (await res.json()) as Reply;
      if (r.state === "printing" && r.task) saveToken(receipt, r.task);
      apply(r);
    } catch {
      setView("failed");
    }
  };

  const front = product ? frontRender(product) : null;

  return (
    <section aria-labelledby="first-look-title" className={cx(SHEET, "flex flex-col gap-4 p-5 shadow-none sm:p-6")} data-testid="first-look" data-state={view}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="first-look-title" className="font-display m-0 text-[22px] leading-[1.15]">
          First look
        </h2>
        {view === "done" && <span className="measure rounded-pill border border-line px-2.5 py-0.5 text-[12px] text-ink-2">{FIRST_LOOK_COPY.tag}</span>}
      </div>

      {/* Fixed square box: every state has the same footprint, so nothing shifts. */}
      <div className="relative mx-auto aspect-square w-full max-w-[320px] overflow-hidden rounded-md bg-paper">
        {view === "idle" && (
          <div className="flex size-full items-center justify-center">
            <button type="button" className={btn("secondary", "md")} onClick={() => void start()}>
              {FIRST_LOOK_COPY.button}
            </button>
          </div>
        )}
        {view === "printing" && (
          <div className="flex size-full flex-col items-center justify-center gap-4 p-6" role="status" aria-live="polite">
            {front && (
              <span
                aria-hidden
                className="first-look-build block aspect-square w-[70%]"
                style={{
                  WebkitMaskImage: `url(${front.src})`,
                  maskImage: `url(${front.src})`,
                  WebkitMaskSize: "contain",
                  maskSize: "contain",
                  WebkitMaskRepeat: "no-repeat",
                  maskRepeat: "no-repeat",
                  WebkitMaskPosition: "center",
                  maskPosition: "center",
                }}
              />
            )}
            <span className="measure text-[13px] text-ink-2">{FIRST_LOOK_COPY.statuses[status]}</span>
          </div>
        )}
        {view === "done" && url && (
          <PrintIn mode="load" className="block size-full">
            <Image src={url} alt="First look" fill sizes="320px" unoptimized className="object-contain" draggable={false} onContextMenu={(e) => e.preventDefault()} />
          </PrintIn>
        )}
        {(view === "capped" || view === "failed") && (
          <div className="flex size-full items-center justify-center p-6 text-center text-body-strong text-ink-2">
            {view === "capped" ? FIRST_LOOK_COPY.paused : FIRST_LOOK_COPY.failed}
          </div>
        )}
      </div>
    </section>
  );
}
