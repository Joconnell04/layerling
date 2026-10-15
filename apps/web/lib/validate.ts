// Validate message payloads against the generated JSON Schemas (contracts payloads.MESSAGE_PAYLOADS).
// The web validates user payloads before insert, and checks assistant payloads on read.

import Ajv2020, { type ValidateFunction } from "ajv/dist/2020";
import { SCHEMAS } from "./contracts.schemas.gen";
import type { MessageKind } from "./contracts";

const ajv = new Ajv2020({ strict: false, allErrors: true });
const cache = new Map<string, ValidateFunction>();

function validator(kind: string): ValidateFunction | null {
  const key = `msg_${kind}`;
  if (!(key in SCHEMAS)) return null;
  let fn = cache.get(key);
  if (!fn) {
    fn = ajv.compile(structuredClone(SCHEMAS[key as keyof typeof SCHEMAS]) as object);
    cache.set(key, fn);
  }
  return fn;
}

export interface ValidationResult {
  ok: boolean;
  errors: string[];
}

export function validatePayload(kind: MessageKind, payload: unknown): ValidationResult {
  if (kind === "prompt") {
    const empty =
      payload && typeof payload === "object" && Object.keys(payload as object).length === 0;
    return empty ? { ok: true, errors: [] } : { ok: false, errors: ["prompt payload must be {}"] };
  }
  const fn = validator(kind);
  if (!fn) return { ok: false, errors: [`unknown kind ${kind}`] };
  const ok = fn(payload) as boolean;
  return {
    ok,
    errors: ok
      ? []
      : (fn.errors ?? []).map((e) => `${e.instancePath || "/"} ${e.message ?? "invalid"}`),
  };
}

export class PayloadError extends Error {
  constructor(
    public kind: string,
    public errors: string[],
  ) {
    super(`Invalid ${kind} payload: ${errors.join("; ")}`);
  }
}

export function assertPayload(kind: MessageKind, payload: unknown): void {
  const r = validatePayload(kind, payload);
  if (!r.ok) throw new PayloadError(kind, r.errors);
}
