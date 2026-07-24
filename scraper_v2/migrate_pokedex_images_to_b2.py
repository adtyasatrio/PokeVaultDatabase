#!/usr/bin/env python3
"""Copy canonical TCGCollector Pokedex images to Backblaze B2.

Only one object is stored per National Pokedex number. After each successful
upload, the same B2 path is written to ``pokemon_pokedex_core`` and every
matching language row in ``pokemon_pokedex``. The command is resumable and
repairs a missing B2 object even when the database path is already populated.

The default mode only prints a plan. Pass ``--execute`` to upload and link.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from migrate_tcgcollector_images_to_b2 import (
    ALLOWED_SOURCE_HOST,
    DEFAULT_FREE_LIMIT_BYTES,
    DEFAULT_RESERVE_BYTES,
    IMAGE_SUFFIXES,
    B2NativeClient,
    ConfigValues,
    ImageTask,
    QuotaExceeded,
    QuotaGuard,
    download_image,
    human_size,
    load_config,
    retry,
)


ROOT = Path(__file__).resolve().parent
FAILURE_LOG = ROOT.parent / ".b2-pokedex-image-migration-failures.jsonl"


def request_json(url: str, service_key: str) -> list[dict]:
    request = urllib.request.Request(
        url,
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_table(
    config: ConfigValues,
    table: str,
    columns: str,
    order: str,
    page_size: int,
    limit: int | None = None,
) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        remaining = None if limit is None else max(limit - len(rows), 0)
        if remaining == 0:
            break
        batch_limit = page_size if remaining is None else min(page_size, remaining)
        query = urllib.parse.urlencode(
            {
                "select": columns,
                "order": order,
                "limit": str(batch_limit),
                "offset": str(offset),
            }
        )
        batch = request_json(
            f"{config.supabase_url}/rest/v1/{table}?{query}",
            config.supabase_service_key,
        )
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if len(batch) < batch_limit:
            break
    return rows


def validate_source_url(source_url: str) -> None:
    parsed = urllib.parse.urlsplit(source_url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != ALLOWED_SOURCE_HOST:
        raise ValueError(f"unsupported source host: {parsed.hostname or '<missing>'}")


def object_path(dex_number: int, source_url: str) -> str:
    suffix = Path(urllib.parse.urlsplit(source_url).path).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise ValueError(f"unsupported image extension: {suffix or '<missing>'}")
    return f"pokedex/{dex_number:04d}{suffix}"


def build_tasks(rows: list[dict]) -> tuple[list[ImageTask], list[str], list[str]]:
    tasks: list[ImageTask] = []
    unavailable: list[str] = []
    invalid: list[str] = []
    for row in rows:
        try:
            dex_number = int(row.get("dex_number") or 0)
        except (TypeError, ValueError):
            dex_number = 0
        if dex_number <= 0:
            invalid.append(f"{row.get('id') or '<missing-id>'}: invalid dex number")
            continue
        source_url = str(row.get("image_url") or "").strip()
        if not source_url:
            unavailable.append(f"#{dex_number:04d}: missing source URL")
            continue
        try:
            validate_source_url(source_url)
            destination = object_path(dex_number, source_url)
        except ValueError as exc:
            invalid.append(f"#{dex_number:04d}: {exc}")
            continue
        tasks.append(
            ImageTask(
                card_id=str(dex_number),
                variant="pokedex",
                source_url=source_url,
                object_path=destination,
                db_column="image_b2_path",
            )
        )
    return tasks, unavailable, invalid


def patch_rows(
    config: ConfigValues,
    table: str,
    dex_number: int,
    object_path_value: str,
    expected_minimum: int,
) -> None:
    url = f"{config.supabase_url}/rest/v1/{table}?dex_number=eq.{dex_number}"
    body = json.dumps({"image_b2_path": object_path_value}).encode("utf-8")

    def operation() -> None:
        request = urllib.request.Request(
            url,
            data=body,
            method="PATCH",
            headers={
                "apikey": config.supabase_service_key,
                "Authorization": f"Bearer {config.supabase_service_key}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            rows = json.loads(response.read().decode("utf-8"))
        if len(rows) < expected_minimum:
            raise RuntimeError(
                f"Supabase updated {len(rows)} {table} rows; expected at least {expected_minimum}"
            )

    retry(operation)


def update_pokedex_paths(config: ConfigValues, task: ImageTask) -> None:
    dex_number = int(task.card_id)
    patch_rows(config, "pokemon_pokedex_core", dex_number, task.object_path, 1)
    patch_rows(config, "pokemon_pokedex", dex_number, task.object_path, 1)


def validate_image_bytes(data: bytes, content_type: str) -> None:
    if not data:
        raise ValueError("downloaded an empty image")
    if content_type == "image/webp" and not (
        data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    ):
        raise ValueError("invalid WebP signature")
    if content_type == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid PNG signature")


def upload_and_link(
    client: B2NativeClient,
    config: ConfigValues,
    task: ImageTask,
    timeout: int,
    quota: QuotaGuard,
) -> None:
    data, content_type = download_image(task, timeout)
    validate_image_bytes(data, content_type)
    quota.reserve(len(data))
    client.upload_object(task, data, content_type)
    update_pokedex_paths(config, task)


def run_parallel(
    label: str,
    tasks: list[ImageTask],
    workers: int,
    operation,
    *,
    stop_on_quota: bool = False,
) -> list[str]:
    failures: list[str] = []
    total = len(tasks)
    if not tasks:
        return failures
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(operation, task): task for task in tasks}
        for completed, future in enumerate(cf.as_completed(future_map), start=1):
            task = future_map[future]
            try:
                future.result()
            except cf.CancelledError:
                continue
            except Exception as exc:
                failures.append(f"#{int(task.card_id):04d}: {exc}")
                if stop_on_quota and isinstance(exc, QuotaExceeded):
                    for pending in future_map:
                        pending.cancel()
                    print("Storage safety ceiling reached; pending work was cancelled.", flush=True)
                    break
            if completed % 100 == 0 or completed == total:
                print(
                    f"{label}: {completed:,}/{total:,}; failures={len(failures):,}",
                    flush=True,
                )
    return failures


def write_failures(path: Path, failures: list[str]) -> None:
    if not failures:
        return
    with path.open("w", encoding="utf-8") as handle:
        for failure in failures:
            handle.write(json.dumps({"error": failure}, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--limit", type=int, help="Limit canonical Pokedex rows for a smoke test.")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--free-limit-bytes", type=int, default=DEFAULT_FREE_LIMIT_BYTES)
    parser.add_argument("--reserve-bytes", type=int, default=DEFAULT_RESERVE_BYTES)
    parser.add_argument("--failure-log", type=Path, default=FAILURE_LOG)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.workers < 1 or args.page_size < 1:
        raise ValueError("workers and page-size must be positive")
    if args.reserve_bytes < 0 or args.free_limit_bytes <= args.reserve_bytes:
        raise ValueError("invalid free limit/reserve combination")

    config = load_config()
    client = B2NativeClient(config)
    print(f"Inspecting B2 bucket: {config.b2_bucket}", flush=True)
    existing = client.list_objects()
    existing_bytes = sum(existing.values())
    print(f"Current visible objects: {len(existing):,} ({human_size(existing_bytes)})", flush=True)

    core_rows = fetch_table(
        config,
        "pokemon_pokedex_core",
        "id,dex_number,image_url,image_b2_path",
        "dex_number.asc",
        args.page_size,
        args.limit,
    )
    localized_rows = fetch_table(
        config,
        "pokemon_pokedex",
        "id,dex_number,image_b2_path",
        "dex_number.asc,id.asc",
        args.page_size,
    )
    localized_paths: dict[int, list[str | None]] = defaultdict(list)
    for row in localized_rows:
        localized_paths[int(row["dex_number"])].append(row.get("image_b2_path"))

    tasks, unavailable, invalid = build_tasks(core_rows)
    missing: list[ImageTask] = []
    relink: list[ImageTask] = []
    complete = 0
    core_by_dex = {int(row["dex_number"]): row for row in core_rows}
    for task in tasks:
        dex_number = int(task.card_id)
        if task.object_path not in existing:
            missing.append(task)
            continue
        core_matches = core_by_dex[dex_number].get("image_b2_path") == task.object_path
        language_values = localized_paths.get(dex_number, [])
        languages_match = bool(language_values) and all(
            value == task.object_path for value in language_values
        )
        if core_matches and languages_match:
            complete += 1
        else:
            relink.append(task)

    print(
        f"Pokedex objects: upload={len(missing):,}; relink={len(relink):,}; "
        f"complete={complete:,}; source unavailable={len(unavailable):,}; invalid={len(invalid):,}",
        flush=True,
    )
    if invalid:
        write_failures(args.failure_log, invalid)
        print("Refusing to continue because source URLs are invalid.", file=sys.stderr)
        return 2
    if not args.execute:
        print("Dry-run complete. Re-run with --execute to upload and link Pokedex images.")
        return 0

    relink_failures = run_parallel(
        "Relinking",
        relink,
        args.workers,
        lambda task: update_pokedex_paths(config, task),
    )
    ceiling = args.free_limit_bytes - args.reserve_bytes
    quota = QuotaGuard(existing_bytes, ceiling)
    print(f"Storage safety ceiling: {human_size(ceiling)}", flush=True)
    upload_failures = run_parallel(
        "Uploading",
        missing,
        args.workers,
        lambda task: upload_and_link(client, config, task, args.timeout, quota),
        stop_on_quota=True,
    )
    failures = [*relink_failures, *upload_failures]
    print(f"Reserved visible storage after this run: {human_size(quota.used_bytes)}", flush=True)
    if failures:
        write_failures(args.failure_log, failures)
        print(
            f"Migration stopped with {len(failures):,} failures; rerun safely to resume.",
            file=sys.stderr,
        )
        return 4
    print("Pokedex image migration completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
