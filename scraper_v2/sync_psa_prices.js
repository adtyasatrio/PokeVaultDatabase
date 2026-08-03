require('dotenv').config({ path: require('path').join(__dirname, '..', 'scraper', '.env') });
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });
const { createClient } = require('@supabase/supabase-js');
const fs = require('fs');

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_KEY;

if (!SUPABASE_KEY || !SUPABASE_URL) {
  console.error('ERROR: SUPABASE_URL or SUPABASE_SERVICE_KEY not set.');
  process.exit(1);
}

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

const CONCURRENCY_LIMIT = 5;
const BATCH_SIZE = 50;
const delay = ms => new Promise(r => setTimeout(r, ms));
const todayIso = () => new Date().toISOString().slice(0, 10);

// Parse API keys from .env (PPT_KEY_1 to PPT_KEY_22 or more)
function loadApiKeys() {
  const keys = [];
  for (let i = 1; i <= 30; i++) {
    const name = `PPT_KEY_${i}`;
    const key = process.env[name];
    if (key) {
      keys.push({ name, key, secretName: name });
    }
  }
  
  if (keys.length === 0) {
    if (process.env.POKEMON_PRICE_TRACKER_API_KEY) {
      keys.push({ name: 'default', key: process.env.POKEMON_PRICE_TRACKER_API_KEY, secretName: 'POKEMON_PRICE_TRACKER_API_KEY' });
    } else {
      console.error("ERROR: No API keys found. Please add PPT_KEY_1 to PPT_KEY_22 in your .env file.");
      process.exit(1);
    }
  }
  return keys;
}

// Ensure rows exist in ppt_api_keys and reset daily limit if new day
async function ensureApiKeys(keys) {
  const today = todayIso();
  for (const key of keys) {
    const { data, error } = await supabase
      .from("ppt_api_keys")
      .select("key_name,status,credit_window_date")
      .eq("key_name", key.name)
      .maybeSingle();

    if (error) throw new Error(`ppt_api_keys read failed: ${error.message}`);

    const shouldReset = data && data.credit_window_date !== today && data.status === "rate_limited";
    
    const patch = {
      key_name: key.name,
      key_secret_name: key.secretName,
      status: shouldReset || !data ? "active" : data.status,
      credit_window_date: today,
      updated_at: new Date().toISOString()
    };
    
    if (shouldReset || !data) {
      patch.credits_used_today = 0;
      patch.daily_remaining = null;
      patch.rate_limited_at = null;
      patch.last_error = null;
    }

    await supabase.from("ppt_api_keys").upsert(patch, { onConflict: "key_name" });
  }
}

// Get keys that are active today
async function getActiveKeys(keys) {
  const today = todayIso();
  const { data, error } = await supabase
    .from("ppt_api_keys")
    .select("key_name,status,daily_remaining,credit_window_date")
    .in("key_name", keys.map((key) => key.name));

  if (error) throw new Error(`ppt_api_keys list failed: ${error.message}`);

  const stateByName = new Map((data || []).map((row) => [row.key_name, row]));
  const active = [];

  for (const key of keys) {
    const state = stateByName.get(key.name);
    if (!state) {
      active.push({ ...key, dailyRemaining: null });
      continue;
    }
    if (state.status === "disabled") continue;
    if (state.credit_window_date !== today) {
      active.push({ ...key, dailyRemaining: null });
      continue;
    }
    if (state.status !== "active") continue;
    active.push({
      ...key,
      dailyRemaining: typeof state.daily_remaining === "number" ? state.daily_remaining : null,
    });
  }
  return active;
}

async function updateKeyStatus(key, patch) {
  const cleanPatch = { ...patch };
  const creditDelta = typeof cleanPatch.credits_used_delta === "number" ? cleanPatch.credits_used_delta : null;
  delete cleanPatch.credits_used_delta;

  if (creditDelta !== null) {
    const { data } = await supabase
      .from("ppt_api_keys")
      .select("credits_used_today,credit_window_date")
      .eq("key_name", key.name)
      .maybeSingle();

    const currentCredits = data?.credit_window_date === todayIso() ? Number(data.credits_used_today || 0) : 0;
    cleanPatch.credits_used_today = currentCredits + creditDelta;
    cleanPatch.credit_window_date = todayIso();
  }

  await supabase.from("ppt_api_keys").upsert({
    key_name: key.name,
    updated_at: new Date().toISOString(),
    ...cleanPatch,
  }, { onConflict: "key_name" });
}

