#!/usr/bin/env python3
import argparse
import logging
import os
import sqlite3
import sys
import time
import json
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

DEFAULT_DB_PATH = "assets/db/poc.db"
PAGE_SIZE = 1000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

def make_supabase_client() -> httpx.Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        log.error("SUPABASE_URL dan SUPABASE_SERVICE_KEY harus diset di .env atau environment!")
        sys.exit(1)
    return httpx.Client(
        base_url=f"{SUPABASE_URL}/rest/v1",
        headers={
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "count=exact",
        },
        timeout=60.0,
    )

def ensure_schema(conn: sqlite3.Connection):
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS cards (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            set_name TEXT,
            card_number TEXT,
            hp TEXT,
            set_id TEXT,
            set_code TEXT,
            image_url TEXT,
            updated_at TEXT,
            rarity TEXT,
            types TEXT,
            attacks TEXT,
            language TEXT,
            english_name TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cards_set_number ON cards(set_id, card_number)")
    
    c.execute("PRAGMA table_info(cards)")
    columns = [row[1] for row in c.fetchall()]
    if "attacks" not in columns:
        c.execute("ALTER TABLE cards ADD COLUMN attacks TEXT")
    if "language" not in columns:
        c.execute("ALTER TABLE cards ADD COLUMN language TEXT")
    if "english_name" not in columns:
        c.execute("ALTER TABLE cards ADD COLUMN english_name TEXT")
        
    conn.commit()

def fetch_sets(sb: httpx.Client) -> dict[str, dict]:
    log.info("Fetching pokemon_sets from Supabase...")
    sets_dict = {}
    offset = 0
    while True:
        resp = sb.get("/pokemon_sets", params={"select": "id,name,ptcgo_code,printed_total", "limit": PAGE_SIZE, "offset": offset})
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        for s in data:
            sets_dict[s["id"]] = s
        if len(data) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    log.info(f"Loaded {len(sets_dict)} sets")
    return sets_dict

def get_display_number(number: str, printed_total) -> str:
    if not printed_total or "/" in number:
        return number
    return f"{number}/{printed_total}"

def fetch_and_insert_cards(sb: httpx.Client, conn: sqlite3.Connection, sets_dict: dict, force: bool, limit):
    c = conn.cursor()
    
    existing_ids = set()
    if not force:
        c.execute("SELECT id FROM cards")
        existing_ids = {row[0] for row in c.fetchall()}
        log.info(f"Existing cards in DB: {len(existing_ids)}")

    offset = 0
    created = 0
    updated = 0
    skipped = 0
    failed = 0
    seen = 0
    
    # Try to get total count
    total_count = None
    try:
        total_resp = sb.get("/pokemon_cards", params={"select": "id", "limit": 1})
        total_resp.raise_for_status()
        content_range = total_resp.headers.get("content-range", "")
        if "/" in content_range:
            total_count_str = content_range.split("/")[-1]
            if total_count_str.isdigit():
                total_count = int(total_count_str)
    except Exception as e:
        log.warning(f"Could not get total count: {e}")
    
    if total_count and not force and limit is None and len(existing_ids) >= total_count:
        log.info(f"local DB already has {len(existing_ids)}/{total_count} cards; stopping metadata fetch")
        return

    pbar_total = limit if limit else total_count
    with tqdm(total=pbar_total, desc="Fetching cards") as pbar:
        while True:
            fetch_limit = PAGE_SIZE
            if limit is not None:
                remaining = limit - seen
                if remaining <= 0:
                    break
                fetch_limit = min(PAGE_SIZE, remaining)

            resp = sb.get("/pokemon_cards", params={
                "select": "id,name,card_number,hp,set_id,image_large,image_small,rarity,types,attacks,language,english_name",
                "limit": fetch_limit,
                "offset": offset,
                "order": "id.asc"
            })
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            
            records = []
            for card in data:
                seen += 1
                card_id = card["id"]
                if not force and card_id in existing_ids:
                    skipped += 1
                    continue
                
                image_url = card.get("image_large") or card.get("image_small")
                if not image_url:
                    failed += 1
                    continue
                
                set_info = sets_dict.get(card.get("set_id"), {})
                
                number = str(card.get("card_number") or "")
                printed_total = set_info.get("printed_total")
                
                types_val = None
                types_arr = card.get("types")
                if isinstance(types_arr, list):
                    types_val = ", ".join(types_arr)
                    
                attacks_val = None
                attacks_arr = card.get("attacks")
                if isinstance(attacks_arr, list):
                    attacks_val = json.dumps(attacks_arr)
                
                record = (
                    card_id,
                    card.get("name") or "Unknown",
                    set_info.get("name"),
                    get_display_number(number, printed_total),
                    card.get("hp"),
                    card.get("set_id"),
                    set_info.get("ptcgo_code"),
                    image_url,
                    datetime.now(timezone.utc).isoformat(),
                    card.get("rarity"),
                    types_val,
                    attacks_val,
                    card.get("language") or "en",
                    card.get("english_name") or ""
                )
                records.append(record)
                
                if card_id in existing_ids:
                    updated += 1
                else:
                    created += 1
                    existing_ids.add(card_id)

            if records:
                c.executemany("""
                    INSERT OR REPLACE INTO cards (id, name, set_name, card_number, hp, set_id, set_code, image_url, updated_at, rarity, types, attacks, language, english_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, records)
                conn.commit()
            
            pbar.update(len(data))
            
            if len(data) < fetch_limit:
                break
            offset += fetch_limit

    log.info(f"Done. cards={len(existing_ids)} created={created} updated={updated} skipped={skipped} failed={failed}")

def run(db_path: str, force: bool, limit):
    sb = make_supabase_client()
    
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    
    try:
        ensure_schema(conn)
        sets_dict = fetch_sets(sb)
        fetch_and_insert_cards(sb, conn, sets_dict, force, limit)
        
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", ("generated_at", datetime.now(timezone.utc).isoformat()))
        c.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", ("source", "Supabase DB"))
        c.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", ("schema_version", "2"))
        conn.commit()
        
        c.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()
        sb.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate offline SQLite DB from Supabase")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="SQLite output path")
    parser.add_argument("--force", action="store_true", help="Rehash cards already present in the DB")
    parser.add_argument("--limit", type=int, help="Stop after this many fetched cards")
    args = parser.parse_args()
    
    run(args.db_path, args.force, args.limit)
