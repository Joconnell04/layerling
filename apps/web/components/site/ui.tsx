// Public site primitives from design system v1 (PKG components/bundle.css, ported to Tailwind
// utilities on the token names). Buttons are pills with a verb; fields are a label above an input.

import type { ReactNode } from "react";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

export type BtnVariant = "primary" | "secondary" | "quiet";

const BTN_BASE =
  "inline-flex select-none items-center justify-center gap-2 whitespace-nowrap font-semibold leading-none no-underline transition-[background-color,color,border-color,transform] duration-[var(--dur-fast)] ease-[var(--ease-out)] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 disabled:active:scale-100 aria-disabled:cursor-not-allowed aria-disabled:opacity-50";

/** Button classes: primary (brick, the one action), secondary (ink outline), quiet (underlined). */
export function btn(variant: BtnVariant = "primary", size: "md" | "lg" = "md", extra?: string): string {
  if (variant === "quiet") {
    return cx(
      BTN_BASE,
      "min-h-11 rounded-none border-b-[1.5px] border-ink px-0 py-1 text-base text-ink hover:text-ink hover:border-brick",
      extra,
    );
  }
  return cx(
    BTN_BASE,
    "rounded-pill border",
    size === "lg" ? "h-14 px-[30px] text-lg" : "h-12 px-[22px] text-base",
    variant === "primary" && "border-transparent bg-brick text-on-brick hover:bg-brick-press hover:text-on-brick",
    variant === "secondary" && "border-ink bg-transparent text-ink hover:bg-hover hover:text-ink",
    extra,
  );
}

/** The paper sheet the order form, the summary and dialogs sit on. */
export const SHEET = "rounded-lg border border-line bg-sheet shadow-elev-sheet";

export const LABEL = "text-label text-ink";
export const TAG = "text-caption text-ink-3";
export const HELP = "text-small text-ink-3";
export const ERROR = "text-label text-danger";

export function inputCls(invalid?: boolean, extra?: string): string {
  return cx(
    "w-full rounded-md border bg-field text-base leading-6 text-ink transition-colors duration-[var(--dur-fast)] focus:border-ink focus:outline-none focus-visible:shadow-focus-ring focus-visible:outline-none",
    invalid ? "border-danger" : "border-line-strong",
    extra,
  );
}

/** Label above a field, an Optional tag on the right, helper or error below. */
export function Field({
  id,
  label,
  optional,
  help,
  helpId,
  error,
  errorId,
  aside,
  children,
  className,
}: {
  id?: string;
  label: ReactNode;
  optional?: boolean;
  help?: ReactNode;
  helpId?: string;
  error?: string | null;
  errorId?: string;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cx("flex min-w-0 flex-col gap-2", className)}>
      <div className="flex items-baseline justify-between gap-2">
        {id ? (
          <label htmlFor={id} className={LABEL}>
            {label}
          </label>
        ) : (
          <span className={LABEL}>{label}</span>
        )}
        {optional && <span className={TAG}>Optional</span>}
        {aside}
      </div>
      {children}
      {error ? (
        <p id={errorId} className={ERROR}>
          {error}
        </p>
      ) : help ? (
        <p id={helpId} className={HELP}>
          {help}
        </p>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------ icons (1.75 px stroke, 24 grid)

const ICON = {
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.75,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  viewBox: "0 0 24 24",
  "aria-hidden": true,
} as const;

export function UploadIcon({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} {...ICON}>
      <path d="M12 16V4" />
      <path d="M6 10l6-6 6 6" />
      <path d="M4 20h16" />
    </svg>
  );
}

export function CloseIcon({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} {...ICON}>
      <path d="M6 6l12 12M18 6L6 18" />
    </svg>
  );
}

export function LockIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} {...ICON}>
      <rect x="4" y="11" width="16" height="10" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" />
    </svg>
  );
}

export function PlusIcon({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} {...ICON}>
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

/** Other: an ink outline of a filament spool, since there is nothing to render yet. */
export function SpoolIcon({ size = 64 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" fill="none" stroke="currentColor" strokeWidth={1.75} strokeLinecap="round" aria-hidden>
      <ellipse cx="32" cy="32" rx="26" ry="26" />
      <circle cx="32" cy="32" r="7" />
      <path d="M32 6v7M32 51v7M6 32h7M51 32h7" />
      <path d="M18 18c3-3 8-5 14-5M46 46c-3 3-8 5-14 5" strokeDasharray="2 4" />
    </svg>
  );
}

/** The five loaded filaments as dots (FROM JACKSON: black, brown, red, blue, white). */
export function FilamentDots({ size = 18 }: { size?: number }) {
  return (
    <span className="inline-flex items-center gap-1.5" aria-hidden>
      {["bg-fil-black", "bg-fil-brown", "bg-fil-red", "bg-fil-blue", "bg-fil-white"].map((c) => (
        <span key={c} className={cx("inline-block rounded-full border border-ink", c)} style={{ width: size, height: size }} />
      ))}
    </span>
  );
}
