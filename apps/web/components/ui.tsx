"use client";

import {
  useId,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type TextareaHTMLAttributes,
} from "react";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

type ButtonVariant = "primary" | "default" | "ghost" | "danger";

export function Button({
  variant = "default",
  size = "md",
  className,
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: "sm" | "md";
}) {
  return (
    <button
      type="button"
      {...rest}
      className={cx(
        "inline-flex select-none items-center justify-center gap-2 whitespace-nowrap rounded-full font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-40",
        size === "md" ? "h-9 px-4 text-sm" : "h-7 px-3 text-xs",
        variant === "primary" && "bg-accent text-accent-fg hover:brightness-110",
        variant === "default" && "border border-ink bg-transparent text-fg hover:bg-hover",
        variant === "ghost" && "font-medium text-muted hover:bg-hover hover:text-fg",
        variant === "danger" && "border border-bad bg-transparent text-bad hover:bg-hover",
        className,
      )}
    >
      {children}
    </button>
  );
}

export type Tone = "faint" | "accent" | "ok" | "run" | "review" | "bad" | "pause";

const TONE_TEXT: Record<Tone, string> = {
  faint: "text-faint",
  accent: "text-accent",
  ok: "text-ok",
  run: "text-run",
  review: "text-review",
  bad: "text-bad",
  pause: "text-pause",
};
const TONE_BG: Record<Tone, string> = {
  faint: "bg-faint",
  accent: "bg-accent",
  ok: "bg-ok",
  run: "bg-run",
  review: "bg-review",
  bad: "bg-bad",
  pause: "bg-pause",
};

export function Dot({ tone, pulse }: { tone: Tone; pulse?: boolean }) {
  return <span className={cx("inline-block size-1.5 shrink-0 rounded-full", TONE_BG[tone], pulse && "pulse")} />;
}

export function Pill({ tone, children, pulse }: { tone: Tone; children: ReactNode; pulse?: boolean }) {
  return (
    <span
      className={cx(
        "inline-flex h-6 items-center gap-1.5 rounded-full border border-line bg-panel px-2 text-xs font-semibold",
        TONE_TEXT[tone],
      )}
    >
      <Dot tone={tone} pulse={pulse} />
      {children}
    </span>
  );
}

export function Tag({
  children,
  className,
  ...rest
}: { children: ReactNode; className?: string } & React.HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      {...rest}
      className={cx(
        "inline-flex h-6 items-center rounded-full border border-line px-2 text-xs text-muted",
        className,
      )}
    >
      {children}
    </span>
  );
}

