require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });
const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');
const { createClient } = require('@supabase/supabase-js');

// ─── Config ───────────────────────────────────────────────────────────────────
const NUM_WORKERS            = parseInt(process.env.NUM_WORKERS || '5', 10);
const DELAY_BETWEEN_CARDS_MS = parseInt(process.env.DELAY_BETWEEN_CARDS_MS || '800', 10);
const WORKER_START_STAGGER_MS= parseInt(process.env.WORKER_START_STAGGER_MS || '50', 10);
const WAIT_AFTER_PAGE_MS     = parseInt(process.env.WAIT_AFTER_PAGE_MS || '700', 10);
const WAIT_AFTER_MODAL_MS    = parseInt(process.env.WAIT_AFTER_MODAL_MS || '500', 10);
const BATCH_INSERT_SIZE      = parseInt(process.env.BATCH_INSERT_SIZE || '50', 10);
const HEADLESS               = process.env.HEADLESS !== 'false';
const CHROME_PATH            = process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const MAX_RETRIES            = parseInt(process.env.MAX_RETRIES || '4', 10);
const RETRY_BACKOFF_MS       = parseInt(process.env.RETRY_BACKOFF_MS || '2500', 10);
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
const SETS_URL_IMAGES = `${BASE_URL}/sets/${tcgPath}?cardCountMode=anyCardVariant&releaseDateOrder=newToOld&displayAs=images`;
const SETS_URL_LIST = `${BASE_URL}/sets/${tcgPath}?cardCountMode=anyCardVariant&releaseDateOrder=newToOld&displayAs=list`;

const BLOCK_HEAVY_ASSETS = true;

async function setupPage(page) {
  await page.setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
  await page.setViewport({ width: 1280, height: 900 });
  if (BLOCK_HEAVY_ASSETS) {
    await page.setRequestInterception(true);
    page.on('request', req => {
      const type = req.resourceType();
      if (['image', 'media', 'font'].includes(type)) req.abort();
      else req.continue();
    });
  }
}

