export function usd(n: number | null | undefined, digits = 2): string {
  return `$${(n ?? 0).toFixed(digits)}`;
}

export function credits(n: number | null | undefined): string {
  return `${Math.round(n ?? 0)} cr`;
}

export function duration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

export function ago(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "";
  const s = Math.max(0, (now - Date.parse(iso)) / 1000);
  if (s < 45) return "now";
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

export function grams(n: number | null | undefined): string {
  return `${(n ?? 0).toFixed(1)} g`;
}

export function pct(n: number): string {
  return `${Math.round(n * 100)}%`;
}

export function titleCase(s: string): string {
  const t = s.replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
}
