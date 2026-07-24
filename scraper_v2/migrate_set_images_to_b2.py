#!/usr/bin/env python3
"""Copy set logos and symbols from TCGCollector to Backblaze B2.

The command is resumable: existing B2 objects are relinked without upload, and
rows whose B2 path is already populated are skipped. Source URL columns are
kept unchanged. The default mode only prints a plan; pass ``--execute`` to
download, upload, and update Supabase.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from migrate_tcgcollector_images_to_b2 import (
    ALLOWED_SOURCE_HOST,
    DEFAULT_FREE_LIMIT_BYTES,
    DEFAULT_RESERVE_BYTES,
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
FAILURE_LOG = ROOT.parent / ".b2-set-image-migration-failures.jsonl"
SET_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}
VARIANTS = (
    ("logo", "logo_url", "logo_b2_path", "set-logos"),
    ("symbol", "symbol_url", "symbol_b2_path", "set-symbols"),
)


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


def fetch_sets(config: ConfigValues, page_size: int, limit: int | None) -> list[dict]:
    columns = "id,logo_url,symbol_url,logo_b2_path,symbol_b2_path"
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
                "order": "id.asc",
                "limit": str(batch_limit),
                "offset": str(offset),
                "or": "(logo_b2_path.is.null,symbol_b2_path.is.null)",
            }
        )
        batch = request_json(
            f"{config.supabase_url}/rest/v1/pokemon_sets?{query}",
            config.supabase_service_key,
        )
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if len(batch) < batch_limit:
            break
    return rows


def safe_set_id(set_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", set_id)


def validate_source_url(source_url: str) -> None:
    parsed = urllib.parse.urlsplit(source_url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != ALLOWED_SOURCE_HOST:
        raise ValueError(f"unsupported source host: {parsed.hostname or '<missing>'}")


def object_path(set_id: str, prefix: str, source_url: str) -> str:
    suffix = Path(urllib.parse.urlsplit(source_url).path).suffix.lower()
    if suffix not in SET_IMAGE_SUFFIXES:
        raise ValueError(f"unsupported image extension: {suffix or '<missing>'}")
    return f"{prefix}/{safe_set_id(set_id)}{suffix}"


def build_tasks(rows: list[dict]) -> tuple[list[ImageTask], list[str], list[str]]:
    tasks: list[ImageTask] = []
    unavailable: list[str] = []
    invalid: list[str] = []
    for row in rows:
        set_id = str(row.get("id") or "").strip()
        if not set_id:
            invalid.append("<missing-id>: missing set id")
            continue
        for variant, source_column, target_column, prefix in VARIANTS:
            if row.get(target_column):
                continue
            source_url = str(row.get(source_column) or "").strip()
            if not source_url:
                unavailable.append(f"{set_id}/{variant}: missing source URL")
                continue
            try:
                validate_source_url(source_url)
                destination = object_path(set_id, prefix, source_url)
            except ValueError as exc:
                invalid.append(f"{set_id}/{variant}: {exc}")
                continue
            tasks.append(
                ImageTask(
                    card_id=set_id,
                    variant=variant,
                    source_url=source_url,
                    object_path=destination,
                    db_column=target_column,
                )
            )
    return tasks, unavailable, invalid


def update_set_path(config: ConfigValues, task: ImageTask) -> None:
    set_filter = urllib.parse.quote(task.card_id, safe="")
    url = f"{config.supabase_url}/rest/v1/pokemon_sets?id=eq.{set_filter}"
    body = json.dumps({task.db_column: task.object_path}).encode("utf-8")

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
        if len(rows) != 1:
            raise RuntimeError(f"Supabase updated {len(rows)} rows instead of one")

    retry(operation)


def validate_image_bytes(data: bytes, content_type: str) -> None:
    if not data:
        raise ValueError("downloaded an empty image")
    if content_type == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid PNG signature")
    if content_type == "image/webp" and not (
        data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    ):
        raise ValueError("invalid WebP signature")
    if content_type == "image/gif" and not data.startswith((b"GIF87a", b"GIF89a")):
        raise ValueError("invalid GIF signature")
    if content_type == "image/svg+xml":
        prefix = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        if b"<svg" not in prefix:
            raise ValueError("invalid SVG document")


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
    update_set_path(config, task)


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
                failures.append(f"{task.card_id}/{task.variant}: {exc}")
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
    parser.add_argument("--limit", type=int, help="Limit sets for a smoke test.")
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

    rows = fetch_sets(config, args.page_size, args.limit)
    tasks, unavailable, invalid = build_tasks(rows)
    already_uploaded = [task for task in tasks if task.object_path in existing]
    missing = [task for task in tasks if task.object_path not in existing]
    logo_count = sum(task.variant == "logo" for task in missing)
    symbol_count = sum(task.variant == "symbol" for task in missing)
    print(
        f"Pending uploads: logos={logo_count:,}, symbols={symbol_count:,}; "
        f"relink={len(already_uploaded):,}; source unavailable={len(unavailable):,}; "
        f"invalid={len(invalid):,}",
        flush=True,
    )

    if invalid:
        write_failures(args.failure_log, invalid)
        print("Refusing to continue because source URLs are invalid.", file=sys.stderr)
        return 2
    if not args.execute:
        print("Dry-run complete. Re-run with --execute to upload and link set images.")
        return 0

    relink_failures = run_parallel(
        "Relinking",
        already_uploaded,
        args.workers,
        lambda task: update_set_path(config, task),
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
    print("Set logo and symbol migration completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
