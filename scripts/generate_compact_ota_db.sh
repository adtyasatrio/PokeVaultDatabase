#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Detect and activate Python virtual environment if it exists
if [[ -f ".venv/bin/activate" ]]; then
  echo "==> Activating Python virtual environment (.venv)..."
  source .venv/bin/activate
elif [[ -f "venv/bin/activate" ]]; then
  echo "==> Activating Python virtual environment (venv)..."
  source venv/bin/activate
fi

SKIP_FETCH=0
SKIP_EMBEDDINGS=0
UPLOAD_TO_GITHUB=0
DATABASE_CHANGED=0
DEFAULT_GENERATE_DB_ARGS=()
GENERATE_DB_ARGS=()

usage() {
  cat <<'EOF'
Generate the compact OTA offline scanner database.

Usage:
  scripts/generate_compact_ota_db.sh [options] [-- generate_db.dart args]

Options:
  --skip-fetch        Do not run `dart run generate_db.dart`.
  --skip-embeddings   Do not run `python3 add_tflite_emb.py`.
  --upload            Upload the output DB to GitHub 'latest' release.
  -h, --help          Show this help.

Examples:
  scripts/generate_compact_ota_db.sh
  scripts/generate_compact_ota_db.sh -- --force
  scripts/generate_compact_ota_db.sh -- --limit 25
  POKEMON_TCG_API_KEY=... scripts/generate_compact_ota_db.sh -- --query 'set.id:sv8'

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

# Pastikan folder build dibuat terlebih dahulu untuk menyimpan file log
mkdir -p build

if [[ "$SKIP_FETCH" -eq 0 ]]; then
  echo "==> Fetching card metadata into assets/db/poc.db"
  set +e
  if [[ "${#GENERATE_DB_ARGS[@]}" -gt 0 ]]; then
    dart run scripts/generate_db.dart \
      ${DEFAULT_GENERATE_DB_ARGS[@]+"${DEFAULT_GENERATE_DB_ARGS[@]}"} \
      ${GENERATE_DB_ARGS[@]+"${GENERATE_DB_ARGS[@]}"} \
      | tee build/fetch_log.txt
  else
    dart run scripts/generate_db.dart \
      ${DEFAULT_GENERATE_DB_ARGS[@]+"${DEFAULT_GENERATE_DB_ARGS[@]}"} \
      | tee build/fetch_log.txt
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
  else
    # Periksa log unduhan untuk mendeteksi penambahan atau perubahan kartu baru
    if grep -qE "created=[1-9][0-9]*|updated=[1-9][0-9]*" build/fetch_log.txt; then
      echo "==> New card updates detected in metadata fetch."
      DATABASE_CHANGED=1
    fi
  fi
  rm -f build/fetch_log.txt
else
  echo "==> Skipping card metadata fetch"
fi

if [[ ! -f assets/models/efficientnet_b0.tflite ]]; then
  echo "==> TFLite model missing; generating assets/models/efficientnet_b0.tflite"
  python3 scripts/generate_tflite_model.py
else
  echo "==> TFLite model already exists"
fi

if [[ "$SKIP_EMBEDDINGS" -eq 0 ]]; then
  echo "==> Generating TFLite embeddings into assets/db/poc.db"
  set +e
  python3 scripts/add_tflite_emb.py | tee build/embedding_log.txt
  EMB_STATUS=${PIPESTATUS[0]}
  set -e

  if [[ "$EMB_STATUS" -ne 0 ]]; then
    echo "==> Embedding generation failed"
    exit "$EMB_STATUS"
  fi

  # Periksa log embedding untuk melihat apakah ada embedding baru yang berhasil dibuat
  if grep -qE "Berhasil: [1-9][0-9]*" build/embedding_log.txt; then
    echo "==> New embeddings successfully generated."
    DATABASE_CHANGED=1
  fi
  rm -f build/embedding_log.txt
else
  echo "==> Skipping embedding generation"
fi


echo "==> Creating compact OTA DB"
python3 scripts/create_compact_offline_db.py \
  --source assets/db/poc.db \
  --output build/offline_db/poc_compact.db

if command -v sqlite3 >/dev/null 2>&1; then
  echo "==> Validating compact OTA DB"
  sqlite3 build/offline_db/poc_compact.db \
    "pragma integrity_check; select count(*), count(tflite_emb_q), avg(length(tflite_emb_q)) from cards;"
else
  echo "==> Warning: 'sqlite3' CLI tool not found. Skipping validation check."
  echo "    To install it on Raspberry Pi: sudo apt update && sudo apt install sqlite3"
fi

echo "==> Done"
ls -lh build/offline_db/poc_compact.db

if [[ "$UPLOAD_TO_GITHUB" -eq 1 ]]; then
  if [[ "$DATABASE_CHANGED" -eq 1 ]]; then
    echo "==> Uploading to GitHub Release (latest)..."
    gh release upload latest build/offline_db/poc_compact.db --repo adtyasatrio/PokeVaultDatabase --clobber
    echo "==> Upload completed"
  else
    echo "==> No card updates or new embeddings detected. Skipping GitHub Release upload."
  fi
fi