function gradeFrom(value) {
  const text = String(value || '').toLowerCase();
  const match =
    text.match(/\bpsa\s*_?\s*(10|[1-9])\b/) ||
    text.match(/\bgrade\s*_?\s*(10|[1-9])\b/) ||
    text.match(/^(10|[1-9])$/);
  if (!match) return null;
  return parseInt(match[1], 10);
}

function numberFrom(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value !== 'string') return null;
  const parsed = parseFloat(value.replace(/[^0-9.]/g, ''));
  return Number.isFinite(parsed) ? parsed : null;
}

function priceFrom(value) {
  const keys = [
    'smartMarketPrice', 'marketPrice', 'marketPrice90Day', 'marketPrice30Day', 'marketPrice7Day',
    'averagePrice', 'average', 'avg', 'medianPrice', 'median', 'salePrice', 'soldPrice',
    'soldAmount', 'finalPrice', 'totalPrice', 'amount', 'price', 'value', 'avgSalePrice',
    'averageSalePrice', 'recentSalePrice', 'lastSalePrice', 'ebayAvg', 'ebayMedian',
    'ebayPrice', 'gradedPrice', 'certPrice'
  ];
  for (const key of keys) {
    const price = numberFrom(value[key]);
    if (price !== null && price > 0) return price;
  }
  return null;
}

function isPsaKey(key) {
  return String(key || '').toLowerCase().includes('psa');
}

function isGradedDataContainerKey(key) {
  const text = String(key || '').toLowerCase();
  return text.includes('ebay') || text.includes('graded') || text.includes('grading') || text.includes('sales');
}

function mapMentionsPsa(value) {
  return Object.entries(value).some(([key, item]) =>
    key.toLowerCase().includes('psa') ||
    (typeof item === 'string' && item.toLowerCase().includes('psa'))
  );
}

function collectPsaPrices(value, points, isPsaScope) {
  if (Array.isArray(value)) {
    for (const item of value) collectPsaPrices(item, points, isPsaScope);
    return;
  }
  if (!value || typeof value !== 'object') return;

  const map = value;
  const directGrade =
    gradeFrom(map.grade) || gradeFrom(map.psaGrade) || gradeFrom(map.gradeValue) || gradeFrom(map.gradeLabel) || gradeFrom(map.title);
  const directPrice = priceFrom(map);
  if ((isPsaScope || mapMentionsPsa(map)) && directGrade !== null && directPrice !== null) {
    points.set(directGrade, directPrice);
  }

  for (const [key, item] of Object.entries(map)) {
    const nextIsPsaScope = isPsaScope || isPsaKey(key);
    const keyGrade = gradeFrom(key);
    if (nextIsPsaScope && keyGrade !== null) {
      const price = item && typeof item === 'object' && !Array.isArray(item) ? priceFrom(item) : numberFrom(item);
      if (price !== null && price > 0) points.set(keyGrade, price);
    }
    if (nextIsPsaScope || isGradedDataContainerKey(key) || (item !== null && typeof item === 'object' && !Array.isArray(item))) {
      collectPsaPrices(item, points, nextIsPsaScope);
    }
  }
}

function parsePsaPrices(json) {
  const points = new Map();
  collectPsaPrices(json, points, false);
  
  let totalPopulation = undefined;
  let gemRate = undefined;
  const popByGrade = new Map();
  const salesByGradeMap = new Map();

  const ebay = json.ebay;
  if (ebay && typeof ebay === 'object') {
    const salesByGrade = ebay.salesByGrade;
    if (salesByGrade && typeof salesByGrade === 'object') {
      for (const [key, val] of Object.entries(salesByGrade)) {
        if (typeof val === 'object' && val !== null) {
          const match = key.match(/^psa(\d+)$/i) || key.match(/^grade(\d+)$/i);
          if (match) {
            const grade = parseInt(match[1], 10);
            salesByGradeMap.set(grade, {
              volume: typeof val.count === 'number' ? val.count : undefined,
              trend: typeof val.marketTrend === 'string' ? val.marketTrend : undefined,
            });
          }
        }
      }
    }
  }

  const psaPrices = Array.from(points.entries())
    .filter(([grade, price]) => grade >= 1 && grade <= 10 && price > 0)
    .map(([grade, price]) => {
      const salesData = salesByGradeMap.get(grade);
      return { 
        grade, 
        price,
        population: popByGrade.get(grade),
        volume: salesData?.volume,
        trend: salesData?.trend
      };
    })
    .sort((a, b) => a.price - b.price);

  return { psaPrices, totalPopulation, gemRate };
}

