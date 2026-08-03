# Pokemon TCG Scraper V2

Folder `scraper_v2` ini adalah **Command Center Terpusat** untuk mengelola seluruh *database* Pokemon TCG Anda. Mulai dari men-*scrape* kartu baru, menyinkronkan harga pasar, hingga me-*generate* dan meng-upload Katalog Model AI untuk aplikasi Scanner.

Kelebihan sistem v2 ini:
- **100% Independen:** Seluruh *tools*, script Python, Model AI (ONNX), dan *Virtual Environment* sudah dikurung secara fisik di folder ini. Anda tidak perlu repot berpindah ke *project* lain (seperti CollectorVision atau PokeVaultFlutter).
- **Fully Automated:** Proses generate katalog hingga *upload* ke *Supabase Storage* berjalan hanya dengan satu baris perintah.
- **Anti-bot & Cloudflare Bypass:** Script *scraper* dilengkapi *stealth mode* dan fitur *retry* jika terblokir.

---

## 🛑 Persiapan Awal (Wajib)

Sebelum bisa menggunakan semua *tools* di sini, jalankan 2 persiapan berikut di terminal pada folder `PokeVaultDatabase` (root):

**1. Install Dependencies Scraper (Node.js)**
```bash
npm install
# Jika ada error Chrome tidak ditemukan, jalankan: npx puppeteer browsers install chrome
```

**2. Setup File `.env`**
Pastikan Anda memiliki file `.env` di folder root (`PokeVaultDatabase`) yang minimal berisi kredensial Supabase Anda:
```env
SUPABASE_URL=https://<project_id>.supabase.co
SUPABASE_SERVICE_KEY=eyJ...
```

*(Catatan: Anda sudah tidak memerlukan environment `COLLECTOR_VISION_PATH` lagi karena model AI sudah tertanam di folder ini).*

---

## 🚀 STEP-BY-STEP WORKFLOW

Jika ada rilis kartu/set Pokemon baru, Anda hanya perlu menjalankan 3 langkah berurutan di bawah ini:

### Step 1: Scrape Data Kartu Baru
Gunakan command ini untuk mengambil data set/kartu dari TCGCollector dan memasukkannya ke database Supabase Anda.

```bash
# Scrape Set spesifik dan paksa timpa data lama (contoh Set ID 11811)
TARGET_SET="11811" FORCE_SCRAPE=true node scraper_v2/scrape_tcgcollector_full.js --en
```
*Gunakan flag `--jp` untuk set Jepang, atau `--id` untuk set Indonesia.*

### Step 2: Sinkronisasi Harga Pasar (Opsional)
Untuk memperbarui kolom `market_price` di database agar sesuai dengan harga *live* di TCGPlayer.

```bash
# Sinkronkan harga untuk semua kartu berbahasa Inggris di database
node scraper_v2/sync_tcgcollector_prices.js --en

# Kartu Jepang
node scraper_v2/sync_tcgcollector_prices.js --jp
```

Secara default, kartu yang sudah diperbarui dalam 24 jam terakhir dilewati.
Gunakan `--force` untuk mengambil ulang semuanya. Sebelum menjalankan sinkronisasi
penuh, tes koneksi API tanpa menulis ke database:

```bash
node scraper_v2/sync_tcgcollector_prices.js --jp --dry-run --limit 10 --force
```

Jangan menjalankan beberapa bahasa secara bersamaan. Kecepatan request dapat
diatur melalui `TCG_PRICE_CONCURRENCY` dan `TCG_PRICE_INTERVAL_MS`, tetapi nilai
default sengaja dibuat konservatif agar tidak memicu pemblokiran TCGPlayer.

### Step 3: Generate & Upload Katalog AI (Scanner)
Agar aplikasi pendeteksi kartu Anda bisa mengenali kartu-kartu baru yang baru saja di-*scrape*, katalog AI harus diperbarui.

```bash
# Lakukan Update Incremental (Hanya memproses kartu baru) & Upload langsung
./scraper_v2/build_and_upload_catalog.sh
```
*(Proses ini akan menjalankan AI Embedder secara lokal, mengonversinya ke format ringan `CVG1` untuk mobile, menge-*zip*, dan langsung mengunggahnya ke Supabase).*

