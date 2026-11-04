-- =====================================================================
-- SHIFT TRACKER — MIGRATION v2 (production hardening)
-- Run once in Supabase Dashboard → SQL Editor. Safe to re-run.
--
--  1. tracker_logs: full swipe list, missing-OUT count, swipe source
--  2. sync_status: heartbeat table used by admin.html (created if missing)
--  3. Integrity: valid states, non-negative durations, one row per user/day
--  4. Indexes for the close-out job, history and admin queries
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. tracker_logs — new columns written by greyt_sync.py v2
-- ---------------------------------------------------------------------
alter table public.tracker_logs
  add column if not exists swipes            jsonb   not null default '[]'::jsonb,
  add column if not exists missed_out_count  integer not null default 0,
  add column if not exists swipe_source      text;

comment on column public.tracker_logs.swipes is
  'Every swipe for the day in order: [{"type":"IN"|"OUT","time":"HH:MM:SS"}]';
comment on column public.tracker_logs.missed_out_count is
  'How many times an IN followed another IN (an OUT swipe was not recorded)';

-- ---------------------------------------------------------------------
-- 2. Integrity
-- ---------------------------------------------------------------------
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'tracker_logs_state_chk') then
    alter table public.tracker_logs
      add constraint tracker_logs_state_chk
      check (current_state in ('NOT_STARTED','ACTIVE','ON_BREAK','COMPLETED')) not valid;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'tracker_logs_nonneg_chk') then
    alter table public.tracker_logs
      add constraint tracker_logs_nonneg_chk
      check (coalesce(accumulated_active_ms,0) >= 0 and coalesce(accumulated_break_ms,0) >= 0) not valid;
  end if;
  -- Upserts rely on this. Most installs already have it; add only if missing.
  if not exists (
    select 1 from pg_index i
    join pg_class t on t.oid = i.indrelid
    where t.relname = 'tracker_logs' and i.indisunique
      and (select array_agg(a.attname order by a.attname)
             from pg_attribute a
            where a.attrelid = t.oid and a.attnum = any(i.indkey)) = array['date_tracked','user_id']::name[]
  ) then
    alter table public.tracker_logs
      add constraint tracker_logs_user_day_key unique (user_id, date_tracked);
  end if;
end $$;

-- ---------------------------------------------------------------------
-- 3. Indexes
-- ---------------------------------------------------------------------
create index if not exists tracker_logs_open_days_idx
  on public.tracker_logs (date_tracked)
  where current_state in ('ACTIVE','ON_BREAK');

create index if not exists tracker_logs_day_idx
  on public.tracker_logs (date_tracked);

-- ---------------------------------------------------------------------
-- 4. sync_status heartbeat (singleton row, id = true)
-- ---------------------------------------------------------------------
create table if not exists public.sync_status (
  id                       boolean primary key default true check (id),
  last_cycle_started_at    timestamptz,
  last_cycle_completed_at  timestamptz,
  users_attempted          integer not null default 0,
  users_synced             integer not null default 0,
  users_skipped            integer not null default 0,
  users_failed             integer not null default 0,
  failed_users             jsonb   not null default '[]'::jsonb,
  agent_host               text,
  agent_version            text
);

alter table public.sync_status add column if not exists last_cycle_started_at timestamptz;
alter table public.sync_status add column if not exists agent_host    text;
alter table public.sync_status add column if not exists agent_version text;

-- Role helper (SECURITY DEFINER so it doesn't re-enter profiles' RLS)
create or replace function public.my_role()
returns text
language sql
security definer
stable
set search_path = public
as $$
  select role from public.profiles where id = auth.uid();
$$;

alter table public.sync_status enable row level security;

-- Only admins can read the heartbeat; only the agent (service role, which
-- bypasses RLS) writes it.
drop policy if exists "sync_status_admin_read" on public.sync_status;
create policy "sync_status_admin_read" on public.sync_status
  for select to authenticated
  using (public.my_role() = 'admin');

-- ---------------------------------------------------------------------
-- 5. Make PostgREST see the changes immediately
-- ---------------------------------------------------------------------
notify pgrst, 'reload schema';
