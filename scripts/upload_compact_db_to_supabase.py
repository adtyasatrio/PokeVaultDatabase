#!/usr/bin/env python3
import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import Dict
from urllib.parse import quote


DEFAULT_SOURCE = "build/offline_db/poc_compact.db"
DEFAULT_BUCKET = "offline-db"
DEFAULT_OBJECT_PATH = "poc_compact.db"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def storage_headers(api_key: str) -> Dict[str, str]:
    return {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
    }


def normalized_url(url: str) -> str:
    return url.rstrip("/")


def encoded_object_path(path: str) -> str:
    return quote(path.strip("/"), safe="/")


def create_bucket_if_missing(
    supabase_url: str,
    api_key: str,
    bucket: str,
    public: bool,
) -> None:
    import requests

    headers = storage_headers(api_key)
    bucket_url = f"{supabase_url}/storage/v1/bucket/{quote(bucket)}"
    response = requests.get(bucket_url, headers=headers, timeout=30)
    if response.status_code == 200:
        print(f"bucket={bucket} exists")
        return
    if response.status_code != 404:
        raise RuntimeError(
            f"failed to check bucket {bucket}: {response.status_code} {response.text}"
        )

    create_url = f"{supabase_url}/storage/v1/bucket"
    payload = {"id": bucket, "name": bucket, "public": public}
    response = requests.post(
        create_url,
        headers={**headers, "Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=30,
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"failed to create bucket {bucket}: {response.status_code} {response.text}"
        )
    print(f"bucket={bucket} created public={str(public).lower()}")


def upload_file(
    supabase_url: str,
    api_key: str,
    source: Path,
    bucket: str,
    object_path: str,
    cache_control: str,
    upsert: bool,
) -> None:
    import requests

    if not source.exists():
        raise FileNotFoundError(f"source file not found: {source}")

    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    upload_url = (
        f"{supabase_url}/storage/v1/object/{quote(bucket)}/"
        f"{encoded_object_path(object_path)}"
    )
    headers = {
        **storage_headers(api_key),
        "Content-Type": content_type,
        "Cache-Control": cache_control,
        "x-upsert": "true" if upsert else "false",
    }

    with source.open("rb") as file:
        response = requests.post(upload_url, headers=headers, data=file, timeout=300)

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"upload failed: {response.status_code} {response.text}"
        )

    size = source.stat().st_size
    public_url = (
        f"{supabase_url}/storage/v1/object/public/{quote(bucket)}/"
        f"{encoded_object_path(object_path)}"
    )
    print(f"uploaded={bucket}/{object_path}")
    print(f"bytes={size}")
    print(f"public_url={public_url}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload compact offline DB to a Supabase Storage bucket."
    )
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--bucket", default=os.getenv("SUPABASE_BUCKET", DEFAULT_BUCKET))
    parser.add_argument(
        "--object-path",
        default=os.getenv("SUPABASE_OBJECT_PATH", DEFAULT_OBJECT_PATH),
    )
    parser.add_argument(
        "--cache-control",
        default=os.getenv("SUPABASE_CACHE_CONTROL", "3600"),
    )
    parser.add_argument("--env-file", action="append", default=[])
    parser.add_argument("--no-upsert", action="store_true")
    parser.add_argument("--create-bucket", action="store_true")
    parser.add_argument("--public-bucket", action="store_true")
    args = parser.parse_args()

    load_env_file(Path(".env"))
    load_env_file(Path("scraper/.env"))
    for env_file in args.env_file:
        load_env_file(Path(env_file))

    supabase_url = os.getenv("SUPABASE_URL")
    api_key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_SERVICE_KEY")
        or os.getenv("SUPABASE_SECRET_KEY")
    )

    if not supabase_url:
        raise RuntimeError("SUPABASE_URL is required")
    if not api_key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY, SUPABASE_SERVICE_KEY, or "
            "SUPABASE_SECRET_KEY is required"
        )

    supabase_url = normalized_url(supabase_url)
    if args.create_bucket:
        create_bucket_if_missing(
            supabase_url=supabase_url,
            api_key=api_key,
            bucket=args.bucket,
            public=args.public_bucket,
        )

    upload_file(
        supabase_url=supabase_url,
        api_key=api_key,
        source=Path(args.source),
        bucket=args.bucket,
        object_path=args.object_path,
        cache_control=args.cache_control,
        upsert=not args.no_upsert,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
