#!/usr/bin/env python3
"""Export a Pokémon scanner catalog directly from Supabase `pokemon_cards`.

This builds a web-scanner compatible bundle:
  - assets/manifest.db.json
  - assets/catalog/pokemon-cards-db-embeddings.f16.bin
  - assets/catalog/pokemon-cards-db-card-ids.json

The catalog is generated from the rows in Supabase, downloading each card's
image and embedding it with the same NeuralEmbedder used by the scanner.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import io
import json
import os
import shutil
import sys
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
DEFAULT_PAGE_SIZE = 200
DEFAULT_MAX_WORKERS = 8
DEFAULT_CHECKPOINT_DIR_NAME = ".pokemon-cards-db-checkpoint"
DEFAULT_SKIP_LOG_NAME = "skipped.jsonl"


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
    url = (
        f"{base_url}/rest/v1/{table_q}"
        f"?select=*&{column_q}=eq.{value_q}&limit=1"
    )
    try:
        rows = _request_json(url, service_key)
    except Exception:
        return None
    return rows[0] if rows else None


def _download_image(url: str) -> Image.Image:
    from curl_cffi import requests
    resp = requests.get(
        url,
        impersonate="chrome",
        timeout=60,
        headers={"Accept": "image/*"},
    )
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content)).convert("RGB")


def _download_row_image(
    card_id: str,
    image_large_url: str | None,
    image_small_url: str | None,
) -> tuple[str, Image.Image | None, str | None]:
    """Download one card image, trying large first and then small."""
    urls = [("large", image_large_url), ("small", image_small_url)]
    last_error: Exception | None = None
    for label, url in urls:
        if not url:
            continue
        try:
            return card_id, _download_image(url), None
        except Exception as exc:
            last_error = exc
            print(f"Download failed for {card_id} ({label}): {exc}", flush=True)
    return card_id, None, str(last_error) if last_error else "missing image URL"


def _fetch_rows(
    config: SupabaseConfig,
    table: str,
    id_column: str,
    name_column: str,
    set_id_column: str,
    image_large_column: str,
    image_small_column: str,
    page_size: int,
    limit: int | None,
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
        if limit is not None and len(rows) >= limit:
            return rows[:limit]
        if len(batch) < page_size:
            break
    return rows


def _image_url_for_row(row: dict, image_large_column: str, image_small_column: str) -> str | None:
    for key in (image_large_column, image_small_column, "image_large", "image_small", "imageLarge", "imageSmall"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


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
        haystack = name_value
        if not any(term.lower() in haystack for term in name_contains):
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


def _load_state(state_path: Path) -> dict | None:
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def _write_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(state_path)


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


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
    parser.add_argument("--table", default=os.environ.get("SUPABASE_POKEMON_TABLE", "pokemon_cards"))
    parser.add_argument("--id-column", default=os.environ.get("SUPABASE_POKEMON_ID_COLUMN", "id"))
    parser.add_argument("--name-column", default=os.environ.get("SUPABASE_POKEMON_NAME_COLUMN", "name"))
    parser.add_argument("--set-id-column", default=os.environ.get("SUPABASE_POKEMON_SET_CODE_COLUMN", "set_id"))
    parser.add_argument(
        "--image-large-column",
        default=os.environ.get("SUPABASE_POKEMON_IMAGE_LARGE_COLUMN", "image_large"),
    )
    parser.add_argument(
        "--image-small-column",
        default=os.environ.get("SUPABASE_POKEMON_IMAGE_SMALL_COLUMN", "image_small"),
    )
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--limit", type=int, default=None, help="Optional max number of rows to export")
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument(
        "--skip-broken-images",
        action="store_true",
        default=True,
        help="Skip rows whose image download/embedding fails (default: on)",
    )
    parser.add_argument(
        "--fail-on-broken-images",
        action="store_false",
        dest="skip_broken_images",
        help="Abort on the first image download/embed failure",
    )
    parser.add_argument(
        "--write-main-manifest",
        action="store_true",
        help="Also copy the generated manifest to assets/manifest.json",
    )
    parser.add_argument(
        "--name-contains",
        action="append",
        default=[],
        help="Keep only cards whose name contains any of these substrings. Repeat for multiple terms.",
    )
    parser.add_argument(
        "--set-name-contains",
        action="append",
        default=[],
        help="Keep only cards whose set name contains any of these substrings. Repeat for multiple terms.",
    )
    parser.add_argument(
        "--checkpoint-dir",
        type=Path,
        default=None,
        help=f"Directory for resume state (default: {DEFAULT_CHECKPOINT_DIR_NAME} under assets/catalog)",
    )
    parser.add_argument(
        "--skip-log-name",
        default=DEFAULT_SKIP_LOG_NAME,
        help="Filename for the skipped-row log inside the checkpoint dir",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = _load_supabase_config()
    out_dir = args.out_dir.resolve()
    assets_dir = out_dir / "assets"
    catalog_dir = assets_dir / "catalog"
    assets_dir.mkdir(parents=True, exist_ok=True)
    catalog_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading rows from {args.table}...", flush=True)
    rows = _fetch_rows(
        config,
        args.table,
        args.id_column,
        args.name_column,
        args.set_id_column,
        args.image_large_column,
        args.image_small_column,
        args.page_size,
        args.limit,
    )
    if not rows:
        raise RuntimeError(f"No rows found in {args.table!r}")

    if args.name_contains or args.set_name_contains:
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
                set_table=os.environ.get("SUPABASE_POKEMON_SET_TABLE", "pokemon_sets"),
                config=config,
            )
        ]
        print(f"Filtered to {len(filtered_rows)} rows from {len(rows)} total rows.", flush=True)
        rows = filtered_rows
        if not rows:
            raise RuntimeError("No rows matched the requested subset filters.")

    embedder = NeuralEmbedder()
    catalog_embeddings_path = catalog_dir / f"{args.catalog_prefix}-embeddings.f16.bin"
    catalog_ids_path = catalog_dir / f"{args.catalog_prefix}-card-ids.json"
    manifest_path = assets_dir / args.manifest_name
    checkpoint_dir = args.checkpoint_dir or (catalog_dir / DEFAULT_CHECKPOINT_DIR_NAME)
    state_path = checkpoint_dir / "state.json"
    ids_tmp_path = checkpoint_dir / "card-ids.jsonl"
    embeddings_tmp_path = checkpoint_dir / "embeddings.f16.bin"
    skip_log_tmp_path = checkpoint_dir / args.skip_log_name
    final_skip_log_path = catalog_dir / f"{args.catalog_prefix}-skipped.jsonl"

    state = _load_state(state_path) or {
        "next_row_index": 0,
        "card_ids": [],
        "skipped_rows": [],
        "rows_total": len(rows),
        "catalog_prefix": args.catalog_prefix,
        "table": args.table,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    start_index = int(state.get("next_row_index", 0))
    card_ids: list[str] = list(state.get("card_ids", []))
    skipped: list[str] = list(state.get("skipped_rows", []))

    # Resume mode appends to the checkpoint files.  Final artifacts are
    # generated only after all rows have been processed successfully.
    ids_tmp_path.parent.mkdir(parents=True, exist_ok=True)
    embeddings_tmp_path.parent.mkdir(parents=True, exist_ok=True)
    ids_mode = "a" if start_index > 0 and ids_tmp_path.exists() else "w"
    emb_mode = "ab" if start_index > 0 and embeddings_tmp_path.exists() else "wb"
    skip_mode = "a" if start_index > 0 and skip_log_tmp_path.exists() else "w"

    print(f"Resume state: row {start_index + 1} of {len(rows)}", flush=True)
    print(_progress_bar(start_index, len(rows), len(skipped)), flush=True)

    with (
        embeddings_tmp_path.open(emb_mode) as embeddings_file,
        ids_tmp_path.open(ids_mode, encoding="utf-8") as ids_file,
        skip_log_tmp_path.open(skip_mode, encoding="utf-8") as skip_file,
    ):
        for start in range(start_index, len(rows), args.page_size):
            batch_rows = rows[start : start + args.page_size]
            tasks: list[tuple[str, str | None, str | None]] = []
            for offset, row in enumerate(batch_rows):
                card_id = str(row.get(args.id_column) or "").strip()
                if not card_id:
                    skipped.append("<missing-id>")
                    skip_file.write(json.dumps({
                        "cardId": "<missing-id>",
                        "reason": "missing id",
                        "rowIndex": start + offset,
                    }, ensure_ascii=False) + "\n")
                    continue
                large_url = row.get(args.image_large_column) or row.get("image_large") or row.get("imageLarge")
                small_url = row.get(args.image_small_column) or row.get("image_small") or row.get("imageSmall")
                tasks.append((card_id, large_url if isinstance(large_url, str) else None, small_url if isinstance(small_url, str) else None))

            if not tasks:
                continue

            print(f"Embedding rows {start + 1}-{start + len(batch_rows)}...", flush=True)
            with cf.ThreadPoolExecutor(max_workers=args.max_workers) as pool:
                results = list(pool.map(lambda task: _download_row_image(*task), tasks))

            images: list[Image.Image] = []
            successful_ids: list[str] = []
            for card_id, image, error in results:
                if image is None:
                    skipped.append(card_id)
                    skip_file.write(json.dumps({
                        "cardId": card_id,
                        "reason": error or "missing image",
                        "rowIndex": start,
                    }, ensure_ascii=False) + "\n")
                    if error:
                        print(f"Skipping {card_id}: {error}", flush=True)
                    continue
                successful_ids.append(card_id)
                images.append(image)

            if not images:
                continue

            try:
                batch_embeddings = embedder.embed(images).astype(np.float32, copy=False)
            except Exception as exc:
                if not args.skip_broken_images:
                    raise
                print(f"Embedding batch failed at rows {start + 1}-{start + len(batch_rows)}: {exc}", flush=True)
                ok_embeddings: list[np.ndarray] = []
                fallback_ids: list[str] = []
                for card_id, image in zip(successful_ids, images):
                    try:
                        ok_embeddings.append(embedder.embed(image).astype(np.float32, copy=False))
                        fallback_ids.append(card_id)
                    except Exception as inner_exc:
                        skipped.append(card_id)
                        skip_file.write(json.dumps({
                            "cardId": card_id,
                            "reason": str(inner_exc),
                            "rowIndex": start,
                        }, ensure_ascii=False) + "\n")
                        print(f"Skipping {card_id}: {inner_exc}", flush=True)
                if not ok_embeddings:
                    continue
                batch_embeddings = np.stack(ok_embeddings, axis=0)
                successful_ids = fallback_ids

            embeddings_file.write(batch_embeddings.astype("<f2", copy=False).tobytes())
            for card_id in successful_ids:
                ids_file.write(json.dumps(card_id) + "\n")
            ids_file.flush()
            embeddings_file.flush()
            card_ids.extend(successful_ids)
            state = {
                "next_row_index": start + len(batch_rows),
                "card_ids": card_ids,
                "skipped_rows": skipped,
                "rows_total": len(rows),
                "catalog_prefix": args.catalog_prefix,
                "table": args.table,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_state(state_path, state)
            print(_progress_bar(start + len(batch_rows), len(rows), len(skipped)), end="\r", flush=True)

    if not card_ids:
        raise RuntimeError("No cards were exported; check image URLs and database rows.")

    catalog_rows = len(card_ids)
    version = datetime.now(timezone.utc).strftime(f"{args.catalog_prefix}-%Y-%m-%d")
    manifest = {
        "version": version,
        "source": "supabase",
        "table": args.table,
        "models": {
            "cornelius": "models/cornelius.onnx",
            "milo": "models/milo.onnx",
        },
        "catalog": {
            "embeddings": f"catalog/{catalog_embeddings_path.name}",
            "card_ids": f"catalog/{catalog_ids_path.name}",
            "rows": catalog_rows,
            "dims": 128,
            "dtype": "float16",
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "skipped_rows": len(skipped),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    catalog_ids_path.write_text(json.dumps(card_ids, indent=2), encoding="utf-8")
    shutil.copy2(skip_log_tmp_path, final_skip_log_path) if skip_log_tmp_path.exists() else None
    shutil.move(embeddings_tmp_path, catalog_embeddings_path)
    ids_tmp_path.unlink(missing_ok=True)
    skip_log_tmp_path.unlink(missing_ok=True)
    state_path.unlink(missing_ok=True)
    try:
        checkpoint_dir.rmdir()
    except OSError:
        pass

    print()

    if args.write_main_manifest:
        shutil.copy2(manifest_path, assets_dir / "manifest.json")

    print(f"Wrote {catalog_embeddings_path}")
    print(f"Wrote {catalog_ids_path}")
    print(f"Wrote {manifest_path}")
    if final_skip_log_path.exists():
        print(f"Wrote {final_skip_log_path}")
    if skipped:
        print(f"Skipped {len(skipped)} rows without usable images.")


if __name__ == "__main__":
    main()
