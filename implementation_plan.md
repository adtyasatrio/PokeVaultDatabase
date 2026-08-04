# Menambahkan Fitur Market Movers (Trend 7 Hari)

Fitur ini akan menghitung persentase perubahan harga (trend) dalam 7 hari terakhir dari data API TCGPlayer, dan menyimpannya ke database Supabase sehingga aplikasi Flutter dapat menampilkan Top Movers/Losers.

## User Review Required

- **Skema Database:** Saya akan menambahkan kolom `trend_7d_pct` (tipe `float4` / `real`) pada tabel `pokemon_cards` di database Supabase kamu. Apakah kamu setuju untuk menambahkan kolom ini? 
- Jika kamu menggunakan Supabase secara lokal atau remote, saya bisa mengeksekusi SQL-nya langsung lewat tools jika kamu memberikan izin, atau kamu bisa menjalankannya sendiri di dashboard SQL editor Supabase:
  ```sql
  ALTER TABLE pokemon_cards ADD COLUMN IF NOT EXISTS trend_7d_pct REAL;
  ```

## Proposed Changes

### ` PokeVaultDatabase`
Akan dilakukan perubahan pada script scraper TCGCollector.

#### [MODIFY] `scraper_v2/sync_tcgcollector_prices.js`
- Memodifikasi fungsi `formatTcgPrices` untuk tidak hanya mengambil `marketPrice` hari ini, tapi juga menelusuri mundur ~7 hari ke belakang (berdasarkan tanggal *bucket*) untuk mendapatkan `marketPrice_7d`.
- Menghitung `trend_pct` dengan rumus `((harga_sekarang - harga_lama) / harga_lama) * 100`. Jika harga lama `0`, tren akan di-set `0` untuk menghindari *infinity*.
- Memodifikasi fungsi `getBestMarketPrice` menjadi `getBestMarketData` yang akan mengembalikan *object* berisi `market_price` dan `trend_7d_pct`.
- Menambahkan `trend_7d_pct` ke dalam objek `data` yang di-update ke Supabase.

## Verification Plan

### Manual Verification
- Menjalankan script scraper dengan flag `--dry-run` pada beberapa kartu untuk melihat apakah hasil kalkulasi `trend_7d_pct` berjalan dengan benar tanpa mengubah database.
- Setelah berhasil, menjalankan tanpa `--dry-run` dan memverifikasi bahwa kolom `trend_7d_pct` di database terisi.
