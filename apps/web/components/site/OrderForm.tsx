"use client";

// The order form (design system v1, V1Order and V1OrderOther on the canvas). One sheet: Product,
// Size (hidden for Other), Photos, What you want, Your name, Contact, then the order summary with
// the only price in the flow. Photos go straight to Storage through signed URLs (start, PUT each,
// finish), so no photo passes through a function body.

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import type { ProductType } from "@/lib/contracts.gen";
import { quote } from "@/lib/pricing";
import {
  DEFAULT_SIZE_TIER,
  ORDER_PRODUCTS,
  TYPING_HINTS,
  isSizeTier,
  isStandard,
  tierMm,
  type SizeTier,
} from "@/lib/products";
import { photoMeta, putFile, type UploadTarget } from "@/lib/upload";
import { OrderSummary } from "./OrderSummary";
import { PhotoDrop, MAX_PHOTOS, type Picked } from "./PhotoDrop";
import { PICK_EVENT, type PickDetail } from "./pick";
import { ProductPicker } from "./ProductPicker";
import { SizeDialog } from "./SizeDialog";
import { SizePicker } from "./SizePicker";
import { TypingHint } from "./TypingHint";
import { ERROR, HELP, LABEL, SHEET, TAG, btn, cx, inputCls } from "./ui";

const MAX_BYTES = 15 * 1024 * 1024;
const TYPES = ["image/jpeg", "image/png", "image/heic", "image/webp"];
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const PHONE = /^[0-9+()\-. ]{5,40}$/;
export const REQUEST_MIN_OTHER = 10;

export const COPY = {
  photos: "Add at least one photo",
  name: "Add your name",
  contact: "Add an email or a phone number",
  email: "Check the email address",
  phone: "Check the phone number",
  otherRequest: `Tell us a bit more, at least ${REQUEST_MIN_OTHER} characters`,
} as const;

type Errors = Partial<Record<string, string>>;

export function acceptPhoto(f: File): string | null {
  const heic = /\.(heic|heif)$/i.test(f.name);
  if (!TYPES.includes(f.type) && !(heic && !f.type)) return "JPEG, PNG, HEIC or WebP";
  if (f.size > MAX_BYTES) return "Under 15 MB each";
  return null;
}

function readParams(): { product?: ProductType; tier?: SizeTier; src?: string } {
  if (typeof window === "undefined") return {};
  const q = new URLSearchParams(window.location.search);
  const p = q.get("product") as ProductType | null;
  const t = q.get("size");
  return {
    product: p && (ORDER_PRODUCTS as readonly string[]).includes(p) ? p : undefined,
    tier: isSizeTier(t) ? t : undefined,
    src: q.get("src") ?? undefined,
  };
}

