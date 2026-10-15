"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState } from "react";
import { LayerlingMark } from "../brand/LayerlingMark";

/** Only same-site studio paths are allowed as the post-login destination. */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//")) return "/jobs";
  return /^\/(jobs|review|filaments|admin)(\/|\?|$)/.test(next) ? next : "/jobs";
}

export function AccessForm({
  fetchImpl = fetch,
  variant = "page",
}: {
  fetchImpl?: typeof fetch;
  /** "inline" is the dashed studio-code box on the landing. */
  variant?: "page" | "inline";
}) {
  const search = useSearchParams();
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (!code.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetchImpl("/api/access", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ code }),
      });
      if (res.ok) {
        // Full navigation so the studio picks the access-code adapter.
        window.location.assign(safeNext(search?.get("next")));
        return;
      }
      const body = (await res.json().catch(() => ({}))) as { retry_after_s?: number; error?: string };
      setError(
        res.status === 429
          ? `Too many attempts. Try again in ${Math.ceil((body.retry_after_s ?? 900) / 60)} min`
          : res.status === 401
            ? "Wrong code"
            : (body.error ?? "Unavailable"),
      );
      setCode("");
    } catch {
      setError("Unavailable");
    } finally {
      setBusy(false);
    }
  };

  const fields = (
    <>
      <label htmlFor={`code-${variant}`} className="text-[15px] font-semibold">
        {variant === "inline" ? "Have a studio code?" : "Access code"}
      </label>
      <div className="flex gap-2.5">
        <input
          id={`code-${variant}`}
          type="password"
          autoComplete="off"
          placeholder="Code"
          autoFocus={variant === "page"}
          value={code}
          onChange={(e) => setCode(e.target.value)}
          className="h-12 min-w-0 flex-1 rounded-md border border-line-strong bg-panel px-4 text-base focus:border-ink focus:outline-none"
        />
        <button
          type="submit"
          disabled={busy || !code.trim()}
          className="h-12 rounded-md bg-ink px-5 text-base font-semibold text-bg disabled:opacity-50"
        >
          {busy ? "Checking" : "Open studio"}
        </button>
      </div>
      {error && (
        <p role="alert" className="text-sm font-semibold text-bad">
          {error}
        </p>
      )}
    </>
  );

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    void submit();
  };

  if (variant === "inline") {
    return (
      <form onSubmit={onSubmit} className="flex flex-col gap-3 rounded-[10px] border border-dashed border-line-strong p-[22px]">
        {fields}
      </form>
    );
  }

  return (
    <form onSubmit={onSubmit} className="mx-auto mt-[12vh] flex w-full max-w-sm flex-col gap-3 px-6">
      <LayerlingMark size={56} />
      <h1 className="font-display text-[40px] font-bold leading-none tracking-[-0.02em]">Studio</h1>
      {fields}
      <Link href="/login" className="pt-3 text-sm text-muted underline">
        Owner sign in
      </Link>
    </form>
  );
}
