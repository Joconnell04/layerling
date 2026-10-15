// The landing page with a pricing.generated.json in the cost model's current shape
// (`from: true` is a flag, the number is `price`). Production showed "[PRICE]" 10 times because
// the cards read `from` as the price. `npm run build` also checks the prerendered HTML
// (scripts/check-landing-price.mjs) against the real file.
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { renderToStaticMarkup } from "react-dom/server";
import Landing from "@/app/(site)/page";
import { PRICE_PLACEHOLDER, priceFrom, type Pricing } from "@/lib/pricing";

vi.mock("@pricing", async () => ({ default: (await import("@/test/fixtures/pricing.generated.json")).default }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));
vi.mock("next/image", () => ({
  // eslint-disable-next-line @next/next/no-img-element
  default: (p: { alt: string; src: string | { src: string } }) => <img alt={p.alt} src={typeof p.src === "string" ? p.src : p.src.src} />,
}));

describe("landing prices", () => {
  it("renders the cost model's prices, never [PRICE]", () => {
    const html = renderToStaticMarkup(<Landing />);
    expect(html).toContain("from $31.99");
    expect(html).toContain("from $57.99");
    expect(html).not.toContain(PRICE_PLACEHOLDER);
  });

  it("prices every product in the repo's current costs/pricing.json", () => {
    const file = join(__dirname, "..", "..", "..", "..", "costs", "pricing.json");
    if (!existsSync(file)) return;
    const pricing = { missing: false, ...JSON.parse(readFileSync(file, "utf8")) } as Pricing;
    for (const product of Object.keys(pricing.products ?? {})) {
      expect(priceFrom(product as never, pricing)).toMatch(/^from \$\d/);
    }
  });
});
