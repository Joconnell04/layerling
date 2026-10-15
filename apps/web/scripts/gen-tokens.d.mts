// Types for scripts/gen-tokens.mjs (imported by lib/tokens.test.ts).
export interface DesignTokens {
  color: { tokens: { name: string; value: string }[] };
  radius: { tokens: { name: string; value: string }[] };
  [group: string]: unknown;
}
export const TOKENS_JSON: string;
export const TOKENS_CSS: string;
export function renderTokens(tokens: DesignTokens): string;
export function readTokens(path?: string): DesignTokens;
