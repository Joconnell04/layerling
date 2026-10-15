"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { NAME_TEXT_MAX, PRODUCT_DEFAULTS, type ProductForm, type ProductType, type Profile } from "@/lib/contracts";
import { askChip, asksFromText, formFromText, formIsChoice, formLabel } from "@/lib/intent";
import { PRODUCT_LABELS, acceptsNameText, addonsFor } from "@/lib/products";
import { useData } from "@/lib/hooks";
import { Button, ErrorLine, Input, Label, Segmented, Textarea, cx } from "./ui";

const SIZE_DEFAULT = (p: ProductType) => PRODUCT_DEFAULTS[p].target_size_mm;
const PRODUCTS = Object.keys(PRODUCT_LABELS) as ProductType[];
const MAX_PHOTOS = 8;

/** Form choice on the new-job form: "ask" leaves it to the intake question. */
type FormPick = ProductForm | "ask";

interface Picked {
  file: File;
  url: string;
}

export function NewJobForm({ onCreated }: { onCreated?: (id: string) => void }) {
  const client = useData();
  const router = useRouter();
  const [photos, setPhotos] = useState<Picked[]>([]);
  const [prompt, setPrompt] = useState("");
  const [product, setProduct] = useState<ProductType>("figurine");
  const [size, setSize] = useState<string>(String(SIZE_DEFAULT("figurine")));
  const [nameText, setNameText] = useState("");
  const [sizeTouched, setSizeTouched] = useState(false);
  const [profile, setProfile] = useState<Profile>("thin_slice");
  // Keychains and magnets: flat charm or 3D figure. Follows the request's own words ("3d", "flat")
  // until picked; "Ask" leaves it to intake, which asks the customer.
  const [formPick, setFormPick] = useState<FormPick | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [drag, setDrag] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const formRef = useRef<HTMLFormElement>(null);

  useEffect(() => () => photos.forEach((p) => URL.revokeObjectURL(p.url)), []); // eslint-disable-line react-hooks/exhaustive-deps

  const add = (files: FileList | File[]) => {
    const images = [...files].filter((f) => f.type.startsWith("image/"));
    setPhotos((cur) =>
      [...cur, ...images.map((file) => ({ file, url: URL.createObjectURL(file) }))].slice(0, MAX_PHOTOS),
    );
  };

  const choice = formIsChoice(product);
  const form: FormPick = formPick ?? formFromText(prompt) ?? "ask";
  const asks = asksFromText(prompt);

  const sizeNum = size.trim() === "" ? null : Number(size);
  const sizeValid = sizeNum === null || (Number.isFinite(sizeNum) && sizeNum >= 10 && sizeNum <= 300);
  const canSubmit = !!client && photos.length > 0 && prompt.trim().length > 0 && sizeValid && !busy;

  const submit = async () => {
    if (!client || !canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const job = await client.createJob({
        prompt,
        productType: product,
        targetSizeMm: sizeNum,
        profile,
        photos: photos.map((p) => p.file),
        addons: addonsFor(product, nameText),
        form: choice && form !== "ask" ? form : null,
      });
      if (onCreated) onCreated(job.id);
      else router.push(`/jobs/${job.id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <form
      ref={formRef}
      className="mx-auto max-w-2xl space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <h1 className="font-display text-[34px] font-bold leading-none tracking-[-0.02em]">New job</h1>

      <div>
        <Label>Photos</Label>
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDrag(true);
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            add(e.dataTransfer.files);
          }}
          className={cx(
            "rounded-xl border border-dashed p-3 transition-colors",
            drag ? "border-accent bg-accent-soft" : "border-line-strong bg-panel",
          )}
        >
          <div className="grid grid-cols-4 gap-2 sm:grid-cols-6">
            {photos.map((p, i) => (
              <div key={p.url} className="group relative aspect-square overflow-hidden rounded-md border border-line bg-bg">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={p.url} alt={`Photo ${i + 1}`} className="size-full object-cover" />
                <span className="num absolute left-1 top-1 rounded-sm bg-bg/80 px-1 text-2xs text-muted">{i + 1}</span>
                <button
                  type="button"
                  aria-label={`Remove photo ${i + 1}`}
                  onClick={() => {
                    URL.revokeObjectURL(p.url);
                    setPhotos((cur) => cur.filter((x) => x !== p));
                  }}
                  className="absolute right-1 top-1 hidden size-5 items-center justify-center rounded-sm bg-bg/85 text-xs text-muted hover:text-fg group-hover:flex"
                >
                  ×
                </button>
              </div>
            ))}
            {photos.length < MAX_PHOTOS && (
              <button
                type="button"
                onClick={() => fileRef.current?.click()}
                className="flex aspect-square flex-col items-center justify-center gap-1 rounded-md border border-line bg-bg text-xs text-muted hover:border-line-strong hover:text-fg"
              >
                <span className="text-lg leading-none">+</span>
                Add
              </button>
            )}
          </div>
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            multiple
            hidden
            data-testid="photo-input"
            onChange={(e) => {
              if (e.target.files) add(e.target.files);
              e.target.value = "";
            }}
          />
        </div>
      </div>

      <div>
        <Label htmlFor="prompt">Request</Label>
        <Textarea
          id="prompt"
          rows={3}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="Keychain of me and my dog hugging"
          maxLength={500}
        />
        {asks.length > 0 && (
          <ul aria-label="Asks" data-testid="asks" className="mt-2 flex flex-wrap items-center gap-1.5">
            <li className="mr-1 text-sm font-semibold">Asks</li>
            {asks.map((a) => {
              const c = askChip(a);
              return (
                <li key={`${a.kind}-${a.text}`} className="inline-flex h-7 items-center gap-1.5 rounded-full border border-line-strong bg-panel px-3 text-sm">
                  <span className="text-muted">{c.kind}</span>
                  <span>{c.text}</span>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <div>
          <Label>Product</Label>
          <Segmented<ProductType>
            label="Product"
            value={product}
            onChange={(v) => {
              setProduct(v);
              if (!sizeTouched) setSize(String(SIZE_DEFAULT(v)));
            }}
            options={PRODUCTS.map((p) => ({ value: p, label: PRODUCT_LABELS[p] }))}
          />
        </div>
        {choice && (
          <div>
            <Label>Form</Label>
            <Segmented<FormPick>
              label="Form"
              value={form}
              onChange={setFormPick}
              options={[
                { value: "ask", label: "Ask" },
                { value: "flat", label: formLabel("flat", product) },
                { value: "figure", label: formLabel("figure", product) },
              ]}
            />
          </div>
        )}
        {acceptsNameText(product) && (
          <div className="w-40">
            <Label htmlFor="name-text">Name text</Label>
            <Input
              id="name-text"
              value={nameText}
              maxLength={NAME_TEXT_MAX}
              placeholder={product === "nameplate" ? "PEPPER" : "Optional"}
              onChange={(e) => setNameText(e.target.value)}
            />
          </div>
        )}
        <div className="w-28">
          <Label htmlFor="size">Size mm</Label>
          <Input
            id="size"
            inputMode="decimal"
            value={size}
            aria-invalid={!sizeValid}
            className={cx("num", !sizeValid && "border-bad")}
            onChange={(e) => {
              setSize(e.target.value);
              setSizeTouched(true);
            }}
          />
        </div>
        <div>
          <Label>Pipeline</Label>
          <Segmented<Profile>
            label="Pipeline"
            value={profile}
            onChange={setProfile}
            options={[
              { value: "thin_slice", label: "Thin slice" },
              { value: "full", label: "Full" },
            ]}
          />
        </div>
      </div>

      <div className="flex items-center gap-3 border-t border-line pt-4">
        <Button type="submit" variant="primary" disabled={!canSubmit}>
          {busy ? "Starting" : "Start job"}
        </Button>
        <span className="num text-sm text-muted">
          {photos.length} photo{photos.length === 1 ? "" : "s"}
        </span>
        <span className="num ml-auto text-sm text-muted">Cap $10 · 400 credits</span>
      </div>
      <ErrorLine>{error}</ErrorLine>
    </form>
  );
}
