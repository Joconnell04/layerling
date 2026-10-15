import { preload } from "react-dom";
import { MotionObserver } from "@/components/site/MotionObserver";
import { SiteNav } from "@/components/site/SiteNav";

// The two latin faces every public page uses (app/site.css), fetched before first paint so text
// never swaps after it and nothing shifts.
const FONTS = ["/fonts/fraunces-latin-full-normal.woff2", "/fonts/instrument-sans-latin-wght-normal.woff2"];

export default function SiteLayout({ children }: { children: React.ReactNode }) {
  for (const href of FONTS) preload(href, { as: "font", type: "font/woff2", crossOrigin: "anonymous" });
  return (
    <div className="flex min-h-dvh flex-col text-body text-ink">
      <SiteNav />
      <main className="flex-1">{children}</main>
      <footer className="border-t border-line">
        <div className="mx-auto flex max-w-[1440px] flex-wrap justify-between gap-x-6 gap-y-2 px-4 py-7 text-small text-ink-2 md:px-[72px]">
          <span>layerling · printed in New York</span>
          <span>
            Sample photo: &ldquo;Beagle puppy portrait&rdquo; by Tim Lloyd,{" "}
            <a href="https://creativecommons.org/licenses/by/2.0/" className="underline">
              CC BY 2.0
            </a>
          </span>
        </div>
      </footer>
      <MotionObserver />
    </div>
  );
}