async function fetchPsaData(tcgplayerId, cardName, key) {
  const url = new URL("https://www.pokemonpricetracker.com/api/v2/cards");
  url.searchParams.set("includeEbay", "true");
  url.searchParams.set("includeHistory", "false");
  url.searchParams.set("days", "90");
  url.searchParams.set("limit", "1");
  
  if (tcgplayerId) {
    url.searchParams.set("tcgPlayerId", String(tcgplayerId));
  } else {
    url.searchParams.set("search", cardName);
  }

  const resp = await fetch(url, {
    headers: {
      "Accept": "application/json",
      "Authorization": `Bearer ${key}`
    }
  });

  let body = null;
  const text = await resp.text();
  try { body = JSON.parse(text); } catch(e) {}

  const dailyRemaining = resp.headers.get("X-RateLimit-Daily-Remaining");
  const creditsConsumed = resp.headers.get("X-API-Calls-Consumed");
  
  const metadata = body?.metadata || {};
  const consumed = metadata.apiCallsConsumed;
  const credits = typeof consumed?.total === "number" ? consumed.total : parseInt(creditsConsumed || '1', 10);
  const remaining = dailyRemaining ? parseInt(dailyRemaining, 10) : null;

  if (resp.status === 429) {
    return { ok: false, rateLimited: true, remaining, credits, error: body?.message || text };
  }
  
  if (!resp.ok) {
    if (String(body?.message || text).toLowerCase().includes("invalid api key")) {
      return { ok: false, invalidKey: true, remaining, credits, error: body?.message || text };
    }
    return { ok: false, error: body?.message || text, remaining, credits };
  }

  const dataArray = Array.isArray(body?.data) ? body.data : [body?.data];
  const bestData = dataArray[0]; 
  
  if (!bestData) {
    return { ok: true, found: false, remaining, credits };
  }

  const parsed = parsePsaPrices(bestData);
  return { ok: true, found: true, remaining, credits, ...parsed };
}

const CHECKPOINT_FILE = require('path').join(__dirname, 'psa_sync_state.json');
function loadCheckpoint() {
  if (fs.existsSync(CHECKPOINT_FILE)) {
    try { return JSON.parse(fs.readFileSync(CHECKPOINT_FILE, 'utf8')); } catch(e) {}
  }
  return { lastId: '', processedIds: [] };
}
function saveCheckpoint(state) {
  fs.writeFileSync(CHECKPOINT_FILE, JSON.stringify(state, null, 2), 'utf8');
}

