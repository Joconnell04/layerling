// Seed data for mock mode. Jobs are produced by running the simulated worker on a virtual clock
// with scripted user replies, so seeded rows have the same shape as live ones.

import type { UserPayloadKind } from "../../contracts";
import type { MessagePayloadMap } from "../../contracts.gen";
import { filamentSlug, hexToLab } from "../../color";
import { addonsFor } from "../../products";
import type { AnyMessage, FilamentRow, JobRow, MessageRow } from "../../db.types";
import { asAny, buildJob, buildPromptMessage, buildUserMessage, type JobDraft } from "./build";
import { MOCK_USER_ID, type MockStore } from "./store";
import { MockWorker } from "./worker";

// Jackson's current Bambu Studio presets (from the orchestrator). Color names and AMS slots are
// ASSUMED from the hex values; edit them on the Filaments screen.
const PRESETS: [brand: string, line: string, material: string, name: string, hex: string, slot: number | null][] = [
  ["Generic", "PLA", "PLA", "Black", "#161616", 1],
  ["Generic", "PLA", "PLA", "Brown", "#7C4B00", 2],
  ["Bambu Lab", "PLA Basic", "PLA", "Red", "#F72323", 3],
  ["Generic", "PETG", "PETG", "Blue", "#0D6284", null],
  ["Generic", "PLA", "PLA", "White", "#FFFFFF", 4],
];

// Catalog colors (owned = false), the way the shared catalog rows look in the filaments table.
// Hex values are Bambu Studio's (filaments/vendor/bambu/filaments_color_codes.json).
const CATALOG: [brand: string, line: string, material: string, name: string, hex: string][] = [
  ["Bambu Lab", "PLA Basic", "PLA", "Gray", "#8E9089"],
  ["Bambu Lab", "PLA Basic", "PLA", "Jade White", "#FFFFFF"],
  ["Bambu Lab", "PLA Basic", "PLA", "Orange", "#FF6A13"],
  ["Bambu Lab", "PLA Matte", "PLA", "Charcoal", "#000000"],
  ["Bambu Lab", "PETG HF", "PETG", "Gray", "#ADB1B2"],
];

type SeedRow = [brand: string, line: string, material: string, name: string, hex: string, slot: number | null, owned: boolean];

export function seedFilaments(store: MockStore, at: string): void {
  const rows: SeedRow[] = [
    ...PRESETS.map(([b, l, m, n, h, slot]): SeedRow => [b, l, m, n, h, slot, true]),
    ...CATALOG.map(([b, l, m, n, h]): SeedRow => [b, l, m, n, h, null, false]),
  ];
  rows.forEach(([brand, line, material, name, hex, slot, owned]) => {
    const [L, a, b] = hexToLab(hex);
    const row: FilamentRow = {
      id: filamentSlug(brand, material, line, name),
      owner_id: owned ? MOCK_USER_ID : null,
      brand,
      line,
      name,
      material,
      hex,
      lab_l: L,
      lab_a: a,
      lab_b: b,
      lab_source: "nominal",
      td_mm: null,
      owned,
      spools: owned ? 1 : 0,
      ams_slot: slot,
      swatch_path: null,
      bambu_filament_id: null,
      slicer_preset: brand === "Bambu Lab" ? `Bambu ${line}` : `${brand} ${material}`,
      density_g_cm3: material === "PETG" ? 1.27 : 1.24,
      notes: null,
      updated_at: at,
    };
    store.putFilament(row);
  });
}

/** Discrete-event scheduler on a virtual clock. */
class VirtualClock {
  t = 0;
  private seq = 0;
  private queue: { at: number; seq: number; fn: () => void }[] = [];

  schedule = (fn: () => void, ms: number) => {
    this.queue.push({ at: this.t + ms, seq: this.seq++, fn });
  };

  drain(): void {
    while (this.queue.length) {
      this.queue.sort((a, b) => a.at - b.at || a.seq - b.seq);
      const next = this.queue.shift()!;
      this.t = Math.max(this.t, next.at);
      next.fn();
    }
  }
}

const MIN = 60_000;
const HOUR = 60 * MIN;

