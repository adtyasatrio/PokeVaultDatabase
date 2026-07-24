alter table public.pokemon_sets
  add column if not exists logo_b2_path text,
  add column if not exists symbol_b2_path text;

comment on column public.pokemon_sets.logo_b2_path is
  'Backblaze B2 object path for the public set logo.';

comment on column public.pokemon_sets.symbol_b2_path is
  'Backblaze B2 object path for the public set symbol.';
