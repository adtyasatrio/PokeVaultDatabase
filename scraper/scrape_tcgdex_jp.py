#!/usr/bin/env python3
"""
TCGdex → Supabase Scraper (Khusus Jepang)
===========================================
Mengunduh data kartu Pokemon bahasa Jepang dari TCGdex dan menyimpannya ke Supabase.
Diperbarui dengan Async/Await agar jauh lebih cepat.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import urllib.parse
from typing import Any

import httpx
from dotenv import load_dotenv
from tqdm.asyncio import tqdm

load_dotenv()

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "")

TCGDEX_BASE = "https://api.tcgdex.net/v2/ja"
CARDS_BATCH_SIZE = 100
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0
CONCURRENCY_LIMIT = 20  # Limit request konkuren ke TCGdex

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scraper_tcgdex.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# HTTP Clients
# ──────────────────────────────────────────────
def make_supabase_client() -> httpx.Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        log.error("SUPABASE_URL dan SUPABASE_SERVICE_KEY harus diset di .env!")
        sys.exit(1)
    return httpx.Client(
        base_url=f"{SUPABASE_URL}/rest/v1",
        headers={
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        timeout=60.0,
    )

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
async def _get_with_retry(client: httpx.AsyncClient, url: str) -> Any:
    delay = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await client.get(url)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", delay * 2))
                log.warning(f"Rate limited. Tunggu {wait:.1f}s...")
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if attempt == MAX_RETRIES:
                raise
            log.warning(f"HTTP {e.response.status_code} pada {url} — retry {attempt}/{MAX_RETRIES}")
            await asyncio.sleep(delay)
            delay *= RETRY_BACKOFF
        except httpx.RequestError as e:
            if attempt == MAX_RETRIES:
                raise
            log.warning(f"Request error pada {url} — retry {attempt}/{MAX_RETRIES}")
            await asyncio.sleep(delay)
            delay *= RETRY_BACKOFF

def _supabase_upsert(sb: httpx.Client, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    resp = sb.post(f"/{table}", content=json.dumps(rows))
    if resp.status_code not in (200, 201):
        log.error(f"Supabase upsert error [{table}]: {resp.status_code} — {resp.text[:500]}")
        resp.raise_for_status()

# ──────────────────────────────────────────────
# Data Mapping
# ──────────────────────────────────────────────
def _map_set(s: dict) -> dict:
    card_count = s.get("cardCount", {})
    return {
        "id": s["id"],
        "name": s.get("name", ""),
        "series": s.get("serie", {}).get("name") if isinstance(s.get("serie"), dict) else None,
        "printed_total": card_count.get("official"),
        "total": card_count.get("total"),
        "release_date": s.get("releaseDate"),
        "logo_url": f"{s['logo']}.webp" if s.get("logo") else None,
        "symbol_url": f"{s['symbol']}.webp" if s.get("symbol") else None,
        "language": "ja"
    }

def _map_card(c: dict, pokemon_en_names: list[str]) -> dict:
    image_url = c.get("image")
    
    english_name = None
    dex_ids = c.get("dexId")
    if dex_ids and isinstance(dex_ids, list) and len(dex_ids) > 0:
        dex_id = dex_ids[0]
        if isinstance(dex_id, int) and 1 <= dex_id <= len(pokemon_en_names):
            english_name = pokemon_en_names[dex_id - 1]

    return {
        "id": c["id"],
        "name": c.get("name", ""),
        "set_id": c.get("set", {}).get("id"),
        "card_number": c.get("localId"),
        "supertype": c.get("category"),
        "subtypes": [c.get("stage")] if c.get("stage") else [],
        "hp": str(c.get("hp")) if c.get("hp") else None,
        "types": c.get("types") or [],
        "rarity": c.get("rarity"),
        "image_large": f"{image_url}/high.webp" if image_url else None,
        "image_small": f"{image_url}/low.webp" if image_url else None,
        "artist": c.get("illustrator"),
        "flavor_text": c.get("description"),
        "regulation_mark": c.get("regulationMark"),
        "evolves_from": c.get("evolveFrom"),
        "market_price": 0.0,
        "language": "ja",
        "english_name": english_name
    }

# ──────────────────────────────────────────────
# Logic
# ──────────────────────────────────────────────
async def fetch_card(client: httpx.AsyncClient, card_id: str, semaphore: asyncio.Semaphore, pokemon_en_names: list[str]):
    async with semaphore:
        c_full = await _get_with_retry(client, f"/cards/{urllib.parse.quote(card_id)}")
        return _map_card(c_full, pokemon_en_names)

async def run_async(limit: int | None = None, target_set_id: str | None = None, force: bool = False):
    sb = make_supabase_client()
    
    log.info("Mengunduh list nama Pokemon bahasa Inggris...")
    async with httpx.AsyncClient(timeout=30.0) as default_client:
        resp_pkmn = await default_client.get("https://raw.githubusercontent.com/sindresorhus/pokemon/main/data/en.json")
        resp_pkmn.raise_for_status()
        pokemon_en_names = resp_pkmn.json()
        
    async with httpx.AsyncClient(base_url=TCGDEX_BASE, headers={"Accept": "application/json"}, timeout=60.0) as tcg:
        log.info("Mengambil daftar set bahasa Jepang dari TCGdex...")
        sets_data = await _get_with_retry(tcg, "/sets")
        
        if target_set_id:
            sets_data = [s for s in sets_data if s["id"] == target_set_id]
            if not sets_data:
                log.error(f"Set '{target_set_id}' tidak ditemukan!")
                return

        if limit:
            sets_data = sets_data[:limit]

        log.info(f"Ditemukan {len(sets_data)} set. Mengambil detail set secara konkruen...")
        
        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
        
        async def fetch_set(s_brief):
            async with semaphore:
                return await _get_with_retry(tcg, f"/sets/{urllib.parse.quote(s_brief['id'])}")
                
        # Fetch all sets concurrently
        tasks = [fetch_set(s) for s in sets_data]
        full_sets = await tqdm.gather(*tasks, desc="Fetching set details")

        # Deduplikasi Set (untuk menghindari error ON CONFLICT DO UPDATE)
        unique_sets_dict = {}
        for s in full_sets:
            unique_sets_dict[s["id"]] = s
        full_sets_unique = list(unique_sets_dict.values())

        log.info(f"Upserting {len(full_sets_unique)} sets ke Supabase...")
        _supabase_upsert(sb, "pokemon_sets", [_map_set(s) for s in full_sets_unique])

        # Hitung set yang belum ada di database untuk incremental
        try:
            resp = sb.get("/pokemon_cards", params={"select": "set_id,language", "language": "eq.ja"})
            existing_set_ids = {row["set_id"] for row in resp.json() if row.get("set_id")}
        except Exception:
            existing_set_ids = set()
            
        sets_to_scrape = [s for s in full_sets_unique if s["id"] not in existing_set_ids]
        if target_set_id:
            sets_to_scrape = full_sets_unique # Force scrape if target is set
        if force:
            sets_to_scrape = full_sets_unique
            
        log.info(f"{len(full_sets_unique) - len(sets_to_scrape)} set sudah ada di DB. Akan memproses {len(sets_to_scrape)} set.")

        total_cards = 0
        for s_full in tqdm(sets_to_scrape, desc="Scraping sets"):
            cards_brief = s_full.get("cards", [])
            
            # Fetch all cards in the set concurrently
            card_tasks = [fetch_card(tcg, c_brief['id'], semaphore, pokemon_en_names) for c_brief in cards_brief]
            mapped_cards = await asyncio.gather(*card_tasks)
            
            # Deduplikasi Kartu
            unique_cards_dict = {}
            for c in mapped_cards:
                unique_cards_dict[c["id"]] = c
            mapped_cards_unique = list(unique_cards_dict.values())
            
            # Upsert cards in batches
            for i in range(0, len(mapped_cards_unique), CARDS_BATCH_SIZE):
                batch = mapped_cards_unique[i : i + CARDS_BATCH_SIZE]
                _supabase_upsert(sb, "pokemon_cards", batch)
                total_cards += len(batch)

        log.info(f"Selesai! Total kartu Jepang di-upsert: {total_cards}")

def main():
    parser = argparse.ArgumentParser(description="Scrape TCGdex JP")
    parser.add_argument("--set-id", help="Target specific set ID (e.g. SV1V)")
    parser.add_argument("--limit", type=int, help="Limit number of sets to scrape (for testing)")
    parser.add_argument("--force", action="store_true", help="Force update semua set meskipun sudah ada di DB")
    args = parser.parse_args()

    asyncio.run(run_async(limit=args.limit, target_set_id=args.set_id, force=args.force))

if __name__ == "__main__":
    main()