export function seedStore(store: MockStore): void {
  const realNow = Date.now();
  const clock = new VirtualClock();
  store.deterministicIds = true;
  store.now = () => new Date(clock.t);
  seedFilaments(store, new Date(realNow - 7 * 24 * HOUR).toISOString());

  const worker = new MockWorker(store, { stepMs: 20_000, schedule: clock.schedule });

  const create = (
    agoMs: number,
    d: Omit<JobDraft, "photos" | "addons">,
    photos: number,
    nameText?: string,
  ): JobRow => {
    clock.t = realNow - agoMs;
    const draft: JobDraft = {
      ...d,
      addons: addonsFor(d.productType, nameText),
      photos: Array.from({ length: photos }, (_, i) => ({ path: `input/photo_${i + 1}.svg` })),
    };
    const job = store.putJob(buildJob(store, MOCK_USER_ID, draft));
    store.putMessage(asAny(buildPromptMessage(store, job)));
    worker.onJobCreated(job.id);
    clock.drain();
    return job;
  };

  const last = <K extends AnyMessage["kind"]>(jobId: string, kind: K) =>
    store
      .messagesFor(jobId)
      .filter((m) => m.kind === kind)
      .at(-1) as MessageRow<K> | undefined;

  const reply = <K extends UserPayloadKind>(
    jobId: string,
    kind: K,
    payload: MessagePayloadMap[K],
    replyTo?: string,
  ) => {
    clock.t += 45_000;
    const job = store.jobs.get(jobId)!;
    const msg = buildUserMessage(store, job, kind, payload, { replyTo });
    store.putMessage(asAny(msg));
    worker.onUserMessage(asAny(msg));
    clock.drain();
  };

  /** Answer every question with its first option, or the option named in `picks`. */
  const answerFirst = (jobId: string, picks: Record<string, string> = {}) => {
    const q = last(jobId, "questions")!;
    reply(
      jobId,
      "answers",
      {
        answers: q.payload.questions.map((x) => ({
          question_id: x.id,
          option_ids: [picks[x.id] ?? x.options[0].id],
          text: null,
        })),
      },
      q.id,
    );
  };

  const approveConcept = (jobId: string, n: 1 | 2) => {
    const c = last(jobId, "concepts")!;
    reply(jobId, "concept_pick", { candidate_id: c.payload.candidates[n - 1].id, approve: true }, c.id);
  };

  const mesh = (jobId: string, action: "approve" | "retexture" | "regenerate", text?: string) => {
    const m = last(jobId, "mesh_review")!;
    reply(jobId, "mesh_decision", { action, text: text ?? null }, m.id);
  };

  // A: done, thin slice keychain with a name plate: a flat charm, picked at intake.
  const a = create(
    4 * 24 * HOUR,
    {
      prompt: "Keychain of me and my dog Biscuit hugging",
      productType: "keychain",
      targetSizeMm: 45,
      profile: "thin_slice",
    },
    2,
    "BISCUIT",
  );
  answerFirst(a.id, { form: "flat" });
  approveConcept(a.id, 2);
  mesh(a.id, "approve");

  // G: done, full profile 3D keychain (the request says 3D): paint went to review, approved, then
  // s10 shipped the proof set (model, paint and simulated print from all eight views).
  const g = create(
    2 * 24 * HOUR,
    {
      prompt: "3D keychain of our dog Olive sitting, wearing her red collar",
      productType: "keychain",
      targetSizeMm: 45,
      profile: "full",
    },
    2,
  );
  answerFirst(g.id);
  approveConcept(g.id, 1);
  mesh(g.id, "approve");
  {
    const r = last(g.id, "review_request")!;
    reply(g.id, "review_decision", { action: "approve", notes: null }, r.id);
  }

  // H: a 3D keychain waiting on the 3D model: the page shows it from all eight views next to the
  // approved concept before the paint credits are spent.
  const h = create(
    60 * MIN,
    {
      prompt: "Keychain of Mochi sitting with a ball",
      productType: "keychain",
      targetSizeMm: 45,
      profile: "thin_slice",
    },
    1,
  );
  answerFirst(h.id, { form: "figure" });
  approveConcept(h.id, 1);

  // I: a flat charm waiting on the concept pick (the product concept, the figure and the check).
  const i = create(
    90 * MIN,
    {
      prompt: "Flat keychain of our pup Juniper sitting",
      productType: "keychain",
      targetSizeMm: 45,
      profile: "thin_slice",
    },
    1,
  );
  answerFirst(i.id);

  // B: full profile, one tweak, one retexture, low paint confidence -> review queue.
  const b = create(
    26 * HOUR,
    {
      prompt: "Figurine of Miso the tabby cat sitting on a cushion",
      productType: "figurine",
      targetSizeMm: 100,
      profile: "full",
    },
    3,
  );
  answerFirst(b.id);
  {
    const c = last(b.id, "concepts")!;
    reply(b.id, "concept_pick", { candidate_id: "r1c1", approve: false }, c.id);
    reply(b.id, "tweak", { text: "Bigger ears, rounder cushion", candidate_id: "r1c1" }, c.id);
  }
  approveConcept(b.id, 1);
  mesh(b.id, "retexture", "Warmer stripes on the back");
  mesh(b.id, "approve");

  // C: paused on the credit cap after repeated regenerations.
  const c = create(
    6 * HOUR,
    {
      prompt: "Three chickens on a fence",
      productType: "figurine",
      targetSizeMm: 90,
      profile: "thin_slice",
    },
    1,
  );
  answerFirst(c.id);
  approveConcept(c.id, 1);
  for (let i = 0; i < 12 && store.jobs.get(c.id)!.status === "waiting_user"; i++) {
    mesh(c.id, "regenerate", "Separate the legs");
  }

  // D: waiting on a concept pick.
  const d = create(
    2 * HOUR,
    {
      prompt: "Ornament of my daughter in a winter hat",
      productType: "ornament",
      targetSizeMm: 75,
      profile: "thin_slice",
    },
    2,
  );
  answerFirst(d.id);

  // F: nameplate with raised name, waiting on the 3D model approval.
  const f = create(
    3 * HOUR,
    {
      prompt: "Nameplate for Pepper's crate with her figurine sitting on it",
      productType: "nameplate",
      targetSizeMm: 120,
      profile: "thin_slice",
    },
    2,
    "PEPPER",
  );
  answerFirst(f.id);
  approveConcept(f.id, 2);

  // E: waiting on intake answers.
  create(
    20 * MIN,
    {
      prompt: "Desk figure of me and my partner with our bikes",
      productType: "figurine",
      targetSizeMm: 110,
      profile: "full",
    },
    3,
  );

  // J: a keychain waiting on intake, with the form question (the request does not say).
  create(
    10 * MIN,
    {
      prompt: "Keychain of our cat Pip",
      productType: "keychain",
      targetSizeMm: 45,
      profile: "thin_slice",
    },
    1,
  );

  store.deterministicIds = false;
  store.now = () => new Date();
}