> **Catatan:** Jika Anda ingin memaksa AI untuk mengekspor/me-*rebuild* seluruh katalog puluhan ribu kartu Anda dari nol, gunakan: `./scraper_v2/build_and_upload_catalog.sh --full` (proses ini bisa memakan waktu berjam-jam).

---

## 📖 Script Tambahan

- **Migrasi gambar TCGCollector ke Backblaze B2:**
  Pastikan variabel `B2_BUCKET`, `B2_S3_ENDPOINT`, `B2_KEY_ID`, dan
  `B2_APPLICATION_KEY` tersedia di `.env`. Jalankan preflight terlebih dahulu;
  perintah ini tidak mengunggah apa pun:
  ```bash
  ./scraper_v2/.venv/bin/python scraper_v2/migrate_tcgcollector_images_to_b2.py
  ```
  Jika preflight lolos batas free tier dan buffer 250 MB, mulai atau lanjutkan
  migrasi dengan:
  ```bash
  ./scraper_v2/.venv/bin/python scraper_v2/migrate_tcgcollector_images_to_b2.py --execute
  ```
  Untuk langsung upload tanpa HEAD preflight kedua kali, gunakan mode direct.
  Mode ini tetap menghentikan upload sebelum melewati batas 9,75 GB:
  ```bash
  ./scraper_v2/.venv/bin/python scraper_v2/migrate_tcgcollector_images_to_b2.py --direct
  ```
  Script aman dijalankan ulang. Objek yang sudah ada akan ditautkan kembali,
  sedangkan database menjadi checkpoint lewat kolom `image_small_b2_path` dan
  `image_large_b2_path`.

- **Migrasi logo dan simbol set ke Backblaze B2:**
  Script ini mengambil `logo_url` dan `symbol_url` TCGCollector, lalu menyimpan
  aset ke prefix `set-logos/` dan `set-symbols/`. Dry-run tidak mengunduh atau
  mengunggah aset:
  ```bash
  ./scraper_v2/.venv/bin/python scraper_v2/migrate_set_images_to_b2.py
  ```
  Mulai atau lanjutkan migrasi dengan:
  ```bash
  ./scraper_v2/.venv/bin/python scraper_v2/migrate_set_images_to_b2.py --execute
  ```
  Script menggunakan 8 worker secara default, berhenti sebelum batas aman
  9,75 GB, dan memperbarui `logo_b2_path`/`symbol_b2_path` hanya setelah objek
  berhasil diunggah. Format PNG, WebP, JPEG, GIF, dan SVG didukung. URL sumber
  lama tidak diubah.

- **Scrape Pokedex Master:**
  Jika Anda membutuhkan daftar lengkap spesies Pokemon (Pokedex), jalankan:
  ```bash
  node scraper_v2/scrape_tcgcollector_pokedex.js
  ```

- **Migrasi gambar Pokedex ke Backblaze B2:**
  Satu gambar canonical disimpan untuk setiap nomor National Pokedex di prefix
  `pokedex/`. Path yang sama ditulis ke `pokemon_pokedex_core` dan semua bahasa
  di `pokemon_pokedex`. Dry-run:
  ```bash
  ./scraper_v2/.venv/bin/python scraper_v2/migrate_pokedex_images_to_b2.py
  ```
  Mulai atau lanjutkan upload:
  ```bash
  ./scraper_v2/.venv/bin/python scraper_v2/migrate_pokedex_images_to_b2.py --execute
  ```
  Script menggunakan 8 worker, dapat memperbaiki objek B2 yang hilang, dan
  tetap mengikuti batas aman penyimpanan 9,75 GB.

- **Re-install AI Virtual Environment (.venv):**
  Jika Anda memindahkan proyek ini ke Mac/PC lain dan script AI tidak bisa berjalan karena *dependencies* hilang, Anda bisa menginstal ulangnya dengan mudah:
  ```bash
  rm -rf scraper_v2/.venv
  ./scraper_v2/setup_venv.sh
  ```
  *(Script di atas akan otomatis membuat `.venv` baru dan mengunduh library seperti `onnxruntime`, `numpy`, dsb sesuai `requirements.txt`).*
