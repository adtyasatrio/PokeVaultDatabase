#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SKIP_FETCH=0
SKIP_EMBEDDINGS=0
UPLOAD_TO_GITHUB=0
DEFAULT_GENERATE_DB_ARGS=(--page-size 100 --timeout-seconds 180 --retries 12)
GENERATE_DB_ARGS=()

usage() {
  cat <<'EOF'
Generate the compact OTA offline scanner database.

Usage:
  tools/generate_compact_ota_db.sh [options] [-- generate_db.dart args]

Options:
  --skip-fetch        Do not run `dart run generate_db.dart`.
  --skip-embeddings   Do not run `python3 add_tflite_emb.py`.
  --upload            Upload the output DB to GitHub 'latest' release.
  -h, --help          Show this help.

Examples:
  tools/generate_compact_ota_db.sh
  tools/generate_compact_ota_db.sh -- --force
  tools/generate_compact_ota_db.sh -- --limit 25
  POKEMON_TCG_API_KEY=... tools/generate_compact_ota_db.sh -- --query 'set.id:sv8'

Output:
  build/offline_db/poc_compact.db
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
    --upload)
      UPLOAD_TO_GITHUB=1
      shift
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

echo "==> Output will be build/offline_db/poc_compact.db"

if [[ "$SKIP_FETCH" -eq 0 ]]; then
  echo "==> Fetching card metadata into assets/db/poc.db"
  set +e
  if [[ "${#GENERATE_DB_ARGS[@]}" -gt 0 ]]; then
    dart run tools/generate_db.dart "${DEFAULT_GENERATE_DB_ARGS[@]}" "${GENERATE_DB_ARGS[@]}" | tee build/fetch_log.txt
  else
    dart run tools/generate_db.dart "${DEFAULT_GENERATE_DB_ARGS[@]}" | tee build/fetch_log.txt
  fi
  FETCH_STATUS=${PIPESTATUS[0]}
  set -e

  if [[ "$FETCH_STATUS" -ne 0 ]]; then
    if [[ -f assets/db/poc.db ]]; then
      echo "==> Metadata fetch failed, continuing with existing assets/db/poc.db"
    else
      echo "==> Metadata fetch failed and assets/db/poc.db does not exist"
      exit "$FETCH_STATUS"
    fi
  fi

  # Smart Auto-Skip if no updates detected (Optional)
  if grep -q "created=0 updated=0" build/fetch_log.txt; then
    echo "==> No new cards or updates detected. Auto-skipping embedding and upload for efficiency."
    rm -f build/fetch_log.txt
    exit 0
  fi
  rm -f build/fetch_log.txt
else
  echo "==> Skipping card metadata fetch"
fi

if [[ ! -f assets/models/efficientnet_b0.tflite ]]; then
  echo "==> TFLite model missing; generating assets/models/efficientnet_b0.tflite"
  python3 tools/generate_tflite_model.py
else
  echo "==> TFLite model already exists"
fi

if [[ "$SKIP_EMBEDDINGS" -eq 0 ]]; then
  echo "==> Generating TFLite embeddings into assets/db/poc.db"
  python3 tools/add_tflite_emb.py
else
  echo "==> Skipping embedding generation"
fi


echo "==> Creating compact OTA DB"
python3 tools/create_compact_offline_db.py \
  --source assets/db/poc.db \
  --output build/offline_db/poc_compact.db

echo "==> Validating compact OTA DB"
sqlite3 build/offline_db/poc_compact.db \
  "pragma integrity_check; select count(*), count(tflite_emb_q), avg(length(tflite_emb_q)) from cards;"

echo "==> Done"
ls -lh build/offline_db/poc_compact.db

if [[ "$UPLOAD_TO_GITHUB" -eq 1 ]]; then
  echo "==> Uploading to GitHub Release (latest)..."
  gh release upload latest build/offline_db/poc_compact.db --repo adtyasatrio/PokeVaultDatabase --clobber
  echo "==> Upload completed"
fi
