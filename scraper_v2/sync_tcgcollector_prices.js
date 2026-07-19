require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });
const { createClient } = require('@supabase/supabase-js');
const fs = require('fs');

// ─── Config ───────────────────────────────────────────────────────────────────
const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_KEY;

if (!SUPABASE_KEY || !SUPABASE_URL) {
  console.error('ERROR: SUPABASE_URL or SUPABASE_SERVICE_KEY not set.');
  process.exit(1);
}

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

const CONCURRENCY_LIMIT = 15; // Jaga agar tidak di-banned
const BATCH_SIZE = 100; // Ukuran batch update ke Supabase

const delay = ms => new Promise(r => setTimeout(r, ms));

// ─── Language Filter ───────────────────────────────────────────────────────────
// Usage: node sync_tcgcollector_prices.js --en  → hanya harga kartu EN
//        node sync_tcgcollector_prices.js --jp  → hanya harga kartu JPN (ja)
//        node sync_tcgcollector_prices.js --id  → hanya harga kartu ID
//        node sync_tcgcollector_prices.js       → semua bahasa
const LANG_MAP = { '--en': 'en', '--jp': 'ja', '--ja': 'ja', '--id': 'id' };
const langArg = process.argv.find(a => LANG_MAP[a]);
const LANG_FILTER = langArg ? LANG_MAP[langArg] : null;

if (LANG_FILTER) {
  console.log(`🔎 Mode: Hanya menyinkronkan harga kartu bahasa "${LANG_FILTER}"`);
} else {
  console.log('🔎 Mode: Menyinkronkan harga SEMUA bahasa (EN, JA, ID)');
}

function normalizeCondition(value) {
  if (!value) return null;
  const normalized = value.toLowerCase().replace(/[^a-z0-9]/g, '');
  if (!normalized) return null;
  
  if (normalized.includes('reverse') && normalized.includes('holo')) return 'reverseholofoil';
  if (normalized.includes('1stedition') && normalized.includes('holo')) return '1steditionholofoil';
  if (normalized.includes('1stedition')) return '1steditionnormal';
  if (normalized.includes('holo') || normalized === 'foil') return 'holofoil';
  if (normalized.includes('normal') || normalized.includes('nonholo') || normalized.includes('unlimited')) return 'normal';
  
  // Fallback: gunakan nilai apa adanya (menangkap variant eksotis JPN, promo, dll)
  return normalized;
}

// Urutan prioritas kondisi: semakin kecil angkanya, semakin diprioritaskan
const CONDITION_PRIORITY = {
  'near mint': 1, 'nm': 1, 'unplayed': 1,
  'lightly played': 2, 'lp': 2,
  'moderately played': 3, 'mp': 3,
  'heavily played': 4, 'hp': 4,
  'damaged': 5,
};

function getConditionPriority(condition) {
  const c = (condition || '').toLowerCase().trim();
  for (const [key, val] of Object.entries(CONDITION_PRIORITY)) {
    if (c.includes(key)) return val;
  }
  return 99; // unknown
}

function formatTcgPrices(results) {
  // pricesMap[typeId] = { market, low, conditionPriority }
  const pricesMap = {};

  for (const r of results) {
    const v = r.variant || '';
    const condPriority = getConditionPriority(r.condition);

    const buckets = r.buckets || [];
    // Urutkan berdasarkan tanggal (ascending), ambil paling baru
    buckets.sort((a, b) => new Date(a.bucketStartDate) - new Date(b.bucketStartDate));
    if (buckets.length === 0) continue;

    const latest = buckets[buckets.length - 1];
    const market = parseFloat(latest.marketPrice || 0);
    const low = parseFloat(latest.lowSalePrice || 0);

    const typeId = normalizeCondition(v);
    if (!typeId) continue;

    if (market > 0) {
      // Simpan jika belum ada, atau ganti jika kondisi saat ini lebih baik (prioritas lebih kecil)
      if (!pricesMap[typeId] || condPriority < pricesMap[typeId]._condPriority) {
        pricesMap[typeId] = {
          market,
          low: low > 0 ? low : null,
          _condPriority: condPriority  // internal tracking, tidak disimpan ke DB
        };
      }
    }
  }

  // Hapus field internal sebelum return
  for (const key of Object.keys(pricesMap)) {
    delete pricesMap[key]._condPriority;
  }

  return Object.keys(pricesMap).length > 0 ? pricesMap : null;
}

function getBestMarketPrice(pricesMap) {
  if (!pricesMap) return null;
  
  const priority = ['holofoil', 'normal', 'reverseholofoil', '1steditionholofoil', '1steditionnormal', 'foil'];
  for (const p of priority) {
    if (pricesMap[p] && pricesMap[p].market > 0) {
      return pricesMap[p].market;
    }
  }
  
  // Fallback: ambil harga pertama yang ada market price-nya (menangkap variant eksotis)
  for (const key of Object.keys(pricesMap)) {
    if (pricesMap[key].market > 0) return pricesMap[key].market;
  }
  
  return null;
}

