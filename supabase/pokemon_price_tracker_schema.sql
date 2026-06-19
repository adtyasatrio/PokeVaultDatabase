-- Pokemon Price Tracker staging tables.
-- Run this once in Supabase SQL Editor before running
-- scraper/scrape_pokemonpricetracker.py.

create extension if not exists pgcrypto;

create table if not exists public.ppt_sets (
  id bigserial primary key,
  language text not null check (language in ('english', 'japanese')),
  ppt_id text,
  tcg_player_id text,
  tcg_player_numeric_id integer,
  name text not null,
  series text,
  release_date date,
  card_count integer,
  image_cdn_url text,
  image_cdn_url_200 text,
  image_cdn_url_400 text,
  image_cdn_url_800 text,
  image_url text,
  price_guide_url text,
  has_price_guide boolean,
  no_price_guide_reason text,
  api_created_at timestamptz,
  api_updated_at timestamptz,
  raw jsonb not null default '{}'::jsonb,
  synced_at timestamptz not null default now(),
  unique (language, tcg_player_numeric_id),
  unique (language, tcg_player_id)
);

create index if not exists idx_ppt_sets_language_release
  on public.ppt_sets (language, release_date);

create index if not exists idx_ppt_sets_name
  on public.ppt_sets using gin (to_tsvector('simple', coalesce(name, '')));

create table if not exists public.ppt_cards (
  id bigserial primary key,
  language text not null check (language in ('english', 'japanese')),
  ppt_id text,
  tcg_player_id text not null,
  set_numeric_id integer,
  set_name text,
  name text not null,
  card_number text,
  rarity text,
  card_type text,
  pokemon_type text,
  energy_type text[],
  flavor_text text,
  hp integer,
  stage text,
  attacks text[],
  weakness jsonb,
  resistance jsonb,
  retreat_cost integer,
  artist text,
  tcg_player_url text,
  price_market numeric,
  price_low numeric,
  listings integer,
  sellers integer,
  primary_printing text,
  price_last_updated timestamptz,
  price_was_corrected boolean,
  variants jsonb,
  printings_available text[],
  image_cdn_url text,
  image_cdn_url_200 text,
  image_cdn_url_400 text,
  image_cdn_url_800 text,
  image_url text,
  external_catalog_id text,
  needs_detailed_scrape boolean,
  data_completeness text,
  last_scraped_at timestamptz,
  api_created_at timestamptz,
  api_updated_at timestamptz,
  raw jsonb not null default '{}'::jsonb,
  synced_at timestamptz not null default now(),
  unique (language, tcg_player_id)
);

create index if not exists idx_ppt_cards_language_set
  on public.ppt_cards (language, set_numeric_id);

create index if not exists idx_ppt_cards_tcg_player
  on public.ppt_cards (tcg_player_id);

create index if not exists idx_ppt_cards_name
  on public.ppt_cards using gin (to_tsvector('simple', coalesce(name, '')));

create table if not exists public.ppt_card_price_variants (
  id bigserial primary key,
  language text not null check (language in ('english', 'japanese')),
  card_tcg_player_id text not null,
  printing text not null,
  market_price numeric,
  low_price numeric,
  condition_used text,
  raw jsonb not null default '{}'::jsonb,
  synced_at timestamptz not null default now(),
  unique (language, card_tcg_player_id, printing)
);

create index if not exists idx_ppt_card_price_variants_card
  on public.ppt_card_price_variants (language, card_tcg_player_id);

create table if not exists public.ppt_sync_runs (
  id uuid primary key default gen_random_uuid(),
  job_name text not null,
  language text check (language in ('english', 'japanese')),
  endpoint text,
  params jsonb not null default '{}'::jsonb,
  status text not null check (status in ('running', 'completed', 'stopped', 'failed')),
  rows_fetched integer not null default 0,
  rows_upserted integer not null default 0,
  api_calls_consumed integer not null default 0,
  api_breakdown jsonb not null default '{}'::jsonb,
  daily_remaining integer,
  minute_remaining integer,
  error_message text,
  started_at timestamptz not null default now(),
  finished_at timestamptz
);

create table if not exists public.ppt_sync_checkpoints (
  id bigserial primary key,
  job_name text not null,
  language text not null check (language in ('english', 'japanese')),
  current_set_numeric_id integer,
  current_set_name text,
  current_offset integer not null default 0,
  last_completed_set_numeric_id integer,
  completed_set_numeric_ids integer[] not null default '{}'::integer[],
  credits_used_today integer not null default 0,
  credit_window_date date not null default current_date,
  daily_remaining integer,
  status text not null default 'idle'
    check (status in ('idle', 'running', 'completed', 'stopped', 'failed')),
  last_error text,
  updated_at timestamptz not null default now(),
  unique (job_name, language)
);

alter table public.ppt_sets enable row level security;
alter table public.ppt_cards enable row level security;
alter table public.ppt_card_price_variants enable row level security;
alter table public.ppt_sync_runs enable row level security;
alter table public.ppt_sync_checkpoints enable row level security;
