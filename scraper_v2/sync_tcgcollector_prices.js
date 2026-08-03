const path = require('path');
require('dotenv').config({ path: path.join(__dirname, '..', '.env'), quiet: true });
const { createClient } = require('@supabase/supabase-js');
const fs = require('fs');

// TCGPlayer's old Infinite price-history endpoint now returns HTTP 403. This
// endpoint is the one currently used by the TCGPlayer product page and exposes
// the current product-level market price without requiring the blocked history
// request.
const PRODUCT_DETAILS_BASE = 'https://mp-search-api.tcgplayer.com/v1/product';

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_KEY;

if (!SUPABASE_KEY || !SUPABASE_URL) {
  console.error('ERROR: SUPABASE_URL or SUPABASE_SERVICE_KEY not set.');
  process.exit(1);
}

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function positiveInteger(value, fallback) {
  const parsed = Number.parseInt(value, 10);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

// Conservative defaults. Override only when the endpoint has been stable for
// several runs, e.g. TCG_PRICE_CONCURRENCY=3 TCG_PRICE_INTERVAL_MS=250.
const CONCURRENCY_LIMIT = positiveInteger(process.env.TCG_PRICE_CONCURRENCY, 2);
const BATCH_SIZE = positiveInteger(process.env.TCG_PRICE_BATCH_SIZE, 50);
const REQUEST_INTERVAL_MS = positiveInteger(process.env.TCG_PRICE_INTERVAL_MS, 400);
const MAX_RETRIES = positiveInteger(process.env.TCG_PRICE_MAX_RETRIES, 4);
const RETRY_BASE_MS = positiveInteger(process.env.TCG_PRICE_RETRY_BASE_MS, 1500);
const RESCRAPE_HOURS = positiveInteger(process.env.TCG_PRICE_RESCRAPE_HOURS, 24);

const args = process.argv.slice(2);
const LANG_MAP = { '--en': 'en', '--jp': 'ja', '--ja': 'ja', '--id': 'id' };
const langArg = args.find(arg => LANG_MAP[arg]);
const LANG_FILTER = langArg ? LANG_MAP[langArg] : null;
const DRY_RUN = args.includes('--dry-run');
const FORCE = args.includes('--force');

function numericArg(name) {
  const index = args.indexOf(name);
  if (index === -1) return null;
  const value = Number.parseInt(args[index + 1], 10);
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`${name} must be followed by a positive integer.`);
  }
  return value;
}

const LIMIT = numericArg('--limit');

class ApiBlockedError extends Error {
  constructor(status, productId) {
    super(`TCGPlayer returned HTTP ${status} for product ${productId}. Sync stopped to avoid extending the block.`);
    this.name = 'ApiBlockedError';
    this.status = status;
    this.productId = productId;
  }
}

// Serialize request start times while still allowing a small number of requests
// to be in flight. This avoids bursts that can trigger TCGPlayer's WAF.
let throttleTail = Promise.resolve();
let nextRequestAt = 0;

async function throttleRequest() {
  const slot = throttleTail.then(async () => {
    const waitMs = Math.max(0, nextRequestAt - Date.now());
    if (waitMs > 0) await delay(waitMs);
    nextRequestAt = Date.now() + REQUEST_INTERVAL_MS;
  });

  throttleTail = slot.catch(() => {});
  await slot;
}

function retryDelay(attempt) {
  const exponential = RETRY_BASE_MS * (2 ** attempt);
  const jitter = Math.floor(Math.random() * 500);
  return exponential + jitter;
}

