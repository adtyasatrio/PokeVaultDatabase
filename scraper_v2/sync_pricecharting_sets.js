require('dotenv').config({ path: require('path').join(__dirname, '..', 'scraper', '.env') });
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });
const { createClient } = require('@supabase/supabase-js');
const cheerio = require('cheerio');

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_KEY;

if (!SUPABASE_KEY || !SUPABASE_URL) {
  console.error('ERROR: SUPABASE_URL or SUPABASE_SERVICE_KEY not set.');
  process.exit(1);
}

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

const TARGET_FILES = [
  'Pokemon XY Card Prices | Holo, Rare, & Graded Cards.html',
  // 'Another Set.html'
];

function normalizeStr(str) {
  return String(str || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

function extractCardNum(str) {
  // Extracts the leading alphanumeric part of a number, e.g., "142/146" -> "142", "SWSH020" -> "swsh020"
  const text = String(str || '').toLowerCase().split('/')[0];
  return text.trim();
}

function parsePrice(str) {
  if (!str) return null;
  const num = parseFloat(str.replace(/[^0-9.]/g, ''));
  return Number.isFinite(num) && num > 0 ? num : null;
}

async function loadCardIndex() {
  console.log('Loading all Pokemon cards from database for matching...');
  
  let allCards = [];
  let hasMore = true;
  let lastId = '';

  while (hasMore) {
    let query = supabase
      .from('pokemon_cards')
      .select('id, tcgplayer_product_id, name, card_number, pokemon_sets(name)')
      .eq('language', 'en')
      .not('tcgplayer_product_id', 'is', null)
      .order('id')
      .limit(1000);
      
    if (lastId) {
      query = query.gt('id', lastId);
    }

    const { data, error } = await query;
    if (error) throw error;
    
    if (data && data.length > 0) {
      allCards.push(...data);
      lastId = data[data.length - 1].id;
    } else {
      hasMore = false;
    }
  }

  console.log(`Loaded ${allCards.length} cards from database.`);

  const index = new Map();
  for (const card of allCards) {
    const normName = normalizeStr(card.name);
    const normNum = extractCardNum(card.card_number);
    const key = `${normName}|${normNum}`;
    
    if (!index.has(key)) index.set(key, []);
    index.get(key).push(card);
  }
  return index;
}

async function run() {
  const cardIndex = await loadCardIndex();

  let successCount = 0;
  let notFoundCount = 0;
  let failCount = 0;

  for (const file of TARGET_FILES) {
    console.log(`\nProcessing File: ${file}`);
    try {
      const fs = require('fs');
      if (!fs.existsSync(file)) {
        console.error(`File not found: ${file}`);
        continue;
      }
      
      const html = fs.readFileSync(file, 'utf8');
      const $ = cheerio.load(html);
      
      const rows = $('#games_table tbody tr');
      console.log(`Found ${rows.length} cards in HTML table.`);

      const promises = [];

      rows.each((i, el) => {
        const titleRaw = $(el).find('td.title a').text().trim();
        if (!titleRaw) return;

        // Parse "Card Name #Number"
        // e.g., "Blastoise EX #142", "Pikachu #SWSH020", "Charizard"
        let pcName = titleRaw;
        let pcNum = '';
        
        const hashIndex = titleRaw.lastIndexOf('#');
        if (hashIndex !== -1) {
          pcName = titleRaw.substring(0, hashIndex).trim();
          pcNum = titleRaw.substring(hashIndex + 1).trim();
        }

        const psa9Price = parsePrice($(el).find('td.cib_price .js-price').text());
        const psa10Price = parsePrice($(el).find('td.new_price .js-price').text());

        const psaPrices = [];
        if (psa9Price) psaPrices.push({ grade: 9, price: psa9Price });
        if (psa10Price) psaPrices.push({ grade: 10, price: psa10Price });

        if (psaPrices.length === 0) {
          return; // The user requested we ONLY care if PSA prices exist, ignore if only Ungraded exists
        }

        // Match against our DB
        const normName = normalizeStr(pcName);
        const normNum = extractCardNum(pcNum);
        
        // Exact Match Attempt
        let matches = cardIndex.get(`${normName}|${normNum}`);
        
        // Fuzzy Match Attempt (if exact fails, try just finding by number if number exists, then check name similarity)
        if (!matches && normNum) {
          // This is a naive fallback: search all keys for matching number and name substring
          for (const [key, cards] of cardIndex.entries()) {
            const [kName, kNum] = key.split('|');
            if (kNum === normNum && (kName.includes(normName) || normName.includes(kName))) {
              matches = cards;
              break;
            }
          }
        }

        if (!matches || matches.length === 0) {
          console.log(`[Not Found] PC: "${titleRaw}" (Name: ${pcName}, Num: ${pcNum}) -> No DB match`);
          notFoundCount++;
          return;
        }

        // Just take the first match if multiple
        const card = matches[0];
        const numericId = parseInt(card.tcgplayer_product_id, 10);
        if (!numericId) return;

        const fetchedAt = new Date();
        const expiresAt = new Date(fetchedAt.getTime() + 7 * 24 * 60 * 60 * 1000); // 7 days TTL

        // Push UPSERT promise
        const p = supabase
          .from('pokemon_psa_price_cache')
          .upsert({
            cache_key: `tcg:${numericId}`,
            tcgplayer_id: numericId,
            card_name: card.name,
            set_name: card.pokemon_sets?.name || null,
            card_number: card.card_number,
            psa_prices: psaPrices,
            total_population: null,
            gem_rate: null,
            source: 'pricecharting',
            fetched_at: fetchedAt.toISOString(),
            expires_at: expiresAt.toISOString(),
          })
          .then(({ error }) => {
            if (error) {
              console.error(`[Error] DB Upsert failed for ${numericId} (${card.name}): ${error.message}`);
              failCount++;
            } else {
              console.log(`[Success] Mapped "${titleRaw}" -> "${card.name}" (${card.card_number}) -> Saved ${psaPrices.length} grades`);
              successCount++;
            }
          });
        
        promises.push(p);
      });

      // Wait for all upserts in this set to finish
      await Promise.all(promises);

    } catch (e) {
      console.error(`Error processing ${file}:`, e);
      failCount++;
    }
  }

  console.log(`\n🎉 PriceCharting Sync Finished!`);
  console.log(`Success (Mapped & Saved): ${successCount}`);
  console.log(`Not Found (No DB Match): ${notFoundCount}`);
  console.log(`Failed: ${failCount}`);
}

run().catch(err => {
  console.error('\nFatal error:', err);
  process.exit(1);
});
