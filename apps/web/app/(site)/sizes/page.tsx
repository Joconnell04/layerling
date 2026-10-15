import type { Metadata } from "next";
import { SizesExplorer } from "@/components/site/SizesExplorer";
import { SIZES_PRODUCTS, type StandardProduct } from "@/lib/products";

export const metadata: Metadata = { title: "Sizes" };

export default async function SizesPage({ searchParams }: { searchParams: Promise<{ product?: string }> }) {
  const { product } = await searchParams;
  const initial = (SIZES_PRODUCTS as readonly string[]).includes(product ?? "") ? (product as StandardProduct) : "keychain";
  return (
    <div className="mx-auto flex max-w-[1440px] flex-col gap-8 px-4 pb-20 pt-10 md:px-[72px] md:pb-28 md:pt-16">
      <header className="flex max-w-[720px] flex-col gap-3">
        <h1 data-reveal="" className="font-display m-0 text-display-l-phone md:text-display-l">Sizes</h1>
        <p data-reveal="" className="m-0 text-lede-phone text-ink-2 md:text-lede">
          Every product comes in four sizes. Here they are next to things you know.
        </p>
      </header>
      <SizesExplorer initialProduct={initial} />
    </div>
  );
}
