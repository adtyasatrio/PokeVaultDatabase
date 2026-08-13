#!/usr/bin/env python3
"""
Pokemon Price Tracker -> Supabase scraper.

This scraper writes into new ppt_* staging tables only. It is designed to be
resume-able across days and to stop before the daily credit budget is exhausted.

Required env vars:
  POKEMON_PRICE_TRACKER_API_KEY
  SUPABASE_URL
  SUPABASE_SERVICE_KEY or SUPABASE_SERVICE_ROLE_KEY

Example:
  python scraper/scrape_pokemonpricetracker.py --language both --max-credits 19000
  python scraper/scrape_pokemonpricetracker.py --language english --set-id 1407 --limit 200
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

import httpx
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

PPT_BASE = "https://www.pokemonpricetracker.com/api/v2"
DEFAULT_JOB_NAME = "pokemon_price_tracker_cards_basic"
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0
CARD_LIMIT_MAX = 200
SET_LIMIT_MAX = 500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scraper_pokemonpricetracker.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


@dataclass
class ApiUsage:
    credits: int = 0
    daily_remaining: Optional[int] = None
    minute_remaining: Optional[int] = None
    breakdown: Optional[dict[str, Any]] = None


class StopBudget(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def make_ppt_client() -> httpx.Client:
    api_key = env_required("POKEMON_PRICE_TRACKER_API_KEY")
    return httpx.Client(
        base_url=PPT_BASE,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        timeout=60.0,
    )


def make_supabase_client() -> httpx.Client:
    supabase_url = env_required("SUPABASE_URL").rstrip("/")
    service_key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        or os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    )
    if not service_key:
        raise RuntimeError("SUPABASE_SERVICE_KEY or SUPABASE_SERVICE_ROLE_KEY is required")

    return httpx.Client(
        base_url=f"{supabase_url}/rest/v1",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        },
        timeout=60.0,
    )


def current_credit_window_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def parse_int_header(headers: httpx.Headers, name: str) -> Optional[int]:
    value = headers.get(name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def usage_from_response(resp: httpx.Response, body: Optional[dict[str, Any]]) -> ApiUsage:
    metadata = (body or {}).get("metadata") or {}
    consumed = metadata.get("apiCallsConsumed")

    credits = parse_int_header(resp.headers, "X-API-Calls-Consumed") or 0
    breakdown: Optional[dict[str, Any]] = None
    if isinstance(consumed, dict):
        credits = int(consumed.get("total") or credits or 0)
        breakdown = consumed.get("breakdown") if isinstance(consumed.get("breakdown"), dict) else None
    elif isinstance(consumed, int):
        credits = consumed

    return ApiUsage(
        credits=credits,
        daily_remaining=parse_int_header(resp.headers, "X-RateLimit-Daily-Remaining"),
        minute_remaining=parse_int_header(resp.headers, "X-RateLimit-Minute-Remaining"),
        breakdown=breakdown,
    )


def get_with_retry(client: httpx.Client, path: str, params: dict[str, Any]) -> Tuple[dict[str, Any], ApiUsage]:
    delay = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.get(path, params=params)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                body_text = resp.text
                try:
                    error_body = resp.json()
                except ValueError:
                    error_body = {}
                error_message = str(error_body.get("error") or error_body.get("message") or body_text)
                daily_remaining = parse_int_header(resp.headers, "X-RateLimit-Daily-Remaining")

                if retry_after:
                    wait = float(retry_after)
                    log.warning("Rate limited by API. Waiting %.1fs...", wait)
                    time.sleep(wait)
                    continue

                if "daily" in error_message.lower() or "credit" in error_message.lower():
                    raise StopBudget(
                        f"API daily credit limit reached for {path}: {error_message}"
                    )

                if daily_remaining == 0:
                    raise StopBudget(f"API daily credit remaining is 0 for {path}")

                wait = delay * 2
                log.warning("Rate limited by API (%s). Waiting %.1fs...", error_message, wait)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            body = resp.json()
            return body, usage_from_response(resp, body)
        except httpx.HTTPStatusError as exc:
            if attempt == MAX_RETRIES:
                raise
            log.warning(
                "HTTP %s from %s. Retry %s/%s in %.1fs",
                exc.response.status_code,
                path,
                attempt,
                MAX_RETRIES,
                delay,
            )
            time.sleep(delay)
            delay *= RETRY_BACKOFF
        except httpx.RequestError as exc:
            if attempt == MAX_RETRIES:
                raise
            log.warning("Request error %s. Retry %s/%s in %.1fs", exc, attempt, MAX_RETRIES, delay)
            time.sleep(delay)
            delay *= RETRY_BACKOFF

    raise RuntimeError(f"Failed to GET {path}")


def supabase_get(sb: httpx.Client, table: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    resp = sb.get(f"/{table}", params=params)
    resp.raise_for_status()
    return resp.json()


def supabase_count(sb: httpx.Client, table: str, filters: dict[str, Any]) -> int:
    params = {"select": "id", **filters}
    resp = sb.get(
        f"/{table}",
        params=params,
        headers={**sb.headers, "Prefer": "count=exact", "Range": "0-0"},
    )
    resp.raise_for_status()
    content_range = resp.headers.get("Content-Range", "")
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[-1]
        return 0 if total == "*" else int(total)
    return len(resp.json())


def supabase_upsert(
    sb: httpx.Client,
    table: str,
    rows: list[dict[str, Any]],
    on_conflict: str,
    return_representation: bool = False,
) -> list[dict[str, Any]]:
    if not rows:
        return []

    prefer = "resolution=merge-duplicates"
    prefer += ",return=representation" if return_representation else ",return=minimal"
    resp = sb.post(
        f"/{table}",
        params={"on_conflict": on_conflict},
        content=json.dumps(rows),
        headers={**sb.headers, "Prefer": prefer},
    )
    if resp.status_code not in (200, 201, 204):
        log.error("Supabase upsert error [%s]: %s - %s", table, resp.status_code, resp.text[:500])
        resp.raise_for_status()
    return resp.json() if return_representation and resp.text else []


def supabase_patch(sb: httpx.Client, table: str, filters: dict[str, Any], values: dict[str, Any]) -> None:
    resp = sb.patch(f"/{table}", params=filters, content=json.dumps(values))
    if resp.status_code not in (200, 204):
        log.error("Supabase patch error [%s]: %s - %s", table, resp.status_code, resp.text[:500])
        resp.raise_for_status()


def create_run(sb: httpx.Client, job_name: str, language: Optional[str], endpoint: str, params: dict[str, Any]) -> Optional[str]:
    rows = supabase_upsert(
        sb,
        "ppt_sync_runs",
        [
            {
                "job_name": job_name,
                "language": language,
                "endpoint": endpoint,
                "params": params,
                "status": "running",
                "started_at": utc_now(),
            }
        ],
        on_conflict="id",
        return_representation=True,
    )
    return rows[0]["id"] if rows else None


def finish_run(
    sb: httpx.Client,
    run_id: Optional[str],
    status: str,
    rows_fetched: int,
    rows_upserted: int,
    credits: int,
    daily_remaining: Optional[int],
    minute_remaining: Optional[int],
    error_message: Optional[str] = None,
) -> None:
    if not run_id:
        return
    supabase_patch(
        sb,
        "ppt_sync_runs",
        {"id": f"eq.{run_id}"},
        {
            "status": status,
            "rows_fetched": rows_fetched,
            "rows_upserted": rows_upserted,
            "api_calls_consumed": credits,
            "daily_remaining": daily_remaining,
            "minute_remaining": minute_remaining,
            "error_message": error_message,
            "finished_at": utc_now(),
        },
    )


def get_checkpoint(sb: httpx.Client, job_name: str, language: str) -> Optional[dict[str, Any]]:
    rows = supabase_get(
        sb,
        "ppt_sync_checkpoints",
        {
            "select": "*",
            "job_name": f"eq.{job_name}",
            "language": f"eq.{language}",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


def update_checkpoint(
    sb: httpx.Client,
    job_name: str,
    language: str,
    status: str,
    current_set_numeric_id: Optional[int] = None,
    current_set_name: Optional[str] = None,
    current_offset: int = 0,
    last_completed_set_numeric_id: Optional[int] = None,
    credits_used_today: int = 0,
    daily_remaining: Optional[int] = None,
    last_error: Optional[str] = None,
) -> None:
    supabase_upsert(
        sb,
        "ppt_sync_checkpoints",
        [
            {
                "job_name": job_name,
                "language": language,
                "status": status,
                "current_set_numeric_id": current_set_numeric_id,
                "current_set_name": current_set_name,
                "current_offset": current_offset,
                "last_completed_set_numeric_id": last_completed_set_numeric_id,
                "credits_used_today": credits_used_today,
                "credit_window_date": current_credit_window_date(),
                "daily_remaining": daily_remaining,
                "last_error": last_error,
                "updated_at": utc_now(),
            }
        ],
        on_conflict="job_name,language",
    )


def to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def map_set(raw: dict[str, Any], language: str) -> dict[str, Any]:
    return {
        "language": language,
        "ppt_id": raw.get("id"),
        "tcg_player_id": raw.get("tcgPlayerId"),
        "tcg_player_numeric_id": raw.get("tcgPlayerNumericId"),
        "name": raw.get("name") or "",
        "series": raw.get("series"),
        "release_date": raw.get("releaseDate"),
        "card_count": raw.get("cardCount"),
        "image_cdn_url": raw.get("imageCdnUrl"),
        "image_cdn_url_200": raw.get("imageCdnUrl200"),
        "image_cdn_url_400": raw.get("imageCdnUrl400"),
        "image_cdn_url_800": raw.get("imageCdnUrl800"),
        "image_url": raw.get("imageUrl"),
        "price_guide_url": raw.get("priceGuideUrl"),
        "has_price_guide": raw.get("hasPriceGuide"),
        "no_price_guide_reason": raw.get("noPriceGuideReason"),
        "api_created_at": raw.get("createdAt"),
        "api_updated_at": raw.get("updatedAt"),
        "raw": raw,
        "synced_at": utc_now(),
    }


def map_card(raw: dict[str, Any], language: str) -> dict[str, Any]:
    prices = raw.get("prices") or {}
    return {
        "language": language,
        "ppt_id": raw.get("id"),
        "tcg_player_id": str(raw.get("tcgPlayerId") or ""),
        "set_numeric_id": raw.get("setId"),
        "set_name": raw.get("setName"),
        "name": raw.get("name") or "",
        "card_number": raw.get("cardNumber"),
        "rarity": raw.get("rarity"),
        "card_type": raw.get("cardType"),
        "pokemon_type": raw.get("pokemonType"),
        "energy_type": raw.get("energyType"),
        "flavor_text": raw.get("flavorText"),
        "hp": to_int(raw.get("hp")),
        "stage": raw.get("stage"),
        "attacks": raw.get("attacks"),
        "weakness": raw.get("weakness"),
        "resistance": raw.get("resistance"),
        "retreat_cost": to_int(raw.get("retreatCost")),
        "artist": raw.get("artist"),
        "tcg_player_url": raw.get("tcgPlayerUrl"),
        "price_market": prices.get("market"),
        "price_low": prices.get("low"),
        "listings": prices.get("listings"),
        "sellers": prices.get("sellers"),
        "primary_printing": prices.get("primaryPrinting"),
        "price_last_updated": prices.get("lastUpdated"),
        "price_was_corrected": prices.get("priceWasCorrected"),
        "variants": raw.get("variants"),
        "printings_available": raw.get("printingsAvailable"),
        "image_cdn_url": raw.get("imageCdnUrl"),
        "image_cdn_url_200": raw.get("imageCdnUrl200"),
        "image_cdn_url_400": raw.get("imageCdnUrl400"),
        "image_cdn_url_800": raw.get("imageCdnUrl800"),
        "image_url": raw.get("imageUrl"),
        "external_catalog_id": raw.get("externalCatalogId"),
        "needs_detailed_scrape": raw.get("needsDetailedScrape"),
        "data_completeness": raw.get("dataCompleteness"),
        "last_scraped_at": raw.get("lastScrapedAt"),
        "api_created_at": raw.get("createdAt"),
        "api_updated_at": raw.get("updatedAt"),
        "raw": raw,
        "synced_at": utc_now(),
    }


def map_price_variants(raw: dict[str, Any], language: str) -> list[dict[str, Any]]:
    card_id = str(raw.get("tcgPlayerId") or "")
    variants = raw.get("variants")
    if not card_id or not isinstance(variants, dict):
        return []

    rows: list[dict[str, Any]] = []
    for printing, value in variants.items():
        if not isinstance(value, dict):
            continue
        rows.append(
            {
                "language": language,
                "card_tcg_player_id": card_id,
                "printing": str(printing),
                "market_price": value.get("marketPrice"),
                "low_price": value.get("lowPrice"),
                "condition_used": value.get("conditionUsed"),
                "raw": value,
                "synced_at": utc_now(),
            }
        )
    return rows


def extract_data_array(body: dict[str, Any]) -> list[dict[str, Any]]:
    data = body.get("data") or []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    return []


def sync_sets(ppt: httpx.Client, sb: httpx.Client, language: str, request_delay: float) -> Tuple[int, int]:
    offset = 0
    total_upserted = 0
    total_credits = 0

    with tqdm(desc=f"sets:{language}", unit="set") as progress:
        while True:
            params = {
                "language": language,
                "limit": SET_LIMIT_MAX,
                "offset": offset,
                "sortBy": "releaseDate",
                "sortOrder": "asc",
            }
            body, usage = get_with_retry(ppt, "/sets", params)
            sets = extract_data_array(body)
            total_credits += usage.credits
            if not sets:
                break

            rows = [map_set(item, language) for item in sets]
            supabase_upsert(sb, "ppt_sets", rows, on_conflict="language,tcg_player_id")
            total_upserted += len(rows)
            progress.update(len(rows))

            metadata = body.get("metadata") or {}
            if not metadata.get("hasMore") or len(sets) < SET_LIMIT_MAX:
                break
            offset += len(sets)
            time.sleep(request_delay)

    return total_upserted, total_credits


def fetch_local_sets(sb: httpx.Client, language: str, set_id: Optional[int] = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "select": "tcg_player_numeric_id,name,card_count,release_date",
        "language": f"eq.{language}",
        "tcg_player_numeric_id": "not.is.null",
        "order": "release_date.asc.nullslast,name.asc",
        "limit": "10000",
    }
    if set_id is not None:
        params["tcg_player_numeric_id"] = f"eq.{set_id}"
    return supabase_get(sb, "ppt_sets", params)


def should_stop_for_budget(credits_used: int, estimated_next_cost: int, max_credits: int) -> bool:
    return credits_used + estimated_next_cost > max_credits


def sync_cards_for_language(
    ppt: httpx.Client,
    sb: httpx.Client,
    language: str,
    job_name: str,
    max_credits: int,
    daily_reserve: int,
    limit: int,
    request_delay: float,
    set_id: Optional[int],
    force: bool,
    variants: bool,
) -> Tuple[int, int, Optional[int], Optional[int]]:
    checkpoint = get_checkpoint(sb, job_name, language)
    if checkpoint and checkpoint.get("credit_window_date") == current_credit_window_date():
        credits_used = int(checkpoint.get("credits_used_today") or 0)
        daily_remaining = checkpoint.get("daily_remaining")
    else:
        credits_used = 0
        daily_remaining = None
    minute_remaining = None

    sets = fetch_local_sets(sb, language, set_id)
    if not sets:
        log.warning("No local ppt_sets found for language=%s. Syncing sets first may be needed.", language)
        return 0, credits_used, daily_remaining, minute_remaining

    rows_upserted = 0
    update_checkpoint(
        sb,
        job_name,
        language,
        status="running",
        credits_used_today=credits_used,
        daily_remaining=daily_remaining,
    )

    for set_row in sets:
        numeric_id = set_row.get("tcg_player_numeric_id")
        set_name = set_row.get("name") or ""
        card_count = set_row.get("card_count")
        if numeric_id is None:
            continue

        existing = 0 if force else supabase_count(
            sb,
            "ppt_cards",
            {"language": f"eq.{language}", "set_numeric_id": f"eq.{numeric_id}"},
        )
        if card_count and existing >= int(card_count) and not force:
            update_checkpoint(
                sb,
                job_name,
                language,
                status="running",
                current_set_numeric_id=numeric_id,
                current_set_name=set_name,
                current_offset=existing,
                last_completed_set_numeric_id=numeric_id,
                credits_used_today=credits_used,
                daily_remaining=daily_remaining,
            )
            continue

        checkpoint_offset = 0
        if checkpoint and checkpoint.get("current_set_numeric_id") == numeric_id:
            checkpoint_offset = int(checkpoint.get("current_offset") or 0)

        # Only trust an offset written by this scraper's checkpoint. Existing
        # partial rows may come from manual tests/searches and are not
        # guaranteed to be the first N rows in the API's sorted set response.
        # Re-fetching from 0 costs a few duplicate upserts, but prevents holes.
        offset = 0 if force else checkpoint_offset
        progress_total = int(card_count) if card_count else None
        with tqdm(total=progress_total, initial=min(offset, progress_total or offset), desc=f"{language}:{set_name[:28]}", unit="card") as progress:
            while True:
                remaining_in_set = None
                if card_count:
                    remaining_in_set = max(int(card_count) - offset, 0)
                    if remaining_in_set == 0:
                        break

                request_limit = min(limit, remaining_in_set) if remaining_in_set is not None else limit
                estimated_cost = request_limit
                if should_stop_for_budget(credits_used, estimated_cost, max_credits):
                    update_checkpoint(
                        sb,
                        job_name,
                        language,
                        status="stopped",
                        current_set_numeric_id=numeric_id,
                        current_set_name=set_name,
                        current_offset=offset,
                        credits_used_today=credits_used,
                        daily_remaining=daily_remaining,
                    )
                    raise StopBudget(
                        f"Stopping before request: credits_used={credits_used}, "
                        f"next_estimated={estimated_cost}, max_credits={max_credits}"
                    )

                params = {
                    "language": language,
                    "setId": str(numeric_id),
                    "limit": request_limit,
                    "offset": offset,
                    "sortBy": "cardNumber",
                    "sortOrder": "asc",
                    "includeHistory": "false",
                    "includeEbay": "false",
                    "includeCardmarket": "false",
                }
                body, usage = get_with_retry(ppt, "/cards", params)
                cards = extract_data_array(body)
                credits_used += usage.credits or request_limit
                daily_remaining = usage.daily_remaining
                minute_remaining = usage.minute_remaining

                if not cards:
                    break

                card_rows = [map_card(card, language) for card in cards]
                supabase_upsert(sb, "ppt_cards", card_rows, on_conflict="language,tcg_player_id")
                if variants:
                    variant_rows = [row for card in cards for row in map_price_variants(card, language)]
                    supabase_upsert(
                        sb,
                        "ppt_card_price_variants",
                        variant_rows,
                        on_conflict="language,card_tcg_player_id,printing",
                    )

                rows_upserted += len(card_rows)
                offset += len(cards)
                progress.update(len(cards))
                update_checkpoint(
                    sb,
                    job_name,
                    language,
                    status="running",
                    current_set_numeric_id=numeric_id,
                    current_set_name=set_name,
                    current_offset=offset,
                    credits_used_today=credits_used,
                    daily_remaining=daily_remaining,
                )

                metadata = body.get("metadata") or {}
                if daily_remaining is not None and daily_remaining <= daily_reserve:
                    update_checkpoint(
                        sb,
                        job_name,
                        language,
                        status="stopped",
                        current_set_numeric_id=numeric_id,
                        current_set_name=set_name,
                        current_offset=offset,
                        credits_used_today=credits_used,
                        daily_remaining=daily_remaining,
                    )
                    raise StopBudget(
                        f"Stopping after saving batch because daily remaining={daily_remaining} <= reserve={daily_reserve}"
                    )

                if not metadata.get("hasMore") or len(cards) < request_limit:
                    break
                time.sleep(request_delay)

        update_checkpoint(
            sb,
            job_name,
            language,
            status="running",
            current_set_numeric_id=numeric_id,
            current_set_name=set_name,
            current_offset=offset,
            last_completed_set_numeric_id=numeric_id,
            credits_used_today=credits_used,
            daily_remaining=daily_remaining,
        )

    update_checkpoint(
        sb,
        job_name,
        language,
        status="completed",
        current_offset=0,
        credits_used_today=credits_used,
        daily_remaining=daily_remaining,
    )
    return rows_upserted, credits_used, daily_remaining, minute_remaining


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resume-able Pokemon Price Tracker basic card scraper for Supabase."
    )
    parser.add_argument("--language", choices=["english", "japanese", "both"], default="both")
    parser.add_argument("--job-name", default=DEFAULT_JOB_NAME)
    parser.add_argument("--max-credits", type=int, default=19000)
    parser.add_argument("--daily-reserve", type=int, default=100)
    parser.add_argument("--limit", type=int, default=CARD_LIMIT_MAX)
    parser.add_argument("--set-id", type=int, help="Only scrape one numeric TCGPlayer GroupId.")
    parser.add_argument("--sets-only", action="store_true", help="Only sync ppt_sets.")
    parser.add_argument("--skip-sets", action="store_true", help="Use existing ppt_sets and skip /sets.")
    parser.add_argument("--force", action="store_true", help="Start each selected set from offset 0 and upsert over existing rows.")
    parser.add_argument("--no-variants", action="store_true", help="Do not populate ppt_card_price_variants.")
    parser.add_argument("--request-delay", type=float, default=1.1, help="Delay between API requests; 1.1s stays under 60 calls/minute.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit < 1 or args.limit > CARD_LIMIT_MAX:
        raise RuntimeError(f"--limit must be between 1 and {CARD_LIMIT_MAX}")

    languages = ["english", "japanese"] if args.language == "both" else [args.language]
    ppt = make_ppt_client()
    sb = make_supabase_client()

    total_rows = 0
    total_credits = 0
    last_daily_remaining = None
    last_minute_remaining = None
    run_id = create_run(
        sb,
        args.job_name,
        None if args.language == "both" else args.language,
        "/cards",
        vars(args),
    )

    try:
        for language in languages:
            if not args.skip_sets:
                upserted_sets, set_credits = sync_sets(ppt, sb, language, args.request_delay)
                total_credits += set_credits
                log.info("Synced %s sets for %s. set_endpoint_credits=%s", upserted_sets, language, set_credits)

            if args.sets_only:
                continue

            rows, credits, daily_remaining, minute_remaining = sync_cards_for_language(
                ppt=ppt,
                sb=sb,
                language=language,
                job_name=args.job_name,
                max_credits=args.max_credits,
                daily_reserve=args.daily_reserve,
                limit=args.limit,
                request_delay=args.request_delay,
                set_id=args.set_id,
                force=args.force,
                variants=not args.no_variants,
            )
            total_rows += rows
            total_credits = max(total_credits, credits)
            last_daily_remaining = daily_remaining
            last_minute_remaining = minute_remaining

        finish_run(
            sb,
            run_id,
            "completed",
            rows_fetched=total_rows,
            rows_upserted=total_rows,
            credits=total_credits,
            daily_remaining=last_daily_remaining,
            minute_remaining=last_minute_remaining,
        )
        log.info("Done. rows_upserted=%s credits_used_or_observed=%s", total_rows, total_credits)
        return 0
    except StopBudget as exc:
        log.warning("%s", exc)
        finish_run(
            sb,
            run_id,
            "stopped",
            rows_fetched=total_rows,
            rows_upserted=total_rows,
            credits=total_credits,
            daily_remaining=last_daily_remaining,
            minute_remaining=last_minute_remaining,
            error_message=str(exc),
        )
        return 0
    except Exception as exc:
        log.exception("Scrape failed")
        finish_run(
            sb,
            run_id,
            "failed",
            rows_fetched=total_rows,
            rows_upserted=total_rows,
            credits=total_credits,
            daily_remaining=last_daily_remaining,
            minute_remaining=last_minute_remaining,
            error_message=str(exc),
        )
        return 1
    finally:
        ppt.close()
        sb.close()


if __name__ == "__main__":
    raise SystemExit(main())
