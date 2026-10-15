// "@pricing" is a bundler alias (next.config.ts, vitest.config.ts): lib/pricing.generated.json when
// scripts/copy-pricing.mjs has run, else the checked-in lib/pricing.placeholder.json.
// lib/pricing.ts reads it as its own Pricing type.
declare module "@pricing" {
  const data: {
    missing?: boolean;
    updated_at?: string;
    currency?: string;
    products?: Record<string, Record<string, unknown>>;
    other?: Record<string, unknown>;
  };
  export default data;
}
