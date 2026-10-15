// Drift guards: the hand-written mirrors in lib/contracts.ts and lib/db.types.ts must match the
// Python contracts and the migration.

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import {
  JOB_CAP_CREDITS,
  JOB_CAP_USD,
  MESSAGE_ROLES,
  NAME_TEXT_MAX,
  PRODUCT_DEFAULTS,
  PROFILES,
  STAGES,
  STAGE_STATUSES,
  WORKER_HEARTBEAT_S,
  WORKER_ONLINE_S,
} from "./contracts";
import {
  JOB_ACCESS,
  JOB_STATUSES,
  ORDER_STATUSES,
  PRODUCT_TYPES,
  PROFILE_VALUES,
  SPEND_VENDORS,
} from "./db.types";
import { SCHEMAS } from "./contracts.schemas.gen";

const repo = join(__dirname, "..", "..", "..");
const read = (p: string) => readFileSync(join(repo, p), "utf8");
const stagesPy = read("contracts/src/p2p_contracts/stages.py");
const payloadsPy = read("contracts/src/p2p_contracts/payloads.py");
const jobPy = read("contracts/src/p2p_contracts/job.py");
// All migrations in order; later ones replace earlier check constraints.
const migrations = readdirSync(join(repo, "supabase/migrations"))
  .filter((f) => f.endsWith(".sql"))
  .sort()
  .map((f) => read(`supabase/migrations/${f}`));
const sql = migrations.join("\n");

function sqlEnum(name: string): string[] {
  const m = new RegExp(`create type public\\.${name} as enum \\(([^;]+)\\);`).exec(sql);
  if (!m) throw new Error(`enum ${name} not found`);
  return [...m[1].matchAll(/'([^']+)'/g)].map((x) => x[1]);
}

/** Values of the last `check (<column> in (...))` in migration order, optionally within one table. */
function sqlCheck(column: string, table?: string): string[] {
  const src = table
    ? (new RegExp(`create table public\\.${table} \\(([\\s\\S]+?)\\n\\);`).exec(sql)?.[1] ?? "")
    : sql;
  const all = [...src.matchAll(new RegExp(`check \\(${column} in\\s*\\(([^)]+)\\)\\)`, "g"))];
  const m = all.at(-1);
  if (!m) throw new Error(`check on ${column} not found`);
  return [...m[1].matchAll(/'([^']+)'/g)].map((x) => x[1]);
}

const pyNum = (name: string) => Number(new RegExp(`^${name} = ([0-9.]+)`, "m").exec(jobPy)?.[1]);

describe("stages.py", () => {
  it("has the same stages, numbers, names and gates", () => {
    const re =
      /StageInfo\(\s*id=S\.(\w+),\s*number=(\d+),\s*name="([^"]+)",\s*gate=Gate\.(\w+)/g;
    const py = [...stagesPy.matchAll(re)].map((m) => ({
      enumName: m[1],
      number: Number(m[2]),
      name: m[3],
      gate: m[4].toLowerCase(),
    }));
    expect(py).toHaveLength(10);
    py.forEach((p, i) => {
      expect(STAGES[i].number).toBe(p.number);
      expect(STAGES[i].name).toBe(p.name);
      expect(STAGES[i].gate).toBe(p.gate);
      const value = new RegExp(`${p.enumName} = "([^"]+)"`).exec(stagesPy)?.[1];
      expect(STAGES[i].id).toBe(value);
    });
  });

  it("has the same thin slice profile", () => {
    const m = /PROFILE_THIN_SLICE[^=]*=\s*\(([^)]+)\)/.exec(stagesPy)!;
    const names = [...m[1].matchAll(/S\.(\w+)/g)].map((x) => x[1]);
    const ids = names.map((n) => new RegExp(`${n} = "([^"]+)"`).exec(stagesPy)![1]);
    expect(PROFILES.thin_slice).toEqual(ids);
  });
});

describe("payloads.py MESSAGE_PAYLOADS", () => {
  it("has the same kinds and roles", () => {
    const block = /MESSAGE_PAYLOADS: dict[^{]*\{([^}]+)\}/.exec(payloadsPy)![1];
    const py = Object.fromEntries(
      [...block.matchAll(/"(\w+)":\s*\("(\w+)"/g)].map((m) => [m[1], m[2]]),
    );
    expect(MESSAGE_ROLES).toEqual(py);
  });

  it("has a generated schema for every kind with a payload", () => {
    const withPayload = Object.keys(MESSAGE_ROLES).filter((k) => k !== "prompt");
    expect(Object.keys(SCHEMAS).map((k) => k.replace(/^msg_/, "")).sort()).toEqual(
      withPayload.sort(),
    );
  });
});

describe("migration", () => {
  it("enums match", () => {
    expect(sqlEnum("stage_id")).toEqual(STAGES.map((s) => s.id));
    expect(sqlEnum("stage_status")).toEqual(STAGE_STATUSES);
    expect(sqlEnum("job_status")).toEqual(JOB_STATUSES);
  });

  it("check constraints match", () => {
    expect(sqlCheck("product_type")).toEqual(PRODUCT_TYPES);
    expect(sqlCheck("product_type", "orders")).toEqual(PRODUCT_TYPES);
    expect(sqlCheck("status", "orders")).toEqual(ORDER_STATUSES);
    expect(sqlCheck("access")).toEqual(JOB_ACCESS);
    expect(sqlCheck("profile")).toEqual(PROFILE_VALUES);
    expect(sqlCheck("vendor")).toEqual(SPEND_VENDORS);
    expect(sqlCheck("kind")).toEqual(Object.keys(MESSAGE_ROLES));
  });

  it("orders limits match the order form", () => {
    expect(sql).toMatch(/name_text\s+text check \(char_length\(name_text\) <= 24\)/);
    expect(sql).toMatch(/15728640/);
  });
});

describe("job.py", () => {
  it("has the same caps, heartbeat and text limit", () => {
    expect(pyNum("JOB_CAP_USD")).toBe(JOB_CAP_USD);
    expect(pyNum("JOB_CAP_CREDITS")).toBe(JOB_CAP_CREDITS);
    expect(pyNum("WORKER_HEARTBEAT_S")).toBe(WORKER_HEARTBEAT_S);
    expect(pyNum("WORKER_ONLINE_S")).toBe(WORKER_ONLINE_S);
    expect(jobPy).toMatch(new RegExp(`text: str = Field\\(min_length=1, max_length=${NAME_TEXT_MAX}\\)`));
  });

  it("has the same product types and PRODUCT_DEFAULTS", () => {
    const kinds = Object.fromEntries(
      [...jobPy.matchAll(/^\s+([A-Z_]+) = "([a-z_]+)"/gm)].map((m) => [m[1], m[2]]),
    );
    const block = /PRODUCT_DEFAULTS[^{]*\{([\s\S]+?)\n\}/.exec(jobPy)![1];
    const py: Record<string, { target_size_mm: number; addons: string[] }> = {};
    for (const m of block.matchAll(/ProductType\.(\w+): \{\s*"target_size_mm": ([0-9.]+), "addons": \[([^\]]*)\]/g)) {
      py[kinds[m[1]]] = {
        target_size_mm: Number(m[2]),
        addons: [...m[3].matchAll(/AddOnKind\.(\w+)/g)].map((a) => kinds[a[1]]),
      };
    }
    expect(Object.keys(py).sort()).toEqual([...PRODUCT_TYPES].sort());
    expect(py).toEqual(PRODUCT_DEFAULTS);
  });
});
