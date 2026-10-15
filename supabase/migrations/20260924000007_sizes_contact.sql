-- Size tiers (xs, s, m, l) on jobs and orders; orders need email or phone, not both; quantity moves
-- to checkout (column kept, default 1, not asked on the order form).
alter table public.jobs add column size_tier text check (size_tier in ('xs', 's', 'm', 'l'));
alter table public.orders add column size_tier text check (size_tier in ('xs', 's', 'm', 'l'));
alter table public.orders alter column email drop not null;
alter table public.orders drop constraint if exists orders_email_check;
alter table public.orders add constraint orders_email_check
  check (email is null or email ~* '^[^@\s]+@[^@\s]+\.[^@\s]+$');
alter table public.orders add constraint orders_contact_check check (email is not null or phone is not null);
alter table public.orders alter column request drop not null;
alter table public.orders drop constraint if exists orders_request_check;
alter table public.orders add constraint orders_request_check
  check (request is null or char_length(request) <= 2000);
alter table public.orders add constraint orders_other_needs_request
  check (product_type <> 'other' or (request is not null and char_length(request) >= 10));
