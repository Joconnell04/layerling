// Order form validation. Limits mirror supabase/migrations/20260924000004_orders_access.sql and
// 20260924000007_sizes_contact.sql: email or phone (at least one), a size tier for standard
// products, request optional except for Other (10 characters or more), quantity 1 (set at
// checkout), no name_text (a name goes in the request). Prices come from costs/pricing.json
// (lib/pricing): the server computes the quote itself and rejects an order whose shown price no
// longer matches.

import type { ProductType } from "../contracts.gen";
import { PRODUCT_TYPES } from "../db.types";
import { quote, type PriceQuote } from "../pricing";
import { DEFAULT_SIZE_TIER, isSizeTier, isStandard, tierMm, type SizeTier } from "../products";

export const ORDER_LIMITS = {
  maxPhotos: 4,
  minPhotos: 1,
  maxPhotoBytes: 15 * 1024 * 1024, // bucket file_size_limit 15728640
  name: 120,
  request: 2000,
  requestMinOther: 10,
  phone: 40,
  email: 254,
  source: 40,
} as const;

/** Error copy shared with the form (components/site/OrderForm.tsx). */
export const ORDER_COPY = {
  name: "Add your name",
  contact: "Add an email or a phone number",
  email: "Check the email address",
  phone: "Check the phone number",
  product: "Pick a product",
  size: "Pick a size",
  otherRequest: `Tell us a bit more, at least ${ORDER_LIMITS.requestMinOther} characters`,
  request: `Max ${ORDER_LIMITS.request} characters`,
  photosMin: "Add at least one photo",
  photosMax: `Max ${ORDER_LIMITS.maxPhotos} photos`,
  photoType: "Photos must be JPEG, PNG, HEIC or WebP",
  photoSize: "Each photo must be under 15 MB",
  price: "Price changed",
} as const;

export const PHOTO_TYPES: Record<string, string> = {
  "image/jpeg": "jpg",
  "image/png": "png",
  "image/heic": "heic",
  "image/webp": "webp",
};
const EXT_TYPES: Record<string, string> = {
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  png: "image/png",
  heic: "image/heic",
  heif: "image/heic",
  webp: "image/webp",
};

// Same pattern as the orders.email check constraint.
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const PHONE = /^[0-9+()\-. ]{5,40}$/;

/** What the browser tells us about a photo before uploading it. */
export interface PhotoMeta {
  name: string;
  type: string;
  size: number;
}

export interface OrderPhoto extends PhotoMeta {
  type: string; // normalized MIME type
  ext: string;
  file?: Blob;
}

export interface OrderInput {
  name: string;
  email: string | null;
  phone: string | null;
  product_type: ProductType;
  size_tier: SizeTier | null;
  size_mm: number | null;
  quantity: 1;
  name_text: null;
  request: string | null;
  source: string | null;
  photos: OrderPhoto[];
  /** Honeypot filled: store as spam, skip the email. */
  spam: boolean;
  /** The server's quote from pricing.json at order time (not a column; carried in the receipt). */
  quote: PriceQuote;
}

export type OrderErrorKey =
  | "name"
  | "email"
  | "phone"
  | "contact"
  | "product_type"
  | "size_tier"
  | "request"
  | "photos"
  | "price"
  | "form";
export type OrderErrors = Partial<Record<OrderErrorKey, string>>;
export type OrderValidation = { ok: true; value: OrderInput } | { ok: false; errors: OrderErrors; quote?: PriceQuote };

type Field = FormDataEntryValue | null | undefined;

function str(v: Field): string {
  return typeof v === "string" ? v.trim() : "";
}

function isBlob(v: unknown): v is Blob & { name?: string } {
  return typeof Blob !== "undefined" && v instanceof Blob;
}

/** MIME type from the upload, falling back to the extension (some browsers send HEIC as ""). */
export function photoType(file: { type?: string; name?: string }): { type: string; ext: string } | null {
  const t = (file.type || "").toLowerCase();
  if (PHOTO_TYPES[t]) return { type: t, ext: PHOTO_TYPES[t] };
  const ext = /\.([a-z0-9]+)$/i.exec(file.name ?? "")?.[1]?.toLowerCase();
  if (ext && EXT_TYPES[ext] && (!t || t === "application/octet-stream")) {
    return { type: EXT_TYPES[ext], ext: PHOTO_TYPES[EXT_TYPES[ext]] };
  }
  return null;
}

export interface FormLike {
  get(name: string): Field;
  getAll(name: string): FormDataEntryValue[];
}

/** Multipart form (tests, small uploads): fields plus the photo files themselves. */
export function validateOrder(form: FormLike): OrderValidation {
  const files = form
    .getAll("photos")
    .filter((v): v is File => isBlob(v))
    .filter((f) => f.size > 0);
  const r = validateOrderFields(
    (k) => form.get(k),
    files.map((f) => ({ name: f.name ?? "photo", type: f.type, size: f.size })),
  );
  if (r.ok) r.value.photos.forEach((p, i) => (p.file = files[i]));
  return r;
}

