import { missingOptionalColumn, withoutOptionalColumns } from "./normalize";

describe("optional job columns (migration 20260924000011)", () => {
  it("knows PostgREST's missing column error and retries without form and custom_asks", () => {
    expect(missingOptionalColumn({ code: "PGRST204", message: "Could not find the 'form' column of 'jobs' in the schema cache" })).toBe(true);
    expect(missingOptionalColumn({ message: 'column "custom_asks" of relation "jobs" does not exist' })).toBe(true);
    expect(missingOptionalColumn({ code: "23514", message: 'new row violates check constraint "jobs_form_check"' })).toBe(false);
    expect(missingOptionalColumn({ code: "PGRST204", message: "Could not find the 'title' column" })).toBe(false);
    expect(missingOptionalColumn(null)).toBe(false);
    expect(withoutOptionalColumns({ prompt: "p", form: "flat", custom_asks: [] })).toEqual({ prompt: "p" });
  });
});
