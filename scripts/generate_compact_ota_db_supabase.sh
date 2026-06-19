#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SKIP_FETCH=0
SKIP_EMBEDDINGS=0
SOURCE_PATH="build/offline_db/poc_compact.db"
BUCKET="${SUPABASE_BUCKET:-offline-db}"
OBJECT_PATH="${SUPABASE_OBJECT_PATH:-poc_compact.db}"
CACHE_CONTROL="${SUPABASE_CACHE_CONTROL:-3600}"
UPSERT=1
CREATE_BUCKET=0
PUBLIC_BUCKET=0
ENV_FILES=()
GENERATE_DB_ARGS=()

usage() {
  cat <<'EOF'
Generate the compact OTA offline scanner database and upload it to Supabase Storage.

Usage:
  scripts/generate_compact_ota_db_supabase.sh [options] [-- generate_db.dart args]

Options:
  --skip-fetch              Do not run `dart run generate_db.dart`.
  --skip-embeddings         Do not run `python3 add_tflite_emb.py`.
  --bucket <name>           Supabase Storage bucket. Default: offline-db.
  --object-path <path>      Object path inside the bucket. Default: poc_compact.db.
  --source <path>           Local compact DB path. Default: build/offline_db/poc_compact.db.
  --cache-control <value>   Cache-Control value. Default: 3600.
  --no-upsert               Fail if the object already exists.
  --create-bucket           Create the bucket if it is missing.
  --public-bucket           With --create-bucket, create it as a public bucket.
  --env-file <path>         Load Supabase env vars from a dotenv file.
  -h, --help                Show this help.

Required env:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SERVICE_KEY or SUPABASE_SECRET_KEY

Examples:
  scripts/generate_compact_ota_db_supabase.sh
  scripts/generate_compact_ota_db_supabase.sh --bucket offline-db --object-path latest/poc_compact.db
  scripts/generate_compact_ota_db_supabase.sh --skip-fetch --skip-embeddings --create-bucket --public-bucket
  scripts/generate_compact_ota_db_supabase.sh -- --force

Output:
  Supabase Storage object: <bucket>/<object-path>
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-fetch)
      SKIP_FETCH=1
      shift
      ;;
    --skip-embeddings)
      SKIP_EMBEDDINGS=1
      shift
      ;;
    --bucket)
      BUCKET="$2"
      shift 2
      ;;
    --object-path)
      OBJECT_PATH="$2"
      shift 2
      ;;
    --source)
      SOURCE_PATH="$2"
      shift 2
      ;;
    --cache-control)
      CACHE_CONTROL="$2"
      shift 2
      ;;
    --no-upsert)
      UPSERT=0
      shift
      ;;
    --create-bucket)
      CREATE_BUCKET=1
      shift
      ;;
    --public-bucket)
      PUBLIC_BUCKET=1
      shift
      ;;
    --env-file)
      ENV_FILES+=("$2")
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      GENERATE_DB_ARGS+=("$@")
      break
      ;;
    *)
      GENERATE_DB_ARGS+=("$1")
      shift
      ;;
  esac
done

PIPELINE_ARGS=()
if [[ "$SKIP_FETCH" -eq 1 ]]; then
  PIPELINE_ARGS+=("--skip-fetch")
fi
if [[ "$SKIP_EMBEDDINGS" -eq 1 ]]; then
  PIPELINE_ARGS+=("--skip-embeddings")
fi
if [[ "${#GENERATE_DB_ARGS[@]}" -gt 0 ]]; then
  PIPELINE_ARGS+=("--")
  PIPELINE_ARGS+=("${GENERATE_DB_ARGS[@]}")
fi

echo "==> Generating compact DB without GitHub upload"
if [[ "${#PIPELINE_ARGS[@]}" -gt 0 ]]; then
  scripts/generate_compact_ota_db.sh "${PIPELINE_ARGS[@]}"
else
  scripts/generate_compact_ota_db.sh
fi

UPLOAD_ARGS=(
  "--source" "$SOURCE_PATH"
  "--bucket" "$BUCKET"
  "--object-path" "$OBJECT_PATH"
  "--cache-control" "$CACHE_CONTROL"
)
if [[ "$UPSERT" -eq 0 ]]; then
  UPLOAD_ARGS+=("--no-upsert")
fi
if [[ "$CREATE_BUCKET" -eq 1 ]]; then
  UPLOAD_ARGS+=("--create-bucket")
fi
if [[ "$PUBLIC_BUCKET" -eq 1 ]]; then
  UPLOAD_ARGS+=("--public-bucket")
fi
if [[ "${#ENV_FILES[@]}" -gt 0 ]]; then
  for env_file in "${ENV_FILES[@]}"; do
    UPLOAD_ARGS+=("--env-file" "$env_file")
  done
fi

echo "==> Uploading compact DB to Supabase Storage"
python3 scripts/upload_compact_db_to_supabase.py "${UPLOAD_ARGS[@]}"

echo "==> Done"