async function fetchProductDetails(productId) {
  const url = `${PRODUCT_DETAILS_BASE}/${encodeURIComponent(productId)}/details`;

  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    await throttleRequest();

    let response;
    try {
      response = await fetch(url, {
        headers: {
          Accept: 'application/json',
          'User-Agent': 'Mozilla/5.0 (compatible; PokeVaultPriceSync/3.0)'
        },
        signal: AbortSignal.timeout(20000)
      });
    } catch (error) {
      if (attempt + 1 === MAX_RETRIES) {
        return {
          success: false,
          status: null,
          reason: 'network_error',
          detail: error.message
        };
      }
      await delay(retryDelay(attempt));
      continue;
    }

    if (response.ok) {
      try {
        return { success: true, status: response.status, data: await response.json() };
      } catch (error) {
        return {
          success: false,
          status: response.status,
          reason: 'invalid_json',
          detail: error.message
        };
      }
    }

    if (response.status === 404) {
      return { success: false, status: 404, reason: 'product_not_found' };
    }

    const retryable = response.status === 403 || response.status === 429 || response.status >= 500;
    if (retryable && attempt + 1 < MAX_RETRIES) {
      const waitMs = retryDelay(attempt);
      console.warn(`\nHTTP ${response.status} for ${productId}; retrying in ${waitMs}ms (${attempt + 1}/${MAX_RETRIES})...`);
      await delay(waitMs);
      continue;
    }

    if (response.status === 403 || response.status === 429) {
      throw new ApiBlockedError(response.status, productId);
    }

    return {
      success: false,
      status: response.status,
      reason: `http_${response.status}`
    };
  }

  return { success: false, status: null, reason: 'retry_exhausted' };
}

async function processProduct(productId) {
  const response = await fetchProductDetails(productId);
  if (!response.success) return { productId, ...response };

  const marketPrice = Number(response.data?.marketPrice);
  if (!Number.isFinite(marketPrice) || marketPrice <= 0) {
    return {
      productId,
      success: false,
      status: response.status,
      reason: 'no_market_price',
      detail: response.data?.productName || null
    };
  }

  return {
    productId,
    success: true,
    status: response.status,
    productName: response.data?.productName || null,
    data: {
      market_price: marketPrice,
      scraped_at: new Date().toISOString()
    }
  };
}

async function mapWithConcurrency(items, limit, worker) {
  const results = new Array(items.length);
  let nextIndex = 0;

  async function runWorker() {
    while (true) {
      const index = nextIndex++;
      if (index >= items.length) return;
      results[index] = await worker(items[index]);
    }
  }

  const workerCount = Math.min(limit, items.length);
  await Promise.all(Array.from({ length: workerCount }, () => runWorker()));
  return results;
}

function sanitizeLogValue(value) {
  return String(value ?? '-').replace(/[\t\r\n]+/g, ' ');
}

function appendFailure(logPath, result) {
  const line = [
    result.productId,
    result.status ?? '-',
    result.reason || 'unknown',
    sanitizeLogValue(result.detail)
  ].join('\t');
  fs.appendFileSync(logPath, `${line}\n`, 'utf8');
}

async function fetchProductIds() {
  console.log('Fetching distinct tcgplayer_product_id from database...');
  const allProductIds = new Set();
  const cutoff = new Date(Date.now() - RESCRAPE_HOURS * 60 * 60 * 1000).toISOString();
  const cutoffMs = Date.parse(cutoff);
  const pageSize = LIMIT ? Math.min(1000, Math.max(50, LIMIT * 2)) : 1000;
  let lastId = '';

  while (true) {
    let query = supabase
      .from('pokemon_cards')
      .select('id, tcgplayer_product_id, scraped_at')
      .not('tcgplayer_product_id', 'is', null)
      .order('id')
      .limit(pageSize);

    if (LANG_FILTER) query = query.eq('language', LANG_FILTER);
    if (lastId) query = query.gt('id', lastId);

    const { data, error } = await query;
    if (error) throw new Error(`Supabase read failed: ${error.message}`);
    if (!data || data.length === 0) break;

    for (const row of data) {
      const scrapedAtMs = row.scraped_at ? Date.parse(row.scraped_at) : null;
      const isFresh = scrapedAtMs !== null && Number.isFinite(scrapedAtMs) && scrapedAtMs >= cutoffMs;
      if (row.tcgplayer_product_id && (FORCE || !isFresh)) {
        allProductIds.add(String(row.tcgplayer_product_id));
      }
      if (LIMIT && allProductIds.size >= LIMIT) break;
    }
    lastId = data[data.length - 1].id;
    if (LIMIT && allProductIds.size >= LIMIT) break;
  }

  let productIds = Array.from(allProductIds);
  if (LIMIT) productIds = productIds.slice(0, LIMIT);
  return { productIds, cutoff };
}

