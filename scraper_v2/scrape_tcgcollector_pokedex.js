require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });
const puppeteer = require('puppeteer');
const { createClient } = require('@supabase/supabase-js');

// ─── Config ───────────────────────────────────────────────────────────────────
const BATCH_INSERT_SIZE      = parseInt(process.env.BATCH_INSERT_SIZE || '100', 10);
const HEADLESS               = process.env.HEADLESS !== 'false';
const CHROME_PATH            = process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const SUPABASE_URL           = process.env.SUPABASE_URL;
const SUPABASE_KEY           = process.env.SUPABASE_SERVICE_KEY;

if (!SUPABASE_KEY || !SUPABASE_URL) {
  console.error('ERROR: SUPABASE_URL or SUPABASE_SERVICE_KEY not set.');
  process.exit(1);
}

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);
const delay = (ms) => new Promise(r => setTimeout(r, ms));

let langOpt = 'en';
if (process.argv.includes('--jp')) langOpt = 'jp';
else if (process.argv.includes('--id')) langOpt = 'id';
const tcgPath = langOpt === 'en' ? 'intl' : langOpt;
const dbLang = langOpt === 'jp' ? 'ja' : langOpt;

const BASE_URL = 'https://www.tcgcollector.com';
const POKEDEX_URL = `${BASE_URL}/pokedex/${tcgPath}?cardCountMode=anyCardVariant`;

async function setupPage(page) {
  await page.setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
  await page.setViewport({ width: 1280, height: 900 });
  await page.setRequestInterception(true);
  page.on('request', req => {
    const type = req.resourceType();
    if (['image', 'media', 'font'].includes(type)) req.abort();
    else req.continue();
  });
}

const extractPokedexEntries = () => {
  const entries = [];
  document.querySelectorAll('.pokedex-grid-item').forEach(el => {
    const id = el.getAttribute('data-pokemon-id');
    const nameEl = el.querySelector('.pokedex-grid-item-name');
    const numberEl = el.querySelector('.pokedex-grid-item-number');
    const imgEl = el.querySelector('.pokedex-grid-item-image');
    const progressEl = el.querySelector('.progress-label');

    let dex_number = 0;
    if (numberEl) {
      const match = numberEl.textContent.match(/#0*(\d+)/);
      if (match) dex_number = parseInt(match[1], 10);
    }

    let card_count = 0;
    if (progressEl) {
      const match = progressEl.textContent.match(/\d+\/(\d+)/);
      if (match) card_count = parseInt(match[1], 10);
    }

    let image_url = null;
    if (imgEl) {
      const srcset = imgEl.getAttribute('srcset');
      if (srcset) {
        const parts = srcset.split(',').map(s => s.trim().split(/\s+/)).filter(p => p.length === 2);
        parts.sort((a, b) => parseInt(a[1]) - parseInt(b[1]));
        if (parts.length > 0) image_url = parts[parts.length - 1][0];
      }
      if (!image_url) image_url = imgEl.getAttribute('src');
    }

    if (id && nameEl && dex_number > 0) {
      entries.push({
        dex_number,
        name: nameEl.textContent.trim(),
        card_count,
        image_url
      });
    }
  });
  return entries;
};

async function run() {
  console.log(`Starting Pokedex scrape for language: ${langOpt} (${dbLang})`);
  console.log(`URL: ${POKEDEX_URL}`);

  const browser = await puppeteer.launch({
    headless: HEADLESS ? 'new' : false,
    executablePath: CHROME_PATH,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });

  const page = await browser.newPage();
  await setupPage(page);

  console.log('Navigating to Pokedex page...');
  await page.goto(POKEDEX_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  
  console.log('Waiting for Pokedex entries to load...');
  try {
    await page.waitForSelector('.pokedex-grid-item', { timeout: 15000 });
  } catch (e) {
    console.warn('Timeout menunggu .pokedex-grid-item. Mungkin kena Cloudflare, coba jalankan dengan HEADLESS=false.');
  }

  console.log('Scrolling page to load all entries (this might take a few seconds)...');
  await page.evaluate(async () => {
    await new Promise((resolve) => {
      let totalHeight = 0;
      const distance = 500;
      const timer = setInterval(() => {
        const scrollHeight = document.body.scrollHeight;
        window.scrollBy(0, distance);
        totalHeight += distance;
        if (totalHeight >= scrollHeight) {
          clearInterval(timer);
          resolve();
        }
      }, 100);
    });
  });
  
  await delay(2000);

  const entries = await page.evaluate(extractPokedexEntries);
  console.log(`Found ${entries.length} Pokemon in the Pokedex!`);

  if (entries.length === 0) {
    console.error('No entries found. Scrape failed.');
    await browser.close();
    return;
  }

  const dbEntries = entries.map(entry => ({
    id: `tcgc_${dbLang}_pokedex_${entry.dex_number}`,
    dex_number: entry.dex_number,
    name: entry.name,
    language: dbLang,
    card_count: entry.card_count,
    image_url: entry.image_url,
    scraped_at: new Date().toISOString()
  }));

  console.log(`Inserting into Supabase table pokemon_pokedex...`);
  
  let successCount = 0;
  for (let i = 0; i < dbEntries.length; i += BATCH_INSERT_SIZE) {
    const batch = dbEntries.slice(i, i + BATCH_INSERT_SIZE);
    const { error } = await supabase
      .from('pokemon_pokedex')
      .upsert(batch, { onConflict: 'id' });

    if (error) {
      console.error(`Error inserting batch ${i}:`, error.message);
    } else {
      successCount += batch.length;
      process.stdout.write(`\rInserted: ${successCount} / ${dbEntries.length}`);
    }
  }
  console.log(`\nDone! Successfully saved ${successCount} entries.`);

  await browser.close();
}

run().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
