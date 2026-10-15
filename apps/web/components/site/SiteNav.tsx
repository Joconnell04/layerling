"use client";

// Public site header: logo left; What we make, Sizes, How it works, Studio and a secondary
// "Start an order" pill right. Under 768 px the links fold into a Menu button.

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useId, useState } from "react";
import { LayerlingMark } from "../brand/LayerlingMark";
import { CloseIcon, LockIcon, btn, cx } from "./ui";

const LINKS = [
  { href: "/#make", label: "What we make" },
  { href: "/sizes", label: "Sizes" },
  { href: "/#how", label: "How it works" },
];

export function SiteNav() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const menuId = useId();

  useEffect(() => setOpen(false), [pathname]);

  const link = (l: { href: string; label: string }, extra?: string) => {
    const current = l.href === pathname;
    return (
      <Link
        key={l.href}
        href={l.href}
        aria-current={current ? "page" : undefined}
        className={cx("text-ink no-underline hover:text-brick", current && "font-semibold", extra)}
        onClick={() => setOpen(false)}
      >
        {l.label}
      </Link>
    );
  };

  return (
    <header className="border-b border-line">
      <nav
        aria-label="Main"
        className="mx-auto flex h-16 max-w-[1440px] items-center justify-between gap-8 px-4 md:h-[88px] md:px-[72px]"
      >
        <Link href="/" className="flex items-center gap-2 text-ink no-underline hover:text-ink" aria-label="layerling home">
          <LayerlingMark size={36} className="size-[30px] md:size-9" />
          <span className="font-wordmark text-[24px] leading-none tracking-[-0.5px] md:text-[29px]">layerling</span>
        </Link>
        <div className="hidden items-center gap-8 text-body md:flex">
          {LINKS.map((l) => link(l))}
          <Link href="/access" className="flex items-center gap-2 text-ink no-underline hover:text-brick">
            <LockIcon />
            Studio
          </Link>
          <Link href="/#order" className={btn("secondary", "md", "h-11 px-[22px]")}>
            Start an order
          </Link>
        </div>
        <button
          type="button"
          className="-mr-2.5 flex size-11 items-center justify-center rounded-pill text-ink hover:bg-hover md:hidden"
          aria-label={open ? "Close menu" : "Menu"}
          aria-expanded={open}
          aria-controls={menuId}
          onClick={() => setOpen((o) => !o)}
        >
          {open ? (
            <CloseIcon size={22} />
          ) : (
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" aria-hidden>
              <path d="M4 7h16M4 12h16M4 17h16" />
            </svg>
          )}
        </button>
      </nav>
      <div id={menuId} hidden={!open} className="border-t border-line px-4 pb-6 pt-2 md:hidden">
        <div className="flex flex-col text-lg">
          {LINKS.map((l) => link(l, "flex min-h-12 items-center border-b border-line"))}
          <Link href="/access" className="flex min-h-12 items-center gap-2 border-b border-line text-ink no-underline">
            <LockIcon />
            Studio
          </Link>
        </div>
        <Link href="/#order" className={btn("primary", "md", "mt-5 w-full")} onClick={() => setOpen(false)}>
          Start an order
        </Link>
      </div>
    </header>
  );
}
