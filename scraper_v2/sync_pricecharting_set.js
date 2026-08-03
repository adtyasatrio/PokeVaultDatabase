const path = require('path');
const { spawnSync } = require('child_process');
const fs = require('fs');

require('dotenv').config({
  path: path.join(__dirname, '..', 'scraper', '.env'),
  quiet: true,
});
require('dotenv').config({ path: path.join(__dirname, '..', '.env'), quiet: true });

const { createClient } = require('@supabase/supabase-js');
const cheerio = require('cheerio');

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_KEY;
const TTL_DAYS = 7;

function readArg(name) {
  const index = process.argv.indexOf(name);
  return index === -1 ? null : process.argv[index + 1];
}

function normalizeName(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

function normalizeNumber(value) {
  const number = String(value || '').split('/')[0].trim().toLowerCase();
  return /^\d+$/.test(number) ? String(Number(number)) : number;
}

function parseTitle(value) {
  const title = String(value || '').trim();
  const hashIndex = title.lastIndexOf('#');
  if (hashIndex === -1) return { title, name: title, number: '' };
  return {
    title,
    name: title.slice(0, hashIndex).trim(),
    number: title.slice(hashIndex + 1).trim(),
  };
}

function parsePrice(value) {
  const parsed = Number.parseFloat(String(value || '').replace(/[^0-9.]/g, ''));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

async function fetchHtml(url) {
  const response = await fetch(url, {
    headers: {
      accept: 'text/html,application/xhtml+xml',
      'user-agent':
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/138 Safari/537.36',
    },
    signal: AbortSignal.timeout(30_000),
  }).catch(() => null);

  if (response?.ok) return response.text();
  if (response && response.status !== 403) {
    throw new Error(`PriceCharting returned HTTP ${response.status}`);
  }

  const python = path.join(__dirname, '.venv', 'bin', 'python');
  const pythonCode = [
    'from curl_cffi import requests',
    'import sys',
    'response = requests.get(sys.argv[1], impersonate="chrome110", timeout=30)',
    'response.raise_for_status()',
    'sys.stdout.write(response.text)',
  ].join('; ');
  const result = spawnSync(python, ['-c', pythonCode, url], {
    encoding: 'utf8',
    maxBuffer: 20 * 1024 * 1024,
  });
  if (result.status !== 0) {
    throw new Error(result.stderr.trim() || 'curl_cffi failed to fetch PriceCharting');
  }
  return result.stdout;
}

async function main() {
  const url = readArg('--url');
  const dataFile = readArg('--data-file');
  const setId = readArg('--set-id');
  const shouldCommit = process.argv.includes('--commit');

  if ((!url && !dataFile) || !setId) {
    throw new Error(
      'Usage: node scraper_v2/sync_pricecharting_set.js (--url <pricecharting-url> | --data-file <json>) --set-id <pokemon_sets.id> [--commit]',
    );
  }
  if (!SUPABASE_URL || !SUPABASE_KEY) {
    throw new Error('SUPABASE_URL or SUPABASE_SERVICE_KEY is not configured');
  }

  const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);
  const { data: cards, error: cardError } = await supabase
    .from('pokemon_cards')
    .select('id, name, card_number, tcgplayer_product_id, pokemon_sets(name)')
    .eq('set_id', setId)
    .eq('language', 'en')
    .not('tcgplayer_product_id', 'is', null);

  if (cardError) throw cardError;
  if (!cards?.length) throw new Error(`No English cards found for set_id=${setId}`);

  const cardIndex = new Map();
  for (const card of cards) {
    const key = `${normalizeName(card.name)}|${normalizeNumber(card.card_number)}`;
    const matches = cardIndex.get(key) || [];
    matches.push(card);
    cardIndex.set(key, matches);
  }

  let scrapedRows;
  if (dataFile) {
    scrapedRows = JSON.parse(fs.readFileSync(dataFile, 'utf8'));
  } else {
    const html = await fetchHtml(url);
    const $ = cheerio.load(html);
    scrapedRows = $('#games_table tbody tr')
      .map((_, element) => ({
        title: $(element).find('td.title a').first().text().trim(),
        grade9: $(element).find('td.cib_price .js-price').first().text().trim(),
        grade10: $(element).find('td.new_price .js-price').first().text().trim(),
      }))
      .get()
      .filter((row) => row.title);
  }
  if (!Array.isArray(scrapedRows) || !scrapedRows.length) {
    throw new Error('PriceCharting input did not contain any table rows');
  }

  const matches = [];
  const unmatched = [];
  const ambiguous = [];
  const seenTcgplayerIds = new Set();

  for (const scrapedRow of scrapedRows) {
    const parsed = parseTitle(scrapedRow.title);
    if (!parsed.title) continue;

    const grade9 = parsePrice(scrapedRow.grade9);
    const grade10 = parsePrice(scrapedRow.grade10);
    if (!grade9 && !grade10) continue;

    const key = `${normalizeName(parsed.name)}|${normalizeNumber(parsed.number)}`;
    const candidates = cardIndex.get(key) || [];

    if (candidates.length === 0) {
      unmatched.push(parsed.title);
      continue;
    }
    if (candidates.length > 1) {
      ambiguous.push(parsed.title);
      continue;
    }

    const card = candidates[0];
    const tcgplayerId = Number(card.tcgplayer_product_id);
    if (!Number.isSafeInteger(tcgplayerId) || seenTcgplayerIds.has(tcgplayerId)) continue;
    seenTcgplayerIds.add(tcgplayerId);

    const psaPrices = [];
    if (grade9) psaPrices.push({ grade: 9, price: grade9 });
    if (grade10) psaPrices.push({ grade: 10, price: grade10 });
    matches.push({ card, tcgplayerId, psaPrices, pricechartingTitle: parsed.title });
  }

  console.log(
    JSON.stringify(
      {
        mode: shouldCommit ? 'commit' : 'dry-run',
        setId,
        databaseCards: cards.length,
        pricechartingRows: scrapedRows.length,
        matchedCards: matches.length,
        unmatchedPricedRows: unmatched.length,
        ambiguousRows: ambiguous.length,
        sampleMatches: matches.slice(0, 8).map((match) => ({
          pricecharting: match.pricechartingTitle,
          database: `${match.card.name} ${match.card.card_number}`,
          tcgplayerId: match.tcgplayerId,
          psaPrices: match.psaPrices,
        })),
        sampleUnmatched: unmatched.slice(0, 12),
      },
      null,
      2,
    ),
  );

  if (!shouldCommit) return;
  if (!matches.length) throw new Error('Refusing to commit because no cards matched');
  if (ambiguous.length) throw new Error('Refusing to commit because one or more matches are ambiguous');

  const fetchedAt = new Date();
  const expiresAt = new Date(fetchedAt.getTime() + TTL_DAYS * 24 * 60 * 60 * 1000);
  const rows = matches.map(({ card, tcgplayerId, psaPrices }) => ({
    cache_key: `tcg:${tcgplayerId}`,
    tcgplayer_id: tcgplayerId,
    card_name: card.name,
    set_name: card.pokemon_sets?.name || null,
    card_number: card.card_number,
    psa_prices: psaPrices,
    total_population: null,
    gem_rate: null,
    source: 'pricecharting',
    fetched_at: fetchedAt.toISOString(),
    expires_at: expiresAt.toISOString(),
    updated_at: fetchedAt.toISOString(),
  }));

  for (let start = 0; start < rows.length; start += 100) {
    const { error } = await supabase
      .from('pokemon_psa_price_cache')
      .upsert(rows.slice(start, start + 100), { onConflict: 'cache_key' });
    if (error) throw error;
  }

  console.log(`Committed ${rows.length} White Flare PSA cache rows.`);
}

main().catch((error) => {
  console.error(error.message || error);
  process.exit(1);
});
