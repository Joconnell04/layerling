-- Product types and exact-geometry add-ons (keychain loop, nameplate text, magnet pocket).
alter table public.jobs drop constraint jobs_product_type_check;
alter table public.jobs add constraint jobs_product_type_check
  check (product_type in ('figurine', 'keychain', 'nameplate', 'ornament', 'magnet', 'other'));
alter table public.jobs add column addons jsonb not null default '[]'::jsonb;  -- contracts.job.AddOn[]
