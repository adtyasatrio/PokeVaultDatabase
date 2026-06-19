-- Dev-only Pokemon Price Tracker Edge Function support tables.
-- API key values must stay in Supabase Secrets; this table stores names/status only.

create table if not exists public.ppt_api_keys (
  id bigserial primary key,
  key_name text not null unique,
  key_secret_name text not null,
  status text not null default 'active'
    check (status in ('active', 'rate_limited', 'disabled')),
  daily_remaining integer,
  credits_used_today integer not null default 0,
  credit_window_date date not null default current_date,
  last_used_at timestamptz,
  rate_limited_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_ppt_api_keys_status
  on public.ppt_api_keys (status, credit_window_date);

create table if not exists public.ppt_edge_runs (
  id uuid primary key default gen_random_uuid(),
  job_name text not null,
  language text not null check (language in ('english', 'japanese')),
  status text not null check (status in ('running', 'completed', 'stopped', 'failed')),
  key_name text,
  cards_upserted integer not null default 0,
  cards_skipped integer not null default 0,
  cards_failed integer not null default 0,
  credits_used integer not null default 0,
  sets_upserted integer not null default 0,
  keys_rate_limited integer not null default 0,
  email_sent_at timestamptz,
  email_error text,
  params jsonb not null default '{}'::jsonb,
  error_message text,
  started_at timestamptz not null default now(),
  finished_at timestamptz
);

create index if not exists idx_ppt_edge_runs_started
  on public.ppt_edge_runs (started_at desc);

alter table public.ppt_edge_runs
  add column if not exists cards_skipped integer not null default 0,
  add column if not exists cards_failed integer not null default 0,
  add column if not exists keys_rate_limited integer not null default 0,
  add column if not exists email_sent_at timestamptz,
  add column if not exists email_error text;

alter table public.ppt_api_keys enable row level security;
alter table public.ppt_edge_runs enable row level security;
