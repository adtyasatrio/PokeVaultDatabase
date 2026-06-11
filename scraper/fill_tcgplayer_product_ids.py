#!/usr/bin/env python3
"""
TCGPlayer Product ID Filler (via redirect)
==========================================
Script ini mengikuti redirect dari URL proxy pokemontcg.io
(https://prices.pokemontcg.io/tcgplayer/{card-id}) untuk mendapatkan
URL TCGPlayer asli (https://www.tcgplayer.com/product/XXXXX/...) dan
mengekstrak Product ID-nya, lalu menyimpannya ke kolom tcgplayer_product_id
di Supabase.

Cara pakai:
  python3 scraper/fill_tcgplayer_product_ids.py
  python3 scraper/fill_tcgplayer_product_ids.py --set-id sv3pt5
  python3 scraper/fill_tcgplayer_product_ids.py --limit 100  # test dulu

Env vars (dari .env atau environment):
  SUPABASE_URL          — https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  — service_role key (bukan anon key!)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
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
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "")

REQUEST_DELAY = 0.1   # detik antar redirect request
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0
SUPABASE_PAGE = 1000  # rows per page saat fetch dari Supabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("fill_tcgplayer_ids.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# HTTP Clients
# ──────────────────────────────────────────────
def make_supabase_client() -> httpx.Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        log.error("SUPABASE_URL dan SUPABASE_SERVICE_KEY harus diset!")
        sys.exit(1)
    return httpx.Client(
        base_url=f"{SUPABASE_URL}/rest/v1",
        headers={
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        timeout=30.0,
    )


def make_redirect_client() -> httpx.Client:
    """Client khusus untuk follow redirect — stop setelah redirect pertama.
    
    Kita hanya butuh redirect pertama karena URL ke-2 sudah mengandung
    Product ID dalam parameter ?u=https://tcgplayer.com/product/XXXXX
    """
    return httpx.Client(
        follow_redirects=False,   # Jangan follow otomatis, kita handle manual
        timeout=10.0,
        headers={"User-Agent": "Mozilla/5.0 (compatible; PokeVault/1.0)"},
    )


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def _extract_product_id_from_url(url: str | None) -> int | None:
    """Extract TCGPlayer Product ID dari URL asli TCGPlayer.

    Format yang didukung:
    - https://www.tcgplayer.com/product/12345/...
    - https://www.tcgplayer.com/search/...?productId=12345
    """
    if not url:
        return None
    # Match /product/XXXXX/ pattern
    match = re.search(r'/product/(\d+)', url)
    if match:
        return int(match.group(1))
    # Match productId= query param
    match = re.search(r'[?&]productId=(\d+)', url)
    if match:
        return int(match.group(1))
    return None


def _follow_redirect_get_product_id(
    client: httpx.Client, proxy_url: str
) -> int | None:
    """Follow redirect dari URL proxy pokemontcg.io ke TCGPlayer asli.
    
    Karena URL tujuan redirect langsung berisi parameter ?u=https://tcgplayer.com/product/XXXXX
    kita bisa langsung parse dari header 'Location' tanpa perlu follow HTTP redirect-nya!
    """
    delay = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.get(proxy_url)
            # URL bisa ada di Location header kalau status 301/302
            location = resp.headers.get("Location")
            if location:
                from urllib.parse import unquote
                decoded = unquote(location)
                product_id = _extract_product_id_from_url(decoded)
                if product_id:
                    return product_id
            return None
        except httpx.TimeoutException:
            if attempt == MAX_RETRIES:
                return None
            time.sleep(delay)
            delay *= RETRY_BACKOFF
        except Exception:
            if attempt == MAX_RETRIES:
                return None
            time.sleep(delay)
            delay *= RETRY_BACKOFF
    return None


# ──────────────────────────────────────────────
# Supabase helpers
# ──────────────────────────────────────────────
def fetch_cards_needing_update(
    sb: httpx.Client, set_id: str | None = None, limit: int | None = None
) -> list[dict]:
    """Ambil kartu dari Supabase yang punya tcgplayer_url tapi belum ada product_id."""
    all_rows = []
    offset = 0
    page_size = SUPABASE_PAGE

    log.info("Fetching kartu dari Supabase yang perlu di-update...")
    while True:
        params: dict[str, Any] = {
            "select": "id,tcgplayer_url",
            "tcgplayer_url": "not.is.null",
            "tcgplayer_product_id": "is.null",
            "limit": str(min(page_size, limit - len(all_rows)) if limit else page_size),
            "offset": str(offset),
            "order": "id",
        }
        if set_id:
            params["set_id"] = f"eq.{set_id}"

        resp = sb.get("/pokemon_cards", params=params)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break

        all_rows.extend(rows)
        log.info(f"  Fetched {len(all_rows)} kartu dari Supabase...")

        if limit and len(all_rows) >= limit:
            break
        if len(rows) < page_size:
            break
        offset += page_size

    return all_rows


def upsert_product_ids(sb: httpx.Client, rows: list[dict]) -> None:
    """Update tcgplayer_product_id ke Supabase secara individual."""
    if not rows:
        return
    for row in rows:
        card_id = row.pop("id")
        resp = sb.patch(f"/pokemon_cards?id=eq.{card_id}", content=json.dumps(row))
        if resp.status_code not in (200, 201, 204):
            log.error(f"Supabase patch error for {card_id}: {resp.status_code} — {resp.text[:300]}")
            resp.raise_for_status()

# ──────────────────────────────────────────────
# Main Logic
# ──────────────────────────────────────────────
def run(set_id: str | None = None, limit: int | None = None, batch_size: int = 50) -> None:
    sb = make_supabase_client()
    redirect_client = make_redirect_client()

    # 1. Fetch kartu yang perlu diupdate dari Supabase
    cards = fetch_cards_needing_update(sb, set_id=set_id, limit=limit)
    log.info(f"Total {len(cards)} kartu perlu diisi tcgplayer_product_id.\n")

    if not cards:
        log.info("✅ Semua kartu sudah punya tcgplayer_product_id!")
        return

    # Test redirect dulu untuk validasi
    first_url = cards[0].get("tcgplayer_url", "")
    log.info(f"Test redirect: {first_url}")
    test_id = _follow_redirect_get_product_id(redirect_client, first_url)
    if test_id:
        log.info(f"✅ Redirect berhasil! Product ID: {test_id}")
    else:
        log.warning(f"⚠️  Redirect tidak menghasilkan Product ID dari URL: {first_url}")
        log.warning("   Kemungkinan URL tidak redirect ke TCGPlayer asli.")

    # 2. Follow redirect per-kartu & kumpulkan hasilnya, upsert per-batch
    total_success = 0
    total_failed = 0
    batch: list[dict] = []

    with tqdm(cards, desc="Following redirects", unit="kartu") as pbar:
        for card in pbar:
            card_id = card.get("id")
            proxy_url = card.get("tcgplayer_url")
            if not card_id or not proxy_url:
                total_failed += 1
                continue

            time.sleep(REQUEST_DELAY)
            product_id = _follow_redirect_get_product_id(redirect_client, proxy_url)

            if product_id:
                batch.append({"id": card_id, "tcgplayer_product_id": product_id})
                total_success += 1
            else:
                total_failed += 1

            # Upsert ke Supabase setiap batch_size kartu
            if len(batch) >= batch_size:
                upsert_product_ids(sb, batch)
                log.info(
                    f"  ✅ Batch {len(batch)} kartu di-upsert "
                    f"(total berhasil: {total_success})"
                )
                batch = []

            pbar.set_postfix({"ok": total_success, "skip": total_failed})

    # Upsert sisa batch
    if batch:
        upsert_product_ids(sb, batch)

    log.info(f"\n🎉 Selesai!")
    log.info(f"   ✅ Berhasil: {total_success} kartu diisi tcgplayer_product_id")
    log.info(f"   ❌ Gagal/Skip: {total_failed} kartu (tidak ada redirect ke TCGPlayer)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Isi tcgplayer_product_id via redirect dari URL proxy pokemontcg.io",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--set-id",
        metavar="SET_ID",
        help="Hanya update satu set tertentu (misal: swsh12, sv3pt5)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Batasi jumlah kartu yang diproses (untuk testing)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Jumlah kartu per batch upsert ke Supabase (default: 50)",
    )
    args = parser.parse_args()

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("❌ Error: SUPABASE_URL dan SUPABASE_SERVICE_KEY belum diset!")
        print("   Copy scraper/.env.example ke scraper/.env dan isi nilai-nilainya.")
        sys.exit(1)

    run(set_id=args.set_id, limit=args.limit, batch_size=args.batch_size)


if __name__ == "__main__":
    main()
