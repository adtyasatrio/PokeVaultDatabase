#!/usr/bin/env python3
"""Retry and append cards that were previously skipped during catalog export.

This updates an existing Pokémon catalog in-place without rebuilding it from
scratch. It reads the exporter skip log, tries to fetch/embed the missing
cards again, and also auto-discovers any new rows in `pokemon_cards` that are
not already present in the catalog, appending them automatically.

Typical usage:

    ./.venv/bin/python scripts/update_pokemon_cards_catalog_skips.py

By default it looks for:
  - examples/web_scanner/assets/catalog/pokemon-cards-db-skipped.jsonl
  - examples/web_scanner/assets/manifest.db.json

It updates:
  - examples/web_scanner/assets/catalog/pokemon-cards-db-embeddings.f16.bin
  - examples/web_scanner/assets/catalog/pokemon-cards-db-card-ids.json
  - examples/web_scanner/assets/manifest.db.json
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import io
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from PIL import Image

from collector_vision.embedders.neural import NeuralEmbedder

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = ROOT / "catalog"
DEFAULT_MANIFEST_NAME = "manifest.db.json"
DEFAULT_CATALOG_PREFIX = "pokemon-cards-db"
DEFAULT_SKIP_LOG_NAME = "pokemon-cards-db-skipped.jsonl"
DEFAULT_PAGE_SIZE = 200


@dataclass(frozen=True)
class SupabaseConfig:
    base_url: str
    service_key: str


def _load_supabase_config() -> SupabaseConfig:
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT.parent / ".env", override=False)
    base_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not base_url or not service_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
    return SupabaseConfig(base_url.rstrip("/"), service_key)


def _request_json(url: str, service_key: str) -> list[dict]:
    req = urllib.request.Request(
        url,
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_rows(
    config: SupabaseConfig,
    table: str,
    id_column: str,
    name_column: str,
    set_id_column: str,
    image_large_column: str,
    image_small_column: str,
    page_size: int,
) -> list[dict]:
    table_q = urllib.parse.quote(table, safe="")
    columns = ",".join(
        [
            urllib.parse.quote(id_column, safe=""),
            urllib.parse.quote(name_column, safe=""),
            urllib.parse.quote(set_id_column, safe=""),
            urllib.parse.quote(image_large_column, safe=""),
            urllib.parse.quote(image_small_column, safe=""),
        ]
    )
    rows: list[dict] = []
    offset = 0
    while True:
        url = (
            f"{config.base_url}/rest/v1/{table_q}"
            f"?select={columns}&order={urllib.parse.quote(id_column, safe='')}.asc"
            f"&limit={page_size}&offset={offset}"
        )
        batch = _request_json(url, config.service_key)
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        print(f"Fetched {len(rows)} rows...", flush=True)
        if len(batch) < page_size:
            break
    return rows


def _query_single_row(
    base_url: str,
    service_key: str,
    table: str,
    column: str,
    value: str,
) -> dict | None:
    table_q = urllib.parse.quote(table, safe="")
    column_q = urllib.parse.quote(column, safe="")
    value_q = urllib.parse.quote(value, safe="")
    url = f"{base_url}/rest/v1/{table_q}?select=*&{column_q}=eq.{value_q}&limit=1"
    try:
        rows = _request_json(url, service_key)
    except Exception:
        return None
    return rows[0] if rows else None


def _download_image(url: str) -> Image.Image:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "image/*",
            "User-Agent": "CollectorVision/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    return Image.open(io.BytesIO(data)).convert("RGB")


def _download_row_image(card_id: str, large_url: str | None, small_url: str | None) -> tuple[str, Image.Image | None, str | None]:
    last_error: Exception | None = None
    for label, url in (("large", large_url), ("small", small_url)):
        if not url:
            continue
        try:
            return card_id, _download_image(url), None
        except Exception as exc:
            print(f"Download failed for {card_id} ({label}): {exc}", flush=True)
            last_error = exc
    return card_id, None, str(last_error) if "last_error" in locals() else "missing image URL"


def _row_matches_filters(
    row: dict,
    *,
    name_column: str,
    set_id_column: str,
    name_contains: list[str],
    set_name_contains: list[str],
    set_name_cache: dict[str, str | None],
    set_table: str,
    config: SupabaseConfig,
) -> bool:
    name_value = str(row.get(name_column) or row.get("name") or "").lower()
    set_id = str(row.get(set_id_column) or row.get("set_id") or "").strip()

    if name_contains:
        if not any(term.lower() in name_value for term in name_contains):
            return False

    if set_name_contains:
        if not set_id:
            return False
        if set_id not in set_name_cache:
            set_row = _query_single_row(config.base_url, config.service_key, set_table, "id", set_id)
            if set_row is None:
                set_row = _query_single_row(config.base_url, config.service_key, set_table, "ptcgo_code", set_id)
            set_name_cache[set_id] = str((set_row or {}).get("name") or (set_row or {}).get("ptcgo_code") or "")
        set_name = (set_name_cache.get(set_id) or "").lower()
        if not any(term.lower() in set_name for term in set_name_contains):
            return False

    return True


def _load_manifest(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _load_ids(card_ids_path: Path) -> list[str]:
    if not card_ids_path.exists():
        raise FileNotFoundError(f"Card ID file not found: {card_ids_path}")
    return json.loads(card_ids_path.read_text(encoding="utf-8"))


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_skip_entries(skip_log_path: Path) -> list[dict]:
    if not skip_log_path.exists():
        raise FileNotFoundError(f"Skip log not found: {skip_log_path}")
    entries: list[dict] = []
    for line in skip_log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("cardId"):
            entries.append(entry)
    return entries


def _write_skip_entries(skip_log_path: Path, entries: list[dict]) -> None:
    tmp = skip_log_path.with_suffix(".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    tmp.replace(skip_log_path)


def _progress_bar(done: int, total: int, skipped: int) -> str:
    total = max(total, 1)
    width = 28
    ratio = min(max(done / total, 0.0), 1.0)
    filled = int(width * ratio)
    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {done}/{total} ({ratio * 100:5.1f}%) exported={done - skipped} skipped={skipped}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--manifest-name", default=DEFAULT_MANIFEST_NAME)
    parser.add_argument("--catalog-prefix", default=DEFAULT_CATALOG_PREFIX)
    parser.add_argument("--skip-log-name", default=DEFAULT_SKIP_LOG_NAME)
    parser.add_argument("--table", default=os.environ.get("SUPABASE_POKEMON_TABLE", "pokemon_cards"))
    parser.add_argument("--id-column", default=os.environ.get("SUPABASE_POKEMON_ID_COLUMN", "id"))
    parser.add_argument("--name-column", default=os.environ.get("SUPABASE_POKEMON_NAME_COLUMN", "name"))
    parser.add_argument("--set-id-column", default=os.environ.get("SUPABASE_POKEMON_SET_CODE_COLUMN", "set_id"))
    parser.add_argument("--image-large-column", default=os.environ.get("SUPABASE_POKEMON_IMAGE_LARGE_COLUMN", "image_large"))
    parser.add_argument("--image-small-column", default=os.environ.get("SUPABASE_POKEMON_IMAGE_SMALL_COLUMN", "image_small"))
    parser.add_argument("--set-table", default=os.environ.get("SUPABASE_POKEMON_SET_TABLE", "pokemon_sets"))
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--card-id", action="append", default=[], help="Append/recover a specific card id. Repeatable.")
    parser.add_argument("--name-contains", action="append", default=[], help="Append cards whose name contains any of these substrings. Repeatable.")
    parser.add_argument("--set-name-contains", action="append", default=[], help="Append cards whose set name contains any of these substrings. Repeatable.")
    parser.add_argument("--no-recover-skips", action="store_true", help="Skip the skip-log recovery step.")
    parser.add_argument("--no-append-new", action="store_true", help="Skip the new-card append step.")
    parser.add_argument(
        "--no-auto-discover-new",
        action="store_true",
        help="Only append explicit card IDs / filters; do not auto-scan the DB for new rows.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = _load_supabase_config()
    out_dir = args.out_dir.resolve()
    assets_dir = out_dir / "assets"
    catalog_dir = assets_dir / "catalog"
    manifest_path = assets_dir / args.manifest_name
    skip_log_path = catalog_dir / args.skip_log_name

    manifest = _load_manifest(manifest_path)
    catalog = manifest.get("catalog") or {}
    embeddings_path = assets_dir / (catalog.get("embeddings") or f"catalog/{args.catalog_prefix}-embeddings.f16.bin")
    card_ids_path = assets_dir / (catalog.get("card_ids") or f"catalog/{args.catalog_prefix}-card-ids.json")
    if not embeddings_path.exists():
        raise FileNotFoundError(f"Embeddings file not found: {embeddings_path}")

    existing_ids = _load_ids(card_ids_path)
    existing_id_set = set(existing_ids)
    embedder = NeuralEmbedder()
    recovered_ids: list[str] = []
    recovered_embeddings: list[np.ndarray] = []
    unresolved_entries: list[dict] = []

    if not args.no_recover_skips:
        skip_entries = _load_skip_entries(skip_log_path) if skip_log_path.exists() else []
        recoverable: list[dict] = []
        for entry in skip_entries:
            card_id = str(entry.get("cardId") or "").strip()
            if not card_id or card_id in existing_id_set:
                continue
            recoverable.append(entry)

        if recoverable:
            print(f"Recovering {len(recoverable)} skipped cards...", flush=True)
            
            def _recover_task(entry: dict) -> tuple[dict, Image.Image | None, str | None]:
                card_id = str(entry.get("cardId") or "").strip()
                row = _query_single_row(config.base_url, config.service_key, args.table, args.id_column, card_id)
                if row is None:
                    return entry, None, "row not found"
                large_url = row.get(args.image_large_column) or row.get("image_large") or row.get("imageLarge")
                small_url = row.get(args.image_small_column) or row.get("image_small") or row.get("imageSmall")
                _, image, error = _download_row_image(card_id, large_url if isinstance(large_url, str) else None, small_url if isinstance(small_url, str) else None)
                return entry, image, error

            for start in range(0, len(recoverable), args.page_size):
                batch_entries = recoverable[start : start + args.page_size]
                print(f"Recovering batch {start + 1}-{start + len(batch_entries)} of {len(recoverable)}...", flush=True)
                with cf.ThreadPoolExecutor(max_workers=args.max_workers) as pool:
                    results = list(pool.map(_recover_task, batch_entries))
                
                images_to_embed: list[Image.Image] = []
                valid_entries: list[dict] = []
                for entry, image, error in results:
                    if image is None:
                        unresolved_entries.append({**entry, "reason": error or "missing image"})
                    else:
                        images_to_embed.append(image)
                        valid_entries.append(entry)
                
                if images_to_embed:
                    try:
                        batch_emb = embedder.embed(images_to_embed).astype(np.float32, copy=False)
                        for entry, emb in zip(valid_entries, batch_emb):
                            recovered_ids.append(str(entry.get("cardId") or "").strip())
                            recovered_embeddings.append(emb)
                    except Exception as exc:
                        print(f"Batch embedding failed: {exc}. Trying sequentially...", flush=True)
                        for entry, img in zip(valid_entries, images_to_embed):
                            try:
                                emb = embedder.embed(img).astype(np.float32, copy=False)
                                recovered_ids.append(str(entry.get("cardId") or "").strip())
                                recovered_embeddings.append(emb)
                            except Exception as inner_exc:
                                unresolved_entries.append({**entry, "reason": str(inner_exc)})
        elif skip_entries:
            unresolved_entries.extend(skip_entries)

    append_candidates: list[tuple[str, dict]] = []
    if not args.no_append_new:
        explicit_rows: dict[str, dict] = {}
        for card_id in args.card_id:
            row = _query_single_row(config.base_url, config.service_key, args.table, args.id_column, card_id)
            if row is not None:
                explicit_rows[str(card_id)] = row

        filtered_rows: list[dict] = []
        auto_discover = not args.no_auto_discover_new and not (args.card_id or args.name_contains or args.set_name_contains)
        if args.name_contains or args.set_name_contains or auto_discover:
            rows = _fetch_rows(
                config,
                args.table,
                args.id_column,
                args.name_column,
                args.set_id_column,
                args.image_large_column,
                args.image_small_column,
                args.page_size,
            )
            set_name_cache: dict[str, str | None] = {}
            filtered_rows = [
                row for row in rows
                if _row_matches_filters(
                    row,
                    name_column=args.name_column,
                    set_id_column=args.set_id_column,
                    name_contains=args.name_contains,
                    set_name_contains=args.set_name_contains,
                    set_name_cache=set_name_cache,
                    set_table=args.set_table,
                    config=config,
                )
            ]
            if auto_discover:
                print(f"Auto-discovered {len(filtered_rows)} candidate new cards from the database.", flush=True)

        for row in [*explicit_rows.values(), *filtered_rows]:
            card_id = str(row.get(args.id_column) or "").strip()
            if not card_id or card_id in existing_id_set:
                continue
            if any(candidate_id == card_id for candidate_id, _ in append_candidates):
                continue
            append_candidates.append((card_id, row))

        if append_candidates:
            print(f"Appending {len(append_candidates)} new cards...", flush=True)
        elif args.card_id or args.name_contains or args.set_name_contains:
            print("No new cards matched the append criteria.")

    new_ids: list[str] = []
    new_embeddings: list[np.ndarray] = []
    pending_rows = append_candidates
    if pending_rows:
        print(_progress_bar(0, len(pending_rows), len(unresolved_entries)), flush=True)

        def _download_task(task_args: tuple[str, str | None, str | None]) -> tuple[str, Image.Image | None, str | None]:
            return _download_row_image(*task_args)

        for start in range(0, len(pending_rows), args.page_size):
            batch_candidates = pending_rows[start : start + args.page_size]
            tasks: list[tuple[str, str | None, str | None]] = []
            for card_id, row in batch_candidates:
                large_url = row.get(args.image_large_column) or row.get("image_large") or row.get("imageLarge")
                small_url = row.get(args.image_small_column) or row.get("image_small") or row.get("imageSmall")
                tasks.append((card_id, large_url if isinstance(large_url, str) else None, small_url if isinstance(small_url, str) else None))

            with cf.ThreadPoolExecutor(max_workers=args.max_workers) as pool:
                results = list(pool.map(_download_task, tasks))

            images_to_embed: list[Image.Image] = []
            valid_ids: list[str] = []
            for card_id, image, error in results:
                if image is None:
                    unresolved_entries.append({"cardId": card_id, "reason": error or "missing image", "source": "append"})
                else:
                    images_to_embed.append(image)
                    valid_ids.append(card_id)

            if images_to_embed:
                try:
                    batch_emb = embedder.embed(images_to_embed).astype(np.float32, copy=False)
                    for card_id, emb in zip(valid_ids, batch_emb):
                        new_ids.append(card_id)
                        new_embeddings.append(emb)
                except Exception as exc:
                    print(f"Batch embedding failed: {exc}. Trying sequentially...", flush=True)
                    for card_id, img in zip(valid_ids, images_to_embed):
                        try:
                            emb = embedder.embed(img).astype(np.float32, copy=False)
                            new_ids.append(card_id)
                            new_embeddings.append(emb)
                        except Exception as inner_exc:
                            unresolved_entries.append({"cardId": card_id, "reason": str(inner_exc), "source": "append"})

            print(_progress_bar(start + len(batch_candidates), len(pending_rows), len(unresolved_entries)), end="\r", flush=True)
        print()

    total_added = len(recovered_ids) + len(new_ids)
    if total_added == 0:
        if unresolved_entries:
            _write_skip_entries(skip_log_path, unresolved_entries)
        print("No cards were recovered or appended.")
        return

    append_vectors = recovered_embeddings + new_embeddings
    append_ids = recovered_ids + new_ids

    with embeddings_path.open("ab") as embeddings_file:
        embeddings_file.write(np.stack(append_vectors, axis=0).astype("<f2", copy=False).tobytes())

    existing_ids.extend(append_ids)
    card_ids_path.write_text(json.dumps(existing_ids, indent=2), encoding="utf-8")

    manifest["catalog"]["rows"] = len(existing_ids)
    manifest["skipped_rows"] = len(unresolved_entries)
    manifest["recovered_rows"] = manifest.get("recovered_rows", 0) + len(recovered_ids)
    manifest["appended_rows"] = manifest.get("appended_rows", 0) + len(new_ids)
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    _write_skip_entries(skip_log_path, unresolved_entries)

    print(f"Recovered {len(recovered_ids)} cards.")
    print(f"Appended {len(new_ids)} new cards.")
    print(f"Remaining skipped cards: {len(unresolved_entries)}")
    print(f"Updated {embeddings_path}")
    print(f"Updated {card_ids_path}")
    print(f"Updated {manifest_path}")
    print(f"Updated {skip_log_path}")


if __name__ == "__main__":
    main()
