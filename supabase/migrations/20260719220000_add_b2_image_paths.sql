alter table public.pokemon_cards
  add column if not exists image_small_b2_path text,
  add column if not exists image_large_b2_path text;

comment on column public.pokemon_cards.image_small_b2_path is
  'Private Backblaze B2 object path for the small card image; not a signed URL.';
comment on column public.pokemon_cards.image_large_b2_path is
  'Private Backblaze B2 object path for the high-resolution card image; not a signed URL.';
