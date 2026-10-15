import type { Metadata } from "next";
import { Suspense } from "react";
import { AccessForm } from "@/components/site/AccessForm";

export const metadata: Metadata = { title: "Studio", robots: { index: false } };

export default function AccessPage() {
  return (
    <Suspense>
      <AccessForm />
    </Suspense>
  );
}
