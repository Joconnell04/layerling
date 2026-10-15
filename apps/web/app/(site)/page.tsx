import Image from "next/image";
import Link from "next/link";
import { OrderSection } from "@/components/site/OrderSection";
import { PrintIn } from "@/components/site/PrintIn";
import { ProductLineup } from "@/components/site/ProductLineup";
import { FilamentDots, btn } from "@/components/site/ui";

const STEPS = [
  {
    n: 1,
    title: "Send photos",
    line: "One to four clear photos. Say what you want, or just pick a product.",
  },
  {
    n: 2,
    title: "Approve a preview",
    line: "A person checks your photos, then you get a 3D preview in the real filament colors. Ask for changes before anything prints.",
  },
  {
    n: 3,
    title: "We print it",
    line: "In color, no paint. Ready in [TURNAROUND]. Pickup in New York or shipping: [DELIVERY].",
  },
];

export default function Landing() {
  return (
    <>
      <section className="layer-lines">
        <div className="mx-auto grid max-w-[1440px] items-center gap-10 px-4 pb-16 pt-10 md:px-[72px] md:pb-20 md:pt-16 lg:grid-cols-[minmax(0,600px)_minmax(0,1fr)] lg:gap-0">
          <div className="flex flex-col gap-6 md:gap-7">
            <h1 className="font-display m-0 text-balance text-display-xl-phone md:text-display-xl">Your pet, printed in color.</h1>
            <p className="m-0 max-w-[480px] text-lede-phone text-ink-2 md:text-lede">
              Send a photo. We print it in up to five colors as a keychain, nameplate, figure, ornament or magnet.
            </p>
            <div className="flex flex-wrap items-center gap-7">
              <Link href="#order" className={btn("primary", "lg")}>
                Start an order
              </Link>
              <Link href="/sizes" className={btn("quiet", "md", "text-lg")}>
                See sizes
              </Link>
            </div>
            <div className="flex flex-wrap items-center gap-x-2 gap-y-2 pt-2">
              <FilamentDots size={16} />
              <span className="pl-1 text-[15px] leading-normal text-ink-2">
                Up to 5 colors · Checked by a person before it prints · Made in New York
              </span>
            </div>
          </div>
          <figure className="m-0 flex flex-col items-end gap-2 lg:-mr-10">
            <PrintIn mode="load" className="aspect-[2077/1128] w-full max-w-[800px]">
              <Image
                src="/products/group.png"
                alt="The same beagle as a figure, nameplate, ornament, magnet and keychain"
                width={2077}
                height={1128}
                priority
                sizes="(min-width: 1024px) 800px, 100vw"
                className="block h-full w-full object-contain"
              />
            </PrintIn>
            <figcaption className="measure text-measure text-ink-3 lg:pr-10">One beagle, five ways, at true relative scale.</figcaption>
          </figure>
        </div>
      </section>

      <section id="make" aria-labelledby="make-title" className="scroll-mt-4">
        <div className="mx-auto flex max-w-[1440px] flex-col gap-10 px-4 pb-16 pt-16 md:gap-12 md:px-[72px] md:pb-[104px] md:pt-24">
          <div className="flex flex-wrap items-end justify-between gap-x-10 gap-y-4">
            <div className="flex flex-col gap-3.5">
              <h2 id="make-title" data-reveal="" className="font-display m-0 text-display-l-phone md:text-display-l">
                What we make
              </h2>
              <p data-reveal="" className="m-0 max-w-[660px] text-lede-phone text-ink-2 md:text-lede">
                Every print starts from your photos. Pick a product, then a size.
              </p>
            </div>
            <div className="flex items-center gap-2.5 pb-1.5">
              <span className="measure text-measure text-ink-3">Shown at M, to scale</span>
              <Link href="/sizes" className={btn("quiet")}>
                Compare sizes
              </Link>
            </div>
          </div>
          <ProductLineup />
        </div>
      </section>

      <section id="how" aria-labelledby="how-title" className="drift-scope scroll-mt-4 bg-tint">
        <div className="mx-auto flex max-w-[1440px] flex-col gap-10 px-4 py-16 md:gap-14 md:px-[72px] md:py-24">
          <h2 id="how-title" data-reveal="" className="font-display m-0 text-display-l-phone md:text-display-l">
            How it works
          </h2>
          <ol className="m-0 grid list-none grid-cols-1 gap-12 p-0 md:grid-cols-3 md:gap-10">
            <li className="flex flex-col gap-7">
              <div className="flex h-[340px] items-center justify-center md:h-[380px]">
                <div className="drift" style={{ "--drift": "24px" } as React.CSSProperties}>
                  <figure className="m-0 w-[290px] -translate-y-3 -rotate-3 rounded-[6px] border border-line bg-sheet p-3 pb-4 shadow-elev-photo">
                    <Image
                      src="/samples/beagle-photo.jpg"
                      alt="Phone photo of a beagle puppy"
                      width={456}
                      height={480}
                      sizes="266px"
                      className="block h-[280px] w-full rounded-[3px] object-cover"
                    />
                  </figure>
                </div>
              </div>
              <Step {...STEPS[0]} />
            </li>
            <li className="flex flex-col gap-7">
              <div className="flex h-[340px] items-center justify-center md:h-[380px]">
                <div className="drift" style={{ "--drift": "12px" } as React.CSSProperties}>
                  <figure className="m-0 w-[290px] translate-y-1 rotate-2 rounded-[6px] border border-line bg-sheet p-3 pb-4">
                    <Image
                      src="/samples/beagle-concept.png"
                      alt="Concept image of the beagle as a figure"
                      width={560}
                      height={560}
                      sizes="266px"
                      className="block h-[280px] w-full rounded-[3px] bg-paper object-contain"
                    />
                    <figcaption className="pt-2.5 text-[13px] text-ink-2">Concept</figcaption>
                  </figure>
                </div>
              </div>
              <Step {...STEPS[1]} />
            </li>
            <li className="flex flex-col gap-7">
              <div className="flex h-[340px] items-center justify-center md:h-[380px]">
                <div className="drift" style={{ "--drift": "4px" } as React.CSSProperties}>
                  <PrintIn className="size-[340px] translate-y-2.5 md:size-[380px]">
                    <Image
                      src="/products/figurine.png"
                      alt="The printed figure in brown, white and black"
                      width={1200}
                      height={1200}
                      sizes="380px"
                      className="block h-full w-full object-contain"
                    />
                  </PrintIn>
                </div>
              </div>
              <Step {...STEPS[2]} />
            </li>
          </ol>
        </div>
      </section>

      <OrderSection className="py-16 md:py-[104px]" />
    </>
  );
}

function Step({ n, title, line }: { n: number; title: string; line: string }) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-baseline gap-3">
        <span className="measure text-measure text-ink-2">{n}</span>
        <span className="text-[20px] font-semibold leading-normal">{title}</span>
      </div>
      <p className="m-0 max-w-[380px] text-body text-ink-2">{line}</p>
    </div>
  );
}
