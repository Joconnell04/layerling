-- Claude runs on the subscription (usd 0); keep the list-price equivalent and how it was billed.
alter table public.spend
  add column usd_list numeric(10,4) not null default 0,
  add column billing text not null default 'api'
    check (billing in ('api', 'subscription', 'credits', 'gpu_seconds', 'free'));
drop view if exists public.spend_by_vendor;
create view public.spend_by_vendor with (security_invoker = true) as
  select vendor, billing, sum(usd) as usd, sum(usd_list) as usd_list, sum(credits) as credits, count(*) as events
  from public.spend where not test_mode group by vendor, billing;