async function updateSupabase(results, logPath) {
  const successful = results.filter(result => result.success);
  if (DRY_RUN) return { updated: successful.length, updateFailed: 0 };

  const updates = await Promise.all(successful.map(async result => {
    let query = supabase
      .from('pokemon_cards')
      .update(result.data)
      .eq('tcgplayer_product_id', result.productId);

    // Keep a language-specific run scoped to that language even if a product ID
    // is ever reused across TCGPlayer product lines.
    if (LANG_FILTER) query = query.eq('language', LANG_FILTER);

    const { error } = await query;
    return { result, error };
  }));

  let updated = 0;
  let updateFailed = 0;
  for (const { result, error } of updates) {
    if (error) {
      updateFailed++;
      appendFailure(logPath, {
        productId: result.productId,
        status: null,
        reason: 'supabase_update_error',
        detail: error.message
      });
    } else {
      updated++;
    }
  }
  return { updated, updateFailed };
}

async function run() {
  const mode = LANG_FILTER || 'all';
  console.log(`Mode: language=${mode}, dryRun=${DRY_RUN}, force=${FORCE}`);
  console.log(`Rate: concurrency=${CONCURRENCY_LIMIT}, interval=${REQUEST_INTERVAL_MS}ms, retries=${MAX_RETRIES}`);

  const { productIds, cutoff } = await fetchProductIds();
  console.log(FORCE
    ? 'Checkpoint disabled by --force.'
    : `Skipping products refreshed after ${cutoff} (${RESCRAPE_HOURS}h checkpoint).`);
  console.log(`Found ${productIds.length} unique products to sync.`);

  if (productIds.length === 0) {
    console.log('Nothing to update.');
    return;
  }

  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  const drySuffix = DRY_RUN ? '_dry_run' : '';
  const logPath = path.join(__dirname, '..', `sync_price_failed_${mode}${drySuffix}_${timestamp}.log`);
  fs.writeFileSync(logPath, 'product_id\thttp_status\treason\tdetail\n', 'utf8');
  console.log(`Failure log: ${logPath}`);

  let processed = 0;
  let fetched = 0;
  let failed = 0;
  let updated = 0;
  let updateFailed = 0;

  for (let index = 0; index < productIds.length; index += BATCH_SIZE) {
    const batch = productIds.slice(index, index + BATCH_SIZE);
    const results = await mapWithConcurrency(batch, CONCURRENCY_LIMIT, processProduct);

    for (const result of results) {
      if (result.success) {
        fetched++;
      } else {
        failed++;
        appendFailure(logPath, result);
      }
    }

    const dbResult = await updateSupabase(results, logPath);
    updated += dbResult.updated;
    updateFailed += dbResult.updateFailed;
    processed += batch.length;

    process.stdout.write(
      `\rProcessed: ${processed}/${productIds.length} | Price found: ${fetched} | ` +
      `${DRY_RUN ? 'Would update' : 'Updated'}: ${updated} | Failed: ${failed + updateFailed}`
    );
  }

  console.log(`\nSync completed. Price found: ${fetched}, ${DRY_RUN ? 'Would update' : 'Updated'}: ${updated}, Failed: ${failed + updateFailed}`);
  console.log(`Failure log: ${logPath}`);
}

run().catch(error => {
  if (error instanceof ApiBlockedError) {
    console.error(`\nSTOPPED: ${error.message}`);
    console.error('Wait before retrying, lower the request rate, and do not run multiple language jobs simultaneously.');
  } else {
    console.error('\nFatal error:', error);
  }
  process.exit(1);
});
