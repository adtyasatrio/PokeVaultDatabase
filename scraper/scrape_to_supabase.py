#!/usr/bin/env python3
"""
Pokemon TCG → Supabase Scraper
================================
Mengunduh semua data kartu dari pokemontcg.io dan menyimpannya ke Supabase
sehingga Flutter app tidak bergantung pada API eksternal saat runtime.

Cara pakai:
  python scrape_to_supabase.py                      # Scrape semua set (skip yang sudah ada)
  python scrape_to_supabase.py --force              # Paksa re-scrape semua set
  python scrape_to_supabase.py --set-id base1       # Hanya satu set tertentu
  python scrape_to_supabase.py --sets-only          # Hanya scrape sets, skip kartu

Env vars (dari .env atau environment):
  POKEMONTCG_API_KEY    — API key pokemontcg.io
  SUPABASE_URL          — https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  — service_role key (bukan anon key!)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any

import httpx
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
POKEMONTCG_API_KEY: str = os.getenv("POKEMONTCG_API_KEY", "")
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "")

POKEMONTCG_BASE = "https://api.pokemontcg.io/v2"
PAGE_SIZE = 250  # Max yang diizinkan pokemontcg.io
CARDS_BATCH_SIZE = 50  # Jumlah kartu per batch upsert ke Supabase
REQUEST_DELAY = 0.15  # Detik antar request ke pokemontcg.io (aman untuk key user)
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0  # Faktor backoff eksponensial

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scraper.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# HTTP Clients
# ──────────────────────────────────────────────
def make_tcg_client() -> httpx.Client:
    headers: dict[str, str] = {"Accept": "application/json"}
    if POKEMONTCG_API_KEY:
        headers["X-Api-Key"] = POKEMONTCG_API_KEY
    return httpx.Client(
        base_url=POKEMONTCG_BASE,
        headers=headers,
        timeout=60.0,
    )


def make_supabase_client() -> httpx.Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        log.error(
            "SUPABASE_URL dan SUPABASE_SERVICE_KEY harus diset di .env atau environment!"
        )
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
def _get_with_retry(client: httpx.Client, url: str, params: dict | None = None) -> Any:
    """GET dengan retry eksponensial. Returns parsed JSON."""
    delay = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.get(url, params=params)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", delay * 2))
                log.warning(f"Rate limited. Tunggu {wait:.1f}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if attempt == MAX_RETRIES:
                raise
            log.warning(f"HTTP {e.response.status_code} — retry {attempt}/{MAX_RETRIES} dalam {delay:.1f}s")
            time.sleep(delay)
            delay *= RETRY_BACKOFF
        except httpx.RequestError as e:
            if attempt == MAX_RETRIES:
                raise
            log.warning(f"Request error: {e} — retry {attempt}/{MAX_RETRIES} dalam {delay:.1f}s")
            time.sleep(delay)
            delay *= RETRY_BACKOFF


def _supabase_upsert(sb: httpx.Client, table: str, rows: list[dict]) -> None:
    """Upsert batch rows ke Supabase."""
    if not rows:
        return
    resp = sb.post(f"/{table}", content=json.dumps(rows))
    if resp.status_code not in (200, 201):
        log.error(f"Supabase upsert error [{table}]: {resp.status_code} — {resp.text[:500]}")
        resp.raise_for_status()


# ──────────────────────────────────────────────
# Data Mapping: pokemontcg.io → Supabase schema
# ──────────────────────────────────────────────
def _map_set(s: dict) -> dict:
    images = s.get("images") or {}
    return {
        "id": s["id"],
        "name": s.get("name", ""),
        "series": s.get("series"),
        "printed_total": s.get("printedTotal"),
        "total": s.get("total"),
        "ptcgo_code": s.get("ptcgoCode"),
        "release_date": s.get("releaseDate"),
        "symbol_url": images.get("symbol"),
        "logo_url": images.get("logo"),
    }


def _map_card(c: dict) -> dict:
    images = c.get("images") or {}
    tcg = c.get("tcgplayer") or {}
    prices_raw = tcg.get("prices") or {}
    set_info = c.get("set") or {}

    # Hitung market price terbaik
    market_price = 0.0
    for variant_key in ("holofoil", "normal", "reverseHolofoil", "1stEditionHolofoil"):
        variant = prices_raw.get(variant_key) or {}
        val = variant.get("market") or variant.get("mid") or 0
        if isinstance(val, (int, float)) and val > 0:
            market_price = float(val)
            break

    return {
        "id": c["id"],
        "name": c.get("name", ""),
        "set_id": set_info.get("id"),
        "card_number": c.get("number"),
        "supertype": c.get("supertype"),
        "subtypes": c.get("subtypes") or [],
        "hp": c.get("hp"),
        "types": c.get("types") or [],
        "rarity": c.get("rarity"),
        "image_large": images.get("large"),
        "image_small": images.get("small"),
        "artist": c.get("artist"),
        "flavor_text": c.get("flavorText"),
        "regulation_mark": c.get("regulationMark"),
        "evolves_from": c.get("evolvesFrom"),
        "evolves_to": c.get("evolvesTo") or [],
        "rules": c.get("rules") or [],
        "abilities": c.get("abilities") or [],
        "attacks": c.get("attacks") or [],
        "weaknesses": c.get("weaknesses") or [],
        "resistances": c.get("resistances") or [],
        "retreat_cost": c.get("retreatCost") or [],
        "converted_retreat_cost": c.get("convertedRetreatCost"),
        "legalities": c.get("legalities"),
        "tcgplayer_url": tcg.get("url"),
        "tcgplayer_prices": prices_raw,
        "market_price": market_price,
    }


# ──────────────────────────────────────────────
# Core Scraping Logic
# ──────────────────────────────────────────────
def fetch_all_sets(tcg: httpx.Client) -> list[dict]:
    log.info("Fetching semua Pokemon TCG sets...")
    data = _get_with_retry(tcg, "/sets", params={
        "pageSize": str(PAGE_SIZE),
        "orderBy": "releaseDate",
        "select": "id,name,series,printedTotal,total,ptcgoCode,releaseDate,images",
    })
    sets = data.get("data", [])
    log.info(f"Ditemukan {len(sets)} sets")
    return sets


def get_existing_set_ids(sb: httpx.Client) -> set[str]:
    """Ambil ID sets yang sudah ada di Supabase."""
    resp = sb.get("/pokemon_sets", params={"select": "id"})
    resp.raise_for_status()
    return {row["id"] for row in resp.json()}


def scrape_cards_for_set(tcg: httpx.Client, sb: httpx.Client, set_id: str) -> int:
    """
    Scrape semua kartu untuk satu set. Returns jumlah kartu yang di-upsert.
    """
    total_upserted = 0
    page = 1

    while True:
        time.sleep(REQUEST_DELAY)
        data = _get_with_retry(tcg, "/cards", params={
            "q": f'set.id:"{set_id}"',
            "page": str(page),
            "pageSize": str(PAGE_SIZE),
            "orderBy": "number",
        })
        cards_raw = data.get("data", [])
        if not cards_raw:
            break

        # Map dan upsert ke Supabase dalam batch
        mapped = [_map_card(c) for c in cards_raw]
        for i in range(0, len(mapped), CARDS_BATCH_SIZE):
            batch = mapped[i : i + CARDS_BATCH_SIZE]
            _supabase_upsert(sb, "pokemon_cards", batch)
            total_upserted += len(batch)

        # Cek apakah masih ada halaman berikutnya
        total_count = data.get("totalCount", 0)
        if page * PAGE_SIZE >= total_count or len(cards_raw) < PAGE_SIZE:
            break
        page += 1

    return total_upserted


# ──────────────────────────────────────────────
# Main Entry Point
# ──────────────────────────────────────────────
def run(
    force: bool = False,
    target_set_id: str | None = None,
    sets_only: bool = False,
) -> None:
    tcg = make_tcg_client()
    sb = make_supabase_client()

    # 1. Fetch + upsert semua sets
    all_sets = fetch_all_sets(tcg)

    if target_set_id:
        all_sets = [s for s in all_sets if s["id"] == target_set_id]
        if not all_sets:
            log.error(f"Set '{target_set_id}' tidak ditemukan!")
            sys.exit(1)

    log.info(f"Upserting {len(all_sets)} sets ke Supabase...")
    _supabase_upsert(sb, "pokemon_sets", [_map_set(s) for s in all_sets])
    log.info("✅ Sets berhasil di-upsert")

    if sets_only:
        log.info("--sets-only mode: skip kartu. Selesai!")
        return

    # 2. Tentukan mana yang perlu di-scrape
    if force:
        sets_to_scrape = all_sets
        log.info(f"--force mode: scrape ulang semua {len(sets_to_scrape)} sets")
    else:
        existing_ids = get_existing_set_ids(sb)
        # Set yang sudah ada di Supabase dan punya kartu → check count
        # Untuk simplicity: skip set yang ID-nya ada di Supabase
        # (kecuali force mode)
        sets_to_scrape = all_sets  # Kita akan cek per-set
        log.info(f"Mode incremental: {len(existing_ids)} sets sudah ada di DB")

    # 3. Cek set mana yang sudah punya kartu (incremental mode)
    if not force and not target_set_id:
        scraped_set_ids = set()
        offset = 0
        limit = 1000
        while True:
            resp = sb.get("/pokemon_cards", params={
                "select": "set_id",
                "limit": str(limit),
                "offset": str(offset)
            })
            if resp.status_code != 200:
                break
            data = resp.json()
            if not data:
                break
            scraped_set_ids.update(row["set_id"] for row in data if row.get("set_id"))
            if len(data) < limit:
                break
            offset += limit

        sets_to_scrape = [s for s in all_sets if s["id"] not in scraped_set_ids]
        log.info(
            f"Incremental: {len(scraped_set_ids)} sets sudah ada kartunya, "
            f"{len(sets_to_scrape)} sets perlu di-scrape"
        )

    if not sets_to_scrape:
        log.info("✅ Semua set sudah ada di database. Tidak ada yang perlu di-scrape.")
        log.info("   Gunakan --force untuk re-scrape ulang semua set.")
        return

    # 4. Scrape kartu per set
    total_cards = 0
    with tqdm(sets_to_scrape, desc="Scraping sets", unit="set") as pbar:
        for s in pbar:
            set_id = s["id"]
            set_name = s.get("name", set_id)
            pbar.set_description(f"Scraping: {set_name}")
            try:
                count = scrape_cards_for_set(tcg, sb, set_id)
                total_cards += count
                log.info(f"  ✅ {set_name} ({set_id}): {count} kartu")
            except Exception as e:
                log.error(f"  ❌ {set_name} ({set_id}): GAGAL — {e}")
                log.info("  → Lanjut ke set berikutnya. Jalankan ulang untuk retry.")

    log.info(f"\n🎉 Selesai! Total kartu di-upsert: {total_cards}")
    log.info("   Jalankan ulang kapan saja — scraper bersifat incremental (idempotent).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape Pokemon TCG database dari pokemontcg.io → Supabase",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-scrape semua set meskipun sudah ada di database",
    )
    parser.add_argument(
        "--set-id",
        metavar="SET_ID",
        help="Hanya scrape satu set tertentu (misal: base1, swsh12)",
    )
    parser.add_argument(
        "--sets-only",
        action="store_true",
        help="Hanya upsert data sets, skip scraping kartu",
    )
    args = parser.parse_args()

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("❌ Error: SUPABASE_URL dan SUPABASE_SERVICE_KEY belum diset!")
        print("   Copy scraper/.env.example ke scraper/.env dan isi nilai-nilainya.")
        sys.exit(1)

    run(
        force=args.force,
        target_set_id=args.set_id,
        sets_only=args.sets_only,
    )


if __name__ == "__main__":
    main()
