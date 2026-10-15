// What happens after an order, honest about who does what. Times stay placeholders until Jackson
// sets them ([TURNAROUND], [DELIVERY]). Markers: now is brick with a brick-wash ring, later is an
// outlined dot; a 1 px line-strong rule joins them. Vertical beside the order form, a row of four
// on the confirmation page.

import { cx } from "./ui";

export const STEPS = [
  { title: "A person checks your photos", line: "Usually within [TURNAROUND]. We ask if we need a better photo." },
  { title: "You approve a preview", line: "A 3D preview in the real filament colors. Change anything." },
  { title: "We print it", line: "In color, no paint. Nothing is charged until you approve." },
  { title: "Pickup in New York or shipping", line: "[DELIVERY]" },
] as const;

function Dot({ now }: { now: boolean }) {
  return (
    <span
      aria-hidden
      className={cx(
        "relative m-[5px] block size-3.5 shrink-0 rounded-full border-[1.5px]",
        now ? "border-brick bg-brick shadow-[0_0_0_4px_var(--color-brick-wash)]" : "border-line-strong bg-paper",
      )}
    />
  );
}

export function NextSteps({ layout = "column", current }: { layout?: "column" | "row"; current?: number }) {
  if (layout === "row") {
    return (
      <ol className="m-0 grid list-none grid-cols-1 gap-6 p-0 md:grid-cols-4">
        {STEPS.map((s, i) => (
          <li key={s.title} className="relative flex gap-3 md:flex-col" aria-current={i === current ? "step" : undefined}>
            {i < STEPS.length - 1 && (
              <span aria-hidden className="absolute left-[11.5px] top-6 -bottom-6 w-px bg-line-strong md:-right-5 md:bottom-auto md:left-[30px] md:top-3 md:h-px md:w-auto" />
            )}
            <Dot now={i === current} />
            <div className="flex flex-col gap-1">
              <span className="measure text-[12px] text-ink-3">{i === current ? "Now" : `Step ${i + 1}`}</span>
              <span className="text-body-strong">{s.title}</span>
              <span className="text-small text-ink-2">{s.line}</span>
            </div>
          </li>
        ))}
      </ol>
    );
  }
  return (
    <ol className="m-0 list-none p-0">
      {STEPS.map((s, i) => (
        <li key={s.title} className="relative grid grid-cols-[24px_minmax(0,1fr)] gap-3 pb-5" aria-current={i === current ? "step" : undefined}>
          {i < STEPS.length - 1 && <span aria-hidden className="absolute -bottom-0.5 left-[11.5px] top-6 w-px bg-line-strong" />}
          <Dot now={i === current} />
          <div>
            <div className="text-body-strong">{s.title}</div>
            <div className="text-small text-ink-2">{s.line}</div>
          </div>
        </li>
      ))}
    </ol>
  );
}
