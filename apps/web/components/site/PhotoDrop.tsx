"use client";

// The photo field: a dashed dropzone that becomes a row of up to four thumbnails with an Add tile.
// Upload logic lives in OrderForm (direct signed uploads); this is only the picker.

import { useRef, useState } from "react";
import { CloseIcon, ERROR, HELP, LABEL, UploadIcon, cx } from "./ui";

export interface Picked {
  file: File;
  url: string | null;
}

export const MAX_PHOTOS = 4;
const ACCEPT = "image/jpeg,image/png,image/heic,image/webp,.heic,.heif";

export function PhotoDrop({
  photos,
  onAdd,
  onRemove,
  error,
}: {
  photos: Picked[];
  onAdd: (files: FileList | File[]) => void;
  onRemove: (p: Picked) => void;
  error?: string | null;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const pick = () => fileRef.current?.click();
  const drop = {
    onDragOver: (e: React.DragEvent) => {
      e.preventDefault();
      setOver(true);
    },
    onDragLeave: () => setOver(false),
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      setOver(false);
      onAdd(e.dataTransfer.files);
    },
  };
  return (
    <div className="flex flex-col gap-2.5">
      <span id="lbl-photos" className={LABEL}>
        Photos
      </span>
      {photos.length === 0 ? (
        <button
          type="button"
          onClick={pick}
          {...drop}
          aria-describedby="photos-help"
          aria-invalid={error ? true : undefined}
          className={cx(
            "flex min-h-24 w-full items-center justify-center gap-2 rounded-md border-[1.5px] border-dashed text-body font-medium transition-colors duration-[var(--dur-fast)]",
            over ? "border-brick bg-brick-wash text-ink" : error ? "border-danger bg-paper text-ink-2" : "border-line-strong bg-paper text-ink-2 hover:border-ink hover:text-ink",
          )}
        >
          <UploadIcon />
          {over ? "Drop to add" : "Add 1 to 4 photos"}
        </button>
      ) : (
        <div {...drop} className={cx("flex flex-wrap gap-2 rounded-md", over && "outline-[1.5px] outline-dashed outline-offset-4 outline-brick")}>
          {photos.map((p, i) => (
            <div key={`${p.file.name}-${i}`} className="relative size-[74px] shrink-0 sm:size-32">
              {p.url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={p.url} alt={`Photo ${i + 1}`} className="block size-full rounded-sm object-cover" />
              ) : (
                <span className="flex size-full items-center justify-center break-all rounded-sm border border-line bg-paper p-1.5 text-center text-caption text-ink-2">
                  {p.file.name}
                </span>
              )}
              <button
                type="button"
                aria-label={`Remove photo ${i + 1}`}
                onClick={() => onRemove(p)}
                className="group absolute -right-1.5 -top-1.5 flex size-11 items-center justify-center rounded-pill"
              >
                <span className="flex size-8 items-center justify-center rounded-pill bg-sheet/90 text-ink group-hover:bg-sheet">
                  <CloseIcon />
                </span>
              </button>
            </div>
          ))}
          {photos.length < MAX_PHOTOS && (
            <button
              type="button"
              onClick={pick}
              className="flex size-[74px] shrink-0 flex-col items-center justify-center gap-1.5 rounded-md border-[1.5px] border-dashed border-line-strong bg-paper text-[14px] font-medium leading-5 text-ink-2 hover:border-ink hover:text-ink sm:size-32"
            >
              <UploadIcon />
              Add
            </button>
          )}
        </div>
      )}
      <input
        ref={fileRef}
        type="file"
        accept={ACCEPT}
        multiple
        hidden
        aria-labelledby="lbl-photos"
        data-testid="order-photos"
        onChange={(e) => {
          if (e.target.files) onAdd(e.target.files);
          e.target.value = "";
        }}
      />
      {error ? (
        <p className={ERROR} role="alert">
          {error}
        </p>
      ) : (
        <p id="photos-help" className={cx(HELP, "m-0")}>
          JPEG, PNG, HEIC or WebP, 15 MB each
        </p>
      )}
    </div>
  );
}
