// What we make: the five products standing on one shelf at one px-per-mm (M sizes), bottom-aligned,
// a 1 mm ruler under the shelf, then name, size range, one line and "from $X". Items link to the
// order form with that product picked. The scale comes from the container width (app/site.css
// .lineup), so it is set before paint and never shifts; on phones the shelf scrolls sideways.

import { LINEUP_PRODUCTS, PRODUCT_LINES, PUBLIC_LABELS, sizeRange } from "@/lib/products";
import { priceFromLabel } from "@/lib/pricing";
import { FrontImage } from "./FrontImage";
import { ProductLink } from "./ProductLink";
import { Ruler } from "./Ruler";

export function ProductLineup() {
  return (
    <div className="lineup -mx-4 px-4 md:mx-0 md:px-0">
      <div className="lineup-track">
        <div className="lineup-row items-end">
          {LINEUP_PRODUCTS.map((p, i) => (
            <ProductLink
              key={p}
              product={p}
              label={`${PUBLIC_LABELS[p]}: start an order`}
              className="lineup-item flex items-end justify-start no-underline"
            >
              <FrontImage product={p} tier="m" ppm="var(--ppm)" maxPpm={3.4} align="start" print={{ step: i }} />
            </ProductLink>
          ))}
        </div>
        <div className="border-t-[1.5px] border-ink" />
        <Ruler ppm="var(--ppm)" lengthMm={480} />
        <div className="lineup-row mt-3">
          {LINEUP_PRODUCTS.map((p) => {
            const price = priceFromLabel(p);
            return (
              <div key={p} className="lineup-item flex flex-col gap-1.5 pr-3">
                <ProductLink product={p} className="font-display text-display-s text-ink no-underline hover:text-brick">
                  {PUBLIC_LABELS[p]}
                </ProductLink>
                <span className="measure text-measure text-ink-2">{sizeRange(p)}</span>
                <span className="text-small text-ink-2">{PRODUCT_LINES[p]}</span>
                {price && (
                  <span className="text-body-strong num" data-testid={`price-${p}`}>
                    {price}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
