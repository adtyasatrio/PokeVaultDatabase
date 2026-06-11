# Pokemon TCG → Supabase Scraper

Script Python untuk mengunduh seluruh database Pokemon TCG dari [pokemontcg.io](https://dev.pokemontcg.io/) dan menyimpannya ke Supabase PokeVault — sehingga Flutter app tidak bergantung pada API eksternal saat runtime.

## Setup

### 1. Buat virtual environment

```bash
cd scraper
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Konfigurasi `.env`

Copy `.env.example` ke `.env` dan isi nilai-nilainya:

```bash
cp .env.example .env
```

Edit `.env`:
```
POKEMONTCG_API_KEY=f4f8ae57-c3db-4ac5-89c6-1986b6b5b397
SUPABASE_URL=https://vpfjmgefygjhabuizdsq.supabase.co
SUPABASE_SERVICE_KEY=<ambil dari Supabase Dashboard → Settings → API → service_role>
```

> ⚠️ **PENTING**: Gunakan **service_role** key (bukan anon key). Key ini bypass RLS dan punya akses penuh ke database. Jangan expose ke client!

## Cara Pakai

### Test dengan 1 set dulu (direkomendasikan pertama kali)
```bash
python scrape_to_supabase.py --set-id base1
```

### Scrape semua (pertama kali, bisa butuh 1-3 jam)
```bash
python scrape_to_supabase.py
```

### Update incremental (set baru saja)
```bash
python scrape_to_supabase.py
# Otomatis skip set yang sudah ada kartunya
```

### Force re-scrape semua (untuk update harga)
```bash
python scrape_to_supabase.py --force
```

### Hanya update data sets (tanpa kartu)
```bash
python scrape_to_supabase.py --sets-only
```

## Estimated Time

| Mode | Estimasi |
|------|----------|
| 1 set (`base1`, 102 kartu) | ~30 detik |
| Full scrape (~150 sets, ~18.000 kartu) | 1–3 jam |
| Update incremental (set baru) | Beberapa menit |

## Rate Limits

Dengan API key pokemontcg.io:
- **20.000 request/hari**
- Script menggunakan delay 0.15s antar request
- Retry otomatis dengan exponential backoff jika rate limited

## Output

- Progress bar per set
- Log disimpan ke `scraper.log`
- Idempotent: aman dijalankan berulang kali (upsert, bukan insert)
