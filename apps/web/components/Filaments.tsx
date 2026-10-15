"use client";

import { useMemo, useRef, useState } from "react";
import { filamentSlug, formatLab, hexToLab, isHex, normalizeHex, type Lab } from "@/lib/color";
import type { FilamentRow } from "@/lib/db.types";
import type { FilamentInput } from "@/lib/data/types";
import { useData, useFilaments, useSwatchUrl } from "@/lib/hooks";
import { Button, Empty, ErrorLine, Input, Tip, cx, inputCls } from "./ui";

const SLOTS = Array.from({ length: 16 }, (_, i) => i + 1);
const COLS = "grid-cols-[40px_minmax(0,1.4fr)_64px_84px_150px_64px_72px_56px_76px]";

export function displayLab(f: Pick<FilamentRow, "hex" | "lab_l" | "lab_a" | "lab_b" | "lab_source">): {
  lab: Lab;
  measured: boolean;
} {
  if (f.lab_source === "measured") return { lab: [f.lab_l, f.lab_a, f.lab_b], measured: true };
  return { lab: hexToLab(f.hex), measured: false };
}

function toInput(f: FilamentRow): FilamentInput {
  return {
    id: f.id,
    brand: f.brand,
    line: f.line,
    name: f.name,
    material: f.material,
    hex: f.hex,
    owned: f.owned,
    spools: f.spools,
    ams_slot: f.ams_slot,
    td_mm: f.td_mm,
    notes: f.notes,
  };
}

export function Filaments() {
  const client = useData();
  const { filaments, error, put } = useFilaments();
  const [adding, setAdding] = useState(false);
  const [rowError, setRowError] = useState<string | null>(null);

  const slotCount = useMemo(() => {
    const m = new Map<number, number>();
    for (const f of filaments ?? []) if (f.ams_slot != null) m.set(f.ams_slot, (m.get(f.ams_slot) ?? 0) + 1);
    return m;
  }, [filaments]);

  const save = async (f: FilamentRow, patch: Partial<FilamentInput>) => {
    if (!client) return;
    setRowError(null);
    put({ ...f, ...patch } as FilamentRow); // optimistic
    try {
      put(await client.saveFilament({ ...toInput(f), ...patch }));
    } catch (e) {
      put(f);
      setRowError((e as Error).message);
    }
  };

  const owned = (filaments ?? []).filter((f) => f.owned).length;
  const loaded = (filaments ?? []).filter((f) => f.ams_slot != null).length;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <h1 className="font-display text-[34px] font-bold leading-none tracking-[-0.02em]">Filaments</h1>
        <span className="num text-sm text-muted">{owned} owned</span>
        <span className="num text-sm text-muted">{loaded} in AMS</span>
        <span className="num text-sm text-muted">{(filaments ?? []).length - owned} catalog</span>
        <div className="ml-auto">
          <Button variant="primary" onClick={() => setAdding(true)} disabled={adding}>
            Add spool
          </Button>
        </div>
      </div>
      <div className="overflow-x-auto rounded-xl border border-line bg-panel">
        <div className="min-w-[860px]">
          <div className={cx("grid items-center gap-3 border-b border-line px-4 py-2.5 text-xs text-muted", COLS)}>
            <span />
            <span>Name</span>
            <span>Material</span>
            <span>Hex</span>
            <span>Lab</span>
            <span>Owned</span>
            <span>AMS slot</span>
            <span>Spools</span>
            <span>Swatch</span>
          </div>
          {adding && (
            <AddRow
              existing={new Set((filaments ?? []).map((f) => f.id))}
              onCancel={() => setAdding(false)}
              onSave={async (input) => {
                if (!client) return;
                const row = await client.saveFilament(input);
                put(row);
                setAdding(false);
              }}
            />
          )}
          {error && <Empty>{error}</Empty>}
          {filaments === null && !error && <Empty>Loading</Empty>}
          {filaments && filaments.length === 0 && !adding && <Empty>No filaments</Empty>}
          <ul>
            {(filaments ?? []).map((f) => (
              <FilamentLine
                key={f.id}
                f={f}
                dupSlot={f.ams_slot != null && (slotCount.get(f.ams_slot) ?? 0) > 1}
                onSave={(patch) => void save(f, patch)}
                onSwatch={async (file) => {
                  if (!client) return;
                  try {
                    put(await client.uploadSwatch(f.id, file));
                  } catch (e) {
                    setRowError((e as Error).message);
                  }
                }}
              />
            ))}
          </ul>
        </div>
      </div>
      <ErrorLine>{rowError}</ErrorLine>
    </div>
  );
}