async function scrapeSets(browser) {
  console.log(`\n--- SCRAPING SETS (${dbLang.toUpperCase()}) ---`);
  const page = await browser.newPage();
  await setupPage(page);

  console.log('Navigating to TCGCollector sets (images view)...');
  await page.goto(SETS_URL_IMAGES, { waitUntil: 'networkidle2', timeout: 30000 });

  let scrollAttempts = 0;
  while (scrollAttempts < 60) {
    const prev = await page.evaluate('document.body.scrollHeight');
    await page.evaluate('window.scrollTo(0, document.body.scrollHeight)');
    await delay(500);
    const next = await page.evaluate('document.body.scrollHeight');
    if (next === prev) break;
    scrollAttempts++;
  }

  const setsMap = {};
  const setsImages = await page.evaluate(() => {
    const results = [];
    document.querySelectorAll('.set-logo-grid-item').forEach(item => {
      const nameLink = item.querySelector('a.set-logo-grid-item-name');
      if (!nameLink) return;
      const name = nameLink.getAttribute('title') || nameLink.textContent.trim();
      const codeEl = item.querySelector('.set-logo-grid-item-code');
      const setCode = codeEl ? codeEl.textContent.trim() : '';
      const symbolImg = item.querySelector('img.set-logo-grid-item-symbol');
      let symbolUrl = symbolImg ? (symbolImg.getAttribute('src') || symbolImg.getAttribute('data-src') || '') : '';
      if (symbolUrl && !symbolUrl.startsWith('http')) symbolUrl = `https://www.tcgcollector.com${symbolUrl}`;
      const logoImg = item.querySelector('img.set-logo-grid-item-logo');
      let logoUrl = logoImg ? (logoImg.getAttribute('src') || logoImg.getAttribute('data-src') || '') : '';
      if (logoUrl && !logoUrl.startsWith('http')) logoUrl = `https://www.tcgcollector.com${logoUrl}`;
      const dateEl = item.querySelector('.set-logo-grid-item-release-date');
      let releaseDate = dateEl ? dateEl.textContent.trim() : '';
      if (releaseDate) {
         try {
            const d = new Date(releaseDate);
            if (!isNaN(d.getTime())) {
               releaseDate = d.toISOString().split('T')[0];
            }
         } catch(e) {}
      }
      
      let series = '';
      const groupContainer = item.closest('.set-search-result-group') || item.parentElement?.parentElement;
      if (groupContainer) {
        const h2 = groupContainer.querySelector('h2.set-logo-grid-title, h2');
        if (h2) series = h2.textContent.trim();
      }
      let href = nameLink.getAttribute('href') || '';
      let fullUrl = href.startsWith('http') ? href : `https://www.tcgcollector.com${href}`;
      fullUrl = fullUrl.split('?')[0];
      
      // Heuristic: Jika ini main set (tidak mengandung kata promo/deck/box dll), berikan sedikit flag internal jika diperlukan
      // Tapi biasanya di frontend yang disortir adalah date DESC
      results.push({ name, set_code: setCode, logo_url: logoUrl, symbol_url: symbolUrl, url: fullUrl, release_date: releaseDate, series });
    });
    return results;
  });

  setsImages.forEach(s => { setsMap[s.url] = s; });
  console.log(`Extracted ${setsImages.length} sets from images view.`);

  console.log('Navigating to list view for card counts...');
  await page.goto(SETS_URL_LIST, { waitUntil: 'networkidle2', timeout: 30000 });
  scrollAttempts = 0;
  while (scrollAttempts < 60) {
    const prev = await page.evaluate('document.body.scrollHeight');
    await page.evaluate('window.scrollTo(0, document.body.scrollHeight)');
    await delay(500);
    const next = await page.evaluate('document.body.scrollHeight');
    if (next === prev) break;
    scrollAttempts++;
  }

  const listData = await page.evaluate(() => {
    const results = [];
    document.querySelectorAll('.set-list-item').forEach(item => {
      const nameLink = item.querySelector('a.set-list-item-name');
      if (!nameLink) return;
      let href = nameLink.getAttribute('href') || '';
      let fullUrl = href.startsWith('http') ? href : `https://www.tcgcollector.com${href}`;
      fullUrl = fullUrl.split('?')[0];
      let totalCards = 0;
      const progressLabel = item.querySelector('.progress-label');
      if (progressLabel) {
         const match = progressLabel.textContent.trim().match(/\d+\/(\d+)/);
         if (match) totalCards = parseInt(match[1], 10);
      }
      results.push({ url: fullUrl, total_cards: totalCards });
    });
    return results;
  });

  listData.forEach(l => {
     if (setsMap[l.url]) setsMap[l.url].total_cards = l.total_cards;
  });

  await page.close();

  const sets = Object.values(setsMap);
  console.log(`Upserting ${sets.length} sets to DB...`);
  
  const batch = [];
  const seenIds = new Set();
  
  for (const s of sets) {
    const safeName = s.name.replace(/[^a-zA-Z0-9]/g, '').toLowerCase();
    let setId = `tcgc_${dbLang}_${safeName}`;
    
    let counter = 2;
    while (seenIds.has(setId)) {
      setId = `tcgc_${dbLang}_${safeName}${counter}`;
      counter++;
    }
    seenIds.add(setId);
    
    batch.push({
      id: setId,
      name: s.name,
      series: s.series,
      printed_total: null,
      total: s.total_cards,
      ptcgo_code: s.set_code,
      release_date: s.release_date,
      symbol_url: s.symbol_url,
      logo_url: s.logo_url,
      scraped_at: new Date().toISOString(),
      language: dbLang,
    });
  }
  
  const { error } = await supabase.from('pokemon_sets').upsert(batch, { onConflict: 'id' });
  if (error) {
    console.error('Error upserting sets:', error.message);
    throw error;
  } else {
    console.log('Sets upserted successfully.');
  }

  return sets.map((s, i) => ({ ...s, db_id: batch[i].id }));
}


