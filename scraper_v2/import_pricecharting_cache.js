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
const TTL_DAYS = 7;
const BATCH_SIZE = 100;

function readArg(name) {
  const index = process.argv.indexOf(name);
  return index === -1 ? null : process.argv[index + 1];
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

function mergePrices(existingPrices, pricechartingPrices) {
  const preserved = (Array.isArray(existingPrices) ? existingPrices : []).filter(
    (entry) => ![9, 10].includes(Number(entry?.grade)),
  );
  return [...preserved, ...pricechartingPrices].sort(
    (left, right) => Number(left.grade) - Number(right.grade),
  );
}

async function main() {
  const inputFile = readArg('--input');
  const shouldCommit = process.argv.includes('--commit');
  if (!inputFile) {
    throw new Error(
      'Usage: node scraper_v2/import_pricecharting_cache.js --input <safe-cache-rows.json> [--commit]',
    );
  }
  if (!SUPABASE_URL || !SUPABASE_KEY) {
    throw new Error('SUPABASE_URL or SUPABASE_SERVICE_KEY is not configured');
  }

  const candidates = JSON.parse(fs.readFileSync(inputFile, 'utf8'));
  if (!Array.isArray(candidates) || !candidates.length) {
    throw new Error('Input did not contain any cache candidates');
  }

  const grouped = new Map();
  for (const row of candidates) {
    const rows = grouped.get(row.cache_key) || [];
    rows.push(row);
    grouped.set(row.cache_key, rows);
  }
  const duplicateGroups = [...grouped.entries()].filter(([, rows]) => rows.length > 1);
  const conflictingDuplicateKeys = new Set(
    duplicateGroups
      .filter(([, rows]) => {
        const signatures = new Set(
          rows.map((row) =>
            JSON.stringify({
              tcgplayer_id: row.tcgplayer_id,
              pokemon_card_id: row.pokemon_card_id,
              set_id: row.set_id,
              card_number: row.card_number,
              psa_prices: row.psa_prices,
            }),
          ),
        );
        return signatures.size > 1;
      })
      .map(([key]) => key),
  );
  const uniqueCandidates = [...grouped.entries()]
    .filter(([key]) => !conflictingDuplicateKeys.has(key))
    .map(([, rows]) => rows[0]);

  const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);
  const existingRows = await fetchAll(
    supabase,
    'pokemon_psa_price_cache',
    'cache_key, tcgplayer_id, card_name, set_name, card_number, psa_prices, source, fetched_at, expires_at, created_at, updated_at, total_population, gem_rate',
  );
  const existingByKey = new Map(existingRows.map((row) => [row.cache_key, row]));

  const fetchedAt = new Date();
  const expiresAt = new Date(fetchedAt.getTime() + TTL_DAYS * 24 * 60 * 60 * 1000);
  const rowsToUpsert = uniqueCandidates.map((candidate) => {
    const existing = existingByKey.get(candidate.cache_key);
    const mergedPrices = mergePrices(existing?.psa_prices, candidate.psa_prices);
    const preservedOtherGrades = mergedPrices.some(
      (entry) => ![9, 10].includes(Number(entry.grade)),
    );
    return {
      cache_key: candidate.cache_key,
      tcgplayer_id: candidate.tcgplayer_id,
      card_name: candidate.card_name,
      set_name: candidate.set_name,
      card_number: candidate.card_number,
      psa_prices: mergedPrices,
      source: preservedOtherGrades ? 'mixed' : 'pricecharting',
      fetched_at: fetchedAt.toISOString(),
      expires_at: expiresAt.toISOString(),
      updated_at: fetchedAt.toISOString(),
      total_population: existing?.total_population ?? null,
      gem_rate: existing?.gem_rate ?? null,
    };
  });

  const overlapping = uniqueCandidates.filter((row) => existingByKey.has(row.cache_key));
  const summary = {
    mode: shouldCommit ? 'commit' : 'dry-run',
    inputCandidates: candidates.length,
    uniqueCandidates: uniqueCandidates.length,
    identicalDuplicateRowsCollapsed: duplicateGroups
      .filter(([key]) => !conflictingDuplicateKeys.has(key))
      .reduce((total, [, rows]) => total + rows.length - 1, 0),
    conflictingDuplicateKeysSkipped: conflictingDuplicateKeys.size,
    inserts: uniqueCandidates.length - overlapping.length,
    updates: overlapping.length,
    updatesPreservingOtherGrades: overlapping.filter((candidate) =>
      (existingByKey.get(candidate.cache_key)?.psa_prices || []).some(
        (entry) => ![9, 10].includes(Number(entry.grade)),
      ),
    ).length,
    expiresAt: expiresAt.toISOString(),
  };
  console.log(JSON.stringify(summary, null, 2));

  if (!shouldCommit) return;
  if (!rowsToUpsert.length || conflictingDuplicateKeys.size > 0) {
    throw new Error('Refusing to commit with zero rows or conflicting duplicate cache keys');
  }

  const backupDir = path.dirname(inputFile);
  const backupFile = path.join(
    backupDir,
    `cache-overlap-backup-${fetchedAt.toISOString().replace(/[:.]/g, '-')}.json`,
  );
  fs.writeFileSync(
    backupFile,
    JSON.stringify(overlapping.map((row) => existingByKey.get(row.cache_key)), null, 2),
  );
  console.log(`Backup written: ${backupFile}`);

  for (let start = 0; start < rowsToUpsert.length; start += BATCH_SIZE) {
    const { error } = await supabase
      .from('pokemon_psa_price_cache')
      .upsert(rowsToUpsert.slice(start, start + BATCH_SIZE), { onConflict: 'cache_key' });
    if (error) throw error;
    console.log(`Upserted ${Math.min(start + BATCH_SIZE, rowsToUpsert.length)}/${rowsToUpsert.length}`);
  }
}

main().catch((error) => {
  console.error(error.message || error);
  process.exit(1);
});
