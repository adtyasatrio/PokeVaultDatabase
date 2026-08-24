#!/usr/bin/env python3
"""Copy TCGCollector card images to a private Backblaze B2 bucket.

The script is resumable without a local checkpoint:

* B2 object keys show which images have already been uploaded.
* ``pokemon_cards.image_*_b2_path`` shows which objects are linked.
* Source URLs in ``image_small`` and ``image_large`` are never changed.

By default this is a preflight-only command. Pass ``--execute`` to upload after
the full missing-image size is known to fit below the configured free-tier
limit and reserve.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import hashlib
import json
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
DEFAULT_FREE_LIMIT_BYTES = 10_000_000_000
DEFAULT_RESERVE_BYTES = 250_000_000
ALLOWED_SOURCE_HOST = "static.tcgcollector.com"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True)
class ConfigValues:
    supabase_url: str
    supabase_service_key: str
    b2_bucket: str
    b2_endpoint: str
    b2_key_id: str
    b2_application_key: str


@dataclass(frozen=True)
class ImageTask:
    card_id: str
    variant: str
    source_url: str
    object_path: str
    db_column: str
    expected_size: int = 0


class QuotaExceeded(RuntimeError):
    """Raised before an upload would cross the configured storage ceiling."""


class QuotaGuard:
    def __init__(self, used_bytes: int, ceiling_bytes: int):
        self._lock = threading.Lock()
        self.used_bytes = used_bytes
        self.ceiling_bytes = ceiling_bytes

    def reserve(self, size: int) -> None:
        with self._lock:
            projected = self.used_bytes + size
            if projected > self.ceiling_bytes:
                raise QuotaExceeded(
                    f"upload would reach {human_size(projected)}, above "
                    f"the {human_size(self.ceiling_bytes)} safety ceiling"
                )
            self.used_bytes = projected


class B2NativeClient:
    """Minimal B2 Native API v4 client for listing and uploading small files."""

    def __init__(self, config: ConfigValues):
        self.config = config
        self._auth_lock = threading.Lock()
        self._thread_local = threading.local()
        self.account_id = ""
        self.api_url = ""
        self.authorization_token = ""
        self.bucket_id = ""
        self._authorize()

    @staticmethod
    def _read_json(request: urllib.request.Request, timeout: int = 90) -> dict:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _authorize(self) -> None:
        credentials = f"{self.config.b2_key_id}:{self.config.b2_application_key}".encode("utf-8")
        request = urllib.request.Request(
            "https://api.backblazeb2.com/b2api/v4/b2_authorize_account",
            headers={"Authorization": f"Basic {base64.b64encode(credentials).decode('ascii')}"},
        )
        payload = self._read_json(request)
        storage = payload["apiInfo"]["storageApi"]
        expected_s3_endpoint = str(storage.get("s3ApiUrl") or "").rstrip("/")
        if expected_s3_endpoint and expected_s3_endpoint != self.config.b2_endpoint:
            raise RuntimeError(
                "B2_S3_ENDPOINT does not match the endpoint returned by Backblaze for this key"
            )
        allowed = storage.get("allowed") or {}
        buckets = allowed.get("buckets") or []
        bucket = next(
            (item for item in buckets if item.get("name") == self.config.b2_bucket),
            None,
        )
        if bucket is None:
            raise RuntimeError(
                f"Application key is not scoped to B2 bucket {self.config.b2_bucket!r}"
            )
        capabilities = set(allowed.get("capabilities") or [])
        required = {"listFiles", "writeFiles"}
        missing = sorted(required - capabilities)
        if missing:
            raise RuntimeError(f"B2 application key lacks capabilities: {', '.join(missing)}")
        self.account_id = str(payload["accountId"])
        self.api_url = str(storage["apiUrl"]).rstrip("/")
        self.authorization_token = str(payload["authorizationToken"])
        self.bucket_id = str(bucket["id"])

    def _refresh_authorization(self) -> None:
        with self._auth_lock:
            self._authorize()

    def _api_json(self, endpoint: str, query: dict[str, str] | None = None) -> dict:
        for attempt in range(2):
            suffix = ""
            if query:
                suffix = "?" + urllib.parse.urlencode(query)
            request = urllib.request.Request(
                f"{self.api_url}/b2api/v4/{endpoint}{suffix}",
                headers={"Authorization": self.authorization_token},
            )
            try:
                return self._read_json(request)
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    self._refresh_authorization()
                    continue
                raise
        raise RuntimeError(f"B2 API request failed: {endpoint}")

    def list_objects(self) -> dict[str, int]:
        objects: dict[str, int] = {}
        start_file_name: str | None = None
        while True:
            query = {"bucketId": self.bucket_id, "maxFileCount": "10000"}
            if start_file_name:
                query["startFileName"] = start_file_name
            payload = self._api_json("b2_list_file_names", query)
            for item in payload.get("files", []):
                if item.get("action") == "upload":
                    objects[str(item["fileName"])] = int(item.get("contentLength") or 0)
            start_file_name = payload.get("nextFileName")
            if not start_file_name:
                return objects

    def _get_upload_target(self) -> tuple[str, str]:
        payload = self._api_json("b2_get_upload_url", {"bucketId": self.bucket_id})
        return str(payload["uploadUrl"]), str(payload["authorizationToken"])

    def upload_object(self, task: ImageTask, data: bytes, content_type: str) -> None:
        for attempt in range(4):
            try:
                target = getattr(self._thread_local, "upload_target", None)
                if target is None:
                    target = self._get_upload_target()
                    self._thread_local.upload_target = target
                upload_url, upload_token = target
                request = urllib.request.Request(
                    upload_url,
                    data=data,
                    method="POST",
                    headers={
                        "Authorization": upload_token,
                        "X-Bz-File-Name": urllib.parse.quote(task.object_path, safe="/"),
                        "X-Bz-Content-Sha1": hashlib.sha1(data).hexdigest(),
                        "Content-Type": content_type,
                        "Content-Length": str(len(data)),
                    },
                )
                payload = self._read_json(request, timeout=120)
                if payload.get("fileName") != task.object_path:
                    raise RuntimeError("B2 returned an unexpected file name")
                return
            except urllib.error.HTTPError as exc:
                self._thread_local.upload_target = None
                if attempt < 3 and exc.code in (401, 408, 429, 500, 503):
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                self._thread_local.upload_target = None
                if attempt < 3:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise
        raise RuntimeError("B2 upload failed after retries")


def load_config() -> ConfigValues:
    load_dotenv(ROOT.parent / ".env", override=False)
    names = (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_KEY",
        "B2_BUCKET",
        "B2_S3_ENDPOINT",
        "B2_KEY_ID",
        "B2_APPLICATION_KEY",
    )
    values = {name: (os.environ.get(name) or "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
    return ConfigValues(
        supabase_url=values["SUPABASE_URL"].rstrip("/"),
        supabase_service_key=values["SUPABASE_SERVICE_KEY"],
        b2_bucket=values["B2_BUCKET"],
        b2_endpoint=values["B2_S3_ENDPOINT"].rstrip("/"),
        b2_key_id=values["B2_KEY_ID"],
        b2_application_key=values["B2_APPLICATION_KEY"],
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


def fetch_cards(config: ConfigValues, page_size: int, limit: int | None) -> list[dict]:
    columns = ",".join(
        (
            "id",
            "image_small",
            "image_large",
            "image_small_b2_path",
            "image_large_b2_path",
        )
    )
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
                "or": "(image_small_b2_path.is.null,image_large_b2_path.is.null)",
            }
        )
        url = f"{config.supabase_url}/rest/v1/pokemon_cards?{query}"
        batch = request_json(url, config.supabase_service_key)
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        print(f"Fetched {len(rows):,} cards needing B2 paths...", flush=True)
        if len(batch) < batch_limit:
            break
    return rows


def validate_source_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != ALLOWED_SOURCE_HOST:
        raise ValueError(f"unsupported source host: {parsed.hostname or '<missing>'}")


def safe_card_id(card_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", card_id)


def object_path(card_id: str, variant: str, source_url: str) -> str:
    suffix = Path(urllib.parse.urlsplit(source_url).path).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        suffix = ".jpg"
    return f"{variant}/{safe_card_id(card_id)}{suffix}"


def build_tasks(rows: list[dict]) -> tuple[list[ImageTask], list[str], list[str]]:
    tasks: list[ImageTask] = []
    unavailable: list[str] = []
    invalid: list[str] = []
    variants = (
        ("small", "image_small", "image_small_b2_path"),
        ("hires", "image_large", "image_large_b2_path"),
    )
    for row in rows:
        card_id = str(row.get("id") or "").strip()
        if not card_id:
            invalid.append("<missing-id>: missing card id")
            continue
        for variant, source_column, target_column in variants:
            if row.get(target_column):
                continue
            source_url = str(row.get(source_column) or "").strip()
            if not source_url:
                unavailable.append(f"{card_id}/{variant}: missing source URL")
                continue
            try:
                validate_source_url(source_url)
            except ValueError as exc:
                invalid.append(f"{card_id}/{variant}: {exc}")
                continue
            tasks.append(
                ImageTask(
                    card_id=card_id,
                    variant=variant,
                    source_url=source_url,
                    object_path=object_path(card_id, variant, source_url),
                    db_column=target_column,
                )
            )
    return tasks, unavailable, invalid


def retry(operation, attempts: int = 4):
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 8))
    assert last_error is not None
    raise last_error


def measure_task(task: ImageTask, timeout: int) -> ImageTask:
    def operation() -> int:
        from curl_cffi import requests
        response = requests.head(
            task.source_url,
            headers={"Accept": "image/*", "User-Agent": "PokeVaultImageMigrator/1.0"},
            timeout=timeout,
            impersonate="chrome110"
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        size = int(response.headers.get("Content-Length") or 0)
        if not content_type.startswith("image/"):
            raise ValueError(f"unexpected content type: {content_type}")
        if size <= 0:
            raise ValueError("source did not return Content-Length")
        return size

    return ImageTask(**{**task.__dict__, "expected_size": retry(operation)})


def update_card_path(config: ConfigValues, task: ImageTask) -> None:
    card_filter = urllib.parse.quote(task.card_id, safe="")
    url = f"{config.supabase_url}/rest/v1/pokemon_cards?id=eq.{card_filter}"
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
                "Prefer": "return=minimal",
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status not in (200, 204):
                raise RuntimeError(f"Supabase PATCH returned HTTP {response.status}")

    retry(operation)


def download_image(task: ImageTask, timeout: int) -> tuple[bytes, str]:
    def operation() -> tuple[bytes, str]:
        from curl_cffi import requests
        response = requests.get(
            task.source_url,
            headers={"Accept": "image/*", "User-Agent": "PokeVaultImageMigrator/1.0"},
            timeout=timeout,
            impersonate="chrome110"
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        data = response.content
        if not content_type.startswith("image/"):
            raise ValueError(f"unexpected content type: {content_type}")
        if task.expected_size > 0 and len(data) != task.expected_size:
            raise ValueError(
                f"source size changed after preflight: expected {task.expected_size}, got {len(data)}"
            )
        return data, content_type

    return retry(operation)


def upload_task(
    client: B2NativeClient,
    config: ConfigValues,
    task: ImageTask,
    timeout: int,
    quota_guard: QuotaGuard | None = None,
) -> None:
    data, content_type = download_image(task, timeout)
    if quota_guard is not None:
        # Reservations are intentionally not released on an ambiguous failure:
        # B2 may have stored the object even if the following DB update failed.
        # A rerun rebuilds the exact visible-byte count from B2.
        quota_guard.reserve(len(data))
    client.upload_object(
        task,
        data,
        content_type or mimetypes.guess_type(task.object_path)[0] or "image/jpeg",
    )
    update_card_path(config, task)


def write_failures(path: Path, failures: list[str]) -> None:
    if not failures:
        return
    with path.open("w", encoding="utf-8") as handle:
        for failure in failures:
            handle.write(json.dumps({"error": failure}, ensure_ascii=False) + "\n")


def human_size(value: int) -> str:
    return f"{value / 1_000_000_000:.3f} GB"


def parallel_map(
    label: str,
    tasks: list[ImageTask],
    workers: int,
    function,
    *,
    stop_on_quota: bool = False,
):
    results: list = []
    failures: list[str] = []
    total = len(tasks)
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(function, task): task for task in tasks}
        for done, future in enumerate(cf.as_completed(future_map), start=1):
            task = future_map[future]
            try:
                result = future.result()
                if result is not None:
                    results.append(result)
            except Exception as exc:
                failures.append(f"{task.card_id}/{task.variant}: {exc}")
                if stop_on_quota and isinstance(exc, QuotaExceeded):
                    for pending in future_map:
                        pending.cancel()
                    print("Storage safety ceiling reached; pending uploads were cancelled.", flush=True)
                    break
            if done % 250 == 0 or done == total:
                print(f"{label}: {done:,}/{total:,}; failures={len(failures):,}", flush=True)
    return results, failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Upload after a successful full preflight.")
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Skip HEAD preflight and upload immediately with a per-file quota guard.",
    )
    parser.add_argument("--limit", type=int, help="Limit cards for a smoke test.")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--free-limit-bytes", type=int, default=DEFAULT_FREE_LIMIT_BYTES)
    parser.add_argument("--reserve-bytes", type=int, default=DEFAULT_RESERVE_BYTES)
    parser.add_argument(
        "--failure-log",
        type=Path,
        default=ROOT / ".b2-image-migration-failures.jsonl",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.workers < 1 or args.page_size < 1:
        raise ValueError("workers and page-size must be positive")
    if args.reserve_bytes < 0 or args.free_limit_bytes <= args.reserve_bytes:
        raise ValueError("invalid free limit/reserve combination")

    config = load_config()
    client = B2NativeClient(config)
    print(f"Inspecting private B2 bucket: {config.b2_bucket}", flush=True)
    existing = client.list_objects()
    existing_bytes = sum(existing.values())
    print(f"Current visible B2 objects: {len(existing):,} ({human_size(existing_bytes)})", flush=True)

    rows = fetch_cards(config, args.page_size, args.limit)
    tasks, unavailable, invalid = build_tasks(rows)
    already_uploaded = [task for task in tasks if task.object_path in existing]
    missing = [task for task in tasks if task.object_path not in existing]
    print(
        f"Images: missing={len(missing):,}, already in B2 but unlinked={len(already_uploaded):,}, "
        f"source unavailable={len(unavailable):,}, invalid={len(invalid):,}",
        flush=True,
    )

    ceiling = args.free_limit_bytes - args.reserve_bytes
    if invalid:
        write_failures(args.failure_log, invalid)
        print(f"Refusing to continue with {len(invalid):,} invalid source URLs.", file=sys.stderr)
        return 2

    if args.direct:
        print(f"Direct mode safety ceiling: {human_size(ceiling)}", flush=True)
        _, relink_failures = parallel_map(
            "Relinking",
            already_uploaded,
            args.workers,
            lambda task: update_card_path(config, task),
        )
        quota_guard = QuotaGuard(existing_bytes, ceiling)
        _, upload_failures = parallel_map(
            "Uploading directly",
            missing,
            args.workers,
            lambda task: upload_task(client, config, task, args.timeout, quota_guard),
            stop_on_quota=True,
        )
        failures = [*relink_failures, *upload_failures]
        print(f"Reserved visible storage after this run: {human_size(quota_guard.used_bytes)}", flush=True)
        if failures:
            write_failures(args.failure_log, failures)
            print(
                f"Direct migration stopped with {len(failures):,} failures; rerun safely to resume.",
                file=sys.stderr,
            )
            return 4
        print("Direct migration completed successfully.")
        return 0

    measured, measure_failures = parallel_map(
        "Preflight",
        missing,
        args.workers,
        lambda task: measure_task(task, args.timeout),
    )
    failures = [*invalid, *measure_failures]
    planned_bytes = sum(task.expected_size for task in measured)
    projected = existing_bytes + planned_bytes
    print(f"Planned upload: {human_size(planned_bytes)}", flush=True)
    print(f"Projected visible storage: {human_size(projected)}", flush=True)
    print(f"Safety ceiling: {human_size(ceiling)}", flush=True)

    if failures:
        write_failures(args.failure_log, failures)
        print(f"Preflight failed for {len(failures):,} images; see {args.failure_log}", file=sys.stderr)
        return 2
    if projected > ceiling:
        print(
            "Refusing to upload: projected storage exceeds the free-tier ceiling after reserve.",
            file=sys.stderr,
        )
        return 3
    if not args.execute:
        print("Preflight passed. Re-run with --execute to upload and link the paths.")
        return 0

    _, relink_failures = parallel_map(
        "Relinking",
        already_uploaded,
        args.workers,
        lambda task: update_card_path(config, task),
    )
    _, upload_failures = parallel_map(
        "Uploading",
        measured,
        args.workers,
        lambda task: upload_task(client, config, task, args.timeout),
    )
    failures = [*relink_failures, *upload_failures]
    if failures:
        write_failures(args.failure_log, failures)
        print(f"Migration completed with {len(failures):,} failures; rerun safely to resume.", file=sys.stderr)
        return 4

    print("Migration completed successfully. All uploaded objects are linked in pokemon_cards.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