async function run() {
  const allKeys = loadApiKeys();
  console.log(`Loaded ${allKeys.length} API keys from environment.`);
  
  await ensureApiKeys(allKeys);
  let activeKeys = await getActiveKeys(allKeys);
  console.log(`Found ${activeKeys.length} ACTIVE keys for today.`);

  if (activeKeys.length === 0) {
    console.error("No active keys available for today. Exiting.");
    process.exit(1);
  }

  const state = loadCheckpoint();
  
  // To avoid duplicate processing, load already processed tcgplayer_product_ids
  const processedSet = new Set(state.processedIds);

  console.log('Fetching tcgplayer_product_id from database...');
  let hasMore = true;
  let globalLastId = state.lastId || '';
  
  let successCount = 0;
  let notFoundCount = 0;
  let failCount = 0;

  while (hasMore) {
    let query = supabase
      .from('pokemon_cards')
      .select('id, tcgplayer_product_id, name, card_number, pokemon_sets(name)')
      .eq('language', 'en')
      .not('tcgplayer_product_id', 'is', null)
      .order('id')
      .limit(1000); // Fetch a large block, then process in batches of 50
      
    if (globalLastId) {
      query = query.gt('id', globalLastId);
    }

    const { data: cardsChunk, error } = await query;
    if (error) throw error;

    if (!cardsChunk || cardsChunk.length === 0) {
      hasMore = false;
      break;
    }

    // Filter out already processed TCG IDs within this chunk
    const cardsToProcess = cardsChunk.filter(c => {
      const numericId = parseInt(c.tcgplayer_product_id, 10);
      return numericId && !processedSet.has(numericId);
    });

    if (cardsToProcess.length === 0) {
      globalLastId = cardsChunk[cardsChunk.length - 1].id;
      continue;
    }

    // Process this block in batches
    for (let i = 0; i < cardsToProcess.length; i += BATCH_SIZE) {
      const batchCards = cardsToProcess.slice(i, i + BATCH_SIZE);
      const resultsPromises = [];
      const executing = new Set();
      
      console.log(`\nProcessing batch of ${batchCards.length} cards (Concurrency: ${CONCURRENCY_LIMIT})...`);

      for (const card of batchCards) {
        const numericId = parseInt(card.tcgplayer_product_id, 10);

        const p = (async () => {
          let success = false;
          while (!success) {
            if (activeKeys.length === 0) {
              console.error("All API keys are exhausted. Stopping run.");
              process.exit(0);
            }

            const currentKey = activeKeys[0];
            const result = await fetchPsaData(numericId, card.name, currentKey.key);

            // Log credit usage and update state
            if (result.credits > 0 || result.remaining !== null) {
              const isRateLimited = result.remaining !== null && result.remaining <= 0;
              await updateKeyStatus(currentKey, {
                status: isRateLimited ? "rate_limited" : "active",
                daily_remaining: result.remaining,
                credits_used_delta: result.credits || (result.ok ? 1 : 0),
                last_used_at: new Date().toISOString(),
                rate_limited_at: isRateLimited ? new Date().toISOString() : null,
                last_error: result.ok ? null : result.error,
              });

              if (isRateLimited || result.invalidKey || result.rateLimited) {
                // Thread-safe shift: only remove if it's still the same key
                if (activeKeys[0] && activeKeys[0].name === currentKey.name) {
                  console.log(`Key ${currentKey.name} exhausted or invalid. Switching to next...`);
                  activeKeys.shift(); 
                }
                await delay(200);
                continue; // Retry with next key
              }
            }

            if (!result.ok) {
              console.error(`Failed to fetch ${numericId} with ${currentKey.name}: ${result.error}`);
              failCount++;
              break; 
            }

            if (!result.found || !result.psaPrices?.length) {
              // process.stdout.write(`-`);
              notFoundCount++;
            } else {
              // process.stdout.write(`+`);
              successCount++;
            }

            // ALWAYS save to DB (even if empty, to cache the "not found" state and save future API requests)
            const fetchedAt = new Date();
            const expiresAt = new Date(fetchedAt.getTime() + 7 * 24 * 60 * 60 * 1000); 

            const { error: upsertError } = await supabase
              .from('pokemon_psa_price_cache')
              .upsert({
                cache_key: `tcg:${numericId}`,
                tcgplayer_id: numericId,
                card_name: card.name,
                set_name: card.pokemon_sets?.name || null,
                card_number: card.card_number,
                psa_prices: result.psaPrices || [], // Save empty array
                total_population: result.totalPopulation || null,
                gem_rate: result.gemRate || null,
                source: 'pokemonpricetracker',
                fetched_at: fetchedAt.toISOString(),
                expires_at: expiresAt.toISOString(),
              });

            if (upsertError) {
              console.error(`DB Upsert failed for ${numericId}: ${upsertError.message}`);
              failCount++;
            }
            
            processedSet.add(numericId);
            state.processedIds.push(numericId);
            
            // Keep JSON array from getting too massive
            if (state.processedIds.length > 50000) {
              state.processedIds = state.processedIds.slice(-20000);
            }
            
            success = true;
          }
        })();
        
        executing.add(p);
        p.then(() => executing.delete(p));
        resultsPromises.push(p);

        if (executing.size >= CONCURRENCY_LIMIT) {
          await Promise.race(executing);
        }
        
        // Small delay between kicking off requests to prevent bursting too hard
        await delay(100); 
      }
      
      // Wait for the whole batch to finish
      await Promise.all(resultsPromises);
      console.log(`Stats -> Success: ${successCount} | Empty: ${notFoundCount} | Failed: ${failCount}`);
    }

    // Update globalLastId safely after full chunk is processed
    globalLastId = cardsChunk[cardsChunk.length - 1].id;
    state.lastId = globalLastId;
    saveCheckpoint(state);
  }

  console.log(`\n🎉 Script finished!`);
  console.log(`Success (Found): ${successCount}`);
  console.log(`Success (Empty): ${notFoundCount}`);
  console.log(`Failed: ${failCount}`);
}

run().catch(err => {
  console.error('\nFatal error:', err);
  process.exit(1);
});
