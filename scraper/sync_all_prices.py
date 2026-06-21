import os
import re
import asyncio
import httpx
from datetime import datetime, timezone
from supabase import create_client, Client
from tqdm.asyncio import tqdm

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in environment.")
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def normalize_condition(value: str):
    if not value: return None
    normalized = re.sub(r'[^a-z0-9]', '', value.lower())
    if not normalized: return None
    if normalized == 'foil': return 'foil'
    if 'reverse' in normalized and 'holo' in normalized: return 'reverseholofoil'
    if '1stedition' in normalized and 'holo' in normalized: return '1steditionholofoil'
    if '1stedition' in normalized: return '1steditionnormal'
    if 'holo' in normalized or normalized == 'foil': return 'holofoil'
    if 'normal' in normalized or 'nonholo' in normalized or 'unlimited' in normalized: return 'normal'
    return normalized

def format_tcg_prices(results: list) -> dict:
    prices_map = {}
    for r in results:
        v = r.get('variant', '')
        c = r.get('condition', '').lower()

        # Prioritize Near Mint / Unplayed
        if 'near mint' not in c and 'nm' not in c and 'unplayed' not in c:
            continue

        buckets = r.get('buckets', [])
        # Sort ascending by date
        buckets.sort(key=lambda x: datetime.strptime(x['bucketStartDate'], "%Y-%m-%d"))
        if not buckets: continue

        latest = buckets[-1]
        market = float(latest.get('marketPrice') or 0)
        low = float(latest.get('lowSalePrice') or 0)

        type_id = normalize_condition(v)
        if not type_id: continue

        if market > 0:
            if type_id not in prices_map:
                prices_map[type_id] = {
                    'market': market,
                    'low': low if low > 0 else None
                }
    return prices_map

def get_best_market_price(prices_map: dict) -> float:
    priority = ['holofoil', 'normal', 'reverseholofoil', '1steditionholofoil', '1steditionnormal']
    for p in priority:
        if p in prices_map and prices_map[p]['market'] > 0:
            return prices_map[p]['market']
    
    if prices_map:
        first_key = list(prices_map.keys())[0]
        return prices_map[first_key].get('market', 0)
    return 0

async def fetch_price(client: httpx.AsyncClient, product_id: int):
    url = f"https://infinite-api.tcgplayer.com/price/history/{product_id}/detailed?range=month"
    headers = {
        'Accept': 'application/json',
        'User-Agent': 'PokeVault-AutoSync-Bot/1.1',
        'Referer': 'https://infinite.tcgplayer.com/',
        'Origin': 'https://infinite.tcgplayer.com'
    }
    try:
        response = await client.get(url, headers=headers, timeout=10.0)
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 404:
            return None # Product not found
        else:
            # Rate limit or server error
            return None
    except Exception as e:
        return None

async def process_product(product_id: int, client: httpx.AsyncClient, semaphore: asyncio.Semaphore):
    async with semaphore:
        json_data = await fetch_price(client, product_id)
        if not json_data:
            return product_id, False, None

        results = json_data.get('result', [])
        if not results:
            return product_id, False, None

        tcgplayer_prices = format_tcg_prices(results)
        market_price = get_best_market_price(tcgplayer_prices)

        if not tcgplayer_prices:
            return product_id, False, None
            
        update_data = {
            'market_price': market_price,
            'tcgplayer_prices': tcgplayer_prices,
            'scraped_at': datetime.now(timezone.utc).isoformat()
        }
        
        return product_id, True, update_data

async def main():
    print("Fetching distinct tcgplayer_product_id from database...")
    all_cards = []
    page_size = 1000
    for i in range(0, 30000, page_size):
        response = supabase.table('pokemon_cards') \
            .select('tcgplayer_product_id') \
            .not_.is_('tcgplayer_product_id', 'null') \
            .order('scraped_at', nullsfirst=True) \
            .range(i, i + page_size - 1) \
            .execute()
        if not response.data:
            break
        all_cards.extend(response.data)

    # Filter distinct
    distinct_pids = list(set([c['tcgplayer_product_id'] for c in all_cards]))
    print(f"Found {len(distinct_pids)} unique products to sync.")

    # Process in batches to avoid overwhelming Supabase or memory
    BATCH_SIZE = 100
    CONCURRENCY = 10
    
    semaphore = asyncio.Semaphore(CONCURRENCY)
    
    success_count = 0
    fail_count = 0

    async with httpx.AsyncClient() as client:
        for i in range(0, len(distinct_pids), BATCH_SIZE):
            batch_pids = distinct_pids[i:i + BATCH_SIZE]
            
            tasks = [process_product(pid, client, semaphore) for pid in batch_pids]
            
            results = await tqdm.gather(*tasks, desc=f"Batch {i//BATCH_SIZE + 1}/{(len(distinct_pids)//BATCH_SIZE) + 1}")
            
            for pid, success, update_data in results:
                if success and update_data:
                    # Update DB
                    try:
                        supabase.table('pokemon_cards').update(update_data).eq('tcgplayer_product_id', pid).execute()
                        success_count += 1
                    except Exception as e:
                        fail_count += 1
                else:
                    fail_count += 1
                    
            # Brief pause between batches to be respectful to API
            await asyncio.sleep(1.0)
            
    print(f"\n✅ Sync completed! Processed: {len(distinct_pids)}, Success: {success_count}, Failed: {fail_count}")

if __name__ == "__main__":
    asyncio.run(main())
