import { existsSync } from "node:fs";
import { join } from "node:path";
import type { NextConfig } from "next";

// Prices: lib/pricing.generated.json is written by scripts/copy-pricing.mjs (predev, prebuild).
// If next is started without the npm scripts, fall back to the checked-in placeholder ([PRICE]).
const generated = join(__dirname, "lib", "pricing.generated.json");
const pricing = existsSync(generated) ? "./lib/pricing.generated.json" : "./lib/pricing.placeholder.json";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  turbopack: { root: __dirname, resolveAlias: { "@pricing": pricing } },
  webpack: (config) => {
    config.resolve.alias = { ...config.resolve.alias, "@pricing": join(__dirname, pricing) };
    return config;
  },
  // next dev otherwise writes AGENTS.md and CLAUDE.md into apps/web.
  agentRules: false,
  // No floating dev badge in screenshots.
  devIndicators: false,
};

export default nextConfig;
