import type { Metadata } from "next";
import { OrderSection } from "@/components/site/OrderSection";
import type { ProductType } from "@/lib/contracts.gen";
import { DEFAULT_SIZE_TIER, ORDER_PRODUCTS, isSizeTier } from "@/lib/products";

export const metadata: Metadata = { title: "Order" };

export default async function OrderPage({ searchParams }: { searchParams: Promise<{ product?: string; size?: string }> }) {
  const { product, size } = await searchParams;
  const initialProduct = (ORDER_PRODUCTS as readonly string[]).includes(product ?? "") ? (product as ProductType) : undefined;
  return (
    <OrderSection
      heading="h1"
      initialProduct={initialProduct}
      initialTier={isSizeTier(size) ? size : DEFAULT_SIZE_TIER}
      className="pb-16 pt-7 md:pb-24 md:pt-16"
    />
  );
}
