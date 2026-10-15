-- Shared spools (owner_id null, e.g. the studio's AMS) are readable by any logged-in user;
-- only owners write their own rows.
drop policy if exists filaments_all on public.filaments;
create policy filaments_select on public.filaments for select to authenticated
  using (owner_id is null or (select auth.uid()) = owner_id);
create policy filaments_write on public.filaments for all to authenticated
  using ((select auth.uid()) = owner_id) with check ((select auth.uid()) = owner_id);
