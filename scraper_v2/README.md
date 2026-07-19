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
```

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

- **Scrape Pokedex Master:**
  Jika Anda membutuhkan daftar lengkap spesies Pokemon (Pokedex), jalankan:
  ```bash
  node scraper_v2/scrape_tcgcollector_pokedex.js
  ```

- **Re-install AI Virtual Environment (.venv):**
  Jika Anda memindahkan proyek ini ke Mac/PC lain dan script AI tidak bisa berjalan karena *dependencies* hilang, Anda bisa menginstal ulangnya dengan mudah:
  ```bash
  rm -rf scraper_v2/.venv
  ./scraper_v2/setup_venv.sh
  ```
  *(Script di atas akan otomatis membuat `.venv` baru dan mengunduh library seperti `onnxruntime`, `numpy`, dsb sesuai `requirements.txt`).*