function extractCardData() {
  const extractText = (sel) => {
    const el = document.querySelector(sel);
    return el ? el.textContent.trim().replace(/\s+/g, ' ') : null;
  };
  let name = extractText('h1');
  let hp = extractText('#card-hit-points-value') || extractText('.card-hp') || null;
  if (!hp) {
    const m = document.body.innerText.match(/HP\s+(\d+)/i);
    if (m) hp = m[1];
  }
  let cardNumber = null, rarity = null, artist = null;
  let supertype = 'Pokémon', subtypes = [], types = [];
  let regulation_mark = null, evolves_from = null;
  let abilities = [], attacks = [], weaknesses = [], resistances = [];
  let retreat_cost = [], converted_retreat_cost = 0;
  let legalities = { unlimited: 'Legal' };
  let rules = [], flavor_text = null, tcgplayer_id = null;
  let national_pokedex_numbers = [];

  const typeHeader = document.querySelector('.card-info-header') || document.body;
  Array.from(typeHeader.querySelectorAll('a')).forEach(a => {
    const text = a.textContent.trim();
    const prev = a.previousSibling ? a.previousSibling.textContent.trim() : '';
    if (prev.includes('Evolves from')) evolves_from = text;
    if (['Stage 1','Stage 2','Basic','VMAX','VSTAR','ex','EX','GX','BREAK','Restored','V'].includes(text)) {
      if (!subtypes.includes(text)) subtypes.push(text);
    }
  });

  const energyTypesContainer = document.querySelector('#card-energy-types');
  if (energyTypesContainer) {
    energyTypesContainer.querySelectorAll('img').forEach(el => {
      const v = (el.getAttribute('title') || el.getAttribute('alt') || '').replace('Energy', '').trim();
      if (v) types.push(v);
    });
  }

  const tc = document.querySelector('.card-type-container');
  if (tc) {
    const t = tc.textContent.trim();
    if (t.includes('Pokémon') || t.includes('Pokemon')) supertype = 'Pokémon';
    else if (['Trainer','Supporter','Item','Stadium','Tool'].some(k => t.includes(k))) supertype = 'Trainer';
    else if (t.includes('Energy')) supertype = 'Energy';
    if (t && !['Pokémon','Trainer','Energy'].includes(t) && !subtypes.includes(t)) subtypes.push(t);
  }

  Array.from(document.querySelectorAll('.card-info-footer-item-title')).forEach(h => {
    const title = h.textContent.trim();
    const val = h.nextElementSibling;
    if (!val) return;
    if (title === 'Weakness') {
      const t = val.querySelector('img')?.getAttribute('title');
      if (t) weaknesses = [{ type: t.replace('Energy','').trim(), value: val.textContent.trim() }];
    } else if (title === 'Resistance') {
      const t = val.querySelector('img')?.getAttribute('title');
      const v = val.textContent.trim();
      if (t && v !== '—') resistances = [{ type: t.replace('Energy','').trim(), value: v }];
    } else if (title === 'Retreat Cost') {
      val.querySelectorAll('img').forEach(img => {
        retreat_cost.push((img.getAttribute('title') || '').replace('Energy','').trim() || 'Colorless');
      });
      converted_retreat_cost = retreat_cost.length;
    } else if (title === 'Rarity') {
      const v = val.textContent.trim(); if (v && v !== '—') rarity = v;
    } else if (title === 'Card number') {
      cardNumber = val.textContent.trim();
    } else if (title === 'Regulation mark') {
      regulation_mark = val.textContent.trim();
    } else if (title === 'Illustrators' || title === 'Illustrator') {
      artist = val.textContent.trim();
    } else if (title === 'Pokédex' || title === 'National Pokédex') {
      val.querySelectorAll('a').forEach(a => {
        const pNum = parseInt(a.textContent.replace(/[^0-9]/g, ''), 10);
        if (!isNaN(pNum)) national_pokedex_numbers.push(pNum);
      });
    }
  });

  document.querySelectorAll('.card-ability, .card-effect').forEach(el => {
    const name = (el.querySelector('.card-ability-name') || el.querySelector('.card-effect-name'))?.textContent.trim();
    const type = (el.querySelector('.card-ability-type') || el.querySelector('.card-effect-badge') || el.querySelector('.card-badge'))?.textContent.trim();
    const text = (el.querySelector('.card-ability-text') || el.querySelector('.card-effect-description') || el.querySelector('p'))?.textContent.trim();
    
    if (type && type.toLowerCase().includes('rule')) {
       if (text && !rules.includes(text)) rules.push(text);
    } else if (name) {
       abilities.push({ type: type || 'Ability', name, text: text || '' });
    }
  });

  document.querySelectorAll('.card-rule').forEach(el => {
    const text = (el.querySelector('.card-rule-description') || el.querySelector('p'))?.textContent.trim();
    if (text && !rules.includes(text)) rules.push(text);
  });

  document.querySelectorAll('.card-attack').forEach(el => {
    const name   = el.querySelector('.card-attack-name')?.textContent.trim();
    const damage = el.querySelector('.card-attack-damage')?.textContent.trim();
    const text   = el.querySelector('.card-attack-description, .card-attack-text')?.textContent.trim();
    const cost   = [];
    el.querySelectorAll('.card-attack-energies img').forEach(img =>
      cost.push(img.getAttribute('title') || img.getAttribute('alt') || 'Colorless')
    );
    if (name) attacks.push({ name, cost, convertedEnergyCost: cost.length, damage: damage || '', text: text || '' });
  });

  const imgEl = document.querySelector('img[src*="/content/images/"]');
  let imageUrl = null, imageSmall = null, imageLarge = null;
  if (imgEl) {
    imageUrl = imgEl.getAttribute('src');
    const srcset = imgEl.getAttribute('srcset');
    if (srcset) {
      const parts = srcset.split(',').map(s => s.trim().split(/\s+/)).filter(p => p.length === 2);
      parts.sort((a, b) => parseInt(a[1]) - parseInt(b[1]));
      if (parts.length > 0) {
        imageSmall = parts[0][0];
        imageLarge = parts[parts.length - 1][0];
      }
    }
  }
  if (!imageSmall) imageSmall = imageUrl;
  if (!imageLarge) imageLarge = imageUrl;

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
    tcgplayer_id = extractTcgplayerIdFromHref(href);
    if (tcgplayer_id) break;
  }

  const flavorEl = document.querySelector('.card-flavor-text');
  if (flavorEl) flavor_text = flavorEl.textContent.trim();

  return {
    name, hp, cardNumber, rarity, artist, supertype, subtypes,
    types: [...new Set(types)], imageSmall, imageLarge, regulation_mark, evolves_from,
    abilities, attacks, weaknesses, resistances, retreat_cost, converted_retreat_cost, legalities,
    rules, flavor_text, tcgplayer_id, national_pokedex_numbers
  };
}