function FilamentLine({
  f,
  dupSlot,
  onSave,
  onSwatch,
}: {
  f: FilamentRow;
  dupSlot: boolean;
  onSave: (patch: Partial<FilamentInput>) => void;
  onSwatch: (file: File) => void;
}) {
  const { lab, measured } = displayLab(f);
  const file = useRef<HTMLInputElement>(null);
  const swatch = useSwatchUrl(f.swatch_path);
  return (
    <li
      data-testid={`filament-${f.id}`}
      className={cx("grid h-11 items-center gap-3 border-b border-line/60 px-4 text-sm last:border-b-0 hover:bg-hover", COLS, !f.owned && "text-muted")}
    >
      <span
        className="size-6 rounded-full border border-ink/20"
        style={{ background: f.hex }}
        aria-label={`Swatch ${f.hex}`}
      />
      <span className="min-w-0">
        <Tip label={f.id}>
          <span className="block truncate text-fg">{f.name}</span>
        </Tip>
        <span className="block truncate text-2xs text-faint">
          {f.brand} {f.line}
        </span>
      </span>
      <span className="text-muted">{f.material}</span>
      <span className="font-mono text-xs text-muted">{f.hex}</span>
      <Tip label={measured ? "Measured" : "Nominal, from hex"}>
        <span data-testid="lab" className={cx("num text-xs", measured ? "text-fg" : "text-muted")}>
          {formatLab(lab)}
          {measured && <span className="ml-1 text-2xs text-ok">Measured</span>}
        </span>
      </Tip>
      <label className="inline-flex items-center">
        <input
          type="checkbox"
          checked={f.owned}
          onChange={(e) => onSave({ owned: e.target.checked })}
          className="size-3.5 accent-[var(--color-accent)]"
          aria-label={`Owned ${f.name}`}
        />
      </label>
      <Tip label={dupSlot ? "Slot shared" : "AMS slot"}>
        <select
          aria-label={`AMS slot ${f.name}`}
          value={f.ams_slot ?? ""}
          onChange={(e) => onSave({ ams_slot: e.target.value === "" ? null : Number(e.target.value) })}
          className={inputCls(cx("num h-6 w-16 px-1 text-xs", dupSlot && "border-pause text-pause"))}
        >
          <option value="">None</option>
          {SLOTS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </Tip>
      <input
        aria-label={`Spools ${f.name}`}
        type="number"
        min={0}
        value={f.spools}
        onChange={(e) => onSave({ spools: Math.max(0, Number(e.target.value) || 0) })}
        className={inputCls("num h-6 w-12 px-1 text-xs")}
      />
      <span className="flex items-center gap-1.5">
        {swatch && (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={swatch} alt={`${f.name} swatch`} className="size-6 rounded-sm border border-line object-cover" />
        )}
        <Button size="sm" variant="ghost" onClick={() => file.current?.click()}>
          {swatch ? "Replace" : "Upload"}
        </Button>
        <input
          ref={file}
          type="file"
          accept="image/*"
          hidden
          data-testid={`swatch-input-${f.id}`}
          onChange={(e) => {
            const picked = e.target.files?.[0];
            if (picked) onSwatch(picked);
            e.target.value = "";
          }}
        />
      </span>
    </li>
  );
}

function AddRow({
  existing,
  onSave,
  onCancel,
}: {
  existing: Set<string>;
  onSave: (input: FilamentInput) => Promise<void>;
  onCancel: () => void;
}) {
  const [v, setV] = useState({ brand: "", line: "", name: "", material: "PLA", hex: "#", ams: "", spools: "1" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const hexOk = isHex(v.hex);
  const id = v.brand && v.name ? filamentSlug(v.brand, v.material, v.line, v.name) : "";
  const dup = id && existing.has(id);
  const ok = !!v.brand.trim() && !!v.name.trim() && !!v.material.trim() && hexOk && !dup && !busy;
  const lab = hexOk ? hexToLab(v.hex) : null;
  const set = (k: keyof typeof v) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setV((cur) => ({ ...cur, [k]: e.target.value }));

  const submit = async () => {
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      await onSave({
        brand: v.brand.trim(),
        line: v.line.trim() || v.material.trim(),
        name: v.name.trim(),
        material: v.material.trim(),
        hex: normalizeHex(v.hex),
        owned: true,
        spools: Math.max(0, Number(v.spools) || 1),
        ams_slot: v.ams === "" ? null : Number(v.ams),
      });
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <form
      aria-label="Add spool"
      className="border-b border-line bg-raised/60 px-3 py-2"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="color"
          aria-label="Color"
          value={hexOk ? normalizeHex(v.hex) : "#888888"}
          onChange={(e) => setV((cur) => ({ ...cur, hex: e.target.value.toUpperCase() }))}
          className="h-7 w-9 cursor-pointer rounded-md border border-line-strong bg-bg p-0.5"
        />
        <Input aria-label="Hex" placeholder="#RRGGBB" value={v.hex} onChange={set("hex")} className={cx("w-24 font-mono", v.hex.length > 1 && !hexOk && "border-bad")} />
        <Input aria-label="Brand" placeholder="Brand" value={v.brand} onChange={set("brand")} className="w-28" autoFocus />
        <Input aria-label="Line" placeholder="Line" value={v.line} onChange={set("line")} className="w-28" />
        <Input aria-label="Name" placeholder="Color name" value={v.name} onChange={set("name")} className="w-32" />
        <select aria-label="Material" value={v.material} onChange={set("material")} className={inputCls("w-20")}>
          {["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC"].map((m) => (
            <option key={m}>{m}</option>
          ))}
        </select>
        <select aria-label="AMS slot" value={v.ams} onChange={set("ams")} className={inputCls("num w-20")}>
          <option value="">No slot</option>
          {SLOTS.map((s) => (
            <option key={s} value={s}>
              Slot {s}
            </option>
          ))}
        </select>
        <Input aria-label="Spools" type="number" min={0} value={v.spools} onChange={set("spools")} className="num w-14" />
        <div className="ml-auto flex items-center gap-2">
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={!ok}>
            Save
          </Button>
        </div>
      </div>
      <div className="num mt-1.5 flex gap-3 text-2xs text-faint">
        {id && <span className={dup ? "text-bad" : ""}>{dup ? `${id} exists` : id}</span>}
        {lab && <span>LAB {formatLab(lab)}</span>}
      </div>
      <ErrorLine>{error}</ErrorLine>
    </form>
  );
}
