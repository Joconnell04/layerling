-- Public ordering and access-code sessions.
-- Visitors either place an order (emailed to Jackson) or enter the access code to use the studio.
-- Both paths run through Next.js server routes with the service role, so no anon policies exist.
-- Jobs created from an access-code session have no auth user, so owner_id becomes nullable.
alter table public.jobs       alter column owner_id drop not null;
alter table public.stage_runs alter column owner_id drop not null;
alter table public.messages   alter column owner_id drop not null;
alter table public.filaments  alter column owner_id drop not null;
alter table public.jobs add column access text not null default 'owner'
  check (access in ('owner', 'code'));   -- 'code' = created from an access-code session

create table public.orders (
  id            uuid primary key default gen_random_uuid(),
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  status        text not null default 'new'
                check (status in ('new', 'contacted', 'quoted', 'accepted', 'declined', 'done', 'spam')),
  name          text not null check (char_length(name) between 1 and 120),
  email         text not null check (email ~* '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
  phone         text,
  product_type  text not null check (product_type in
                  ('figurine', 'keychain', 'nameplate', 'ornament', 'magnet', 'other')),
  size_mm       real,
  quantity      integer not null default 1 check (quantity between 1 and 100),
  name_text     text check (char_length(name_text) <= 24),
  request       text not null check (char_length(request) between 1 and 2000),
  photos        jsonb not null default '[]'::jsonb,   -- storage keys in bucket `orders`
  source        text,                                 -- e.g. flyer, web
  notified_at   timestamptz,
  notify_error  text,
  job_id        uuid references public.jobs (id) on delete set null
);
create index orders_created_idx on public.orders (created_at desc);
create index orders_job_idx on public.orders (job_id);
create trigger orders_touch before update on public.orders
  for each row execute function public.touch_updated_at();
alter table public.orders enable row level security;   -- service role only

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('orders', 'orders', false, 15728640, array['image/jpeg', 'image/png', 'image/heic', 'image/webp'])
on conflict (id) do nothing;
