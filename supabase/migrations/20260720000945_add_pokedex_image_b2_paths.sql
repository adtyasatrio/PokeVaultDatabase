alter table public.pokemon_pokedex
  add column if not exists image_b2_path text;

alter table public.pokemon_pokedex_core
  add column if not exists image_b2_path text;

comment on column public.pokemon_pokedex.image_b2_path is
  'Backblaze B2 object path for the public Pokedex image.';

comment on column public.pokemon_pokedex_core.image_b2_path is
  'Backblaze B2 object path for the public canonical Pokedex image.';
