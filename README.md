# PokeVault Database Pipeline

Repository ini berisi sekumpulan *script* untuk membangun, memproses, dan mengunggah *Offline Scanner Database* untuk aplikasi [PokeVault](https://github.com/adtyasatrio/PokeVaultFlutter).

Pipeline ini secara otomatis:
1. Menarik (*fetch*) metadata kartu terbaru dari Pokémon TCG API.
2. Memproses ribuan gambar kartu menggunakan kecerdasan buatan (TFLite `efficientnet_b0`) untuk membuat vektor pencocokan (*embeddings*).
3. Melakukan *quantization* untuk memperkecil ukuran *database* hingga 75%.
4. Mengunggah hasil akhirnya (`poc_compact.db`) secara otomatis ke GitHub Releases.

## 📦 Prasyarat (Requirements)

Sebelum menjalankan *pipeline* ini, pastikan komputer/server (seperti Raspberry Pi) Anda sudah terinstal:

- [Dart SDK](https://dart.dev/get-dart) (>= 3.0.0)
- [Python 3](https://www.python.org/downloads/)
- [GitHub CLI (`gh`)](https://cli.github.com/)

### Instalasi Dependensi

Masuk ke folder repository ini, lalu jalankan perintah berikut:

```bash
# 1. Install library Dart
dart pub get

# 2. Setup dan Install library Python menggunakan Virtual Environment (venv)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Login ke GitHub CLI (Agar bisa upload release secara otomatis)
gh auth login
```

*(Catatan: Setelah menyetel virtual environment, pastikan Anda mengaktifkannya dengan `source .venv/bin/activate` setiap kali ingin menjalankan script Python secara manual. Script otomatis `generate_compact_ota_db.sh` akan mendeteksi dan menggunakan venv ini secara otomatis).*

## 🚀 Cara Penggunaan

Terdapat satu *script* pamungkas `generate_compact_ota_db.sh` yang akan menangani seluruh proses (Incremental Fetch -> TFLite AI -> Compression -> GitHub Upload).

### 1. Uji Coba Pertama Kali (Generate dari Nol)

Jika Anda ingin mereset *database* dari awal (menghapus seluruh *cache* lama), hapus *database* mentahnya lalu jalankan *script*:

```bash
rm -f assets/db/poc.db
./scripts/generate_compact_ota_db.sh
```

*(Catatan: Proses pertama kali ini bisa memakan waktu berjam-jam karena AI harus mengekstrak ciri visual dari ~16.000+ kartu).*

### 2. Update Berkala (Cron Job Mingguan)

*Script* ini sudah sangat cerdas dan mendukung **Incremental Updates**. Ia tidak akan mengulang proses dari awal. Ia hanya akan mencari kartu yang *baru rilis minggu ini*, memproses kartu-kartu baru tersebut saja, dan mengunggah hasil *database* terbarunya ke internet.

Untuk menggunakannya sebagai otomatisasi mingguan, jalankan dengan *flag* `--upload`:

```bash
./scripts/generate_compact_ota_db.sh --upload
```

### 3. Upload ke Supabase Storage

Versi GitHub Release tetap ada dan tidak diubah. Jika ingin mengunggah hasil `poc_compact.db` ke Supabase Storage, gunakan script Supabase:

```bash
SUPABASE_URL=https://vpfjmgefygjhabuizdsq.supabase.co \
SUPABASE_SERVICE_KEY=<service_role_key> \
./scripts/generate_compact_ota_db_supabase.sh --bucket offline-db --object-path poc_compact.db
```

Script ini akan:
1. Menjalankan pipeline compact DB yang sama tanpa flag `--upload` GitHub.
2. Mengunggah `build/offline_db/poc_compact.db` ke bucket Supabase.
3. Menimpa object lama secara default (`upsert`).

Jika environment sudah ada di `scraper/.env`, script akan membacanya otomatis. Untuk membuat bucket jika belum ada:

```bash
./scripts/generate_compact_ota_db_supabase.sh --create-bucket --public-bucket
```

URL public file-nya akan berbentuk:

```text
https://<project-ref>.supabase.co/storage/v1/object/public/offline-db/poc_compact.db
```

#### Contoh Setelan Cron Job di Raspberry Pi:
Untuk menjalankan otomatis setiap hari Minggu jam 03:00 pagi:
```bash
crontab -e
```
Tambahkan baris berikut di bagian paling bawah:
```bash
0 3 * * 0 cd /path/ke/PokeVaultDatabase && ./scripts/generate_compact_ota_db.sh --upload >> cron.log 2>&1
```

## ⚙️ Cara Kerja Script

1. **`generate_db.dart`**: Menghubungi API dan men-*download* informasi tekstual kartu (HP, Rarity, Types) ke dalam `poc.db`. Jika `poc.db` sudah ada, ia hanya akan menambahkan kartu yang belum ada.
2. **`generate_tflite_model.py`**: Membuat "Otak AI" (file `.tflite`) dari *EfficientNetB0* jika belum tersedia.
3. **`add_tflite_emb.py`**: Membedah gambar kartu baru untuk mengekstrak vektor matematika, dan menyimpannya ke kolom `tflite_emb` berformat *Float32*.
4. **`create_compact_offline_db.py`**: Membuang kolom yang tidak perlu, mengubah vektor dari *Float32* menjadi *Uint8* (*Quantization*), lalu mencetaknya ke dalam file `poc_compact.db` yang super ringan.
5. **GitHub CLI (`gh`)**: Mengunggah `poc_compact.db` ke repositori Anda di bawah rilis ber-tag `latest`.

## 📈 Sinkronisasi Harga TCGPlayer Massal

Untuk mengambil harga terbaru dari **seluruh kartu Pokemon** di database utama, gunakan script sinkronisasi harga TCGPlayer V2.

Script ini sudah dilengkapi sistem keamanan (*rate limiting*) dan sistem *checkpoint* otomatis (menggunakan patokan kolom `scraped_at`), sehingga Anda dapat menghentikan dan melanjutkannya kapan saja tanpa mengulang dari awal. Harga yang di-update **hanya** pada tabel master `pokemon_cards` dan tidak menimpa *cost basis* pada koleksi pribadi milik user.

Untuk menjalankannya secara lokal:

```bash
# Inggris
node scraper_v2/sync_tcgcollector_prices.js --en

# Jepang
node scraper_v2/sync_tcgcollector_prices.js --jp
```

Gunakan `--dry-run --limit 10 --force` untuk menguji API tanpa mengubah database.
