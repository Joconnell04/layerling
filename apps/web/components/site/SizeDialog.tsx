"use client";

// "Compare sizes" from the order form: the SizeVisualizer for one product in a native dialog
// (sheet, elev-float). Escape, the close button and a scrim click close it; focus returns to the
// control that opened it. Clicking a size selects it in the form. A bottom sheet on phones.

import Link from "next/link";
import { useEffect, useRef, useState, type RefObject } from "react";
import { PUBLIC_LABELS, tierMm, type SizeTier, type StandardProduct } from "@/lib/products";
import { DEFAULT_REF, REFS, REF_ORDER, type RefKind } from "./references";
import { SegmentedControl } from "./SegmentedControl";
import { SizeVisualizer } from "./SizeVisualizer";
import { CloseIcon, btn } from "./ui";

export function SizeDialog({
  open,
  product,
  tier,
  onPick,
  onClose,
  returnFocus,
}: {
  open: boolean;
  product: StandardProduct;
  tier: SizeTier;
  onPick: (t: SizeTier) => void;
  onClose: () => void;
  returnFocus?: RefObject<HTMLElement | null>;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [reference, setReference] = useState<RefKind>(DEFAULT_REF[product]);
  const [phone, setPhone] = useState(false);

  useEffect(() => setReference(DEFAULT_REF[product]), [product]);

  useEffect(() => {
    const mq = window.matchMedia?.("(max-width: 639px)");
    setPhone(!!mq?.matches);
    const on = () => setPhone(!!mq?.matches);
    mq?.addEventListener?.("change", on);
    return () => mq?.removeEventListener?.("change", on);
  }, []);

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      if (typeof d.showModal === "function") d.showModal();
      else d.setAttribute("open", "");
    } else if (!open && d.open) {
      if (typeof d.close === "function") d.close();
      else d.removeAttribute("open");
      returnFocus?.current?.focus();
    }
  }, [open, returnFocus]);

  const label = PUBLIC_LABELS[product];
  return (
    <dialog
      ref={ref}
      aria-labelledby="size-dialog-title"
      className="ll-dialog"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        // A click on the dialog element itself is a click on the scrim (the content box is inside).
        if (e.target === ref.current) onClose();
      }}
    >
      <div className="flex flex-col gap-5 p-5 sm:gap-[22px] sm:px-8 sm:py-7">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <h2 id="size-dialog-title" className="font-display m-0 text-[24px] leading-[1.1] sm:text-[28px]">
            {label} sizes
          </h2>
          <div className="flex items-center gap-3">
            <SegmentedControl
              label="Compare with"
              options={REF_ORDER.map((k) => ({ value: k, label: REFS[k].label }))}
              value={reference}
              onChange={setReference}
              className="max-sm:hidden"
            />
            <button type="button" aria-label="Close" onClick={onClose} className="-mr-2 flex size-11 items-center justify-center rounded-pill text-ink hover:bg-hover">
              <CloseIcon />
            </button>
          </div>
          <SegmentedControl
            label="Compare with"
            options={REF_ORDER.map((k) => ({ value: k, label: REFS[k].label }))}
            value={reference}
            onChange={setReference}
            className="sm:hidden"
          />
        </div>
        {open && (
          <SizeVisualizer
            product={product}
            reference={reference}
            heightPx={phone ? 200 : 300}
            expectedWidthPx={phone ? 326 : 936}
            highlight={tier}
            onPick={onPick}
          />
        )}
        <p className="measure m-0 text-[13px] text-ink-3">True relative scale</p>
        <div className="flex flex-wrap items-center justify-between gap-4 border-t border-line pt-5">
          <Link href={`/sizes?product=${product}`} className={btn("quiet")}>
            Open the sizes page
          </Link>
          <button type="button" onClick={onClose} className={btn("primary", "md", "max-sm:w-full")}>
            Use {tier.toUpperCase()}, {tierMm(product, tier)} mm
          </button>
        </div>
      </div>
    </dialog>
  );
}
