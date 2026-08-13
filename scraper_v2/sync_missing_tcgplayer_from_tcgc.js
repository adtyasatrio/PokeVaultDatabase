require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });
const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
puppeteer.use(StealthPlugin());
const { createClient } = require('@supabase/supabase-js');
const fs = require('fs');
const path = require('path');

// ─── Config ───────────────────────────────────────────────────────────────────
const NUM_WORKERS            = parseInt(process.env.NUM_WORKERS || '3', 10);
const DELAY_BETWEEN_CARDS_MS = parseInt(process.env.DELAY_BETWEEN_CARDS_MS || '1000', 10);
const WAIT_AFTER_MODAL_MS    = parseInt(process.env.WAIT_AFTER_MODAL_MS || '1500', 10);
const HEADLESS               = process.env.HEADLESS !== 'false';
const CHROME_PATH            = process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const MAX_RETRIES            = parseInt(process.env.MAX_RETRIES || '3', 10);
const RETRY_BACKOFF_MS       = parseInt(process.env.RETRY_BACKOFF_MS || '2500', 10);

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_KEY;

if (!SUPABASE_KEY || !SUPABASE_URL) {
  console.error('ERROR: SUPABASE_URL or SUPABASE_SERVICE_KEY not set.');
  process.exit(1);
}

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);
const delay = (ms) => new Promise(r => setTimeout(r, ms));

const BLOCK_HEAVY_ASSETS = false;

async function setupPage(page) {
  await page.setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
  await page.setViewport({ width: 1280, height: 900 });
  if (BLOCK_HEAVY_ASSETS) {
    await page.setRequestInterception(true);
    page.on('request', req => {
      const type = req.resourceType();
      if (['image', 'media', 'font', 'stylesheet'].includes(type)) req.abort();
      else req.continue();
    });
  }
}

const args = process.argv.slice(2);
const LANG_MAP = { '--en': 'en', '--jp': 'ja', '--ja': 'ja', '--id': 'id' };
const langArg = args.find(arg => LANG_MAP[arg]);
const LANG_FILTER = langArg ? LANG_MAP[langArg] : null;

async function fetchMissingCards() {
  const langPrefix = LANG_FILTER ? `tcgc_${LANG_FILTER}_%` : 'tcgc_%';
  console.log(`Fetching cards from DB missing tcgplayer_product_id (filter: ${langPrefix})...`);
  let allCards = [];
  let lastId = '';
  const pageSize = 500;
  
  while (true) {
    let query = supabase
      .from('pokemon_cards')
      .select('id, name')
      .is('tcgplayer_product_id', null)
      .like('id', langPrefix)
      .order('id')
      .limit(pageSize);

    if (lastId) query = query.gt('id', lastId);

    let data, error;
    for (let dbAttempt = 0; dbAttempt < 3; dbAttempt++) {
      const res = await query;
      data = res.data;
      error = res.error;
      if (!error) break;
      if (error.message && error.message.includes('timeout')) {
        console.warn(`\nSupabase read timeout, retrying (${dbAttempt + 1}/3)...`);
        await delay(2000);
      } else {
        break;
      }
    }

    if (error) {
      console.error('Error fetching cards:', error);
      break;
    }
    
    if (!data || data.length === 0) break;
    
    allCards = allCards.concat(data);
    lastId = data[data.length - 1].id;
    console.log(`Fetched ${allCards.length} cards...`);
  }

  // Filter only IDs that have a numeric tcgc id at the end
  const validCards = allCards
    .map(c => {
      const match = c.id.match(/tcgc_[a-z]+_card_(\d+)$/);
      if (match) {
        return {
          id: c.id,
          name: c.name,
          tcgcId: match[1],
          url: `https://www.tcgcollector.com/cards/${match[1]}`
        };
      }
      return null;
    })
    .filter(Boolean);

  console.log(`Total valid TCGCollector cards missing TCGPlayer ID: ${validCards.length}`);
  return validCards;
}

const extractTcgplayerId = () => {
  const extractTcgplayerIdFromHref = (href) => {
    if (!href) return null;
    const candidates = [href];
    try {
      const decoded = decodeURIComponent(href);
      if (decoded !== href) candidates.push(decoded);
    } catch(_) {}
    try {
      const u = new URL(href).searchParams.get('u');
      if (u) {
        candidates.push(u);
        try { candidates.push(decodeURIComponent(u)); } catch(_) {}
      }
    } catch(_) {}
    for (const candidate of candidates) {
      const m = candidate.match(/tcgplayer\.com\/product\/(\d+)/i) || candidate.match(/\/product\/(\d+)/i);
      if (m) return parseInt(m[1], 10);
    }
    return null;
  };
  
  const hrefs = Array.from(document.querySelectorAll('a[href]')).map(a => a.href);
  const tcgCandidateHrefs = hrefs.filter(href => /tcgplayer|\/product\/|product%2f/i.test(href));
  for (const href of tcgCandidateHrefs) {
    const id = extractTcgplayerIdFromHref(href);
    if (id) return id;
  }
  return null;
};

