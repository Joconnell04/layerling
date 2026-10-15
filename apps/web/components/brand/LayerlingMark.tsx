// layerling mark: a floppy-eared head built from stacked 4 px layer lines, clipped to the head
// shape, with paper-colored eyes and nose and a brick print-bed bar underneath. Paths from the
// design canvas (project/Logo.dc.html).

import { useId } from "react";

type Variant = "ink" | "reversed" | "app" | "favicon";

const COLORS: Record<Variant, { lines: string; face: string; bed: string | null }> = {
  ink: { lines: "var(--color-ink, #1C1A17)", face: "var(--color-bg, #F3EEE4)", bed: "var(--color-accent, #B83C24)" },
  reversed: { lines: "#F3EEE4", face: "#1C1A17", bed: "#E0664A" },
  app: { lines: "#FFF8EE", face: "#B83C24", bed: null },
  favicon: { lines: "#FFF8EE", face: "#B83C24", bed: null },
};

export function HeadClip({ id }: { id: string }) {
  return (
    <clipPath id={id}>
      <circle cx="60" cy="54" r="31" />
      <ellipse cx="28" cy="62" rx="12" ry="25" transform="rotate(14 28 62)" />
      <ellipse cx="92" cy="62" rx="12" ry="25" transform="rotate(-14 92 62)" />
      <ellipse cx="60" cy="76" rx="21" ry="17" />
    </clipPath>
  );
}

/** The mark alone (120 x 120 viewBox). */
export function LayerlingMark({
  size = 40,
  variant = "ink",
  title,
  className,
}: {
  size?: number;
  variant?: Variant;
  title?: string;
  className?: string;
}) {
  const id = `ll-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const c = COLORS[variant];
  const lines =
    variant === "favicon"
      ? [<rect key="solid" x="0" y="20" width="120" height="80" />]
      : variant === "app"
        ? Array.from({ length: 10 }, (_, i) => <rect key={i} x="0" y={20 + i * 8} width="120" height="5" />)
        : Array.from({ length: 13 }, (_, i) => <rect key={i} x="0" y={20 + i * 6} width="120" height="4" />);
  const big = variant === "app" || variant === "favicon";
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 120 120"
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      className={className}
    >
      <defs>
        <HeadClip id={id} />
      </defs>
      <g clipPath={`url(#${id})`} fill={c.lines}>
        {lines}
      </g>
      <circle cx="48" cy="53" r={variant === "favicon" ? 6 : big ? 5 : 4.5} fill={c.face} />
      <circle cx="72" cy="53" r={variant === "favicon" ? 6 : big ? 5 : 4.5} fill={c.face} />
      <ellipse cx="60" cy="71" rx={variant === "favicon" ? 9 : big ? 8 : 7.5} ry={variant === "favicon" ? 7 : big ? 6 : 5.5} fill={c.face} />
      {c.bed && <rect x="34" y="104" width="52" height="5" rx="2.5" fill={c.bed} />}
    </svg>
  );
}

/** Mark plus the Fraunces 700 wordmark. */
export function LayerlingLogo({ size = 36, className }: { size?: number; className?: string }) {
  return (
    <span className={`inline-flex items-center gap-2 ${className ?? ""}`}>
      <LayerlingMark size={size} />
      <span className="font-wordmark text-ink" style={{ fontSize: size * 0.78, lineHeight: 1 }}>
        layerling
      </span>
    </span>
  );
}

export const BRAND = "layerling";
