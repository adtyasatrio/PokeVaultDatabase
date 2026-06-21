#!/usr/bin/env python3
"""
Sync Serebii Japanese Set Logos to Supabase Storage
===================================================
Scrapes Serebii for Japanese Pokemon TCG sets, downloads their logos,
uploads them to Supabase Storage 'set_logos', and updates 'pokemon_sets' table.
"""

import os
import sys
import logging
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv('scraper/.env')

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_SERVICE_KEY = os.getenv('SUPABASE_SERVICE_KEY')
BUCKET_NAME = 'set_logos'

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in scraper/.env")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

def run():
    # 1. Fetch DB Sets
    log.info("Fetching Japanese sets from Supabase...")
    sb_client = httpx.Client(
        base_url=f'{SUPABASE_URL}/rest/v1',
        headers={
            'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
            'apikey': SUPABASE_SERVICE_KEY,
            'Content-Type': 'application/json'
        },
        timeout=60.0
    )
    
    storage_client = httpx.Client(
        base_url=f'{SUPABASE_URL}/storage/v1',
        headers={
            'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
            'apikey': SUPABASE_SERVICE_KEY,
        },
        timeout=60.0
    )

    resp = sb_client.get('/pokemon_sets', params={'language': 'eq.ja', 'select': 'id,name'})
    resp.raise_for_status()
    db_sets = resp.json()
    log.info(f"Found {len(db_sets)} Japanese sets in database.")

    # 2. Fetch Serebii Sets
    log.info("Fetching set list from Serebii...")
    res = httpx.get('https://www.serebii.net/card/japanese.shtml', headers={'User-Agent': 'Mozilla/5.0'})
    res.raise_for_status()
    soup = BeautifulSoup(res.content, 'html.parser')

    serebii_sets = {}
    for a in soup.find_all('a'):
        href = a.get('href')
        if href and '/card/' in href and not href.startswith('http'):
            if href not in serebii_sets:
                serebii_sets[href] = {'name': '', 'img_url': ''}
            
            img = a.find('img')
            if img:
                img_src = img.get('src')
                if img_src:
                    serebii_sets[href]['img_url'] = f'https://www.serebii.net{img_src}'
            
            name = a.text.strip().replace('  ', ' ')
            if name and not serebii_sets[href]['name']:
                serebii_sets[href]['name'] = name

    valid_serebii_sets = [s for s in serebii_sets.values() if s['name'] and s['img_url']]
    log.info(f"Extracted {len(valid_serebii_sets)} sets with images from Serebii.")

    # 3. Match Sets
    matched_sets = []
    for d in db_sets:
        d_name_lower = d['name'].lower()
        for s in valid_serebii_sets:
            s_name_lower = s['name'].lower()
            # Loose matching to accommodate naming differences
            if s_name_lower in d_name_lower or d_name_lower in s_name_lower or d_name_lower.replace(':', '') in s_name_lower:
                matched_sets.append({'db_id': d['id'], 'db_name': d['name'], 'serebii_name': s['name'], 'img_url': s['img_url']})
                break
    
    log.info(f"Matched {len(matched_sets)} sets to update.")

    if not matched_sets:
        log.info("No sets matched. Exiting.")
        return

    # 4. Download & Upload
    updated_count = 0
    with httpx.Client(timeout=30.0) as dl_client:
        for match in tqdm(matched_sets, desc="Processing sets"):
            db_id = match['db_id']
            img_url = match['img_url']
            
            # Download
            try:
                img_resp = dl_client.get(img_url, headers={'User-Agent': 'Mozilla/5.0'})
                img_resp.raise_for_status()
                img_bytes = img_resp.content
            except Exception as e:
                log.error(f"Failed to download {img_url} for {db_id}: {e}")
                continue

            # Upload to Supabase Storage
            file_ext = img_url.split('.')[-1].split('?')[0]
            if not file_ext or len(file_ext) > 4:
                file_ext = 'png'
                
            storage_path = f"{db_id}.{file_ext}"
            
            try:
                # Use multipart/form-data or binary upload depending on the endpoint, but usually binary works for /object/
                upload_resp = storage_client.post(
                    f'/object/{BUCKET_NAME}/{storage_path}',
                    content=img_bytes,
                    headers={'Content-Type': f'image/{file_ext}'}
                )
                
                # If 400 because it already exists, that's fine, we can try to get the URL anyway or PUT
                if upload_resp.status_code in (400, 409):
                    # Trying to overwrite
                    upload_resp = storage_client.put(
                        f'/object/{BUCKET_NAME}/{storage_path}',
                        content=img_bytes,
                        headers={'Content-Type': f'image/{file_ext}'}
                    )
                upload_resp.raise_for_status()
            except Exception as e:
                log.error(f"Failed to upload {storage_path} to Storage: {e}")
                continue

            # Public URL
            public_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{storage_path}"

            # Update Database
            try:
                update_resp = sb_client.patch(
                    f'/pokemon_sets?id=eq.{db_id}',
                    json={'logo_url': public_url, 'symbol_url': public_url}
                )
                update_resp.raise_for_status()
                updated_count += 1
            except Exception as e:
                log.error(f"Failed to update DB for {db_id}: {e}")

    log.info(f"Successfully updated {updated_count} Japanese sets!")

if __name__ == '__main__':
    run()