async function fetchPrice(productId) {
  const url = `https://infinite-api.tcgplayer.com/price/history/${productId}/detailed?range=month`;
  try {
    const response = await fetch(url, {
      headers: {
        'Accept': 'application/json',
        'User-Agent': 'PokeVault-AutoSync-Bot/2.0',
        'Referer': 'https://infinite.tcgplayer.com/',
        'Origin': 'https://infinite.tcgplayer.com'
      }
    });

    if (response.status === 200) {
      return await response.json();
    } else if (response.status === 404) {
      return null;
    }
    return null;
  } catch (e) {
    return null;
  }
}

async function processProduct(productId) {
  const jsonData = await fetchPrice(productId);
  if (!jsonData || !jsonData.result) {
    return { productId, success: false, reason: 'no_data_or_404' };
  }

  // Log raw variants untuk debugging
  const allVariants = jsonData.result.map(r => `${r.variant}|${r.condition}`).join(', ');

  const tcgplayer_prices = formatTcgPrices(jsonData.result);
  if (!tcgplayer_prices) {
    return { productId, success: false, reason: 'no_valid_price', rawVariants: allVariants };
  }

  const market_price = getBestMarketPrice(tcgplayer_prices);

  return {
    productId,
    success: true,
    data: {
      market_price,
      tcgplayer_prices,
      scraped_at: new Date().toISOString()
    }
  };
}

async function run() {
  console.log('Fetching distinct tcgplayer_product_id from database...');
  let hasMore = true;
  let lastId = '';
  const allProductIds = new Set();
  
  while (hasMore) {
    let query = supabase
      .from('pokemon_cards')
      .select('id, tcgplayer_product_id')
      .not('tcgplayer_product_id', 'is', null)
      .order('id')
      .limit(1000);

    if (LANG_FILTER) query = query.eq('language', LANG_FILTER);
      
    if (lastId) query.gt('id', lastId);

    const { data, error } = await query;
    if (error) throw error;
    if (data.length === 0) {
      hasMore = false;
      break;
    }

    for (const row of data) {
      if (row.tcgplayer_product_id) {
        allProductIds.add(row.tcgplayer_product_id);
      }
    }
    lastId = data[data.length - 1].id;
  }

  const distinctPids = Array.from(allProductIds);
  console.log(`Found ${distinctPids.length} unique products to sync.`);

  let successCount = 0;
  let failCount = 0;

  const langSuffix = LANG_FILTER ? `_${LANG_FILTER}` : '_all';
  const logPath = require('path').join(__dirname, '..', `sync_price_failed${langSuffix}.log`);
  // Tulis header langsung di awal (live logging)
  fs.writeFileSync(logPath, `product_id\treason\traw_variants\n`, 'utf8');
  console.log(`\n📄 Log kegagalan (live) tersimpan di: ${logPath}`);

  for (let i = 0; i < distinctPids.length; i += BATCH_SIZE) {
    const batchPids = distinctPids.slice(i, i + BATCH_SIZE);
    
    // Process batch with concurrency limit
    const results = [];
    const executing = new Set();
    
    for (const pid of batchPids) {
      const p = processProduct(pid).then(res => {
        executing.delete(p);
        return res;
      });
      executing.add(p);
      results.push(p);
      
      if (executing.size >= CONCURRENCY_LIMIT) {
        await Promise.race(executing);
      }
    }
    
    const resolvedResults = await Promise.all(results);
    
    // Update DB
    const updatePromises = [];
    for (const res of resolvedResults) {
      if (res.success && res.data) {
        updatePromises.push(
          supabase
            .from('pokemon_cards')
            .update(res.data)
            .eq('tcgplayer_product_id', res.productId)
        );
        successCount++;
      } else {
        failCount++;
        // Append langsung ke file (live update, bisa dibuka saat script masih berjalan)
        fs.appendFileSync(logPath, `${res.productId}\t${res.reason || 'unknown'}\t${res.rawVariants || '-'}\n`, 'utf8');
      }
    }
    
    if (updatePromises.length > 0) {
      await Promise.all(updatePromises);
    }
    
    process.stdout.write(`\rProcessed: ${i + batchPids.length}/${distinctPids.length} | Success: ${successCount} | Failed: ${failCount}`);
    await delay(1000);
  }

  console.log(`\n✅ Sync completed! Processed: ${distinctPids.length}, Success: ${successCount}, Failed: ${failCount}`);
  console.log(`📄 Log tersimpan di: ${logPath}`);
}

run().catch(err => {
  console.error('\nFatal error:', err);
  process.exit(1);
});
