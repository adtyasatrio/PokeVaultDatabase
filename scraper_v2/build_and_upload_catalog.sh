#!/bin/bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ENV_FILE="$DIR/../.env"

echo "==============================================="
echo " POKEMON CATALOG - UNIFIED BUILD & UPLOAD "
echo "==============================================="

if [ ! -f "$ENV_FILE" ]; then
    echo "Error: File .env tidak ditemukan di $ENV_FILE"
    exit 1
fi

# Load variables
SUPABASE_URL=$(grep -m 1 '^SUPABASE_URL=' "$ENV_FILE" | cut -d '=' -f 2- | tr -d '"' | tr -d "'")
SUPABASE_SERVICE_KEY=$(grep -m 1 '^SUPABASE_SERVICE_KEY=' "$ENV_FILE" | cut -d '=' -f 2- | tr -d '"' | tr -d "'")

PYTHON_EXEC="$DIR/.venv/bin/python"

if [ ! -f "$PYTHON_EXEC" ]; then
    echo "Error: Python Virtual Environment tidak ditemukan di $DIR/.venv"
    echo "Gunakan system python jika tersedia, atau jalankan setup dependencies."
    PYTHON_EXEC="python3"
fi

echo "Menggunakan Python: $PYTHON_EXEC"

# 1. RUN AI EMBEDDER (Lokal)
echo ""
echo "[1/3] Menjalankan AI Embedder secara lokal..."
cd "$DIR"
mkdir -p catalog/assets/catalog

if [ "$1" == "--full" ]; then
    echo "Menjalankan FULL EXPORT (membutuhkan waktu lama)..."
    "$PYTHON_EXEC" export_pokemon_cards_catalog.py
else
    echo "Menjalankan UPDATE (incremental)..."
    "$PYTHON_EXEC" update_pokemon_cards_catalog_skips.py
fi

# 2. CONVERT KE CVG1 & ZIP
echo ""
echo "[2/3] Konversi ke format CVG1 & Packaging ZIP..."
cd "$DIR/catalog/assets/catalog"
cp "$DIR/convert_to_cvg1.py" .

# Gunakan python lokal untuk konversi
python3 convert_to_cvg1.py

echo "Membuat zip file..."
zip -j collector_vision_catalog.zip tcgplayer_pokemon_catalog.bin cvg1_id_map.json

# 3. UPLOAD KE SUPABASE
echo ""
echo "[3/3] Upload ke Supabase (offline-db)..."
BUCKET_NAME="offline-db"
FILE_PATH="collector_vision_catalog.zip"
UPLOAD_URL="${SUPABASE_URL}/storage/v1/object/${BUCKET_NAME}/${FILE_PATH}"

HTTP_STATUS=$(curl -s -o /tmp/upload_response.json -w "%{http_code}" -X POST "$UPLOAD_URL" \
  -H "Authorization: Bearer ${SUPABASE_SERVICE_KEY}" \
  -H "Content-Type: application/zip" \
  -H "x-upsert: true" \
  --data-binary @"$FILE_PATH")

if [ "$HTTP_STATUS" -eq 200 ] || [ "$HTTP_STATUS" -eq 201 ]; then
    echo "✅ Berhasil Upload! (HTTP $HTTP_STATUS)"
else
    echo "❌ Gagal upload! (HTTP $HTTP_STATUS)"
    cat /tmp/upload_response.json
    exit 1
fi

echo ""
echo "🎉 PROSES SELESAI! Katalog baru telah berhasil di-generate, dikonversi, dan diupload."
