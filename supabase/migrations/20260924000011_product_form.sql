-- Product form and custom asks on jobs (contracts JobRecord.form, JobRecord.custom_asks).
-- form: a keychain or magnet is a flat charm or a 3D figure, the customer's choice; the new-job
-- form may set it (then intake does not ask), and s01 writes the decision back. Every other
-- product is a 3D figure. custom_asks: props, outfits, poses and names from the request and the
-- answers (contracts CustomAsk), written by s01.
-- The worker writes both columns in their own PATCH and stops trying if the project lacks them,
-- so it runs before and after this migration.
alter table public.jobs
  add column if not exists form text check (form in ('flat', 'figure')),
  add column if not exists custom_asks jsonb not null default '[]'::jsonb;
