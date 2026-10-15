-- photo2print initial schema.
-- Contract owners: worker writes jobs progress, stage_runs, assistant messages, spend, artifacts.
-- The web app (authenticated user) creates jobs, uploads photos, writes user messages, edits filaments.
-- The worker uses the service role and bypasses RLS. Enum values mirror contracts/p2p_contracts.

create extension if not exists pgcrypto;

-- ------------------------------------------------------------------ enums
create type public.stage_id as enum (
  's01_intake', 's02_color_spec', 's03_concept', 's04_multiview', 's05_mesh',
  's06_printability', 's07_baseline', 's08_palette', 's09_paint', 's10_write'
);
create type public.stage_status as enum (
  'pending', 'running', 'waiting_user', 'waiting_vendor', 'succeeded', 'failed', 'paused',
  'skipped', 'stale'
);
create type public.job_status as enum (
  'draft', 'queued', 'running', 'waiting_user', 'paused', 'review', 'done', 'failed', 'cancelled'
);

-- ------------------------------------------------------------------ jobs
create table public.jobs (
  id              uuid primary key default gen_random_uuid(),
  owner_id        uuid not null default auth.uid() references auth.users (id) on delete cascade,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  title           text,
  prompt          text not null,
  product_type    text not null default 'figurine'
                  check (product_type in ('figurine', 'keychain', 'ornament', 'other')),
  profile         text not null default 'thin_slice' check (profile in ('thin_slice', 'full')),
  status          public.job_status not null default 'queued',
  stage           public.stage_id,
  target_size_mm  real,
  settings        jsonb not null default '{}'::jsonb,   -- contracts.job.PrintSettings
  photos          jsonb not null default '[]'::jsonb,   -- [{path, sha256?, subject_ids?}]
  budget_usd      numeric(8, 2) not null default 10 check (budget_usd >= 0),
  budget_credits  integer not null default 400 check (budget_credits >= 0),
  spent_usd       numeric(10, 4) not null default 0,
  spent_credits   integer not null default 0,
  confidence      real check (confidence between 0 and 1),
  review_reason   text,
  pause_reason    text,
  result          jsonb                                  -- contracts.payloads.JobResult
);
create index jobs_owner_created_idx on public.jobs (owner_id, created_at desc);
create index jobs_active_idx on public.jobs (status)
  where status in ('queued', 'running', 'waiting_user', 'paused', 'review');

-- ------------------------------------------------------------------ stage_runs
create table public.stage_runs (
  id           uuid primary key default gen_random_uuid(),
  job_id       uuid not null references public.jobs (id) on delete cascade,
  owner_id     uuid not null references auth.users (id) on delete cascade,
  stage        public.stage_id not null,
  attempt      integer not null check (attempt >= 1),
  status       public.stage_status not null default 'pending',
  started_at   timestamptz,
  finished_at  timestamptz,
  usd          numeric(10, 4) not null default 0,
  credits      integer not null default 0,
  confidence   real,
  manifest     jsonb not null default '{}'::jsonb,       -- contracts.manifest.Manifest
  updated_at   timestamptz not null default now(),
  unique (job_id, stage, attempt)
);
create index stage_runs_owner_idx on public.stage_runs (owner_id);

-- ------------------------------------------------------------------ messages
create table public.messages (
  id           uuid primary key default gen_random_uuid(),
  job_id       uuid not null references public.jobs (id) on delete cascade,
  owner_id     uuid not null default auth.uid() references auth.users (id) on delete cascade,
  created_at   timestamptz not null default now(),
  role         text not null check (role in ('user', 'assistant', 'system')),
  kind         text not null check (kind in (
                 'prompt', 'questions', 'answers', 'concepts', 'concept_pick', 'tweak',
                 'tweak_decision', 'mesh_review', 'mesh_decision', 'result', 'review_request',
                 'review_decision', 'note')),
  stage        public.stage_id,
  body         text,
  payload      jsonb not null default '{}'::jsonb,       -- contracts.payloads.MESSAGE_PAYLOADS
  reply_to     uuid references public.messages (id) on delete set null,
  consumed_at  timestamptz                                -- set by the worker when acted on
);
create index messages_job_created_idx on public.messages (job_id, created_at);
create index messages_owner_idx on public.messages (owner_id);
create index messages_reply_to_idx on public.messages (reply_to);
create index messages_unconsumed_idx on public.messages (created_at)
  where role = 'user' and consumed_at is null;

-- ------------------------------------------------------------------ filaments
create table public.filaments (
  id                 text primary key,                   -- slug, contracts.filaments.Filament.id
  owner_id           uuid not null default auth.uid() references auth.users (id) on delete cascade,
  brand              text not null,
  line               text not null,
  name               text not null,
  material           text not null default 'PLA',
  hex                text not null check (hex ~ '^#[0-9A-Fa-f]{6}$'),
  lab_l              real not null,
  lab_a              real not null,
  lab_b              real not null,
  lab_source         text not null default 'nominal' check (lab_source in ('nominal', 'measured')),
  td_mm              real,
  owned              boolean not null default true,
  spools             integer not null default 1,
  ams_slot           integer check (ams_slot between 1 and 16),
  swatch_path        text,
  bambu_filament_id  text,
  slicer_preset      text,
  density_g_cm3      real not null default 1.24,
  notes              text,
  updated_at         timestamptz not null default now()
);
create index filaments_owner_idx on public.filaments (owner_id);