export function OrderForm({
  fetchImpl = fetch,
  initialProduct = "keychain",
  initialTier = DEFAULT_SIZE_TIER,
}: {
  fetchImpl?: typeof fetch;
  initialProduct?: ProductType;
  initialTier?: SizeTier;
}) {
  const router = useRouter();
  const [product, setProduct] = useState<ProductType>(initialProduct);
  const [tier, setTier] = useState<SizeTier>(initialTier);
  const [photos, setPhotos] = useState<Picked[]>([]);
  const [request, setRequest] = useState("");
  const [requestFocused, setRequestFocused] = useState(false);
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [errors, setErrors] = useState<Errors>({});
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [sizesOpen, setSizesOpen] = useState(false);
  const compareRef = useRef<HTMLElement | null>(null);
  const photosRef = useRef(photos);
  photosRef.current = photos;

  // ?product= and ?size= preselect (the landing is static, so read them after hydration), and
  // same-page links from the lineup pick without a reload.
  useEffect(() => {
    const p = readParams();
    if (p.product) setProduct(p.product);
    if (p.tier) setTier(p.tier);
    const onPick = (e: Event) => {
      const d = (e as CustomEvent<PickDetail>).detail;
      setProduct(d.product);
      setTier(d.tier ?? DEFAULT_SIZE_TIER);
    };
    window.addEventListener(PICK_EVENT, onPick);
    return () => window.removeEventListener(PICK_EVENT, onPick);
  }, []);

  useEffect(() => () => photosRef.current.forEach((p) => p.url && URL.revokeObjectURL(p.url)), []);

  const other = product === "other";
  const setError = (k: string, v?: string) => setErrors((e) => ({ ...e, [k]: v }));

  const add = (files: FileList | File[]) => {
    const next: Picked[] = [];
    let err: string | null = null;
    for (const f of [...files]) {
      const bad = acceptPhoto(f);
      if (bad) err = bad;
      else next.push({ file: f, url: f.type && f.type !== "image/heic" ? URL.createObjectURL(f) : null });
    }
    setPhotos((cur) => [...cur, ...next].slice(0, MAX_PHOTOS));
    setError("photos", err ?? undefined);
  };

  const validate = (name: string): Errors => {
    const e: Errors = {};
    if (!photos.length) e.photos = COPY.photos;
    if (other && request.trim().length < REQUEST_MIN_OTHER) e.request = COPY.otherRequest;
    if (!name) e.name = COPY.name;
    if (!email.trim() && !phone.trim()) e.contact = COPY.contact;
    else {
      if (email.trim() && !EMAIL.test(email.trim())) e.email = COPY.email;
      if (phone.trim() && !PHONE.test(phone.trim())) e.phone = COPY.phone;
    }
    return e;
  };

  const submit = async (form: HTMLFormElement) => {
    if (busy) return;
    const fd = new FormData(form);
    const field = (k: string) => (fd.get(k) as string | null)?.trim() ?? "";
    const name = field("name");
    const local = validate(name);
    if (Object.keys(local).length) {
      setErrors(local);
      const first = form.querySelector<HTMLElement>("[aria-invalid='true']");
      first?.focus();
      return;
    }
    const body = {
      name,
      email: email.trim(),
      phone: phone.trim(),
      product_type: product,
      size_tier: other ? null : tier,
      size_mm: isStandard(product) ? String(tierMm(product, tier)) : "",
      price: quote(product, tier).amount,
      request: request.trim(),
      website: field("website"),
      source: readParams().src ?? "web",
      photos: photos.map((p) => photoMeta(p.file)),
    };
    setBusy(true);
    setErrors({});
    try {
      setStatus("Checking");
      const start = await fetchImpl("/api/orders/start", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      const s = (await start.json()) as {
        order_id?: string;
        ticket?: string;
        uploads?: UploadTarget[];
        errors?: Errors;
        error?: string;
        retry_after_s?: number;
      };
      if (!start.ok) {
        if (s.errors) setErrors(s.errors.price ? { ...s.errors, form: s.errors.form ?? s.errors.price } : s.errors);
        else
          setErrors({
            form:
              start.status === 429
                ? `Too many orders. Try again in ${Math.ceil((s.retry_after_s ?? 600) / 60)} min`
                : (s.error ?? "Order failed"),
          });
        return;
      }
      for (const [i, p] of photos.entries()) {
        setStatus(`Uploading ${i + 1} of ${photos.length}`);
        await putFile(s.uploads![i], p.file, fetchImpl);
      }
      setStatus("Sending");
      const fin = await fetchImpl("/api/orders/finish", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ticket: s.ticket }),
      });
      const f = (await fin.json()) as { order_id?: string; receipt?: string; error?: string };
      if (!fin.ok) {
        setErrors({ form: f.error ?? "Order failed" });
        return;
      }
      router.push(
        f.receipt
          ? `/order/thanks?r=${encodeURIComponent(f.receipt)}`
          : `/order/thanks?ref=${encodeURIComponent((f.order_id ?? "").slice(0, 8))}`,
      );
    } catch (e) {
      setErrors({ form: (e as Error).message || "Order failed" });
    } finally {
      setBusy(false);
      setStatus(null);
    }
  };

  const hints = TYPING_HINTS[product];
  const requestInvalid = !!errors.request;
  const contactInvalid = !!errors.contact;

  return (
    <form
      noValidate
      aria-label="Order"
      className={cx(SHEET, "relative flex min-w-0 flex-col gap-8 p-5 shadow-none sm:gap-9 sm:p-8 sm:shadow-elev-sheet")}
      onSubmit={(e) => {
        e.preventDefault();
        void submit(e.currentTarget);
      }}
    >
      <ProductPicker
        value={product}
        onChange={(p) => {
          setProduct(p);
          if (p !== "other") setError("request", undefined);
        }}
      />

      {isStandard(product) && (
        <SizePicker
          product={product}
          value={tier}
          onChange={setTier}
          onCompare={() => {
            compareRef.current = document.activeElement as HTMLElement | null;
            setSizesOpen(true);
          }}
        />
      )}

      <PhotoDrop
        photos={photos}
        onAdd={add}
        onRemove={(p) => setPhotos((cur) => cur.filter((x) => x !== p))}
        error={errors.photos}
      />

      <div className="flex flex-col gap-2">
        <label htmlFor="request" className={cx(LABEL, "flex items-baseline gap-2")}>
          {other ? "What should we make?" : "What you want"}
          {!other && <span className={TAG}>Optional</span>}
        </label>
        <div className="relative">
          <textarea
            id="request"
            name="request"
            rows={4}
            maxLength={2000}
            value={request}
            required={other}
            aria-invalid={requestInvalid || undefined}
            aria-describedby={cx("request-help", requestInvalid && "request-error") || undefined}
            onChange={(e) => {
              setRequest(e.target.value);
              if (errors.request && e.target.value.trim().length >= REQUEST_MIN_OTHER) setError("request", undefined);
            }}
            onFocus={() => setRequestFocused(true)}
            onBlur={() => {
              setRequestFocused(false);
              if (other && request.trim() && request.trim().length < REQUEST_MIN_OTHER) setError("request", COPY.otherRequest);
            }}
            className={inputCls(requestInvalid, "block min-h-28 resize-none px-3.5 py-3")}
          />
          <TypingHint phrases={hints} active={!requestFocused && !request} />
        </div>
        {requestInvalid && (
          <p id="request-error" className={cx(ERROR, "m-0")} role="alert">
            {errors.request}
          </p>
        )}
        <p id="request-help" className={cx(HELP, "m-0")}>
          {other ? "What it is, roughly how big, and anything we should know." : `For example: ${hints[1] ?? hints[0]}`}
        </p>
      </div>

      <div className="flex flex-col gap-2">
        <label htmlFor="name" className={LABEL}>
          Your name
        </label>
        <input
          id="name"
          name="name"
          autoComplete="name"
          maxLength={120}
          aria-invalid={errors.name ? true : undefined}
          aria-describedby={errors.name ? "name-error" : undefined}
          onChange={(e) => errors.name && e.target.value.trim() && setError("name", undefined)}
          className={inputCls(!!errors.name, "h-12 px-3.5")}
        />
        {errors.name && (
          <p id="name-error" className={cx(ERROR, "m-0")} role="alert">
            {errors.name}
          </p>
        )}
      </div>

      <fieldset className="m-0 flex min-w-0 flex-col gap-2.5 border-0 p-0">
        <legend className="mb-2.5 p-0 text-body-strong">We&rsquo;ll reply by email or text.</legend>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div className="flex min-w-0 flex-col gap-2">
            <label htmlFor="email" className={cx(LABEL, "flex items-baseline gap-2")}>
              Email <span className={TAG}>Optional</span>
            </label>
            <input
              id="email"
              name="email"
              type="email"
              autoComplete="email"
              maxLength={254}
              value={email}
              aria-invalid={contactInvalid || !!errors.email || undefined}
              aria-describedby={contactInvalid ? "contact-error" : errors.email ? "email-error" : undefined}
              onChange={(e) => {
                setEmail(e.target.value);
                if (e.target.value.trim()) setErrors((x) => ({ ...x, contact: undefined, email: undefined }));
              }}
              onBlur={() => email.trim() && !EMAIL.test(email.trim()) && setError("email", COPY.email)}
              className={inputCls(contactInvalid || !!errors.email, "h-12 px-3.5")}
            />
            {errors.email && (
              <p id="email-error" className={cx(ERROR, "m-0")}>
                {errors.email}
              </p>
            )}
          </div>
          <div className="flex min-w-0 flex-col gap-2">
            <label htmlFor="phone" className={cx(LABEL, "flex items-baseline gap-2")}>
              Phone <span className={TAG}>Optional</span>
            </label>
            <input
              id="phone"
              name="phone"
              type="tel"
              autoComplete="tel"
              maxLength={40}
              value={phone}
              aria-invalid={contactInvalid || !!errors.phone || undefined}
              aria-describedby={contactInvalid ? "contact-error" : errors.phone ? "phone-error" : undefined}
              onChange={(e) => {
                setPhone(e.target.value);
                if (e.target.value.trim()) setErrors((x) => ({ ...x, contact: undefined, phone: undefined }));
              }}
              onBlur={() => phone.trim() && !PHONE.test(phone.trim()) && setError("phone", COPY.phone)}
              className={inputCls(contactInvalid || !!errors.phone, "h-12 px-3.5")}
            />
            {errors.phone && (
              <p id="phone-error" className={cx(ERROR, "m-0")}>
                {errors.phone}
              </p>
            )}
          </div>
        </div>
        {contactInvalid && (
          <p id="contact-error" className={cx(ERROR, "m-0")} role="alert">
            {errors.contact}
          </p>
        )}
      </fieldset>

      {/* Honeypot: hidden from people, filled by some bots. */}
      <div aria-hidden className="absolute left-[-9999px] h-0 w-0 overflow-hidden">
        <label htmlFor="website">Website</label>
        <input id="website" name="website" tabIndex={-1} autoComplete="off" />
      </div>

      <OrderSummary product={product} tier={tier} quote={quote(product, tier)}>
        <button type="submit" disabled={busy} className={btn("primary", "lg", "w-full")}>
          {busy ? (status ?? "Sending") : "Send order"}
        </button>
        {errors.form && (
          <p role="alert" className={cx(ERROR, "m-0")}>
            {errors.form}
          </p>
        )}
      </OrderSummary>

      {isStandard(product) && (
        <SizeDialog
          open={sizesOpen}
          product={product}
          tier={tier}
          onPick={setTier}
          onClose={() => setSizesOpen(false)}
          returnFocus={compareRef}
        />
      )}
    </form>
  );
}