async function worker(workerId, browser, cardQueue, dbSet, insertQueue, stats) {
  let page = await browser.newPage();
  await setupPage(page);

  if (WORKER_START_STAGGER_MS > 0) await delay(workerId * WORKER_START_STAGGER_MS);
  
  while (true) {
    const item = cardQueue.next();
    if (!item) break;
    
    const { url, number, total } = item;
    let retries = 0, success = false;
    
    while (retries < MAX_RETRIES && !success) {
      try {
        const response = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
        const status = response ? response.status() : 0;
        if (status === 429 || status >= 500) throw new Error(`HTTP ${status}`);
        if (WAIT_AFTER_PAGE_MS > 0) await delay(WAIT_AFTER_PAGE_MS);

        const title = await page.title();
        if (title.includes('Just a moment') || title.includes('Cloudflare')) {
          console.log(`[W${workerId}] Cloudflare on ${url}. Retrying...`);
          throw new Error('Cloudflare block');
        }

        try {
          const clicked = await page.evaluate(() => {
            const btn = document.querySelector('.show-card-price-details-modal-button');
            if (!btn) return false;
            btn.click();
            return true;
          });
          if (clicked) {
             await page.waitForSelector('.card-price-details-modal-entry-source-button', { timeout: 3000 }).catch(() => {});
          }
        } catch(_) {}

        const cardData = await page.evaluate(extractCardData);
        if (!cardData.name) {
          throw new Error('Name not found');
        }

        const match = url.match(/\/cards\/(\d+)/);
        const tcgcollectorCardId = match ? match[1] : null;
        const cardId = tcgcollectorCardId ? `tcgc_${dbLang}_card_${tcgcollectorCardId}` : `tcgc_${dbLang}_card_${dbSet.db_id}_${cardData.cardNumber || number}`;

        if (cardData.cardNumber) {
          const ptMatch = cardData.cardNumber.match(/\d+\s*\/\s*(\d+)/);
          if (ptMatch && !stats.printed_total) stats.printed_total = parseInt(ptMatch[1], 10);
        }

        insertQueue.push({
          id:                    cardId,
          name:                  cardData.name,
          set_id:                dbSet.db_id,
          card_number:           cardData.cardNumber,
          supertype:             cardData.supertype,
          subtypes:              cardData.subtypes,
          evolves_from:          cardData.evolves_from,
          hp:                    cardData.hp,
          types:                 cardData.types,
          rarity:                cardData.rarity,
          artist:                cardData.artist,
          regulation_mark:       cardData.regulation_mark,
          attacks:               cardData.attacks,
          abilities:             cardData.abilities,
          weaknesses:            cardData.weaknesses,
          resistances:           cardData.resistances,
          retreat_cost:          cardData.retreat_cost,
          converted_retreat_cost: cardData.converted_retreat_cost,
          legalities:            cardData.legalities,
          language:              dbLang,
          english_name:          cardData.name,
          rules:                 cardData.rules,
          flavor_text:           cardData.flavor_text,
          tcgplayer_product_id:  cardData.tcgplayer_id,
          tcgplayer_url:         cardData.tcgplayer_id ? `https://www.tcgplayer.com/product/${cardData.tcgplayer_id}` : null,
          image_large:           cardData.imageLarge,
          image_small:           cardData.imageSmall,
          national_pokedex_numbers: cardData.national_pokedex_numbers,
          scraped_at:            new Date().toISOString(),
        });

        const tcgLog = cardData.tcgplayer_id ? ` [TCGPlayer: ${cardData.tcgplayer_id}]` : '';
        console.log(`[W${workerId}] OK ${cardData.name} (${number}/${total})${tcgLog}`);
        success = true;
      } catch (e) {
        retries++;
        await delay(RETRY_BACKOFF_MS * retries);
      }
    }
    
    if (success) stats.scraped++; else stats.failed++;
    if (DELAY_BETWEEN_CARDS_MS > 0) await delay(DELAY_BETWEEN_CARDS_MS);
  }
  
  await page.close();
}

