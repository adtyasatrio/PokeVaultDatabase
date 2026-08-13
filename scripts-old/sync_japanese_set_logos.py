#!/usr/bin/env python3
"""
Fill broken Japanese set logos from Serebii into Supabase Storage.

Only rows whose logo_url still points to TCGPlayer are considered. Matches use
release date plus a conservative name comparison, with a small reviewed alias
map for sets whose English translations differ substantially.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import logging
import os
import re
from difflib import SequenceMatcher
from typing import Any

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image
from tqdm import tqdm


load_dotenv("scraper/.env")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
BUCKET_NAME = "set_logos"
SEREBII_INDEX = "https://www.serebii.net/card/japanese.shtml"

AUTO_MATCH_THRESHOLD = 0.82
AUTO_MATCH_MARGIN = 0.07

# Reviewed mappings for materially different translations.
MANUAL_ALIASES = {
    "ppt_ja_set_23614": "Mask of Change",
    "ppt_ja_set_23624": "Jet Black Poltergeist",
    "ppt_ja_set_23641": "Pokemon GO x Pokemon Card Game",
    "ppt_ja_set_23679": "Did You See The Fighting Rainbow",
    "ppt_ja_set_23680": "Light-Devouring Darkness",
    "ppt_ja_set_23686": "Charisma of the Wrecked Sky",
    "ppt_ja_set_23693": "Beyond A New Challenge",
    "ppt_ja_set_23882": "Starter Set Tapu Bulu GX",
    "ppt_ja_set_23901": "Freeze Shock",
    "ppt_ja_set_23902": "Ice Burn",
    "ppt_ja_set_23958": "Mega Battle Deck M Charizard EX",
    "ppt_ja_set_23961": "Mega Battle Deck M Rayquaza EX",
    "ppt_ja_set_23965": "Break Combo Deck Golduck BREAK Palkia EX",
    "ppt_ja_set_23987": "Break Evolution Pack Noivern BREAK",
    "ppt_ja_set_23988": "Break Evolution Pack Raichu BREAK",
    "ppt_ja_set_24024": "Big Summit Clash",
    "ppt_ja_set_24080": "Steelix Metal",
    "ppt_ja_set_24081": "Tyranitar Darkness",
    "ppt_ja_set_24260": "Hot Air Arena",
}

# This is a related product, not the same set as 25th Anniversary Collection.
AUTO_MATCH_EXCLUSIONS = {"ppt_ja_set_23847"}

# Serebii's index contains a few stale image paths.
SOURCE_URL_OVERRIDES = {
    "ppt_ja_set_23685": "https://www.serebii.net/card/logo/forbiddenlight.png",
    "ppt_ja_set_23959": "https://www.serebii.net/card/logo/hypermetalchain.png",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def normalize_name(value: str) -> str:
    value = (
        value.lower()
        .replace("pokémon", "pokemon")
        .replace("&", "and")
        .replace("–", "-")
    )
    value = re.sub(
        r"\b(japanese|pokemon|card|cards|expansion|pack|set|jp|ex)\b",
        " ",
        value,
    )
    return re.sub(r"[^a-z0-9]", "", value)


def parse_serebii_date(row_text: str) -> str | None:
    match = re.search(r"([A-Z][a-z]+ \d+(?:st|nd|rd|th)? \d{4})", row_text)
    if not match:
        return None
    clean = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", match.group(1))
    try:
        return dt.datetime.strptime(clean, "%B %d %Y").date().isoformat()
    except ValueError:
        return None


def make_clients() -> tuple[httpx.Client, httpx.Client]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY are required in scraper/.env"
        )

    auth_headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    database = httpx.Client(
        base_url=f"{SUPABASE_URL}/rest/v1",
        headers={**auth_headers, "Content-Type": "application/json"},
        timeout=60.0,
    )
    storage = httpx.Client(
        base_url=f"{SUPABASE_URL}/storage/v1",
        headers=auth_headers,
        timeout=60.0,
    )
    return database, storage


def fetch_pending_sets(database: httpx.Client) -> list[dict[str, Any]]:
    response = database.get(
        "/pokemon_sets",
        params={
            "language": "eq.ja",
            "logo_url": "like.*tcgplayer*",
            "select": "id,name,release_date,logo_url",
            "limit": "1000",
        },
    )
    response.raise_for_status()
    return response.json()


def fetch_serebii_sets() -> list[dict[str, str | None]]:
    response = httpx.get(
        SEREBII_INDEX,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=60.0,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")

    sets: list[dict[str, str | None]] = []
    for row in soup.select("tr"):
        image = row.select_one('img[src*="/card/logo/"]')
        cells = row.select("td")
        if not image or len(cells) < 2:
            continue

        name = " ".join(cells[1].stripped_strings)
        if not name:
            name = (image.get("alt") or "").replace(" Set Icon", "")
        source_path = image.get("src")
        if not name or not source_path:
            continue

        row_text = " | ".join(" ".join(cell.stripped_strings) for cell in cells)
        sets.append(
            {
                "name": name,
                "date": parse_serebii_date(row_text),
                "url": f"https://www.serebii.net{source_path}",
            }
        )
    return sets


def choose_matches(
    pending: list[dict[str, Any]],
    sources: list[dict[str, str | None]],
) -> list[dict[str, Any]]:
    source_by_name = {normalize_name(str(row["name"])): row for row in sources}
    matches: list[dict[str, Any]] = []

    for target in pending:
        target_id = target["id"]
        alias = MANUAL_ALIASES.get(target_id)
        if alias:
            source = source_by_name.get(normalize_name(alias))
            if source:
                matches.append(
                    {**target, "source": source, "match_type": "manual_alias"}
                )
            else:
                log.warning("Manual source not found for %s: %s", target_id, alias)
            continue

        if target_id in AUTO_MATCH_EXCLUSIONS:
            continue

        target_name = target["name"].split(":", 1)[-1].strip()
        scored: list[tuple[float, dict[str, str | None]]] = []
        for source in sources:
            score = SequenceMatcher(
                None,
                normalize_name(target_name),
                normalize_name(str(source["name"])),
            ).ratio()
            if (
                target.get("release_date")
                and source.get("date")
                and target["release_date"] == source["date"]
            ):
                score += 0.15
            scored.append((score, source))

        scored.sort(key=lambda item: item[0], reverse=True)
        if len(scored) < 2:
            continue
        best_score, best_source = scored[0]
        second_score = scored[1][0]
        if (
            best_score >= AUTO_MATCH_THRESHOLD
            and best_score - second_score >= AUTO_MATCH_MARGIN
        ):
            matches.append(
                {
                    **target,
                    "source": best_source,
                    "match_type": "automatic",
                    "score": round(best_score, 3),
                }
            )
    return matches


def validate_image(content: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(content)) as image:
        image.verify()
    with Image.open(io.BytesIO(content)) as image:
        return image.size


def upload_and_update(
    database: httpx.Client,
    storage: httpx.Client,
    match: dict[str, Any],
) -> None:
    source = match["source"]
    image_url = SOURCE_URL_OVERRIDES.get(match["id"], str(source["url"]))
    image_response = httpx.get(
        image_url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=60.0,
    )
    image_response.raise_for_status()
    width, height = validate_image(image_response.content)
    if width < 100 or height < 20:
        raise RuntimeError(f"Image is unexpectedly small: {width}x{height}")

    storage_path = f"{match['id']}.png"
    upload_response = storage.post(
        f"/object/{BUCKET_NAME}/{storage_path}",
        content=image_response.content,
        headers={"Content-Type": "image/png", "x-upsert": "true"},
    )
    upload_response.raise_for_status()

    public_url = (
        f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{storage_path}"
    )
    update_response = database.patch(
        "/pokemon_sets",
        params={"id": f"eq.{match['id']}"},
        json={"logo_url": public_url},
    )
    update_response.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync verified Japanese set logos from Serebii"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Upload matched logos and update pokemon_sets. Default is dry-run.",
    )
    args = parser.parse_args()

    database, storage = make_clients()
    pending = fetch_pending_sets(database)
    sources = fetch_serebii_sets()
    matches = choose_matches(pending, sources)

    log.info(
        "Pending TCGPlayer logos=%d, Serebii sources=%d, verified matches=%d",
        len(pending),
        len(sources),
        len(matches),
    )
    for match in matches:
        log.info(
            "%s -> %s (%s)",
            match["name"],
            match["source"]["name"],
            match["match_type"],
        )

    if not args.apply:
        log.info("Dry-run complete. Re-run with --apply to upload and update.")
        return

    succeeded = 0
    failed = 0
    for match in tqdm(matches, desc="Uploading logos"):
        try:
            upload_and_update(database, storage, match)
            succeeded += 1
        except Exception as exc:
            failed += 1
            log.error("Failed %s: %s", match["id"], exc)

    log.info("Finished. succeeded=%d failed=%d", succeeded, failed)


if __name__ == "__main__":
    main()
