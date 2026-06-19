# PokemonPriceTracker Japanese Card Scraper Case

Dokumen ini khusus untuk case mengambil data card Japanese dari PokemonPriceTracker ke tabel staging Supabase `ppt_*`.

Tujuan utamanya adalah **mengambil data card baru**, bukan meng-update data card lama yang sudah ada.

## Ringkasan

- Source API: PokemonPriceTracker `/api/v2/cards`
- Target DB: Supabase project `vpfjmgefygjhabuizdsq`
- Language aktif: `japanese`
- Data yang diambil: basic card data + price variants basic
- Data yang tidak diambil: history, eBay, Cardmarket, PSA/population
- Cost: basic card query = sekitar 1 credit per card
- Edge Function: `ppt-sync-cards`
- Function aktif terakhir: version 11

## Command Harian

Load env lokal:

```bash
set -a
source scraper/.env
set +a
```

Jalankan scrape Japanese dengan free API key:

```bash
curl -X POST "$SUPABASE_URL/functions/v1/ppt-sync-cards" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"language":"japanese","batchLimit":10,"maxCardsPerRun":1900,"dailyReserve":0}'
```

Untuk free key dev:

- `batchLimit:10` lebih hemat di ujung set, karena endpoint bisa return card lebih sedikit daripada limit yang diminta tetapi tetap charge sesuai limit.
- `maxCardsPerRun:1900` cocok untuk 19 key x 100 credit/hari.
- `dailyReserve:0` berarti habiskan credit, tidak sisakan cadangan.

## Tabel Yang Dipakai

### `ppt_sets`

Daftar set dari PokemonPriceTracker. Scraper memakai tabel ini untuk tahu urutan set Japanese yang akan diproses.

Jangan dikosongkan kalau ingin resume normal.

### `ppt_cards`

Data card yang berhasil diambil.

Behavior saat ini: duplicate card di-ignore, bukan di-update.

Unique key utama: `language, tcg_player_id`.

### `ppt_card_price_variants`

Detail price variant per card/printing.

Behavior saat ini: duplicate variant di-ignore, bukan di-update.

### `ppt_sync_checkpoints`

Tabel utama untuk resume.

Kolom penting:

- `job_name`
- `language`
- `current_set_numeric_id`
- `current_set_name`
- `current_offset`
- `last_completed_set_numeric_id`
- `completed_set_numeric_ids`
- `credits_used_today`
- `daily_remaining`
- `status`

Jangan reset tabel ini kalau ingin lanjut dari posisi terakhir.

### `ppt_api_keys`

Status setiap API key dev.

Kolom penting:

- `key_name`
- `status`
- `daily_remaining`
- `credits_used_today`
- `credit_window_date`
- `rate_limited_at`
- `last_error`

Besok saat token balik 100, function akan mengaktifkan lagi key yang sebelumnya `rate_limited` jika `credit_window_date` sudah beda hari.

### `ppt_edge_runs`

Log setiap run Edge Function.

Ini bukan penentu resume. Aman dibiarkan menumpuk untuk audit.

## Cara Resume

Untuk resume, jalankan command harian yang sama.

Tidak perlu reset:

- `ppt_sync_checkpoints`
- `ppt_cards`
- `ppt_card_price_variants`
- `ppt_sets`
- `ppt_api_keys`
- `ppt_edge_runs`

Reset hanya perlu kalau memang ingin fresh total dari awal.

## Behavior Multi-Key

Function memakai secret Supabase:

- `PPT_KEY_NAMES`
- `PPT_KEY_1` sampai `PPT_KEY_19`

Logic saat ini:

- key aktif dipakai berurutan
- kalau key tinggal 50 credit, request `limit=50`
- kalau key tinggal 7 credit, request `limit=7`
- kalau key tinggal 0, key ditandai `rate_limited` dan pindah ke key berikutnya
- kalau key invalid, key ditandai `disabled`, dicatat di `invalidKeys`, lalu function lanjut ke key berikutnya
- kalau semua key habis, run berhenti dengan status `stopped`

## Opsi Paid Key

Kalau nanti memakai 1 key berbayar, ada dua cara.

