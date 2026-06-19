import { serve } from "https://deno.land/std@0.177.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.39.3";

const POKEMONTCG_BASE = "https://api.pokemontcg.io/v2";
const PAGE_SIZE = 250;
const CARDS_BATCH_SIZE = 250;

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Map Set
function mapSet(s: any) {
  const images = s.images || {};
  return {
    id: s.id,
    name: s.name || "",
    series: s.series,
    printed_total: s.printedTotal,
    total: s.total,
    ptcgo_code: s.ptcgoCode,
    release_date: s.releaseDate,
    symbol_url: images.symbol,
    logo_url: images.logo,
  };
}

// Map Card
function mapCard(c: any) {
  const images = c.images || {};
  const tcg = c.tcgplayer || {};
  const prices_raw = tcg.prices || {};
  const set_info = c.set || {};

  let market_price = 0.0;
  for (const variant_key of ["holofoil", "normal", "reverseHolofoil", "1stEditionHolofoil"]) {
    const variant = prices_raw[variant_key] || {};
    const val = variant.market || variant.mid || 0;
    if (typeof val === "number" && val > 0) {
      market_price = val;
      break;
    }
  }

  return {
    id: c.id,
    name: c.name || "",
    set_id: set_info.id,
    card_number: c.number,
    supertype: c.supertype,
    subtypes: c.subtypes || [],
    hp: c.hp,
    types: c.types || [],
    rarity: c.rarity,
    image_large: images.large,
    image_small: images.small,
    artist: c.artist,
    flavor_text: c.flavorText,
    regulation_mark: c.regulationMark,
    evolves_from: c.evolvesFrom,
    evolves_to: c.evolvesTo || [],
    rules: c.rules || [],
    abilities: c.abilities || [],
    attacks: c.attacks || [],
    weaknesses: c.weaknesses || [],
    resistances: c.resistances || [],
    retreat_cost: c.retreatCost || [],
    converted_retreat_cost: c.convertedRetreatCost,
    legalities: c.legalities,
    tcgplayer_url: tcg.url,
    tcgplayer_prices: prices_raw,
    market_price: market_price,
  };
}

// Upsert Helper
async function supabaseUpsert(sb: any, table: string, rows: any[]) {
  if (!rows || rows.length === 0) return;
  const { error } = await sb.from(table).upsert(rows);
  if (error) {
    throw new Error(`Supabase upsert error [${table}]: ${error.message}`);
  }
}

// Fetch helper with retry
async function fetchWithRetry(url: string, apiKey: string | undefined): Promise<any> {
  const headers: Record<string, string> = { "Accept": "application/json" };
  if (apiKey) {
    headers["X-Api-Key"] = apiKey;
  }

  let delayMs = 1000;
  for (let attempt = 1; attempt <= 3; attempt++) {
    const resp = await fetch(url, { headers });
    if (resp.status === 429) {
      const wait = resp.headers.get("Retry-After");
      const waitMs = wait ? parseFloat(wait) * 1000 : delayMs * 2;
      console.log(`Rate limited. Waiting ${waitMs}ms...`);
      await sleep(waitMs);
      continue;
    }
    if (!resp.ok) {
      if (attempt === 3) throw new Error(`HTTP Error ${resp.status} - ${resp.statusText}`);
      console.log(`HTTP ${resp.status} - retrying in ${delayMs}ms`);
      await sleep(delayMs);
      delayMs *= 2;
      continue;
    }
    return resp.json();
  }
}

serve(async (req: Request) => {
  try {
    const SUPABASE_URL = Deno.env.get('SUPABASE_URL') || '';
    const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') || '';
    const POKEMONTCG_API_KEY = Deno.env.get('POKEMONTCG_API_KEY');

    if (!SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY) {
      throw new Error("Missing Supabase environment variables");
    }

    const sb = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

    console.log("Fetching sets...");
    const setsData = await fetchWithRetry(`${POKEMONTCG_BASE}/sets?pageSize=${PAGE_SIZE}&orderBy=releaseDate`, POKEMONTCG_API_KEY);
    const sets = setsData.data || [];
    console.log(`Found ${sets.length} sets. Upserting...`);
    
    const mappedSets = sets.map(mapSet);
    await supabaseUpsert(sb, "pokemon_sets", mappedSets);
    console.log("Sets upserted.");

    console.log("Fetching cards...");
    let page = 1;
    let totalUpserted = 0;

    let nextPagePromise = fetchWithRetry(`${POKEMONTCG_BASE}/cards?page=${page}&pageSize=${PAGE_SIZE}&orderBy=number`, POKEMONTCG_API_KEY);

    while (true) {
      console.log(`Processing cards page ${page}...`);
      const cardsData = await nextPagePromise;
      const cardsRaw = cardsData.data || [];
      if (cardsRaw.length === 0) break;

      const totalCount = cardsData.totalCount || 0;
      const hasMore = page * PAGE_SIZE < totalCount && cardsRaw.length === PAGE_SIZE;

      if (hasMore) {
        // Start fetching next page concurrently with upsert
        nextPagePromise = (async () => {
          await sleep(POKEMONTCG_API_KEY ? 50 : 250); // slight delay to prevent 429
          return fetchWithRetry(`${POKEMONTCG_BASE}/cards?page=${page + 1}&pageSize=${PAGE_SIZE}&orderBy=number`, POKEMONTCG_API_KEY);
        })();
      }

      const mappedCards = cardsRaw.map(mapCard);
      
      for (let i = 0; i < mappedCards.length; i += CARDS_BATCH_SIZE) {
        const batch = mappedCards.slice(i, i + CARDS_BATCH_SIZE);
        await supabaseUpsert(sb, "pokemon_cards", batch);
        totalUpserted += batch.length;
      }

      if (!hasMore) {
        break;
      }
      page++;
    }

    console.log(`Done! Total cards upserted: ${totalUpserted}`);

    return new Response(JSON.stringify({ success: true, message: `Upserted ${sets.length} sets and ${totalUpserted} cards` }), {
      headers: { "Content-Type": "application/json" },
    });
  } catch (error: any) {
    console.error("Error:", error);
    return new Response(JSON.stringify({ success: false, error: error.message }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  }
});