/** JSON body from the order form: fields plus photo metadata; the files go to Storage directly. */
export function validateOrderJson(body: unknown): OrderValidation {
  if (!body || typeof body !== "object") return { ok: false, errors: { form: "Bad request" } };
  const b = body as Record<string, unknown>;
  const raw = Array.isArray(b.photos) ? b.photos : [];
  const photos: PhotoMeta[] = raw
    .filter((p): p is Record<string, unknown> => !!p && typeof p === "object")
    .map((p) => ({
      name: String(p.name ?? "photo").slice(0, 200),
      type: String(p.type ?? ""),
      size: Number(p.size ?? 0),
    }))
    .filter((p) => p.size > 0);
  const get = (k: string) => {
    const v = b[k];
    if (k === "price" && v === null) return "null";
    return typeof v === "string" ? v : typeof v === "number" ? String(v) : null;
  };
  return validateOrderFields(get, photos);
}

/** Cents-exact compare. The form sends the amount it showed, or null when it showed no number. */
function samePrice(shown: string, q: PriceQuote): boolean {
  if (shown === "") return true; // older clients send nothing: the server quote stands
  if (shown === "null") return q.amount == null;
  const n = Number(shown);
  return Number.isFinite(n) && q.amount != null && Math.round(n * 100) === Math.round(q.amount * 100);
}

export function validateOrderFields(get: (name: string) => Field, photoMeta: PhotoMeta[]): OrderValidation {
  const errors: OrderErrors = {};

  const name = str(get("name")).replace(/\s+/g, " ");
  if (!name) errors.name = ORDER_COPY.name;
  else if (name.length > ORDER_LIMITS.name) errors.name = `Max ${ORDER_LIMITS.name}`;

  const email = str(get("email")) || null;
  const phone = str(get("phone")) || null;
  if (!email && !phone) errors.contact = ORDER_COPY.contact;
  if (email && (email.length > ORDER_LIMITS.email || !EMAIL.test(email))) errors.email = ORDER_COPY.email;
  if (phone && !PHONE.test(phone)) errors.phone = ORDER_COPY.phone;

  const product = str(get("product_type")) as ProductType;
  const known = PRODUCT_TYPES.includes(product);
  if (!known) errors.product_type = ORDER_COPY.product;

  // The tier is the input; size_mm always comes from the tier table, never from the client.
  const tierRaw = str(get("size_tier")).toLowerCase();
  let size_tier: SizeTier | null = null;
  let size_mm: number | null = null;
  if (known && isStandard(product)) {
    if (!isSizeTier(tierRaw)) errors.size_tier = ORDER_COPY.size;
    else {
      size_tier = tierRaw;
      size_mm = tierMm(product, tierRaw);
    }
  }

  const requestRaw = str(get("request"));
  const request = requestRaw || null;
  if (requestRaw.length > ORDER_LIMITS.request) errors.request = ORDER_COPY.request;
  else if (product === "other" && requestRaw.length < ORDER_LIMITS.requestMinOther) {
    errors.request = ORDER_COPY.otherRequest;
  }

  const source = str(get("source")).slice(0, ORDER_LIMITS.source) || null;

  const photos: OrderPhoto[] = [];
  if (photoMeta.length < ORDER_LIMITS.minPhotos) errors.photos = ORDER_COPY.photosMin;
  else if (photoMeta.length > ORDER_LIMITS.maxPhotos) errors.photos = ORDER_COPY.photosMax;
  else {
    for (const f of photoMeta) {
      const t = photoType(f);
      if (!t) {
        errors.photos = ORDER_COPY.photoType;
        break;
      }
      if (!Number.isFinite(f.size) || f.size > ORDER_LIMITS.maxPhotoBytes) {
        errors.photos = ORDER_COPY.photoSize;
        break;
      }
      photos.push({ name: f.name, size: f.size, ...t });
    }
  }

  const q = known ? quote(product, size_tier ?? DEFAULT_SIZE_TIER) : null;
  if (q && !errors.size_tier && !samePrice(str(get("price")), q)) errors.price = ORDER_COPY.price;

  const spam = str(get("website")).length > 0;

  if (Object.keys(errors).length || !q) return { ok: false, errors, quote: q ?? undefined };
  return {
    ok: true,
    value: {
      name,
      email,
      phone,
      product_type: product,
      size_tier,
      size_mm,
      quantity: 1,
      name_text: null,
      request,
      source,
      photos,
      spam,
      quote: q,
    },
  };
}

export function orderPhotoKey(orderId: string, n: number, ext: string): string {
  return `${orderId}/photo_${n}.${ext}`;
}
