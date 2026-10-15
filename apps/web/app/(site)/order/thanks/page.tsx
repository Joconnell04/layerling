// Order confirmation (V1Confirmation). /order/thanks?r=<receipt> verifies the signed receipt,
// loads the order with the service store and shows the personal version; old ?ref=<8 hex> links
// and invalid or expired receipts show the generic version (reference and steps, no personal data).

import type { Metadata } from "next";
import Image from "next/image";
import Link from "next/link";
import { FirstLook, type FirstLookView } from "@/components/site/FirstLook";
import { NextSteps } from "@/components/site/NextSteps";
import { SummaryLine } from "@/components/site/OrderSummary";
import { SHEET, btn, cx } from "@/components/site/ui";
import type { OrderRow } from "@/lib/db.types";
import { quote as quoteFor, type PriceQuote } from "@/lib/pricing";
import { DEFAULT_SIZE_TIER, isStandard } from "@/lib/products";
import { serverEnv } from "@/lib/server/env";
import { firstLookOnLoad } from "@/lib/server/firstLook";
import { maskEmail, maskPhone, shortRef, verifyReceipt } from "@/lib/server/receipt";
import { getStore } from "@/lib/server/store";

export const metadata: Metadata = { title: "Order received", robots: { index: false } };
export const dynamic = "force-dynamic";

interface Loaded {
  order: OrderRow;
  quote: PriceQuote;
  photoUrls: string[];
  firstLookUrl: string | null;
}

async function load(receipt: string | undefined): Promise<Loaded | null> {
  const r = await verifyReceipt(receipt);
  if (!r) return null;
  try {
    const store = getStore();
    const order = await store.getOrder(r.id);
    if (!order) return null;
    const photoUrls = await Promise.all(order.photos.slice(0, 4).map((k) => store.signedUrl("orders", k, 3600).catch(() => "")));
    const firstLookUrl = await firstLookOnLoad(store, order.id).catch(() => null);
    return { order, quote: r.quote ?? quoteFor(order.product_type, order.size_tier ?? DEFAULT_SIZE_TIER), photoUrls, firstLookUrl };
  } catch {
    return null;
  }
}

const VIEWS: readonly FirstLookView[] = ["idle", "printing", "done", "capped", "failed"];

export default async function ThanksPage({
  searchParams,
}: {
  searchParams: Promise<{ r?: string; ref?: string; view?: string }>;
}) {
  const { r, ref, view } = await searchParams;
  const env = serverEnv();
  const loaded = await load(r);
  const safeRef = loaded ? shortRef(loaded.order.id) : ref && /^[0-9a-f]{1,8}$/i.test(ref) ? ref.toLowerCase() : null;
  const firstName = loaded?.order.name.split(" ")[0];
  const reply = loaded
    ? loaded.order.email
      ? `We'll reply at ${maskEmail(loaded.order.email)}`
      : loaded.order.phone
        ? `We'll reply by text to ${maskPhone(loaded.order.phone)}`
        : null
    : null;
  const product = loaded?.order.product_type;
  const tier = loaded?.order.size_tier ?? DEFAULT_SIZE_TIER;
  // Mock mode only: ?view= pins a first look state for screenshots.
  const pinned = env.mock && VIEWS.includes(view as FirstLookView) ? (view as FirstLookView) : undefined;
  const showFirstLook = !!loaded && !!r && (env.firstLookEnabled || !!loaded.firstLookUrl || !!pinned);

  return (
    <div className="mx-auto flex max-w-[1120px] flex-col gap-10 px-4 pb-20 pt-10 md:px-10 md:pb-28 md:pt-16">
      <header className="flex flex-col gap-3">
        <h1 className="font-display m-0 text-display-m-phone md:text-display-m" data-testid="thanks-title">
          {firstName ? `Got it, ${firstName}.` : "Got it."}
        </h1>
        {(safeRef || reply) && (
          <p className="m-0 text-body text-ink-2">
            {safeRef && (
              <>
                Order <span className="measure text-ink">{safeRef}</span>
              </>
            )}
            {safeRef && reply && " · "}
            {reply}
          </p>
        )}
      </header>

      {loaded && product && (
        <div className={cx("grid grid-cols-1 gap-6", showFirstLook && "md:grid-cols-[minmax(0,1fr)_360px]")}>
          <section aria-label="Your order" className={cx(SHEET, "flex flex-col gap-5 self-start p-5 sm:p-7")}>
            {loaded.photoUrls.some(Boolean) && (
              <ul className="m-0 flex list-none flex-wrap gap-3 p-0" aria-label="Your photos">
                {loaded.photoUrls.filter(Boolean).map((u, i) => (
                  <li key={i} className="relative size-20 overflow-hidden rounded-md bg-paper shadow-elev-photo">
                    <Image src={u} alt={`Photo ${i + 1}`} fill sizes="80px" unoptimized className="object-cover" />
                  </li>
                ))}
              </ul>
            )}
            <SummaryLine product={product} tier={tier} quote={loaded.quote} />
            {loaded.order.request && <p className="m-0 whitespace-pre-wrap text-body text-ink-2">{loaded.order.request}</p>}
          </section>
          {showFirstLook && (
            <FirstLook
              receipt={r!}
              product={isStandard(product) ? product : null}
              initialUrl={pinned === "done" ? "/samples/beagle-concept.png" : loaded.firstLookUrl}
              initialView={pinned}
            />
          )}
        </div>
      )}

      <section aria-labelledby="next-title" className="flex flex-col gap-6">
        <h2 id="next-title" className="font-display m-0 text-[28px] leading-[1.1]">
          What happens next
        </h2>
        <NextSteps layout="row" current={0} />
      </section>

      <div className="flex flex-wrap items-center gap-4 border-t border-line pt-6">
        <p className="m-0 text-body text-ink-2">Add details later: reply to our email or text.</p>
        <Link href="/" className={btn("quiet", "md", "ml-auto")}>
          Home
        </Link>
      </div>
    </div>
  );
}