/** Hover or focus tooltip. Content is details, never instructions. */
export function Tip({
  label,
  children,
  side = "top",
  className,
}: {
  label: ReactNode;
  children: ReactNode;
  side?: "top" | "bottom";
  className?: string;
}) {
  const id = useId();
  return (
    <span className={cx("group/tip relative inline-flex", className)} aria-describedby={id}>
      {children}
      <span
        id={id}
        role="tooltip"
        className={cx(
          "pointer-events-none absolute left-1/2 z-50 w-max max-w-72 -translate-x-1/2 rounded-md bg-ink px-2 py-1 text-xs font-normal normal-case tracking-normal text-bg opacity-0 shadow-md transition-opacity delay-150 group-focus-within/tip:opacity-100 group-hover/tip:opacity-100",
          side === "top" ? "bottom-full mb-1.5" : "top-full mt-1.5",
        )}
      >
        {label}
      </span>
    </span>
  );
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  size = "md",
  label,
}: {
  options: { value: T; label: ReactNode; disabled?: boolean; hint?: string }[];
  value: T;
  onChange: (v: T) => void;
  size?: "sm" | "md";
  label?: string;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex overflow-hidden rounded-full border border-ink">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          disabled={o.disabled}
          title={o.hint}
          onClick={() => onChange(o.value)}
          className={cx(
            "font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-35",
            size === "md" ? "h-9 px-4 text-sm" : "h-7 px-3 text-xs",
            value === o.value ? "bg-ink text-bg" : "text-fg hover:bg-hover",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Panel({
  title,
  right,
  children,
  active,
  className,
  bodyClassName,
  id,
  collapsed,
  onToggle,
}: {
  title: ReactNode;
  right?: ReactNode;
  children?: ReactNode;
  active?: boolean;
  className?: string;
  bodyClassName?: string;
  id?: string;
  /** With onToggle, the header gets a Show/Hide button and the body hides when collapsed. */
  collapsed?: boolean;
  onToggle?: () => void;
}) {
  return (
    <section
      id={id}
      className={cx(
        "rounded-lg border bg-panel",
        active ? "border-accent shadow-[0_0_0_3px_var(--color-accent-soft)]" : "border-line",
        className,
      )}
    >
      <header
        className={cx(
          "flex min-h-11 items-center justify-between gap-3 px-4 py-1.5",
          !(collapsed && onToggle) && children !== undefined && "border-b border-line",
        )}
      >
        <h2 className="font-display flex min-w-0 items-center gap-2 text-lg font-semibold text-fg">{title}</h2>
        <div className="flex shrink-0 items-center gap-2">
          {right}
          {onToggle && (
            <Button size="sm" variant="ghost" onClick={onToggle} aria-expanded={!collapsed}>
              {collapsed ? "Show" : "Hide"}
            </Button>
          )}
        </div>
      </header>
      {children !== undefined && !(collapsed && onToggle) && (
        <div className={cx("p-4", bodyClassName)}>{children}</div>
      )}
    </section>
  );
}

const INPUT_BASE =
  "rounded-md border border-line-strong/70 bg-panel text-fg placeholder:text-faint focus:border-ink focus:outline-none disabled:opacity-50";

/**
 * Input classes with defaults (h-7, px-2, text-sm, w-full) that the caller's classes replace
 * rather than fight (Tailwind does not resolve conflicting utilities by attribute order).
 */
export function inputCls(className = "", opts: { full?: boolean } = { full: true }): string {
  const has = (re: RegExp) => re.test(className);
  return cx(
    INPUT_BASE,
    !has(/(^|\s)h-/) && "h-9",
    !has(/(^|\s)px-/) && "px-3",
    !has(/(^|\s)text-(2xs|xs|sm|base|lg)/) && "text-sm",
    opts.full && !has(/(^|\s)(w-|flex-1|size-)/) && "w-full",
    className,
  );
}

export const inputClass = inputCls("", { full: false });

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={inputCls(props.className)} />;
}

export function Textarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      {...props}
      className={cx(
        "w-full resize-none rounded-md border border-line-strong/70 bg-panel px-3 py-2 text-sm text-fg placeholder:text-faint focus:border-ink focus:outline-none",
        props.className,
      )}
    />
  );
}

export function Label({ children, htmlFor }: { children: ReactNode; htmlFor?: string }) {
  return (
    <label htmlFor={htmlFor} className="mb-1.5 block text-sm font-semibold text-fg">
      {children}
    </label>
  );
}

export function Meter({
  value,
  max,
  tone = "accent",
  label,
}: {
  value: number;
  max: number;
  tone?: Tone;
  label?: string;
}) {
  const pct = Math.max(0, Math.min(1, max ? value / max : 0));
  return (
    <div
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={value}
      className="h-1.5 w-full overflow-hidden rounded-full bg-line"
    >
      <div className={cx("h-full rounded-full", TONE_BG[tone])} style={{ width: `${pct * 100}%` }} />
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="px-3 py-10 text-center text-sm text-faint">{children}</div>;
}

export function ErrorLine({ children }: { children: ReactNode }) {
  if (!children) return null;
  return (
    <p role="alert" className="text-xs text-bad">
      {children}
    </p>
  );
}

/** Inline popover anchored to a trigger. No browser dialogs. */
export function Popover({
  trigger,
  children,
  align = "right",
  side = "bottom",
}: {
  trigger: (props: { open: boolean; toggle: () => void }) => ReactNode;
  children: (close: () => void) => ReactNode;
  align?: "left" | "right";
  side?: "top" | "bottom";
}) {
  const [open, setOpen] = useState(false);
  return (
    <div
      className="relative"
      onKeyDown={(e) => {
        if (e.key === "Escape") setOpen(false);
      }}
    >
      {trigger({ open, toggle: () => setOpen((o) => !o) })}
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} aria-hidden />
          <div
            className={cx(
              "absolute z-50 min-w-60 rounded-lg border border-line bg-panel p-3 shadow-lg",
              side === "top" ? "bottom-full mb-1.5" : "top-full mt-1.5",
              align === "right" ? "right-0" : "left-0",
            )}
          >
            {children(() => setOpen(false))}
          </div>
        </>
      )}
    </div>
  );
}
