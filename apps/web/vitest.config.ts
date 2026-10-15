import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./", import.meta.url)),
      // Tests never depend on the cost model: always the checked-in placeholder ([PRICE]).
      "@pricing": fileURLToPath(new URL("./lib/pricing.placeholder.json", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./test/setup.ts"],
    // Room for several 5 s waits (test/setup.ts) when the machine is busy.
    testTimeout: 20_000,
    include: ["**/*.test.ts", "**/*.test.tsx"],
    exclude: ["node_modules", ".next"],
  },
});
