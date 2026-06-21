import os
import asyncio
from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

response = supabase.table('pokemon_cards').select('tcgplayer_product_id').limit(10).execute()
print(response.data)
