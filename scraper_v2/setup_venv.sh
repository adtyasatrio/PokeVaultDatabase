#!/bin/bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

echo "==============================================="
echo " SETTING UP PYTHON VIRTUAL ENVIRONMENT (.venv) "
echo "==============================================="

if [ -d ".venv" ]; then
    echo "Folder .venv sudah ada! Jika ingin install ulang, hapus folder .venv terlebih dahulu."
    exit 0
fi

echo "1. Membuat Virtual Environment..."
python3 -m venv .venv

echo "2. Mengaktifkan Virtual Environment & Menginstall Dependencies..."
# Aktivasi venv dan install requirements di dalam shell yang sama
source .venv/bin/activate

# Upgrade pip ke versi terbaru (opsional tapi disarankan)
pip install --upgrade pip

# Install dependencies dari requirements.txt
pip install -r requirements.txt

echo ""
echo "✅ Instalasi selesai!"
echo "Model AI dan script Catalog sekarang sudah bisa berjalan."