async function worker(workerId, browser, cardQueue, stats) {
  let page = await browser.newPage();
  await setupPage(page);

  while (true) {
    const item = cardQueue.next();
    if (!item) break;
    
    const { id, name, tcgcId, url, index, total } = item;
    let retries = 0, success = false;
    
    while (retries < MAX_RETRIES && !success) {
      try {
        const response = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
        const status = response ? response.status() : 0;
        if (status === 404) {
          console.log(`[W${workerId}] 404 Not Found: ${url}. Skipping.`);
          success = true;
          break; // Don't retry 404
        }
        if (status === 429 || status >= 500) throw new Error(`HTTP ${status}`);

        const title = await page.title();
        if (title.includes('Just a moment') || title.includes('Cloudflare')) {
          console.log(`[W${workerId}] Cloudflare on ${url}. Retrying...`);
          throw new Error('Cloudflare block');
        }

        // Open modal
        try {
          const clicked = await page.evaluate(() => {
            const btn = document.querySelector('.show-card-price-details-modal-button');
            if (!btn) return false;
            btn.click();
            return true;
          });
          if (clicked) {
             await page.waitForSelector('.card-price-details-modal-entry-source-button', { timeout: 3000 }).catch(() => {});
             if (WAIT_AFTER_MODAL_MS > 0) await delay(WAIT_AFTER_MODAL_MS);
          }
        } catch(_) {}

        const tcgplayerId = await page.evaluate(extractTcgplayerId);

        if (tcgplayerId) {
          const tcgplayerUrl = `https://www.tcgplayer.com/product/${tcgplayerId}`;
          const { error } = await supabase
            .from('pokemon_cards')
            .update({
              tcgplayer_product_id: tcgplayerId,
              tcgplayer_url: tcgplayerUrl,
              scraped_at: new Date().toISOString()
            })
            .eq('id', id);

          if (error) {
            console.error(`[W${workerId}] Failed to update DB for ${id}:`, error.message);
          } else {
            console.log(`[W${workerId}] [${index}/${total}] OK ${name} (${id}) -> TCGPlayer: ${tcgplayerId}`);
            stats.updated++;
          }
        } else {
          console.log(`[W${workerId}] [${index}/${total}] No TCGPlayer ID found for ${name} (${id})`);
          stats.notFound++;
        }
        
        success = true;
      } catch (e) {
        retries++;
        console.log(`[W${workerId}] Error on ${url} (Retry ${retries}/${MAX_RETRIES}): ${e.message}`);
        await delay(RETRY_BACKOFF_MS * retries);
      }
    }
    
    if (!success) stats.failed++;
    if (DELAY_BETWEEN_CARDS_MS > 0) await delay(DELAY_BETWEEN_CARDS_MS);
  }
  
  await page.close();
}

function createCardQueue(cards) {
  let index = 0;
  return {
    next() {
      if (index >= cards.length) return null;
      const current = index++;
      return { ...cards[current], index: current + 1, total: cards.length };
    }
  };
}

async function main() {
  const cardsToScrape = await fetchMissingCards();
  if (cardsToScrape.length === 0) {
    console.log('No cards to process. Exiting.');
    return;
  }

  const launchOptions = {
    headless: HEADLESS,
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-blink-features=AutomationControlled',
      '--window-size=1280,800',
    ],
    defaultViewport: { width: 1280, height: 800 },
  };
  if (CHROME_PATH) launchOptions.executablePath = CHROME_PATH;

  const browser = await puppeteer.launch(launchOptions);
  const cardQueue = createCardQueue(cardsToScrape);
  const stats = { updated: 0, notFound: 0, failed: 0 };
  
  console.log(`\nStarting workers to fetch TCGPlayer IDs...`);
  
  await Promise.all(
    Array.from({ length: Math.min(NUM_WORKERS, cardsToScrape.length) }, (_, idx) =>
      worker(idx + 1, browser, cardQueue, stats)
    )
  );

  await browser.close();
  console.log(`\n🎉 All done!`);
  console.log(`Updated: ${stats.updated}`);
  console.log(`No TCGPlayer ID on site: ${stats.notFound}`);
  console.log(`Failed: ${stats.failed}`);
}

main().catch(console.error);
