const fs = require('fs');
const path = require('path');

require('dotenv').config({
  path: path.join(__dirname, '..', 'scraper', '.env'),
  quiet: true,
});
require('dotenv').config({ path: path.join(__dirname, '..', '.env'), quiet: true });

const { createClient } = require('@supabase/supabase-js');

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_KEY;

function readArg(name) {
  const index = process.argv.indexOf(name);
  return index === -1 ? null : process.argv[index + 1];
}

function normalize(value) {
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

function parsePricechartingSetName(value) {
  const name = String(value || '').trim();
  const japanese = name.startsWith('Pokemon Japanese ');
  return {
    language: japanese ? 'ja' : 'en',
    name: name.replace(/^Pokemon Japanese /, '').replace(/^Pokemon /, ''),
  };
}

async function fetchAll(supabase, table, columns) {
  const pageSize = 1000;
  const rows = [];
  for (let start = 0; ; start += pageSize) {
    const { data, error } = await supabase
      .from(table)
      .select(columns)
      .range(start, start + pageSize - 1);
    if (error) throw error;
    rows.push(...data);
    if (data.length < pageSize) return rows;
  }
}

async function main() {
  const scrapeDir = readArg('--scrape-dir');
  if (!scrapeDir) {
    throw new Error('Usage: node scraper_v2/analyze_pricecharting_category.js --scrape-dir <directory>');
  }
  if (!SUPABASE_URL || !SUPABASE_KEY) {
    throw new Error('SUPABASE_URL or SUPABASE_SERVICE_KEY is not configured');
  }

  const manifest = JSON.parse(fs.readFileSync(path.join(scrapeDir, 'manifest.json'), 'utf8'));
  const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);
  const [sets, cards, existingCache] = await Promise.all([
    fetchAll(supabase, 'pokemon_sets', 'id, name, language'),
    fetchAll(
      supabase,
      'pokemon_cards',
      'id, set_id, language, name, card_number, tcgplayer_product_id',
    ),
    fetchAll(supabase, 'pokemon_psa_price_cache', 'cache_key, tcgplayer_id, psa_prices, source'),
  ]);

  const setIndex = new Map();
  for (const set of sets) {
    const key = `${set.language}|${normalize(set.name)}`;
    const candidates = setIndex.get(key) || [];
    candidates.push(set);
    setIndex.set(key, candidates);
  }

  const cardsBySet = new Map();
  const cardCountByTcgplayerId = new Map();
  for (const card of cards) {
    const setCards = cardsBySet.get(card.set_id) || [];
    setCards.push(card);
    cardsBySet.set(card.set_id, setCards);
    if (card.tcgplayer_product_id != null) {
      const id = String(card.tcgplayer_product_id);
      cardCountByTcgplayerId.set(id, (cardCountByTcgplayerId.get(id) || 0) + 1);
    }
  }

  const summary = {
    scrapedSets: manifest.sets.length,
    scrapedRows: 0,
    pricedRows: 0,
    exactSetMatches: 0,
    ambiguousSetMatches: 0,
    unmatchedSets: 0,
    exactCardMatches: 0,
    safeCacheRows: 0,
    duplicateTcgplayerIdRows: 0,
    cardsWithoutTcgplayerId: 0,
    unmatchedPricedRows: 0,
  };
  const unmatchedSets = [];
  const ambiguousSets = [];
  const unmatchedRows = [];
  const safeRows = [];

  for (const entry of manifest.sets) {
    const parsedSet = parsePricechartingSetName(entry.name);
    const setCandidates =
      setIndex.get(`${parsedSet.language}|${normalize(parsedSet.name)}`) || [];
    const setData = JSON.parse(
      fs.readFileSync(path.join(scrapeDir, `${entry.slug}.json`), 'utf8'),
    );
    summary.scrapedRows += setData.rows.length;

    const pricedRows = setData.rows.filter((row) => parsePrice(row.grade9) || parsePrice(row.grade10));
    summary.pricedRows += pricedRows.length;

    if (setCandidates.length === 0) {
      summary.unmatchedSets += 1;
      unmatchedSets.push({ pricecharting: entry.name, slug: entry.slug, pricedRows: pricedRows.length });
      continue;
    }
    if (setCandidates.length > 1) {
      summary.ambiguousSetMatches += 1;
      ambiguousSets.push({
        pricecharting: entry.name,
        candidates: setCandidates.map((set) => ({ id: set.id, name: set.name })),
      });
      continue;
    }

    summary.exactSetMatches += 1;
    const set = setCandidates[0];
    const cardIndex = new Map();
    for (const card of cardsBySet.get(set.id) || []) {
      const key = `${normalize(card.name)}|${normalizeNumber(card.card_number)}`;
      const candidates = cardIndex.get(key) || [];
      candidates.push(card);
      cardIndex.set(key, candidates);
    }

    for (const row of pricedRows) {
      const parsed = parseTitle(row.title);
      const candidates =
        cardIndex.get(`${normalize(parsed.name)}|${normalizeNumber(parsed.number)}`) || [];
      if (candidates.length !== 1) {
        summary.unmatchedPricedRows += 1;
        if (unmatchedRows.length < 100) {
          unmatchedRows.push({ set: entry.name, title: row.title, candidates: candidates.length });
        }
        continue;
      }

      summary.exactCardMatches += 1;
      const card = candidates[0];
      if (card.tcgplayer_product_id == null) {
        summary.cardsWithoutTcgplayerId += 1;
        continue;
      }
      const tcgplayerId = String(card.tcgplayer_product_id);
      if ((cardCountByTcgplayerId.get(tcgplayerId) || 0) !== 1) {
        summary.duplicateTcgplayerIdRows += 1;
        continue;
      }

      const psaPrices = [];
      const grade9 = parsePrice(row.grade9);
      const grade10 = parsePrice(row.grade10);
      if (grade9) psaPrices.push({ grade: 9, price: grade9 });
      if (grade10) psaPrices.push({ grade: 10, price: grade10 });
      summary.safeCacheRows += 1;
      safeRows.push({
        cache_key: `tcg:${tcgplayerId}`,
        tcgplayer_id: Number(tcgplayerId),
        pokemon_card_id: card.id,
        card_name: card.name,
        set_id: set.id,
        set_name: set.name,
        card_number: card.card_number,
        language: card.language,
        psa_prices: psaPrices,
        pricecharting_product_id: row.productId ? Number(row.productId) : null,
        pricecharting_url: row.href,
      });
    }
  }

  const report = {
    summary,
    existingCacheOverlap: {},
    unmatchedSets,
    ambiguousSets,
    unmatchedRowSample: unmatchedRows,
  };
  const candidateKeys = new Set(safeRows.map((row) => row.cache_key));
  const overlappingRows = existingCache.filter((row) => candidateKeys.has(row.cache_key));
  const sourceCounts = {};
  for (const row of overlappingRows) {
    sourceCounts[row.source] = (sourceCounts[row.source] || 0) + 1;
  }
  report.existingCacheOverlap = {
    rows: overlappingRows.length,
    rowsWithPrices: overlappingRows.filter(
      (row) => Array.isArray(row.psa_prices) && row.psa_prices.length > 0,
    ).length,
    rowsWithGradesOtherThan9Or10: overlappingRows.filter(
      (row) =>
        Array.isArray(row.psa_prices) &&
        row.psa_prices.some((price) => ![9, 10].includes(Number(price.grade))),
    ).length,
    sources: sourceCounts,
  };
  fs.writeFileSync(path.join(scrapeDir, 'analysis.json'), JSON.stringify(report, null, 2));
  fs.writeFileSync(path.join(scrapeDir, 'safe-cache-rows.json'), JSON.stringify(safeRows));
  console.log(JSON.stringify(report, null, 2));
}

main().catch((error) => {
  console.error(error.message || error);
  process.exit(1);
});