### Opsi 1: Edge Function Batch Kecil

Edge Function Supabase punya batas runtime, jadi jangan menjalankan 20.000 card dalam satu invocation.

Contoh aman untuk paid key:

```bash
curl -X POST "$SUPABASE_URL/functions/v1/ppt-sync-cards" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"language":"japanese","batchLimit":50,"maxCardsPerRun":1000,"dailyReserve":100}'
```

Run command ini berkali-kali atau lewat scheduler. Setiap run akan lanjut dari `ppt_sync_checkpoints`.

### Opsi 2: Python Lokal Untuk Long-Running Scrape

Jika ingin menghabiskan jatah paid key harian dalam proses panjang tanpa batas runtime Edge Function, pakai script Python lokal.

Pastikan `.env` berisi paid key:

```text
POKEMON_PRICE_TRACKER_API_KEY=ISI_KEY_BERBAYAR_KAMU
```

Lalu jalankan:

```bash
python3 scraper/scrape_pokemonpricetracker.py \
  --language japanese \
  --limit 50 \
  --max-credits 20000 \
  --daily-reserve 100
```

Catatan:

- Python script cocok untuk 1 paid key yang ingin jalan lama dari laptop/server.
- Python script memakai `POKEMON_PRICE_TRACKER_API_KEY` dari `scraper/.env`.
- Python script tidak memakai 19 key dari Supabase Secrets.
- Resume tetap memakai tabel `ppt_sync_checkpoints`.
- Kalau laptop mati atau proses stop, jalankan command yang sama untuk lanjut.

## Catatan Penting Tentang `card_count`

Jangan percaya penuh `ppt_sets.card_count` untuk menentukan jumlah card yang bisa diambil dari endpoint `/cards`.

Contoh yang pernah terjadi:

- `Expansion Pack` punya `card_count=491`
- tapi endpoint `/cards` basic sudah habis di sekitar 102 card

Karena request kosong tetap bisa memakan credit, function version 7 menambahkan tracking:

- jika endpoint `/cards` return habis / tidak ada `hasMore`, set ditandai selesai
- set selesai dicatat di `ppt_sync_checkpoints.completed_set_numeric_ids`
- run berikutnya akan skip set itu walaupun `card_count` masih terlihat besar

Set yang sudah di-seed sebagai selesai dari hasil test:

- `23721` Expansion Pack
- `23722` Pokemon Jungle
- `23723` Mystery of the Fossils
- `23724` Rocket Gang
- `23740` Expansion Pack (No Rarity)

## Email Result

Function mengirim summary ke email jika `RESEND_API_KEY` tersedia di Supabase secrets.

Default email tujuan:

```text
underground182@yahoo.com
```

Summary berisi:

- status
- language
- cards upserted
- cards skipped
- cards failed
- credits used
- keys rate limited
- stopped reason

## Kalau Ada Sisa Credit Tapi Function Bilang Tidak Ada Active Key

Kemungkinan row di `ppt_api_keys` masih berstatus `rate_limited`.

Cek dulu status key. Kalau ada `daily_remaining > 0` tapi status masih `rate_limited`, aktifkan lagi hanya key tersebut:

```sql
update public.ppt_api_keys
set status = 'active',
    rate_limited_at = null,
    last_error = null,
    updated_at = now()
where coalesce(daily_remaining, 0) > 0
  and status = 'rate_limited';
```

Setelah itu run command harian lagi.

## Kalau Mau Fresh Total

Fresh total berarti mulai ulang data PPT dari nol. Ini berbeda dari resume.

Tabel yang biasanya dikosongkan:

- `ppt_card_price_variants`
- `ppt_cards`
- `ppt_sync_checkpoints`
- `ppt_edge_runs`
- `ppt_api_keys`

`ppt_sets` boleh dibiarkan agar tidak perlu sync sets ulang.

Jangan lakukan fresh total kalau hanya ingin lanjut besok.

## File Terkait

- `supabase/functions/ppt-sync-cards/index.ts`
- `supabase/pokemon_price_tracker_schema.sql`
- `supabase/pokemon_price_tracker_edge_schema.sql`
- `scraper/scrape_pokemonpricetracker.py`