-- ------------------------------------------------------------------ spend ledger
create table public.spend (
  id          bigint generated always as identity primary key,
  owner_id    uuid references auth.users (id) on delete cascade,
  at          timestamptz not null default now(),
  job_id      uuid references public.jobs (id) on delete set null,
  stage       public.stage_id,
  vendor      text not null check (vendor in ('anthropic', 'meshy', 'runpod')),
  kind        text not null,
  usd         numeric(10, 4) not null default 0,
  credits     integer not null default 0,
  ref         text,
  test_mode   boolean not null default false
);
create index spend_owner_at_idx on public.spend (owner_id, at desc);
create index spend_job_idx on public.spend (job_id);

create view public.spend_by_vendor with (security_invoker = true) as
  select vendor, sum(usd) as usd, sum(credits) as credits, count(*) as events
  from public.spend where not test_mode group by vendor;

-- ------------------------------------------------------------------ vendor webhook inbox
-- Written only by the meshy-webhook edge function (service role). The worker subscribes.
create table public.meshy_events (
  id            bigint generated always as identity primary key,
  received_at   timestamptz not null default now(),
  task_id       text not null,
  status        text,
  event         jsonb not null,
  processed_at  timestamptz
);
create index meshy_events_task_idx on public.meshy_events (task_id);
create index meshy_events_unprocessed_idx on public.meshy_events (received_at)
  where processed_at is null;

-- ------------------------------------------------------------------ worker heartbeat
create table public.workers (
  id          text primary key,
  last_seen   timestamptz not null default now(),
  version     text,
  meshy_mode  text check (meshy_mode in ('test', 'live')),
  info        jsonb not null default '{}'::jsonb
);

-- ------------------------------------------------------------------ updated_at
create or replace function public.touch_updated_at() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.updated_at := now();
  return new;
end $$;
create trigger jobs_touch before update on public.jobs
  for each row execute function public.touch_updated_at();
create trigger stage_runs_touch before update on public.stage_runs
  for each row execute function public.touch_updated_at();
create trigger filaments_touch before update on public.filaments
  for each row execute function public.touch_updated_at();

-- ------------------------------------------------------------------ RLS
alter table public.jobs         enable row level security;
alter table public.stage_runs   enable row level security;
alter table public.messages     enable row level security;
alter table public.filaments    enable row level security;
alter table public.spend        enable row level security;
alter table public.meshy_events enable row level security;  -- no policies: service role only
alter table public.workers      enable row level security;

create policy jobs_select on public.jobs for select to authenticated
  using ((select auth.uid()) = owner_id);
create policy jobs_insert on public.jobs for insert to authenticated
  with check ((select auth.uid()) = owner_id and status in ('draft', 'queued'));
create policy jobs_update on public.jobs for update to authenticated
  using ((select auth.uid()) = owner_id) with check ((select auth.uid()) = owner_id);

create policy stage_runs_select on public.stage_runs for select to authenticated
  using ((select auth.uid()) = owner_id);

create policy messages_select on public.messages for select to authenticated
  using ((select auth.uid()) = owner_id);
create policy messages_insert on public.messages for insert to authenticated
  with check (
    (select auth.uid()) = owner_id
    and role = 'user'
    and exists (select 1 from public.jobs j
                where j.id = job_id and j.owner_id = (select auth.uid()))
  );

create policy filaments_all on public.filaments for all to authenticated
  using ((select auth.uid()) = owner_id) with check ((select auth.uid()) = owner_id);

create policy spend_select on public.spend for select to authenticated
  using ((select auth.uid()) = owner_id);

create policy workers_select on public.workers for select to authenticated using (true);

-- ------------------------------------------------------------------ realtime
alter publication supabase_realtime add table
  public.jobs, public.stage_runs, public.messages, public.meshy_events, public.workers;

-- ------------------------------------------------------------------ storage
-- inputs:    <job_id>/input/photo_<n>.<ext>    user uploads, worker reads
-- artifacts: <job_id>/<stage>/a<N>/<file>      worker writes, user reads
-- swatches:  <owner_id>/<filament_id>.<ext>    user uploads
insert into storage.buckets (id, name, public) values
  ('inputs', 'inputs', false), ('artifacts', 'artifacts', false), ('swatches', 'swatches', false)
on conflict (id) do nothing;

create or replace function public.owns_job_folder(object_name text) returns boolean
language sql stable security invoker set search_path = '' as $$
  select exists (
    select 1 from public.jobs j
    where j.id::text = (storage.foldername(object_name))[1]
      and j.owner_id = (select auth.uid())
  );
$$;

create policy inputs_rw on storage.objects for all to authenticated
  using (bucket_id = 'inputs' and public.owns_job_folder(name))
  with check (bucket_id = 'inputs' and public.owns_job_folder(name));
create policy artifacts_read on storage.objects for select to authenticated
  using (bucket_id = 'artifacts' and public.owns_job_folder(name));
create policy swatches_rw on storage.objects for all to authenticated
  using (bucket_id = 'swatches' and (storage.foldername(name))[1] = (select auth.uid())::text)
  with check (bucket_id = 'swatches' and (storage.foldername(name))[1] = (select auth.uid())::text);
