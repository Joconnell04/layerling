import type { JobResult } from "@/lib/contracts.gen";
import type { AnyMessage, FilamentRow, JobRow } from "@/lib/db.types";
import { draftChanged, draftFrom, nearestSlot, paletteEdit, pendingPaletteEdit, pickerGroups } from "@/lib/palette";
import { resultVerdict } from "./DecisionRail";
import { sizeText } from "./ModelStage";

const result = (over: Partial<JobResult> = {}): JobResult => ({
  threemf: "s10_export/a1/model.3mf",
  render_sheet: null,
  preview_glb: null,
  print_time_s: null,
  filaments: [],
  baseline_only: false,
  slice_ok: true,
  ...over,
});

const row = (id: string, name: string, over: Partial<FilamentRow> = {}) =>
  ({ id, name, brand: "Bambu Lab", line: "PLA Basic", material: "PLA", hex: "#808080", owned: true, ams_slot: null, ...over }) as FilamentRow;

describe("model stage helpers", () => {
  it("labels size by the axis it measures", () => {
    const none = { flat: false, widthMm: null, heightMm: null, reliefMm: null, depthMm: null };
    expect(sizeText({ ...none, heightMm: 45, widthMm: 30, depthMm: 27, targetMm: 45 }, false)).toBe("W 30 · H 45 mm");
    expect(sizeText({ ...none, flat: true, widthMm: 20.5, heightMm: 45, reliefMm: 10.3, targetMm: 45 }, false)).toBe(
      "W 20.5 · H 45 · T 10.3 mm",
    );
    expect(sizeText({ ...none, targetMm: 120 }, true)).toBe("120 mm plate target");
    expect(sizeText({ ...none, targetMm: 80 }, false)).toBe("80 mm target");
  });
});

describe("result verdict", () => {
  const job = { review_reason: null, result: null } as unknown as JobRow;

  it("is ready only when the slicer gate passed", () => {
    expect(resultVerdict(job, result())).toEqual({ ready: true, reason: null });
    expect(resultVerdict(job, result({ slice_ok: false }))).toEqual({ ready: false, reason: "Failed the slicer gate" });
    expect(resultVerdict(job, result({ slice_ok: null as never }))).toEqual({ ready: false, reason: "Not checked by the slicer gate" });
    expect(resultVerdict({ ...job, review_reason: "Wall under 0.8 mm" }, result({ slice_ok: false })).reason).toBe("Wall under 0.8 mm");
    expect(resultVerdict(job).ready).toBe(false);
  });
});

describe("palette edits", () => {
  const usage = [
    { slot: 2, filament_id: "generic-pla-white", grams: 6 },
    { slot: 1, filament_id: "generic-pla-brown", grams: 9 },
  ];
  const lib = [
    row("generic-pla-brown", "Brown", { brand: "Generic", line: "PLA", hex: "#7C4B00" }),
    row("generic-pla-white", "White", { brand: "Generic", line: "PLA", hex: "#FFFFFF" }),
    row("bambu-pla-basic-gray", "Gray", { owned: false, hex: "#8E9089" }),
  ];

  it("starts the draft from the result in slot order", () => {
    const d = draftFrom(usage);
    expect(d).toEqual([
      { from: 1, id: "generic-pla-brown" },
      { from: 2, id: "generic-pla-white" },
    ]);
    expect(draftChanged(d, usage)).toBe(false);
  });

  it("is recolor for swaps only and refit when a color is added or removed", () => {
    const d = draftFrom(usage);
    const swapped = [d[0], { ...d[1], id: "bambu-pla-basic-gray" }];
    expect(paletteEdit(swapped, usage, lib)).toEqual({
      slots: ["generic-pla-brown", "bambu-pla-basic-gray"],
      mode: "recolor",
      source: "any",
    });
    expect(paletteEdit([...d, { from: null, id: "generic-pla-white" }], usage, lib)).toEqual({
      slots: ["generic-pla-brown", "generic-pla-white", "generic-pla-white"],
      mode: "refit",
      source: "owned",
    });
    // Removing a slot and adding another keeps the count but is still a refit.
    expect(paletteEdit([d[0], { from: null, id: "generic-pla-white" }], usage, lib).mode).toBe("refit");
  });

  it("orders the picker: owned first, then catalog by line, with search", () => {
    const all = [
      row("bambu-petg-hf-gray", "Gray", { owned: false, line: "PETG HF", material: "PETG" }),
      row("bambu-pla-basic-orange", "Orange", { owned: false }),
      row("generic-pla-black", "Black", { brand: "Generic", line: "PLA", ams_slot: 1 }),
      row("bambu-pla-basic-gray", "Gray", { owned: false }),
      row("generic-pla-brown", "Brown", { brand: "Generic", line: "PLA", ams_slot: 2 }),
    ];
    const groups = pickerGroups(all);
    expect(groups.map((g) => [g.label, g.items.map((f) => f.id)])).toEqual([
      ["Owned", ["generic-pla-black", "generic-pla-brown"]],
      ["Bambu PETG HF", ["bambu-petg-hf-gray"]],
      ["Bambu PLA Basic", ["bambu-pla-basic-gray", "bambu-pla-basic-orange"]],
    ]);
    expect(pickerGroups(all, "gray pla").map((g) => g.items.map((f) => f.id))).toEqual([["bambu-pla-basic-gray"]]);
    expect(pickerGroups(all, "zzz")).toEqual([]);
  });

  it("is pending from a palette edit until a newer result arrives", () => {
    const m = (kind: string, at: string) => ({ id: at, kind, created_at: at }) as unknown as AnyMessage;
    expect(pendingPaletteEdit([m("result", "1"), m("palette_edit", "2")])?.id).toBe("2");
    expect(pendingPaletteEdit([m("result", "1"), m("palette_edit", "2"), m("result", "3")])).toBeNull();
    expect(pendingPaletteEdit([m("result", "1")])).toBeNull();
  });

  it("finds the slot nearest a color", () => {
    const hex = (id: string) => lib.find((f) => f.id === id)?.hex ?? null;
    expect(nearestSlot(draftFrom(usage), hex, "#8E7040")).toBe(0);
    expect(nearestSlot(draftFrom(usage), hex, "#F0F0F0")).toBe(1);
  });
});