function createCardQueue(cardUrls) {
  let index = 0;
  return {
    next() {
      if (index >= cardUrls.length) return null;
      const current = index++;
      return { url: cardUrls[current], number: current + 1, total: cardUrls.length };
    }
  };
}

async function startFlusher(insertQueue, stats) {
  while (!stats.done) {
    if (insertQueue.length >= BATCH_INSERT_SIZE) {
      const batch = insertQueue.splice(0, BATCH_INSERT_SIZE);
      const { error } = await supabase.from('pokemon_cards').upsert(batch, { onConflict: 'id' });
      if (error) console.error(`[DB] Upsert error: ${error.message}`);
      else stats.inserted += batch.length;
    } else {
      await delay(400);
    }
  }
  while (insertQueue.length > 0) {
    const batch = insertQueue.splice(0, BATCH_INSERT_SIZE);
    const { error } = await supabase.from('pokemon_cards').upsert(batch, { onConflict: 'id' });
    if (!error) stats.inserted += batch.length;
  }
}

async function main() {
  const launchOptions = {
    headless: HEADLESS,
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-blink-features=AutomationControlled',
      '--disable-background-timer-throttling',
      '--disable-renderer-backgrounding',
      '--disable-backgrounding-occluded-windows',
      '--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling',
      '--window-size=1280,800',
    ],
    defaultViewport: { width: 1280, height: 800 },
  };
  if (CHROME_PATH) launchOptions.executablePath = CHROME_PATH;

  const browser = await puppeteer.launch(launchOptions);
  
  // Phase 1: Scrape Sets
  const setsToScrape = await scrapeSets(browser);

  // Phase 2: Scrape Cards
  console.log(`\n--- SCRAPING CARDS (${dbLang.toUpperCase()}) ---`);
  const listPage = await browser.newPage();
  await setupPage(listPage);

  // Let's just limit to the first set for test run if TEST_RUN is enabled
  const TEST_RUN = process.env.TEST_RUN === 'true';
  const TARGET_SET = process.env.TARGET_SET;
  const FORCE_SCRAPE = process.env.FORCE_SCRAPE === 'true';
  let targetSets = TEST_RUN ? setsToScrape.slice(0, 1) : setsToScrape;

  if (TARGET_SET) {
    targetSets = targetSets.filter(s => 
      s.name.toLowerCase().includes(TARGET_SET.toLowerCase()) || 
      (s.db_id && s.db_id.toLowerCase().includes(TARGET_SET.toLowerCase())) ||
      s.url.toLowerCase().includes(TARGET_SET.toLowerCase())
    );
    console.log(`Filtered down to ${targetSets.length} sets matching TARGET_SET="${TARGET_SET}"`);
  }

  const progressFile = path.join(__dirname, `tcgcollector_scrape_progress_${dbLang}.json`);
  let progress = fs.existsSync(progressFile) ? JSON.parse(fs.readFileSync(progressFile, 'utf8')) : {};

  for (const set of targetSets) {
    if (progress[set.db_id] && !FORCE_SCRAPE) {
      console.log(`\nSet: ${set.name} — Skipping, already marked as done in progress file.`);
      continue;
    }
    
    console.log(`\nSet: ${set.name}`);
    const setUrl = set.url + (set.url.includes('?') ? '&' : '?') + 'setCardCountMode=anyCardVariant';
    
    await listPage.goto(setUrl, { waitUntil: 'networkidle2', timeout: 30000 });
    
    let title = await listPage.title();
    let cfRetries = 0;
    while ((title.includes('Just a moment') || title.includes('Cloudflare')) && cfRetries < 12) {
      console.log(`[Cloudflare] Blocked on set list page (${set.name}). Waiting 5s...`);
      await delay(5000);
      title = await listPage.title();
      cfRetries++;
    }

    if (title.includes('Just a moment') || title.includes('Cloudflare')) {
      console.log(`[Cloudflare] Could not bypass for set ${set.name}. Skipping...`);
      continue;
    }

    for (let s = 0; s < 60; s++) {
      const before = await listPage.evaluate('document.body.scrollHeight');
      await listPage.evaluate('window.scrollTo(0, document.body.scrollHeight)');
      await delay(500);
      const after = await listPage.evaluate('document.body.scrollHeight');
      if (after === before) break;
    }

    const cardUrls = await listPage.evaluate(() => {
      const urls = new Set();
      document.querySelectorAll('a[href*="/cards/"]').forEach(l => {
        const href = l.getAttribute('href');
        if (href && href.match(/\/cards\/\d+/)) {
          urls.add(href.startsWith('http') ? href : `https://www.tcgcollector.com${href}`);
        }
      });
      return [...urls];
    });

    if (cardUrls.length === 0) {
      console.log(`  No cards found for ${set.name}`);
      continue;
    }
    
    console.log(`  Found ${cardUrls.length} cards`);

    const insertQueue = [];
    const cardQueue = createCardQueue(TEST_RUN ? cardUrls.slice(0, 5) : cardUrls);
    const stats = { done: false, inserted: 0, scraped: 0, failed: 0, printed_total: null };
    
    const flusherPromise = startFlusher(insertQueue, stats);
    await Promise.all(
      Array.from({ length: Math.min(NUM_WORKERS, TEST_RUN ? 5 : cardUrls.length) }, (_, idx) =>
        worker(idx + 1, browser, cardQueue, set, insertQueue, stats)
      )
    );
    stats.done = true;
    await flusherPromise;

    if (stats.printed_total) {
      await supabase.from('pokemon_sets').update({ printed_total: stats.printed_total }).eq('id', set.db_id);
      console.log(`  Updated set ${set.name} with printed_total: ${stats.printed_total}`);
    }
    console.log(`✅ Done: ${set.name} — ${stats.scraped} scraped, ${stats.inserted} inserted.`);
    
    progress[set.db_id] = true;
    fs.writeFileSync(progressFile, JSON.stringify(progress, null, 2));
  }

  await browser.close();
  console.log('\n🎉 All done!');
}

main().catch(console.error);
